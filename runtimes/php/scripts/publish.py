#!/usr/bin/env python3
"""Publish complete verified bundles with factual notes and immutable tags."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
from urllib.parse import quote


def validate_assets(directory):
    archives = sorted(directory.glob("*.tar.gz"))
    if not archives:
        raise ValueError("No verified archives")
    assets = []
    for archive in archives:
        checksum = archive.with_name(archive.name + ".sha256")
        metadata = archive.with_name(archive.name[:-7] + ".manifest.json")
        manifest = json.loads(metadata.read_text())
        if manifest.get("verification") != "PASSED":
            raise ValueError("Cannot publish an unverified archive")
        fields = checksum.read_text().split()
        if len(fields) != 2 or fields[1] != archive.name:
            raise ValueError("Invalid checksum identity")
        with archive.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != fields[0]:
            raise ValueError("Archive checksum mismatch")
        root = archive.name[:-7]
        with tarfile.open(archive) as tar:
            members = {}
            for member in tar.getmembers():
                path = Path(member.name)
                canonical = str(path)
                if (path.is_absolute() or ".." in path.parts or not path.parts
                        or path.parts[0] != root or member.name.rstrip("/") != canonical
                        or not (member.isfile() or member.isdir()) or canonical in members):
                    raise ValueError("Unsafe archive layout")
                members[canonical] = member
            packaged = members.get(root + "/manifest.json")
            if not packaged or not packaged.isfile() or json.load(tar.extractfile(packaged)) != manifest:
                raise ValueError("Sidecar and packaged manifest differ")
            try:
                parts = [manifest["php"]["version"], manifest["target"], manifest["profile"], manifest["architecture"], manifest["build_id"]]
            except (KeyError, TypeError) as error:
                raise ValueError("Missing archive identity") from error
            if (type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1 or manifest.get("runtime") != "php"
                    or any(not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", value) for value in parts)
                    or not re.fullmatch(r"8\.[2-5]\.[0-9]+", parts[0])
                    or parts[1] not in {"glibc", "musl"} or parts[2] not in {"minimal", "full"}
                    or parts[3] != "x86_64" or "php-" + "-".join(parts) != root):
                raise ValueError("Invalid archive identity")
            for executable in ("bin/php", "bin/php-fpm", "bin/php-ext", "libexec/php", "libexec/php-fpm"):
                candidate = members.get(root + "/" + executable)
                if not candidate or not candidate.isfile() or candidate.size == 0 or candidate.mode & 0o111 != 0o111:
                    raise ValueError(f"Archive lost executable file/permissions: {executable}")
        assets.extend([archive, checksum, metadata])
    return assets


def validate_plan(plan, manifests, tag, repository, commit):
    if (not isinstance(plan, dict) or type(plan.get("schema_version")) is not int or plan["schema_version"] != 1
            or plan.get("repository") != repository or plan.get("commit") != commit
            or plan.get("release_tag") != tag or plan.get("publish") is not True
            or plan.get("has_updates") is not True or plan.get("event") not in {"schedule", "workflow_dispatch"}
            or not all(isinstance(plan.get(key), str) and re.fullmatch(r"[1-9][0-9]*", plan[key]) for key in ("run_id", "run_attempt"))):
        raise ValueError("Invalid publication plan identity")
    matrix = plan.get("matrix")
    if not isinstance(matrix, dict) or not isinstance(matrix.get("include"), list) or not matrix["include"]:
        raise ValueError("Empty/invalid publication matrix")
    expected = set()
    for row in matrix["include"]:
        if not isinstance(row, dict):
            raise ValueError("Invalid publication matrix row")
        version = row.get("php_version")
        if (not isinstance(version, str) or not re.fullmatch(r"8\.[2-5]\.(?:0|[1-9][0-9]*)", version)
                or row.get("php_series") != ".".join(version.split(".")[:2])
                or row.get("target") not in {"glibc", "musl"} or row.get("profile") not in {"full", "minimal"}
                or row.get("architecture") != "x86_64"):
            raise ValueError("Invalid publication matrix version/modes")
        previous = row.get("previous_version")
        if previous is not None and (not isinstance(previous, str) or not re.fullmatch(r"8\.[2-5]\.(?:0|[1-9][0-9]*)", previous) or not previous.startswith(row["php_series"] + ".")):
            raise ValueError("Invalid previous published version")
        identity = (version, row["target"], row["profile"], row["architecture"])
        if identity in expected:
            raise ValueError("Duplicate publication matrix row")
        expected.add(identity)
    actual = set()
    for manifest in manifests:
        try:
            identity = (manifest["php"]["version"], manifest["target"], manifest["profile"], manifest["architecture"])
            builder_commit = manifest["builder"]["commit"]
        except (KeyError, TypeError) as error:
            raise ValueError("Missing planned bundle metadata") from error
        if identity in actual or builder_commit != commit:
            raise ValueError("Duplicate bundle or mismatched builder commit")
        actual.add(identity)
    if actual != expected:
        raise ValueError("Published bundles do not exactly match the build plan")


def release_notes(manifests, repository, commit, plan=None):
    lines = ["# PHP runtime bundles", "", "Verified PHP CLI/FPM archives built from the selected stable patches.", "",
             "## Included bundles", "", "| PHP | Previous published patch | Target | Profile | Architecture | Build ID |",
             "| --- | --- | --- | --- | --- | --- |"]
    previous = {}
    if plan:
        for row in plan["matrix"]["include"]:
            previous[(row["php_version"], row["target"], row["profile"], row["architecture"])] = row.get("previous_version")
    for manifest in sorted(manifests, key=lambda m: (tuple(map(int, m["php"]["version"].split("."))), m["target"], m["profile"])):
        identity = (manifest["php"]["version"], manifest["target"], manifest["profile"], manifest["architecture"])
        prior = previous.get(identity) or ("Not previously published" if plan else "Not recorded")
        lines.append("| " + " | ".join((*identity[:1], prior, *identity[1:], manifest["build_id"])) + " |")
    lines += ["", "## Extension inventory", ""]
    for manifest in manifests:
        label = f'{manifest["php"]["version"]} / {manifest["target"]} / {manifest["profile"]}'
        modules = manifest["php"].get("static_modules")
        if modules is not None and (not isinstance(modules, list) or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_ .-]+", name) for name in modules)):
            raise ValueError("Invalid static extension inventory")
        lines.append(f"- **{label}** static baseline (`php -n`): " + (", ".join(f"`{name}`" for name in sorted(modules)) if modules else "not recorded"))
        shared = manifest.get("shared_modules", [])
        if not isinstance(shared, list):
            raise ValueError("Invalid shared extension inventory")
        if not shared:
            lines.append("  - No shared modules packaged.")
        for module in shared:
            if not isinstance(module, dict) or not isinstance(module.get("name"), str) or not re.fullmatch(r"[a-z][a-z0-9_]*", module["name"]):
                raise ValueError("Invalid shared extension inventory")
            state = module.get("enabled_by_default", {})
            if not isinstance(state, dict):
                raise ValueError("Invalid initial shared extension state")
            def enabled(sapi):
                value = state.get(sapi)
                return "enabled" if value is True else "disabled" if value is False else "not recorded"
            lines.append(f'  - Shared `{module["name"]}`: initial CLI {enabled("cli")}, FPM {enabled("fpm")}.')
    lines += ["", "## Installation and verification", "",
              "- Download the matching `.tar.gz`, `.tar.gz.sha256`, and `.manifest.json` assets.",
              "- Verify SHA-256 before extracting into a new empty installation directory.",
              "- Use bundled `bin/php`, `bin/php-fpm`, and `bin/php-ext`, not raw `libexec` binaries.",
              "- FPM requires a non-root caller and an explicit caller-owned configuration/socket/pid/log.",
              "- `glibc` declares a glibc >= 2.17 baseline; `musl` is fully static. Match the manifest to the host.",
              "- Publication validates archive checksums, identity, packaged manifests, executable modes, and `verification=PASSED`.",
              "- This does not certify an oldest-host run, distinct-minor coexistence, complete source/toolchain authentication, legal inventory, or independent artifact signing.",
              "", "## Build references", "", f"- Builder commit: https://github.com/{repository}/commit/{commit}"]
    if plan:
        lines.append(f'- Workflow: https://github.com/{repository}/actions/runs/{plan["run_id"]} (attempt {plan["run_attempt"]})')
    lines.append(f"- PHP release information: https://www.php.net/releases/")
    return "\n".join(lines) + "\n"


def github_json(endpoint, *, method="GET", payload=None, file=None):
    command = ["gh", "api", endpoint, "--method", method]
    options = {"text": True, "timeout": 120}
    if file is not None:
        command += ["--header", "Content-Type: application/octet-stream", "--input", str(file)]
    elif payload is not None:
        command += ["--header", "Content-Type: application/json", "--input", "-"]
        options["input"] = json.dumps(payload)
    return json.loads(subprocess.check_output(command, **options))


def refuse_existing_release(repository, tag):
    # The by-tag REST endpoint excludes drafts; enumerate them with write-scoped credentials.
    for page in range(1, 10001):
        releases = github_json(f"repos/{repository}/releases?per_page=100&page={page}")
        if not isinstance(releases, list) or any(not isinstance(release, dict) or not isinstance(release.get("tag_name"), str) for release in releases):
            raise ValueError("Cannot verify draft/public release absence")
        if any(release["tag_name"] == tag for release in releases):
            raise ValueError("Release/draft already exists; never reuse verified distributions")
        if len(releases) < 100:
            return
    raise ValueError("Release absence pagination limit exceeded")


def publish(directory, tag, repository, commit, plan=None):
    if not re.fullmatch(r"runtimes-[A-Za-z0-9][A-Za-z0-9._-]{0,79}", tag):
        raise ValueError("Invalid release tag")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Invalid repository/commit")
    assets = validate_assets(directory)
    manifests = [json.loads(asset.read_text()) for asset in assets if asset.name.endswith(".manifest.json")]
    if plan is not None:
        validate_plan(plan, manifests, tag, repository, commit)
    notes = release_notes(manifests, repository, commit, plan)
    metadata = json.loads(subprocess.check_output(["gh", "api", f"repos/{repository}"], text=True))
    if metadata["private"]:
        raise ValueError("Anonymous runtime download requires a public repository")
    for endpoint in (f"repos/{repository}/git/ref/tags/{tag}", f"repos/{repository}/releases/tags/{tag}"):
        result = subprocess.run(["gh", "api", endpoint], text=True, capture_output=True)
        if result.returncode == 0:
            raise ValueError("Release/tag already exists; never overwrite verified distributions")
        if "HTTP 404" not in result.stderr:
            raise ValueError(f"Cannot verify release absence: {result.stderr}")
    refuse_existing_release(repository, tag)
    created = github_json(f"repos/{repository}/releases", method="POST", payload={
        "tag_name": tag, "target_commitish": commit, "name": tag,
        "body": notes, "draft": True, "prerelease": False})
    if (not isinstance(created, dict) or type(created.get("id")) is not int or created["id"] <= 0
            or created.get("tag_name") != tag or created.get("draft") is not True):
        raise ValueError("Invalid newly created draft identity")
    release_id = created["id"]
    for asset in assets:
        endpoint = f"https://uploads.github.com/repos/{repository}/releases/{release_id}/assets?name={quote(asset.name, safe='')}"
        uploaded = github_json(endpoint, method="POST", file=asset)
        if (not isinstance(uploaded, dict) or uploaded.get("name") != asset.name
                or uploaded.get("state") != "uploaded" or type(uploaded.get("size")) is not int
                or uploaded["size"] != asset.stat().st_size):
            raise ValueError("Uploaded asset metadata does not match the planned file")
    published = github_json(f"repos/{repository}/releases/{release_id}", method="PATCH", payload={"draft": False})
    if (not isinstance(published, dict) or published.get("id") != release_id
            or published.get("tag_name") != tag or published.get("draft") is not False):
        raise ValueError("Could not confirm publication of the newly created release")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("tag")
    parser.add_argument("repository")
    parser.add_argument("commit")
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()
    policy = json.loads(args.plan.read_text()) if args.plan else None
    publish(args.directory, args.tag, args.repository, args.commit, plan=policy)
