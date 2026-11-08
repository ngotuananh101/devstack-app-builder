#!/usr/bin/env python3
"""Publish a verified Apache bundle as a versioned archive and its sidecars.

The identity of the archive is the version, the target, the architecture and the
build id, which is a digest over the staged tree: the same request built twice
produces the same name only if it produced the same bytes. Assets are created
exclusively, so a rebuild never replaces a published archive.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import tarfile

IDENTITY = re.compile(r"[a-zA-Z0-9_.-]+")
# A bundle without these is not an Apache bundle, whatever its manifest says.
REQUIRED = (
    "bin/httpd",
    "bin/apachectl",
    "lib/musl/ld-musl-x86_64.so.1",
    "modules/mod_ssl.so",
    "modules/mod_http2.so",
    "modules/mod_md.so",
    "modules/mod_brotli.so",
    "modules/mod_lua.so",
    "lib/apr-util-1/apr_dbd_sqlite3-1.so",
)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def package(bundle, output):
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest.get("runtime") != "apache":
        raise ValueError("This packager only accepts Apache bundles")
    if manifest.get("verification") != "PASSED":
        raise ValueError("Real Apache verification must pass before packaging")
    missing = [relative for relative in REQUIRED if not (bundle / relative).is_file()]
    if missing:
        raise ValueError("The bundle is incomplete: " + ", ".join(missing))
    parts = [manifest["apache"]["version"], manifest["target"],
             manifest["architecture"], manifest["build_id"]]
    if any(not IDENTITY.fullmatch(part) for part in parts):
        raise ValueError("Invalid Apache archive identity")
    name = "apache-" + "-".join(parts)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / (name + ".tar.gz")
    checksum = output / (name + ".tar.gz.sha256")
    sidecar = output / (name + ".manifest.json")
    paths = (archive, checksum, sidecar)
    if any(path.exists() or path.is_symlink() for path in paths):
        raise FileExistsError("Refusing to replace Apache assets: " + name)
    created = []
    try:
        # Reserve the whole asset set before writing any of it, and unwind only
        # what this run created, so a failure never leaves a half-published name.
        with archive.open("xb") as stream:
            created.append(archive)
            with checksum.open("x") as sums:
                created.append(checksum)
                with sidecar.open("x") as metadata:
                    created.append(sidecar)
                    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
                        tar.add(bundle, arcname=name)
                    stream.flush()
                    sums.write(digest(archive) + "  " + archive.name + "\n")
                    metadata.write(json.dumps(manifest, indent=2) + "\n")
    except Exception:
        for path in created:
            path.unlink()
        raise
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        print(package(args.bundle, args.output))
    except (ValueError, OSError, FileExistsError) as exc:
        sys.exit("packaging failed: %s\n" % exc)
