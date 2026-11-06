#!/usr/bin/env python3
"""Authenticate pinned Redis release archives before confined extraction."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile

CONFIG = Path(__file__).resolve().parents[1] / "config/sources.json"


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def load_source(version):
    config = json.loads(CONFIG.read_text())
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or version not in config["versions"]:
        raise ValueError("Unsupported version; add an authenticated exact patch to config/sources.json first")
    return dict(config["versions"][version], version=version,
                url="https://download.redis.io/releases/redis-" + version + ".tar.gz",
                checksum_source=config["checksum_source"])


def verify_digest(archive, expected):
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or digest(archive) != expected:
        raise ValueError("Redis source SHA-256 verification failed")


def extract_source(archive, destination):
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        roots = set()
        seen = set()
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts:
                raise ValueError("Unsafe archive path")
            if member.issym():
                # Validate link containment, but never create upstream symlinks.
                target = PurePosixPath(member.linkname)
                depth = len(name.parent.parts)
                if target.is_absolute():
                    raise ValueError("Unsafe archive link")
                for part in target.parts:
                    depth += -1 if part == ".." else (0 if part == "." else 1)
                    if depth < 1:
                        raise ValueError("Archive link escapes its source root")
            elif not (member.isfile() or member.isdir()):
                raise ValueError("Archive special files and hard links are not allowed")
            if str(name) in seen:
                raise ValueError("Duplicate archive path")
            seen.add(str(name))
            roots.add(name.parts[0])
        if len(roots) != 1:
            raise ValueError("Source archive must have one root directory")
        # No extractall: only regular files/directories, no links or special modes.
        for member in members:
            if member.issym():
                continue
            path = destination / member.name
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, path.open("xb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                path.chmod(0o755 if member.mode & 0o111 else 0o644)
    root = destination / roots.pop()
    if not (root / "src/Makefile").is_file():
        raise ValueError("Source archive is missing src/Makefile")
    return root


def plan(arguments):
    config = json.loads(CONFIG.read_text())
    selected = {"version": config["default"], "target": "musl", "architecture": "x86_64"}
    seen = set()
    for value in arguments:
        key = "target" if value in ("glibc", "musl") else "version"
        if key in seen:
            raise ValueError("Duplicate Redis argument: " + key)
        seen.add(key)
        if key == "version":
            load_source(value)
        selected[key] = value
    selected["source"] = load_source(selected["version"])
    return selected


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", type=Path, help="Fresh build workspace for authenticated source")
    parser.add_argument("arguments", nargs="*")
    args = parser.parse_args()
    try:
        request = plan(args.arguments)
        if args.download:
            downloads = args.download / "downloads"
            downloads.mkdir(parents=True, exist_ok=False)
            archive = downloads / ("redis-" + request["version"] + ".tar.gz")
            subprocess.run(["curl", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https", "--max-time", "180", "--output", str(archive), request["source"]["url"]], check=True)
            verify_digest(archive, request["source"]["sha256"])
            extracted = extract_source(archive, args.download / "source")
            (args.download / "request.json").write_text(json.dumps(request, indent=2) + "\n")
            print(extracted)
        else:
            print(json.dumps(request))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
