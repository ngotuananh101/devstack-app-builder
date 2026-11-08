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


# A build takes about an hour, so waiting minutes on a flaky CDN is cheap next
# to losing it. The delays are cumulative: 15s, 45s, 1m45s, 3m45s, 6m45s.
FETCH_RETRY_DELAYS = (15, 30, 60, 120, 180)
FETCH_ATTEMPTS = len(FETCH_RETRY_DELAYS) + 1


# A read-only GitHub release proxy, used only after every direct attempt failed.
# GitHub's `releases/download` surface has answered 504 for every asset for hours
# while the rest of the internet served normally. The pin is what makes the
# substitution safe: the mirror's bytes are verified exactly like the origin's,
# so a mirror that rewrites, truncates or serves an error page is refused.
MIRROR = "https://gh-proxy.org/"


def mirror_for(url):
    """The mirror URL for a GitHub release asset, or None for anything else.

    Only `github.com/.../releases/download/...` has the failure mode this exists
    for. Sending downloads.apache.org or zlib.net through a proxy would turn a
    clear upstream answer into somebody else's bug report.
    """
    if url.startswith("https://github.com/") and "/releases/download/" in url:
        return MIRROR + url
    return None


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
            time.sleep(FETCH_RETRY_DELAYS[attempt - 1])
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
    # Only now, with every direct attempt spent, is a mirror worth trying.
    # Doing it earlier would send traffic through a third party on days when
    # GitHub is perfectly healthy.
    mirror = mirror_for(url)
    if mirror is None:
        destination.unlink(missing_ok=True)
        raise ValueError(
            "Cannot download %s after %d attempts: %s"
            % (url, FETCH_ATTEMPTS, (last.stderr or "").strip()[-1000:])
        )
    direct_error = (last.stderr or "").strip()[-400:]
    last = subprocess.run(
        [
            "curl", "--fail", "--silent", "--show-error", "--location",
            "--proto", "=https", "--proto-redir", "=https",
            "--max-time", "600", "--output", str(destination), mirror,
        ],
        text=True, capture_output=True,
    )
    if last.returncode != 0:
        destination.unlink(missing_ok=True)
        raise ValueError(
            "Cannot download %s after %d attempts, nor from %s: %s (direct: %s)"
            % (url, FETCH_ATTEMPTS, MIRROR, (last.stderr or "").strip()[-400:],
               direct_error)
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


def content_type(url):
    """The Content-Type a HEAD request reports, or None.

    Only ever used to explain a digest mismatch. A proxy or a CDN error page can
    answer ``200`` with an HTML body, and a bare digest comparison cannot tell
    that apart from an upstream that genuinely replaced the archive.
    """
    try:
        result = subprocess.run(
            [
                "curl", "--silent", "--show-error", "--location",
                "--proto", "=https", "--proto-redir", "=https",
                "--max-time", "60", "--head", "--write-out", "%{content_type}",
                "--output", "/dev/null", url,
            ],
            text=True, capture_output=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def fetch_one(entry, work):
    name = entry["name"]
    archive = work / "downloads" / (name + ".tar.gz")
    download(entry["url"], archive)
    actual = digest(archive)
    if actual != entry["sha256"]:
        # Report what arrived, not just that it differed. Size and content type
        # are what separate "upstream replaced the archive" (fix the pin) from
        # "something in the path answered 200 with an error page" (fix the
        # route); without them a CI failure is not readable.
        size = archive.stat().st_size
        kind = content_type(entry["url"])
        # Leave nothing behind. `download` refuses to reuse an existing file, so
        # a kept archive would make the next run fail on the leftover instead of
        # re-fetching, and a bad archive is not worth the disk either.
        archive.unlink()
        raise ValueError(
            "SHA-256 mismatch for %s: expected %s, got %s (%d bytes, "
            "content-type %s)"
            % (name, entry["sha256"], actual, size, kind or "unknown")
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
