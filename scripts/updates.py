#!/usr/bin/env python3
"""Plan all supported PHP bundles and pinned Redis/Apache/PostgreSQL bundles."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REDIS_TARGET = "musl"
APACHE_TARGET = "musl-dynamic"
POSTGRES_TARGET = "musl-dynamic"

# PostgreSQL version records are two-part (18.6, 17.11) or three-part; every
# other runtime publishes strict three-part identities.
THREE_PART = r"[0-9]+\.[0-9]+\.[0-9]+"
POSTGRES_VERSION = r"[0-9]+\.[0-9]+(?:\.[0-9]+)?"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


php = load(ROOT / "runtimes/php/scripts/updates.py", "php_updates")
redis = load(ROOT / "runtimes/redis/scripts/source.py", "redis_source")
apache = load(ROOT / "runtimes/apache/scripts/source.py", "apache_source")
postgresql = load(ROOT / "runtimes/postgresql/scripts/source.py", "postgresql_source")


def published_runtime(repository, *, name, target, version=THREE_PART, validate=None):
    """Read the published identities of one runtime from public release assets.

    Every asset group is validated, not just the ones this run cares about: a
    half-uploaded group or an unverified manifest means the historical record
    cannot be trusted, and a planner that guessed past it could rebuild or skip a
    version forever. Anything that is not this runtime's current target is
    recognised only so it can be ignored deliberately.
    """
    php.validate_repository(repository)
    result = {}
    pattern = re.compile(
        re.escape(name) + r"-(" + version + r")-(musl-dynamic|glibc|musl)"
        r"-([A-Za-z0-9_]+)-([A-Za-z0-9_.-]+)")
    for release in php.pages(f"repos/{repository}/releases"):
        if type(release.get("draft")) is not bool or type(release.get("prerelease")) is not bool:
            raise ValueError("Invalid GitHub release state")
        if release["draft"] or release["prerelease"]:
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str):
            raise ValueError("Invalid GitHub release tag")
        if not tag.startswith("runtimes-"):
            continue
        release_id = php.positive_id(release.get("id"))
        entries = {}
        for asset in php.pages(f"repos/{repository}/releases/{release_id}/assets"):
            asset_name = asset.get("name")
            if not isinstance(asset_name, str) or asset_name in entries:
                raise ValueError("Invalid/duplicate GitHub asset name")
            entries[asset_name] = asset
        roots = set()
        for asset_name in entries:
            if asset_name.startswith(name + "-"):
                for suffix in (".tar.gz.sha256", ".tar.gz", ".manifest.json"):
                    if asset_name.endswith(suffix):
                        roots.add(asset_name[:-len(suffix)])
                        break
        for root in sorted(roots):
            match = pattern.fullmatch(root)
            if not match:
                raise ValueError("Invalid public %s asset identity" % name)
            version, published_target, architecture, build_id = match.groups()
            if architecture != "x86_64" or published_target != target:
                continue  # Another architecture or a retired target is not this baseline.
            names = [root + suffix for suffix in (".tar.gz", ".tar.gz.sha256", ".manifest.json")]
            if any(group not in entries for group in names):
                raise ValueError("Incomplete public %s asset group" % name)
            for group in names:
                asset = entries[group]
                php.positive_id(asset.get("id"))
                if asset.get("state") != "uploaded" or type(asset.get("size")) is not int or asset["size"] <= 0:
                    raise ValueError("Invalid public %s asset state" % name)
            metadata = entries[root + ".manifest.json"]
            if metadata["size"] > php.MAX_MANIFEST_BYTES:
                raise ValueError("Oversized %s manifest" % name)
            manifest = php.gh_json(f"repos/{repository}/releases/assets/{metadata['id']}", asset=True)
            if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
                    or manifest["schema_version"] != 1 or manifest.get("runtime") != name
                    or manifest.get("verification") != "PASSED"):
                raise ValueError("Invalid/unverified public %s manifest" % name)
            try:
                parts = [manifest[name]["version"], manifest["target"],
                         manifest["architecture"], manifest["build_id"]]
                if parts != [version, target, architecture, build_id] or "profile" in manifest:
                    raise ValueError("%s manifest/asset identity mismatch" % name)
                if validate is not None:
                    validate(manifest, version)
            except (KeyError, TypeError) as error:
                raise ValueError("Missing %s manifest identity" % name) from error
            result[(version, target, architecture)] = version
    return result


def validate_postgres_manifest(manifest, version):
    """A PostgreSQL bundle must be dynamic and must declare its own major."""
    if manifest.get("linkage") != "dynamic":
        raise ValueError("Invalid/unverified public PostgreSQL manifest")
    if manifest["postgresql"].get("major") != version.split(".")[0]:
        raise ValueError("PostgreSQL manifest major disagrees with version")


def published_redis(repository):
    return published_runtime(repository, name="redis", target=REDIS_TARGET)


def published_apache(repository):
    return published_runtime(repository, name="apache", target=APACHE_TARGET)


def published_postgresql(repository):
    return published_runtime(repository, name="postgresql", target=POSTGRES_TARGET,
                             version=POSTGRES_VERSION, validate=validate_postgres_manifest)


def pinned_matrix(module, versions, history, *, name, target, bypass):
    """Plan every pinned version of one runtime that is not published yet."""
    rows = []
    for version in versions:
        module.load_source(version)  # Never plan unpinned/unvalidated sources.
        previous = history.get((version, target, "x86_64"))
        if bypass or previous is None:
            rows.append({name + "_version": version, "target": target,
                         "architecture": "x86_64", "previous_version": previous})
    return {"include": rows}


def apache_matrix(repository, *, bypass):
    return pinned_matrix(apache, apache.read("sources.json")["versions"], published_apache(repository),
                         name="apache", target=APACHE_TARGET, bypass=bypass)


def postgresql_matrix(repository, *, bypass):
    versions = json.loads(postgresql.CONFIG.read_text())["versions"]
    return pinned_matrix(postgresql, versions, published_postgresql(repository),
                         name="postgresql", target=POSTGRES_TARGET, bypass=bypass)


def make_plan(*, event, repository, commit, run_id, run_attempt, publish=False, release_tag="", bypass=False):
    policy = php.make_plan(event=event, repository=repository, commit=commit, run_id=run_id,
                           run_attempt=run_attempt, publish=publish, release_tag=release_tag, bypass=bypass)
    scheduled = event == "schedule"
    redis_history = published_redis(repository)
    policy["redis_matrix"] = pinned_matrix(redis, json.loads(redis.CONFIG.read_text())["versions"], redis_history,
                                           name="redis", target=REDIS_TARGET, bypass=bypass)
    policy["apache_matrix"] = apache_matrix(repository, bypass=bypass)
    policy["postgres_matrix"] = postgresql_matrix(repository, bypass=bypass)
    policy["has_updates"] = bool(policy["matrix"]["include"]
                                 or policy["redis_matrix"]["include"]
                                 or policy["apache_matrix"]["include"]
                                 or policy["postgres_matrix"]["include"])
    policy["publish"] = policy["has_updates"] and (scheduled or publish)
    return policy


def write_outputs(path, policy):
    with path.open("a") as output:
        for name in ("matrix", "redis_matrix", "apache_matrix", "postgres_matrix"):
            output.write(name + "=" + json.dumps(policy[name], separators=(",", ":")) + "\n")
        fields = {"has_updates": policy["has_updates"], "has_php_updates": bool(policy["matrix"]["include"]),
                  "has_redis_updates": bool(policy["redis_matrix"]["include"]),
                  "has_apache_updates": bool(policy["apache_matrix"]["include"]),
                  "has_postgres_updates": bool(policy["postgres_matrix"]["include"]),
                  "publish": policy["publish"]}
        for name, value in fields.items():
            output.write(f"{name}={str(value).lower()}\n")
        output.write("release_tag=" + policy["release_tag"] + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    publish = os.environ.get("PUBLISH", "false")
    bypass = os.environ.get("BYPASS", "false")
    if publish not in {"true", "false"}:
        parser.error("Invalid publish input")
    if bypass not in {"true", "false"}:
        parser.error("Invalid bypass input")
    try:
        policy = make_plan(event=os.environ["GITHUB_EVENT_NAME"], repository=os.environ["GITHUB_REPOSITORY"],
                           commit=os.environ["GITHUB_SHA"], run_id=os.environ["GITHUB_RUN_ID"],
                           run_attempt=os.environ["GITHUB_RUN_ATTEMPT"],
                           publish=publish == "true", release_tag=os.environ.get("RELEASE_TAG", ""),
                           bypass=bypass == "true")
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(policy, indent=2) + "\n")
    if args.github_output:
        write_outputs(args.github_output, policy)
    print(f"Selected {len(policy['matrix']['include'])} PHP, {len(policy['redis_matrix']['include'])} Redis, "
          f"{len(policy['apache_matrix']['include'])} Apache and {len(policy['postgres_matrix']['include'])} "
          f"PostgreSQL bundles; publish={policy['publish']}, bypass={policy['bypass']}")


if __name__ == "__main__":
    main()
