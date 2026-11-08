#!/usr/bin/env python3
"""Validate the pinned Apache version and every vendored dependency pin."""
import argparse
import hashlib
import json
from pathlib import Path
import re

CONFIG = Path(__file__).resolve().parents[1] / "config"
SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
FIELD = re.compile(r"[a-zA-Z0-9_.+-]+")


def read(name):
    return json.loads((CONFIG / name).read_text())


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_source(version):
    config = read("sources.json")
    if not SEMVER.fullmatch(version) or version not in config["versions"]:
        raise ValueError(
            "Unsupported version; add an authenticated exact patch to config/sources.json first"
        )
    return dict(
        config["versions"][version],
        version=version,
        url="https://downloads.apache.org/httpd/httpd-" + version + ".tar.gz",
        checksum_source=config["checksum_source"],
    )


def load_dependencies():
    """Return the pinned dependency list, in build order, with pins validated."""
    data = read("dependencies.json")
    if set(data) - {"comment", "dependencies"}:
        raise ValueError("Unknown key in config/dependencies.json")
    dependencies = data["dependencies"]
    names = set()
    for entry in dependencies:
        if set(entry) != {"name", "version", "url", "sha256", "license", "role"}:
            raise ValueError("Invalid dependency pin shape")
        name = entry["name"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) or name in names:
            raise ValueError("Invalid or duplicate dependency name: " + str(name))
        names.add(name)
        if not FIELD.fullmatch(entry["version"]):
            raise ValueError("Invalid dependency version for " + name)
        if not entry["url"].startswith("https://"):
            raise ValueError("Dependency URLs must be HTTPS: " + name)
        if not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"]):
            raise ValueError("Invalid dependency digest for " + name)
        if entry["role"] not in {"loader", "link", "dbd", "core"}:
            raise ValueError("Invalid dependency role for " + name)
    if names != {
        "musl", "zlib", "openssl", "pcre2", "brotli", "nghttp2", "jansson",
        "expat", "libxml2", "lua", "openldap", "sqlite", "unixodbc", "curl",
        "apr", "apr-util",
    }:
        raise ValueError("The vendored dependency set changed; review this deliberately")
    return dependencies


def verify_digest(archive, expected):
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or digest(archive) != expected:
        raise ValueError("SHA-256 verification failed: " + archive.name)


def plan(arguments):
    config = read("sources.json")
    selected = {
        "runtime": "apache",
        "version": config["default"],
        "target": "musl-dynamic",
        "architecture": "x86_64",
    }
    if len(arguments) > 1:
        raise ValueError("Expected at most one Apache version argument.")
    if arguments:
        value = arguments[0]
        if value in ("glibc", "musl", "static"):
            raise ValueError(
                "Apache is built for musl-dynamic only; the libc target is not selectable."
            )
        load_source(value)
        selected["version"] = value
    selected["source"] = load_source(selected["version"])
    selected["dependencies"] = load_dependencies()
    return selected


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("arguments", nargs="*")
    args = parser.parse_args()
    try:
        print(json.dumps(plan(args.arguments)))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
