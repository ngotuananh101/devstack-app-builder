#!/usr/bin/env python3
"""Authenticate pinned PostgreSQL release archives before confined extraction."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile

CONFIG = Path(__file__).resolve().parents[1] / "config/sources.json"
DEFAULT = "18.6"
SERIES = ("18", "17")


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def load_source(version):
    config = json.loads(CONFIG.read_text())
    if not re.fullmatch(r"[0-9]+\.[0-9]+", version) or version not in config["versions"]:
        raise ValueError("Unsupported version; add an authenticated exact patch to config/sources.json first")
    pin = config["versions"][version]
    if set(pin.keys()) != {"series", "sha256", "license"}:
        raise ValueError(f"Invalid pin attributes for PostgreSQL {version}")
    series = pin["series"]
    if series not in SERIES:
        raise ValueError(f"Series {series} not in supported series {SERIES}")
    if version.split(".")[0] != series:
        raise ValueError(f"Version {version} disagrees with series {series}")
    if not re.fullmatch(r"[a-f0-9]{64}", pin["sha256"]):
        raise ValueError(f"Invalid SHA-256 for PostgreSQL {version}")
    return {
        "version": version,
        "series": series,
        "sha256": pin["sha256"],
        "license": pin["license"],
        "url": f"https://ftp.postgresql.org/pub/source/v{version}/postgresql-{version}.tar.bz2",
        "checksum_source": config.get("checksum_source", ""),
    }


def dependencies():
    config = json.loads(CONFIG.read_text())
    deps = config.get("dependencies", {})
    for name, pin in deps.items():
        if not isinstance(pin, dict):
            raise ValueError(f"Dependency pin for {name} must be an object")
        version = pin.get("version", "")
        sha = pin.get("sha256", "")
        if "<" in (version + sha):
            raise ValueError(f"Dependency pin for {name} contains unverified placeholder")
    return deps


def verify_digest(archive, expected):
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or digest(archive) != expected:
        raise ValueError("PostgreSQL source SHA-256 verification failed")


def extract_source(archive, destination):
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, "r:bz2") as tar:
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
    if not (root / "src/include/postgres.h").is_file() or not (root / "contrib/Makefile").is_file():
        raise ValueError("Source archive is missing src/include/postgres.h or contrib/Makefile")
    return root


def plan(arguments):
    if "--profile" in arguments or any(arg in ("full", "minimal") for arg in arguments):
        raise ValueError("PostgreSQL builds all supported majors together; pass no profile.")
    if any(arg in ("glibc", "musl", "musl-dynamic") for arg in arguments):
        raise ValueError("PostgreSQL is musl-dynamic only; the libc target is not selectable.")
    if len(arguments) > 1:
        raise ValueError("Expected at most one PostgreSQL version argument.")
    version = arguments[0] if arguments else DEFAULT
    source_info = load_source(version)
    return {
        "version": version,
        "major": source_info["series"],
        "target": "musl-dynamic",
        "architecture": "x86_64",
        "source": source_info,
    }


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
            archive = downloads / ("postgresql-" + request["version"] + ".tar.bz2")
            subprocess.run(
                [
                    "curl",
                    "--fail",
                    "--location",
                    "--proto",
                    "=https",
                    "--proto-redir",
                    "=https",
                    "--max-time",
                    "180",
                    "--output",
                    str(archive),
                    request["source"]["url"],
                ],
                check=True,
            )
            verify_digest(archive, request["source"]["sha256"])
            extracted = extract_source(archive, args.download / "source")
            (args.download / "request.json").write_text(json.dumps(request, indent=2) + "\n")
            print(extracted)
        else:
            print(json.dumps(request))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
