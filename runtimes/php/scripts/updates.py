#!/usr/bin/env python3
"""Resolve stable PHP patches and select missing/new public runtime bundles."""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request

SERIES = ("8.5", "8.4", "8.3", "8.2")
TARGET = "musl-dynamic"
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_API_BYTES = 8 * 1024 * 1024


def version_tuple(version):
    if not isinstance(version, str) or not re.fullmatch(r"8\.[2-5]\.(?:0|[1-9][0-9]*)", version):
        raise ValueError(f"Invalid stable PHP patch: {version!r}")
    return tuple(map(int, version.split(".")))


def validate_repository(repository):
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")


def latest_php(series):
    if series not in SERIES:
        raise ValueError("Unsupported PHP series")
    request = urllib.request.Request(f"https://www.php.net/releases/index.php?json&version={series}",
                                    headers={"User-Agent": "devstack-runtime-builder", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(MAX_MANIFEST_BYTES + 1)
    if len(body) > MAX_MANIFEST_BYTES:
        raise ValueError("Oversized php.net release metadata")
    info = json.loads(body)
    if not isinstance(info, dict):
        raise ValueError("Invalid php.net release metadata")
    version = info.get("version")
    version_tuple(version)
    if not version.startswith(series + "."):
        raise ValueError("php.net returned a different PHP series")
    sources = info.get("source")
    if not isinstance(sources, list) or not any(
        isinstance(source, dict) and source.get("filename") == f"php-{version}.tar.xz"
        and isinstance(source.get("sha256"), str) and re.fullmatch(r"[a-fA-F0-9]{64}", source["sha256"])
        for source in sources
    ):
        raise ValueError("Missing stable PHP source/checksum metadata")
    return version


def gh_json(endpoint, *, asset=False):
    command = ["gh", "api", endpoint]
    if asset:
        command += ["--header", "Accept: application/octet-stream"]
    body = subprocess.check_output(command, text=True, timeout=60)
    if len(body.encode()) > (MAX_MANIFEST_BYTES if asset else MAX_API_BYTES):
        raise ValueError("Oversized GitHub API response")
    return json.loads(body)


def pages(endpoint):
    for page in range(1, 10001):
        rows = gh_json(f"{endpoint}?per_page=100&page={page}")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Invalid GitHub pagination response")
        yield from rows
        if len(rows) < 100:
            return
    raise ValueError("GitHub pagination limit exceeded")


def positive_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError("Invalid GitHub object ID")
    return value


def published_versions(repository):
    validate_repository(repository)
    result = {}
    for release in pages(f"repos/{repository}/releases"):
        if type(release.get("draft")) is not bool or type(release.get("prerelease")) is not bool:
            raise ValueError("Invalid GitHub release state")
        if release["draft"] or release["prerelease"]:
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str):
            raise ValueError("Invalid GitHub release tag")
        if not tag.startswith("runtimes-"):
            continue
        release_id = positive_id(release.get("id"))
        entries = {}
        for asset in pages(f"repos/{repository}/releases/{release_id}/assets"):
            name = asset.get("name")
            if not isinstance(name, str):
                raise ValueError("Invalid GitHub asset name")
            if name in entries:
                raise ValueError("Duplicate GitHub asset name")
            entries[name] = asset
        # Check every relevant group, including an archive whose manifest is missing.
        roots = set()
        for name in entries:
            if not name.startswith("php-"):
                continue
            for suffix in (".tar.gz.sha256", ".tar.gz", ".manifest.json"):
                if name.endswith(suffix):
                    roots.add(name[:-len(suffix)])
                    break
        for root in sorted(roots):
            if re.fullmatch(r"php-8\.[0-9]+\.[0-9]+-(?:glibc|musl)-(?:full|minimal)-[A-Za-z0-9_]+-[A-Za-z0-9_.-]+", root):
                continue  # Retired static/glibc bundles are never the current target.
            if re.fullmatch(r"php-8\.[0-9]+\.[0-9]+-" + TARGET + r"-[A-Za-z0-9_]+-[A-Za-z0-9_.-]+", root) and not re.fullmatch(r"php-8\.[2-5]\.[0-9]+-" + TARGET + r"-x86_64-[A-Za-z0-9_.-]+", root):
                continue  # A different architecture/series is not this builder's baseline.
            if not re.fullmatch(r"php-8\.[2-5]\.[0-9]+-" + TARGET + r"-x86_64-[A-Za-z0-9_.-]+", root):
                raise ValueError("Invalid public runtime asset identity")
            names = [root + suffix for suffix in (".tar.gz", ".tar.gz.sha256", ".manifest.json")]
            if any(name not in entries for name in names):
                raise ValueError("Incomplete public runtime asset group")
            for name in names:
                asset = entries[name]
                positive_id(asset.get("id"))
                if asset.get("state") != "uploaded" or type(asset.get("size")) is not int or asset["size"] <= 0:
                    raise ValueError("Invalid public runtime asset state")
            metadata = entries[root + ".manifest.json"]
            if metadata["size"] > MAX_MANIFEST_BYTES:
                raise ValueError("Oversized runtime manifest")
            manifest = gh_json(f"repos/{repository}/releases/assets/{metadata['id']}", asset=True)
            if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1 or manifest.get("runtime") != "php" or manifest.get("verification") != "PASSED":
                raise ValueError("Invalid/unverified public runtime manifest")
            try:
                version = manifest["php"]["version"]
                parts = [version, manifest["target"], manifest["architecture"], manifest["build_id"]]
                version_tuple(version)
                if any(not isinstance(part, str) for part in parts) or "php-" + "-".join(parts) != root:
                    raise ValueError("Runtime manifest/asset identity mismatch")
                if manifest["target"] != TARGET or manifest.get("linkage") != "dynamic":
                    raise ValueError("Runtime manifest is not a musl-dynamic bundle")
                if "profile" in manifest:
                    raise ValueError("Runtime manifest must not carry a profile")
                key = (".".join(version.split(".")[:2]), parts[1], parts[2])
            except (KeyError, TypeError) as error:
                raise ValueError("Missing runtime manifest identity") from error
            if key not in result or version_tuple(version) > version_tuple(result[key]):
                result[key] = version
    return result


def plan_updates(latest, published, force=False):
    rows = []
    for series in SERIES:
        if series not in latest:
            continue
        version = latest[series]
        version_tuple(version)
        if not version.startswith(series + "."):
            raise ValueError("Mismatched upstream PHP series")
        previous = published.get((series, TARGET, "x86_64"))
        if previous is not None:
            version_tuple(previous)
            if not previous.startswith(series + "."):
                raise ValueError("Mismatched published PHP series")
        if force or previous is None or version_tuple(version) > version_tuple(previous):
            rows.append({"php_series": series, "php_version": version, "target": TARGET,
                         "architecture": "x86_64", "previous_version": previous})
    if set(latest) - set(SERIES):
        raise ValueError("Unsupported upstream PHP series")
    return rows


def make_plan(*, event, repository, commit, run_id, run_attempt, publish=False, release_tag="", now=None):
    validate_repository(repository)
    if event not in {"schedule", "workflow_dispatch"} or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Invalid workflow event/commit")
    if not all(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value) for value in (run_id, run_attempt)):
        raise ValueError("Invalid workflow run identity")
    if type(publish) is not bool:
        raise ValueError("Invalid publish input")
    scheduled = event == "schedule"
    if scheduled:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        release_tag = f"runtimes-{now.astimezone(datetime.timezone.utc):%Y%m%d}-{run_id}-{run_attempt}"
    if scheduled or publish:
        if not re.fullmatch(r"runtimes-[A-Za-z0-9][A-Za-z0-9._-]{0,79}", release_tag):
            raise ValueError("Invalid or missing NEW release tag")
    if not scheduled and not publish:
        release_tag = ""  # Unused input must never become an Actions output record.
    latest = {series: latest_php(series) for series in SERIES}
    history = published_versions(repository) if scheduled or publish else {}
    rows = plan_updates(latest, history, force=not scheduled)
    return {"schema_version": 1, "event": event, "repository": repository, "commit": commit,
            "run_id": run_id, "run_attempt": run_attempt, "release_tag": release_tag,
            "publish": bool(rows) and (scheduled or publish), "has_updates": bool(rows),
            "matrix": {"include": rows}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    publish_input = os.environ.get("PUBLISH", "false")
    if publish_input not in {"true", "false"}:
        parser.error("Invalid publish input")
    try:
        plan = make_plan(event=os.environ["GITHUB_EVENT_NAME"], repository=os.environ["GITHUB_REPOSITORY"],
                         commit=os.environ["GITHUB_SHA"], run_id=os.environ["GITHUB_RUN_ID"],
                         run_attempt=os.environ["GITHUB_RUN_ATTEMPT"],
                         publish=publish_input == "true", release_tag=os.environ.get("RELEASE_TAG", ""))
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, indent=2) + "\n")
    if args.github_output:
        with args.github_output.open("a") as output:
            output.write("matrix=" + json.dumps(plan["matrix"], separators=(",", ":")) + "\n")
            output.write(f"has_updates={str(plan['has_updates']).lower()}\npublish={str(plan['publish']).lower()}\nrelease_tag={plan['release_tag']}\n")
    print(f"Selected {len(plan['matrix']['include'])} PHP bundles; publish={plan['publish']}")


if __name__ == "__main__":
    main()
