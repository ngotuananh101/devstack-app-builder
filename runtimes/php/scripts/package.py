#!/usr/bin/env python3
"""Versioned archives preserve modes; never overwrite a previous asset."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile


def package(bundle, output):
    manifest = json.loads((bundle / "manifest.json").read_text())
    parts = [manifest["php"]["version"], manifest["target"], manifest["profile"], manifest["architecture"], manifest["build_id"]]
    if any(not re.fullmatch(r"[a-zA-Z0-9_.-]+", value) for value in parts):
        raise ValueError("Invalid archive identity")
    name = "php-" + "-".join(parts)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / (name + ".tar.gz")
    # Exclusive creation makes repeat builds fail instead of replacing assets.
    with archive.open("xb") as stream:
        with tarfile.open(fileobj=stream, mode="w:gz") as tar:
            tar.add(bundle, arcname=name)
    with archive.open("rb") as stream:
        sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(f"{sha256}  {archive.name}\n")
    (output / (name + ".manifest.json")).write_text(json.dumps(manifest, indent=2) + "\n")
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    manifest = json.loads((args.bundle / "manifest.json").read_text())
    if manifest.get("verification") != "PASSED":
        parser.error("Real runtime verification must pass before distribution packaging")
    print(package(args.bundle, args.output))
