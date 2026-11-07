#!/usr/bin/env python3
"""Verify immutable SPC source before extraction. No interpreter runs in it."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import urllib.request
import zipfile


def prepare_dependencies(source, registry, directory):
    """Verify every production dist archive before Composer is allowed to see it."""
    lock_path = source / "composer.lock"
    lock = json.loads(lock_path.read_text())
    hashes = {row["name"]: row["sha256"] for row in registry}
    if set(hashes) != {package["name"] for package in lock["packages"]}:
        raise ValueError("Dependency digest registry differs from upstream Composer lock")
    directory.mkdir()  # isolated downloaded data, never our script directory
    provenance = []
    for index, package in enumerate(lock["packages"]):
        url = package["dist"]["url"]
        archive = directory / f"dependency-{index}.zip"
        request = urllib.request.Request(url, headers={"User-Agent": "DevStack-Runtime-Builder"})
        with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != hashes[package["name"]]:
            raise ValueError(f"Dependency digest mismatch: {package['name']}")
        with zipfile.ZipFile(archive) as zip:
            for member in zip.infolist():
                path = Path(member.filename)
                mode = member.external_attr >> 16
                if path.is_absolute() or ".." in path.parts or (mode & 0o170000) == 0o120000:
                    raise ValueError("Unsafe Composer dependency archive")
        provenance.append({"name": package["name"], "version": package["version"], "url": url, "sha256": digest, "reference": package["dist"]["reference"]})
        package["dist"]["url"] = archive.resolve().as_uri()
        package["dist"]["shasum"] = hashlib.sha1(archive.read_bytes()).hexdigest()
        # If local verified extraction fails, do NOT fall back to git/network.
        package.pop("source", None)
    # All bytes passed verification before any mutable lock reaches Composer.
    lock_path.write_text(json.dumps(lock, indent=2) + "\n")
    return provenance


def apply_source_patches(source, registry):
    """Apply reviewed exact-text patches to verified upstream source.

    The archive digest proves which upstream commit we extracted; this rewrites the few
    files we must change. Both the pre-image and post-image digests are pinned, so drift
    in either direction fails loudly instead of silently producing a differently behaving
    builder.
    """
    applied = []
    for patch in registry["patches"]:
        path = source / patch["file"]
        current = hashlib.sha256(path.read_bytes()).hexdigest()
        if current == patch["after_sha256"]:
            applied.append({"file": patch["file"], "status": "already-applied", "sha256": current, "reason": patch["reason"]})
            continue
        if current != patch["before_sha256"]:
            raise ValueError(f"Pinned source file changed: {patch['file']}")
        text = path.read_text()
        old, new = patch["replace"]["old"], patch["replace"]["new"]
        if text.count(old) != 1:
            raise ValueError(f"Patch anchor is not unique in {patch['file']}")
        path.write_text(text.replace(old, new))
        result = hashlib.sha256(path.read_bytes()).hexdigest()
        if result != patch["after_sha256"]:
            raise ValueError(f"Patch result digest mismatch: {patch['file']}")
        applied.append({"file": patch["file"], "status": "applied", "sha256": result, "reason": patch["reason"]})
    return applied


def extract_verified(archive, destination, expected):
    with archive.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise ValueError("SPC source digest mismatch; refusing extraction/execution")
    destination.mkdir()  # must be new and empty
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                raise ValueError(f"Unsafe source archive member: {member.name}")
        tar.extractall(destination, filter="data")
    children = list(destination.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        raise ValueError("Unexpected source archive layout")
    return children[0]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("lock", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("provenance", type=Path)
    parser.add_argument("dependency_registry", type=Path)
    parser.add_argument("source_patches", type=Path)
    args = parser.parse_args()
    lock = json.loads(args.lock.read_text())
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="spc-download-") as tmp:
        archive = Path(tmp) / "source.tar.gz"
        with urllib.request.urlopen(lock["source_url"], timeout=120) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        source = extract_verified(archive, args.destination, lock["source_sha256"])
    actual = hashlib.sha256((source / "composer.lock").read_bytes()).hexdigest()
    if actual != lock["composer_lock_sha256"]:
        raise ValueError("SPC Composer lock digest mismatch")
    # Reviewed patches land only after the immutable archive proved its identity.
    lock["source_patches"] = apply_source_patches(source, json.loads(args.source_patches.read_text()))
    # The dependency directory is fresh, outside upstream source and scripts.
    lock["composer_packages"] = prepare_dependencies(source, json.loads(args.dependency_registry.read_text()), args.destination.parent / (args.destination.name + "-dependencies"))
    args.provenance.write_text(json.dumps(lock, indent=2) + "\n")
    print(source)
