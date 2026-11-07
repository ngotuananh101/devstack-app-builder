#!/usr/bin/env python3
"""Plan all supported PHP bundles and pinned static-musl Redis bundles."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REDIS_TARGET = "musl"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


php = load(ROOT / "runtimes/php/scripts/updates.py", "php_updates")
redis = load(ROOT / "runtimes/redis/scripts/source.py", "redis_source")


def published_redis(repository):
    php.validate_repository(repository)
    result = {}
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
            name = asset.get("name")
            if not isinstance(name, str) or name in entries:
                raise ValueError("Invalid/duplicate GitHub asset name")
            entries[name] = asset
        roots = set()
        for name in entries:
            if name.startswith("redis-"):
                for suffix in (".tar.gz.sha256", ".tar.gz", ".manifest.json"):
                    if name.endswith(suffix):
                        roots.add(name[:-len(suffix)])
                        break
        for root in sorted(roots):
            match = re.fullmatch(r"redis-([0-9]+\.[0-9]+\.[0-9]+)-(?:glibc|musl)-([A-Za-z0-9_]+)-([A-Za-z0-9_.-]+)", root)
            if not match:
                raise ValueError("Invalid public Redis asset identity")
            version, architecture, build_id = match.groups()
            if architecture != "x86_64":
                continue
            if f"redis-{version}-{REDIS_TARGET}-{architecture}-{build_id}" != root:
                continue  # Retired glibc bundles are never the current target.
            names = [root + suffix for suffix in (".tar.gz", ".tar.gz.sha256", ".manifest.json")]
            if any(name not in entries for name in names):
                raise ValueError("Incomplete public Redis asset group")
            for name in names:
                asset = entries[name]
                php.positive_id(asset.get("id"))
                if asset.get("state") != "uploaded" or type(asset.get("size")) is not int or asset["size"] <= 0:
                    raise ValueError("Invalid public Redis asset state")
            metadata = entries[root + ".manifest.json"]
            if metadata["size"] > php.MAX_MANIFEST_BYTES:
                raise ValueError("Oversized Redis manifest")
            manifest = php.gh_json(f"repos/{repository}/releases/assets/{metadata['id']}", asset=True)
            if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
                    or manifest["schema_version"] != 1 or manifest.get("runtime") != "redis"
                    or manifest.get("verification") != "PASSED"):
                raise ValueError("Invalid/unverified public Redis manifest")
            try:
                parts = [manifest["redis"]["version"], manifest["target"], manifest["architecture"], manifest["build_id"]]
                if parts != [version, REDIS_TARGET, architecture, build_id] or "profile" in manifest:
                    raise ValueError("Redis manifest/asset identity mismatch")
            except (KeyError, TypeError) as error:
                raise ValueError("Missing Redis manifest identity") from error
            result[(version, REDIS_TARGET, architecture)] = version
    return result


def make_plan(*, event, repository, commit, run_id, run_attempt, publish=False, release_tag="", bypass=False):
    policy = php.make_plan(event=event, repository=repository, commit=commit, run_id=run_id,
                           run_attempt=run_attempt, publish=publish, release_tag=release_tag, bypass=bypass)
    scheduled = event == "schedule"
    history = published_redis(repository)
    config = json.loads(redis.CONFIG.read_text())
    rows = []
    for version in config["versions"]:
        redis.load_source(version)  # Never plan unpinned/unvalidated sources.
        for target in (REDIS_TARGET,):
            previous = history.get((version, target, "x86_64"))
            if bypass or previous is None:
                rows.append({"redis_version": version, "target": target, "architecture": "x86_64", "previous_version": previous})
    policy["redis_matrix"] = {"include": rows}
    policy["has_updates"] = bool(policy["matrix"]["include"] or rows)
    policy["publish"] = policy["has_updates"] and (scheduled or publish)
    return policy


def write_outputs(path, policy):
    with path.open("a") as output:
        for name in ("matrix", "redis_matrix"):
            output.write(name + "=" + json.dumps(policy[name], separators=(",", ":")) + "\n")
        fields = {"has_updates": policy["has_updates"], "has_php_updates": bool(policy["matrix"]["include"]),
                  "has_redis_updates": bool(policy["redis_matrix"]["include"]), "publish": policy["publish"]}
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
    print(f"Selected {len(policy['matrix']['include'])} PHP and {len(policy['redis_matrix']['include'])} Redis bundles; publish={policy['publish']}, bypass={policy['bypass']}")


if __name__ == "__main__":
    main()
