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


# The pinned PostgreSQL sources are authoritative: a plan may only name a
# version the repository has already authenticated an archive digest for.
POSTGRES_SOURCES = Path(__file__).resolve().parents[2] / "postgresql/config/sources.json"
POSTGRES_EXECUTABLES = ("bin/postgres", "bin/initdb", "bin/pg_ctl", "bin/psql", "bin/pg_dump", "bin/pg_config")


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
            links = []
            for member in tar.getmembers():
                path = Path(member.name)
                canonical = str(path)
                redis_alias = (manifest.get("runtime") == "redis" and canonical in {
                    root + "/bin/redis-sentinel", root + "/bin/redis-check-rdb", root + "/bin/redis-check-aof"}
                    and member.issym() and member.linkname == "redis-server")
                # PostgreSQL ships versioned shared objects with the usual
                # `libpq.so -> libpq.so.5.18` aliases. Accept only relative
                # links whose target is a real file inside this same archive.
                postgres_alias = (manifest.get("runtime") == "postgresql" and member.issym()
                                  and not member.linkname.startswith("/")
                                  and ".." not in Path(member.linkname).parts)
                if (path.is_absolute() or ".." in path.parts or not path.parts
                        or path.parts[0] != root or member.name.rstrip("/") != canonical
                        or not (member.isfile() or member.isdir() or redis_alias or postgres_alias)
                        or canonical in members):
                    raise ValueError("Unsafe archive layout")
                members[canonical] = member
                if postgres_alias:
                    links.append(canonical)
            packaged = members.get(root + "/manifest.json")
            if not packaged or not packaged.isfile() or json.load(tar.extractfile(packaged)) != manifest:
                raise ValueError("Sidecar and packaged manifest differ")
            for link in links:
                member = members[link]
                target = str(Path(link).parent / member.linkname)
                destination = members.get(target)
                if not target.startswith(root + "/") or not destination or not destination.isfile():
                    raise ValueError(f"Archive link does not resolve inside the bundle: {link}")
            runtime = manifest.get("runtime")
            try:
                if runtime == "php":
                    parts = [manifest["php"]["version"], manifest["target"], manifest["architecture"], manifest["build_id"]]
                    executables = ("bin/php", "bin/php-fpm", "bin/php-ext", "libexec/php", "libexec/php-fpm")
                    valid = (re.fullmatch(r"8\.[2-5]\.[0-9]+", parts[0]) and parts[2] == "x86_64"
                             and manifest.get("linkage") == "dynamic" and "profile" not in manifest
                             and isinstance(manifest.get("loader"), dict)
                             and manifest["loader"].get("path") == "lib/musl/ld-musl-x86_64.so.1")
                    loader = members.get(root + "/lib/musl/ld-musl-x86_64.so.1")
                    if not loader or not loader.isfile() or loader.size == 0 or loader.mode & 0o111 != 0o111:
                        raise ValueError("Archive lost the bundled musl loader")
                elif runtime == "redis":
                    parts = [manifest["redis"]["version"], manifest["target"], manifest["architecture"], manifest["build_id"]]
                    valid = re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", parts[0]) and parts[2] == "x86_64" and "profile" not in manifest
                    executables = ("bin/redis-server", "bin/redis-cli", "bin/redis-benchmark")
                    for name in ("redis-sentinel", "redis-check-rdb", "redis-check-aof"):
                        candidate = members.get(root + "/bin/" + name)
                        if not candidate or not candidate.issym() or candidate.linkname != "redis-server":
                            raise ValueError("Missing/unsafe Redis alias")
                elif runtime == "postgresql":
                    parts = [manifest["postgresql"]["version"], manifest["target"], manifest["architecture"], manifest["build_id"]]
                    executables = POSTGRES_EXECUTABLES
                    valid = (re.fullmatch(r"1[78]\.[0-9]+(?:\.[0-9]+)?", parts[0]) and parts[1] == "musl-dynamic"
                             and parts[2] == "x86_64"
                             and manifest.get("linkage") == "dynamic" and "profile" not in manifest
                             and isinstance(manifest.get("loader"), dict)
                             and manifest["loader"].get("path") == "lib/musl/ld-musl-x86_64.so.1")
                    # A musl-dynamic server without its bundled loader cannot boot
                    # on a host that has no musl of its own.
                    loader = members.get(root + "/lib/musl/ld-musl-x86_64.so.1")
                    if not loader or not loader.isfile() or loader.size == 0 or loader.mode & 0o111 != 0o111:
                        raise ValueError("Archive lost the bundled musl loader")
                else:
                    raise ValueError("Unsupported archive runtime")
            except (KeyError, TypeError) as error:
                raise ValueError("Missing archive identity") from error
            if (type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1
                    or any(not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", value) for value in parts)
                    or not valid or runtime + "-" + "-".join(parts) != root):
                raise ValueError("Invalid archive identity")
            for executable in executables:
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
    if not isinstance(matrix, dict) or not isinstance(matrix.get("include"), list):
        raise ValueError("Empty/invalid publication matrix")
    redis_matrix = plan.get("redis_matrix", {"include": []})
    if not isinstance(redis_matrix, dict) or not isinstance(redis_matrix.get("include"), list):
        raise ValueError("Empty/invalid publication matrix")
    postgres_matrix = plan.get("postgres_matrix", {"include": []})
    if not isinstance(postgres_matrix, dict) or not isinstance(postgres_matrix.get("include"), list):
        raise ValueError("Empty/invalid publication matrix")
    # Any one runtime with rows is a real release: a PostgreSQL-only plan is not
    # an empty matrix, so this guard is a three-way or rather than a pair.
    if not (matrix["include"] or redis_matrix["include"] or postgres_matrix["include"]):
        raise ValueError("Empty/invalid publication matrix")
    expected = set()
    for row in matrix["include"]:
        if not isinstance(row, dict):
            raise ValueError("Invalid publication matrix row")
        version = row.get("php_version")
        if (not isinstance(version, str) or not re.fullmatch(r"8\.[2-5]\.(?:0|[1-9][0-9]*)", version)
                or row.get("php_series") != ".".join(version.split(".")[:2])
                or row.get("target") != "musl-dynamic" or row.get("architecture") != "x86_64"
                or "profile" in row):
            raise ValueError("Invalid publication matrix version/modes")
        previous = row.get("previous_version")
        if previous is not None and (not isinstance(previous, str) or not re.fullmatch(r"8\.[2-5]\.(?:0|[1-9][0-9]*)", previous) or not previous.startswith(row["php_series"] + ".")):
            raise ValueError("Invalid previous published version")
        identity = ("php", version, row["target"], row["architecture"])
        if identity in expected:
            raise ValueError("Duplicate publication matrix row")
        expected.add(identity)
    for row in redis_matrix["include"]:
        if (not isinstance(row, dict) or not isinstance(row.get("redis_version"), str)
                or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", row["redis_version"])
                or row.get("target") != "musl" or row.get("architecture") != "x86_64" or "profile" in row):
            raise ValueError("Invalid Redis publication matrix")
        previous = row.get("previous_version")
        if previous is not None and previous != row["redis_version"]:
            raise ValueError("Invalid previous Redis version")
        identity = ("redis", row["redis_version"], row["target"], row["architecture"])
        if identity in expected:
            raise ValueError("Duplicate publication matrix row")
        expected.add(identity)
    pinned = set(json.loads(POSTGRES_SOURCES.read_text())["versions"])
    for row in postgres_matrix["include"]:
        version = row.get("postgresql_version") if isinstance(row, dict) else None
        if (not isinstance(version, str) or not re.fullmatch(r"1[78]\.[0-9]+(?:\.[0-9]+)?", version)
                or version not in pinned
                or row.get("target") != "musl-dynamic" or row.get("architecture") != "x86_64" or "profile" in row):
            raise ValueError("Invalid PostgreSQL publication matrix")
        previous = row.get("previous_version")
        if previous is not None and previous != version:
            raise ValueError("Invalid previous PostgreSQL version")
        identity = ("postgresql", version, row["target"], row["architecture"])
        if identity in expected:
            raise ValueError("Duplicate publication matrix row")
        expected.add(identity)
    actual = set()
    for manifest in manifests:
        try:
            runtime = manifest["runtime"]
            if runtime == "php":
                if manifest.get("linkage") != "dynamic" or "profile" in manifest:
                    raise ValueError("Invalid planned runtime")
                identity = (runtime, manifest["php"]["version"], manifest["target"], manifest["architecture"])
            elif runtime == "redis" and "profile" not in manifest:
                identity = (runtime, manifest["redis"]["version"], manifest["target"], manifest["architecture"])
            elif runtime == "postgresql" and "profile" not in manifest and manifest.get("linkage") == "dynamic":
                identity = (runtime, manifest["postgresql"]["version"], manifest["target"], manifest["architecture"])
            else:
                raise ValueError("Invalid planned runtime")
            builder_commit = manifest["builder"]["commit"]
        except (KeyError, TypeError) as error:
            raise ValueError("Missing planned bundle metadata") from error
        if identity in actual or builder_commit != commit:
            raise ValueError("Duplicate bundle or mismatched builder commit")
        actual.add(identity)
    if actual != expected:
        raise ValueError("Published bundles do not exactly match the build plan")


def _php_section(manifests, plan):
    lines = ["## PHP runtime bundles", "", "Verified PHP CLI/FPM archives built from the selected stable patches.", "",
             "### Included bundles", "", "| PHP | Previous published patch | Target | Architecture | Build ID |",
             "| --- | --- | --- | --- | --- |"]
    previous = {}
    if plan:
        for row in plan["matrix"]["include"]:
            previous[(row["php_version"], row["target"], row["architecture"])] = row.get("previous_version")
    for manifest in sorted(manifests, key=lambda m: (tuple(map(int, m["php"]["version"].split("."))), m["target"])):
        identity = (manifest["php"]["version"], manifest["target"], manifest["architecture"])
        prior = previous.get(identity) or ("Not previously published" if plan else "Not recorded")
        lines.append("| " + " | ".join((identity[0], prior, identity[1], identity[2], manifest["build_id"])) + " |")
    lines += ["", "### Extension inventory", ""]
    for manifest in manifests:
        label = f'{manifest["php"]["version"]} / {manifest["target"]}'
        modules = manifest["php"].get("static_modules")
        if modules is not None and (not isinstance(modules, list) or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_ .-]+", name) for name in modules)):
            raise ValueError("Invalid static extension inventory")
        lines.append(f"- **{label}** static baseline (`php -n`): " + (", ".join(f"`{name}`" for name in sorted(modules)) if modules else "not recorded"))
        shared = manifest.get("shared_modules", [])
        if not isinstance(shared, list):
            raise ValueError("Invalid shared extension inventory")
        builtin = manifest.get("builtin_modules", [])
        if not isinstance(builtin, list):
            raise ValueError("Invalid built-in extension inventory")
        for module in builtin:
            if (not isinstance(module, dict) or not isinstance(module.get("name"), str)
                    or not re.fullmatch(r"[a-z][a-z0-9_]*", module["name"])
                    or module.get("always_loaded") is not True or module.get("directive") is not None):
                raise ValueError("Invalid built-in extension inventory")
            lines.append(f'  - Always-loaded built-in `{module["name"]}`: {module.get("runtime_name")} is compiled into PHP, so it has no INI or toggle.')
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
    lines += ["", "### Installation and verification", "",
              "- Download the matching `.tar.gz`, `.tar.gz.sha256`, and `.manifest.json` assets.",
              "- Verify SHA-256 before extracting into a new empty installation directory.",
              "- Use bundled `bin/php`, `bin/php-fpm`, and `bin/php-ext`, not raw `libexec` binaries.",
              "- FPM requires a non-root caller and an explicit caller-owned configuration/socket/pid/log.",
              "- `musl-dynamic` bundles link against musl libc and ship their own loader in `lib/musl/`; launch through `bin/` so the bundled loader is used.",
              "- Publication validates archive checksums, identity, packaged manifests, executable modes, and `verification=PASSED`.",
              "- This does not certify an oldest-host run, distinct-minor coexistence, complete source/toolchain authentication, legal inventory, or independent artifact signing.",
              "- PHP release information: https://www.php.net/releases/"]
    return lines


def _redis_section(manifests):
    lines = ["## Redis native bundles", "", "No profiles; Redis core built with native compilers, without Docker or TLS.", "",
             "| Redis | Target | Architecture | Linkage | Build ID |", "| --- | --- | --- | --- | --- |"]
    for manifest in sorted(manifests, key=lambda m: (tuple(map(int, m["redis"]["version"].split("."))), m["target"])):
        libc = manifest.get("libc", {})
        static = libc.get("static")
        if static is not True:
            raise ValueError("Redis bundles must be fully static musl binaries")
        lines.append("| " + " | ".join((manifest["redis"]["version"], manifest["target"], manifest["architecture"], "fully static musl", manifest["build_id"])) + " |")
    lines += ["", "- Binaries: `redis-server`, `redis-cli`, `redis-benchmark`, `redis-sentinel`, `redis-check-rdb`, `redis-check-aof`.",
              "- Redis bundles are fully static musl x86_64 binaries; they need no libc on the host.",
              "- Verify SHA-256 before extraction; retain upstream source archives and license notices.",
              "- This release does not configure or start Redis in DevStack."]
    return lines


def _postgres_section(manifests):
    ordered = sorted(manifests, key=lambda m: (tuple(map(int, m["postgresql"]["version"].split("."))), m["target"]))
    lines = ["## PostgreSQL server bundles", "",
             "No profiles; a complete server built with musl and its own loader, without Perl, Python, or Tcl embedding.", "",
             "| PostgreSQL | Target | Architecture | Linkage | Build ID |", "| --- | --- | --- | --- | --- |"]
    for manifest in ordered:
        lines.append("| " + " | ".join((manifest["postgresql"]["version"], manifest["target"], manifest["architecture"],
                                       "dynamic musl", manifest["build_id"])) + " |")
    lines += ["", "### Module inventory", ""]
    for manifest in ordered:
        label = f'{manifest["postgresql"]["version"]} / {manifest["target"]}'
        modules = manifest.get("modules")
        if not isinstance(modules, dict):
            raise ValueError("Invalid PostgreSQL module inventory")
        included, skipped = modules.get("included"), modules.get("skipped")
        if not isinstance(included, list) or not isinstance(skipped, list):
            raise ValueError("Invalid PostgreSQL module inventory")
        grouped = {}
        for module in included:
            if (not isinstance(module, dict) or not isinstance(module.get("name"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]+", module["name"])
                    or not isinstance(module.get("kind"), str) or not re.fullmatch(r"[a-z]+", module["kind"])):
                raise ValueError("Invalid PostgreSQL module inventory")
            grouped.setdefault(module["kind"], []).append(module["name"])
        lines.append(f"- **{label}** built modules, grouped by kind:")
        for kind in sorted(grouped):
            lines.append(f"  - `{kind}`: " + ", ".join(f"`{name}`" for name in sorted(grouped[kind])))
        if not grouped:
            lines.append("  - No optional modules packaged.")
        reasons = []
        for module in skipped:
            if (not isinstance(module, dict) or not isinstance(module.get("name"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]+", module["name"])
                    or not isinstance(module.get("reason"), str) or not module["reason"]):
                raise ValueError("Invalid PostgreSQL module inventory")
            reasons.append(f'`{module["name"]}` ({module["reason"]})')
        lines.append("  - Not packaged: " + ("; ".join(sorted(reasons)) if reasons else "nothing was excluded."))
    lines += ["", "### Locale", ""]
    providers = set()
    for manifest in ordered:
        locale = manifest.get("locale")
        if (not isinstance(locale, dict) or not isinstance(locale.get("default_provider"), str)
                or not isinstance(locale.get("default_locale"), str)
                or locale.get("alternate") is not None and not isinstance(locale.get("alternate"), str)):
            raise ValueError("Invalid PostgreSQL locale record")
        providers.add(locale["default_provider"])
        label = f'{manifest["postgresql"]["version"]} / {manifest["target"]}'
        description = (f'- **{label}** records the `{locale["default_provider"]}` provider with '
                       f'`{locale["default_locale"]}` as its default locale')
        if locale.get("alternate"):
            description += f' and `{locale["alternate"]}` as its alternate locale'
        lines.append(description + ".")
    # Describe only the providers these manifests actually record: 18.x offers
    # an alternate collation that 17.x does not carry.
    lines.append("")
    if providers == {"builtin"}:
        lines.append("Ordering under the `builtin` provider is deterministic code-point order: it never consults a "
                     "host libc's `strcoll`, so the same database sorts identically on every host. Collations are "
                     "named by provider and locale (the built-in `C.UTF-8` entry is catalogued as `pg_c_utf8`), "
                     "never by a bare SQL locale literal.")
    else:
        lines.append("Collations are named by provider and locale, never by a bare SQL locale literal; the default "
                     "provider recorded above determines whether ordering consults a host libc's `strcoll`.")
    lines += ["", "### Installation and verification", "",
              "- Download the matching `.tar.gz`, `.tar.gz.sha256`, and `.manifest.json` assets.",
              "- Verify SHA-256 before extracting into a new empty installation directory.",
              "- Launch through `bin/` (`postgres`, `initdb`, `pg_ctl`, `psql`, `pg_dump`, `pg_config`), not the raw ELFs in `libexec/bin/`.",
              "- A server must run as a non-root caller with an explicit caller-owned data directory and socket.",
              "- `musl-dynamic` bundles link against musl libc and ship their own loader in `lib/musl/`; launch through `bin/` so the bundled loader is used.",
              "- Publication validates archive checksums, identity, packaged manifests, executable modes, and `verification=PASSED`.",
              "- This release does not configure or start PostgreSQL in DevStack."]
    return lines


def release_notes(manifests, repository, commit, plan=None):
    """Describe exactly the bundles in this release, one section per runtime.

    The sections are chosen, not appended in place: a PostgreSQL-only release
    must not inherit the PHP header, and a mixed release states each runtime's
    own contract in a stable order.
    """
    php_manifests = [manifest for manifest in manifests if manifest.get("runtime") == "php"]
    redis_manifests = [manifest for manifest in manifests if manifest.get("runtime") == "redis"]
    postgres_manifests = [manifest for manifest in manifests if manifest.get("runtime") == "postgresql"]
    sections = []
    if php_manifests:
        sections.append(_php_section(php_manifests, plan))
    if redis_manifests:
        sections.append(_redis_section(redis_manifests))
    if postgres_manifests:
        sections.append(_postgres_section(postgres_manifests))
    if not sections:
        raise ValueError("No recognized runtime bundles to describe")
    runtimes = [name for name, present in (("PHP", php_manifests), ("Redis", redis_manifests), ("PostgreSQL", postgres_manifests)) if present]
    lines = ["# DevStack runtime bundles", "",
             "Verified " + "/".join(runtimes) + " archives from the exact build plan.", ""]
    for index, section in enumerate(sections):
        if index:
            lines.append("")
        lines += section
    lines += ["", "## Build references", "", f"- Builder commit: https://github.com/{repository}/commit/{commit}"]
    if plan:
        lines.append(f'- Workflow: https://github.com/{repository}/actions/runs/{plan["run_id"]} (attempt {plan["run_attempt"]})')
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
