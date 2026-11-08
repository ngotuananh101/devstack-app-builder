#!/usr/bin/env python3
"""Download, authenticate and extract every pinned source into fresh directories.

Nothing is extracted before its digest matches the pin. Each archive lands in its
own new directory, and each source tree gets its own, so a malicious archive cannot
reach a sibling.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import time

TAR_SAFE_TYPES = {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


FETCH_ATTEMPTS = 4
FETCH_RETRY_DELAY = 5


def download(url, destination):
    """Download one pinned source, retrying a transient gateway failure.

    GitHub's release CDN answers 504 for a single asset while the rest of the
    same release serves normally, and the failing assets recover at different
    moments; a build needs every one of them at once. Curl's own ``--retry``
    covers transfer errors but a 504 arrives as an ordinary HTTP status, so the
    attempts are made here. A digest that does not match its pin is never
    retried: that is a changed archive, not a flaky connection.
    """
    if destination.exists():
        raise ValueError("Refusing to reuse an existing download: " + destination.name)
    last = None
    for attempt in range(FETCH_ATTEMPTS):
        if attempt:
            time.sleep(FETCH_RETRY_DELAY * attempt)
        last = subprocess.run(
            [
                "curl", "--fail", "--silent", "--show-error", "--location",
                "--proto", "=https", "--proto-redir", "=https",
                "--max-time", "600", "--output", str(destination), url,
            ],
            text=True, capture_output=True,
        )
        if last.returncode == 0:
            return
    destination.unlink(missing_ok=True)
    raise ValueError(
        "Cannot download %s after %d attempts: %s"
        % (url, FETCH_ATTEMPTS, (last.stderr or "").strip()[-1000:])
    )


def members(tar):
    """Validate every member before extracting anything."""
    listing = tar.getmembers()
    roots = set()
    seen = set()
    for member in listing:
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts or not name.parts:
            raise ValueError("Unsafe archive path: " + member.name)
        if member.issym() or member.islnk():
            target = PurePosixPath(member.linkname)
            if target.is_absolute() or ".." in target.parts:
                raise ValueError("Unsafe archive link: " + member.name)
        elif member.type not in TAR_SAFE_TYPES:
            raise ValueError("Unsupported archive member type: " + member.name)
        if str(name) in seen:
            raise ValueError("Duplicate archive path: " + member.name)
        seen.add(str(name))
        roots.add(name.parts[0])
    if len(roots) != 1:
        raise ValueError("Source archive must have exactly one root directory")
    return listing, roots.pop()


def extract(archive, destination):
    """Extract regular files and directories only; links are never created.

    Each member keeps its archived mtime. Extraction normally stamps everything
    with the current time, which makes the order of the tar the order of the
    files: jansson ships aclocal.m4 before configure.ac, so the generated file
    would look stale, make would try to regenerate it with the packaged
    `missing` script, and the build would die on an autotools version mismatch.
    Directories are stamped last, deepest first, so writing their children does
    not disturb them.
    """
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, "r:*") as tar:
        listing, root = members(tar)
        directories = []
        for member in listing:
            if member.issym() or member.islnk():
                continue
            path = destination / member.name
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
                directories.append((path, member))
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, path.open("xb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                path.chmod(0o755 if member.mode & 0o111 else 0o644)
                os.utime(path, (member.mtime, member.mtime))
        for path, member in sorted(directories, key=lambda row: len(row[0].parts), reverse=True):
            os.utime(path, (member.mtime, member.mtime))
    return destination / root


def fetch_one(entry, work):
    name = entry["name"]
    archive = work / "downloads" / (name + ".tar.gz")
    download(entry["url"], archive)
    actual = digest(archive)
    if actual != entry["sha256"]:
        raise ValueError(
            "SHA-256 mismatch for %s: expected %s, got %s"
            % (name, entry["sha256"], actual)
        )
    return extract(archive, work / "sources" / name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("work", type=Path)
    parser.add_argument("request", type=Path, help="plan JSON printed by source.py")
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    (args.work / "downloads").mkdir()
    (args.work / "sources").mkdir()

    resolved = {}
    for entry in request["dependencies"]:
        resolved[entry["name"]] = str(fetch_one(entry, args.work))
    httpd = request["source"]
    resolved["httpd"] = str(fetch_one(dict(httpd, name="httpd"), args.work))
    print(json.dumps(resolved, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc) + "\n")
