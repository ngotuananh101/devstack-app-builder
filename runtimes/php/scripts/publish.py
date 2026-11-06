#!/usr/bin/env python3
"""Explicit publication of verified assets. Refuse duplicate tags/releases."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile


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
            if (manifest.get("schema_version") != 1 or manifest.get("runtime") != "php"
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


def publish(directory, tag, repository, commit):
    if not re.fullmatch(r"runtimes-[A-Za-z0-9][A-Za-z0-9._-]{0,79}", tag):
        raise ValueError("Invalid release tag")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Invalid repository/commit")
    assets = validate_assets(directory)
    metadata = json.loads(subprocess.check_output(["gh", "api", f"repos/{repository}"], text=True))
    if metadata["private"]:
        raise ValueError("Anonymous runtime download requires a public repository")
    for endpoint in (f"repos/{repository}/git/ref/tags/{tag}", f"repos/{repository}/releases/tags/{tag}"):
        result = subprocess.run(["gh", "api", endpoint], text=True, capture_output=True)
        if result.returncode == 0:
            raise ValueError("Release/tag already exists; never overwrite verified distributions")
        if "HTTP 404" not in result.stderr:
            raise ValueError(f"Cannot verify release absence: {result.stderr}")
    subprocess.run(["gh", "release", "create", tag, "--repo", repository, "--target", commit, "--title", tag, "--notes", "Verified PHP CLI/FPM bundles. Check SHA-256 and the manifest before installation. glibc/full includes optional shared apcu/redis; musl modules are static.", *map(str, assets)], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("tag")
    parser.add_argument("repository")
    parser.add_argument("commit")
    args = parser.parse_args()
    publish(args.directory, args.tag, args.repository, args.commit)
