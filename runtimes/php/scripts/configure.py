#!/usr/bin/env python3
"""Resolve the PHP extension set for the single supported musl-dynamic target."""
import argparse
import json
import os
from pathlib import Path
import re

SUPPORTED = {"8.2", "8.3", "8.4", "8.5"}
# PHP 8.5 links OPcache into every binary (ext/opcache/config.m4 requests a static
# module unconditionally) and no longer produces opcache.so. Its configure flags
# also disappeared, so a phpize build succeeds but emits no module to install.
# Keep it in the configured extension set, but never ask SPC for a shared build.
BUILTIN = {"8.5": ["opcache"]}
TARGET = "musl-dynamic"
SPC_TARGET = "x86_64-linux-musl -dynamic"
# Defaults match a healthy CI runner. A build host on a network that drops
# concurrent connections can lower the parallelism (and raise retries) instead of
# patching the crafted file. Fetch settings do not affect the produced binaries.
DEFAULT_PARALLEL = 8
DEFAULT_RETRY = 3


def download_parallel():
    return _bounded_int("SPC_DOWNLOAD_PARALLEL", DEFAULT_PARALLEL, minimum=1)


def download_retry():
    return _bounded_int("SPC_DOWNLOAD_RETRY", DEFAULT_RETRY, minimum=0)


def _bounded_int(name, default, minimum):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if not re.fullmatch(r"[0-9]+", raw) or int(raw) < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return int(raw)


def resolve(php_dir, version):
    if not re.fullmatch(r"8\.[2-5](?:\.[0-9]+)?", version) or ".".join(version.split(".")[:2]) not in SUPPORTED:
        raise ValueError("Unsupported PHP version")
    config = Path(php_dir) / "config"

    def names(name):
        result = (config / name).read_text().strip()
        names = result.split(",")
        if len(names) != len(set(names)) or any(not re.fullmatch(r"[a-z][a-z0-9_]*", item) for item in names):
            raise ValueError(f"Invalid extension list: {config / name}")
        return names

    static = names("static-extensions.txt")
    configured = names("shared-extensions.txt")
    builtin = BUILTIN.get(".".join(version.split(".")[:2]), [])
    missing = set(builtin) - set(configured)
    if missing:
        raise ValueError(f"Built-in extensions must be listed in shared-extensions.txt: {sorted(missing)}")
    overlap = set(static) & set(configured)
    if overlap:
        raise ValueError(f"Extensions cannot be both static and shared: {sorted(overlap)}")
    shared = [name for name in configured if name not in builtin]
    return {"version": version, "target": TARGET, "spc_target": SPC_TARGET, "static": static, "shared": shared, "builtin": builtin}


def craft(resolution, parallel=None, retry=None):
    # SPC v3 handles shared-extensions separately from extensions. No duplicated
    # build-shared override: CraftCommand constructs that option itself.
    parallel = download_parallel() if parallel is None else parallel
    retry = download_retry() if retry is None else retry
    return (f'php-version: "{resolution["version"]}"\n'
            f'extensions: "{",".join(resolution["static"])}"\n'
            f'shared-extensions: "{",".join(resolution["shared"])}"\n'
            'sapi: [cli, fpm]\ndebug: false\nbuild-options:\n  no-strip: false\n'
            f'download-options:\n  parallel: {parallel}\n  retry: {retry}\n'
            'craft-options:\n  doctor: false\n')


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("php_dir", type=Path)
    parser.add_argument("version")
    parser.add_argument("craft_path", type=Path)
    parser.add_argument("resolution_path", type=Path)
    args = parser.parse_args()
    try:
        resolution = resolve(args.php_dir, args.version)
        parallel, retry = download_parallel(), download_retry()
    except ValueError as exc:
        parser.error(str(exc))
    args.craft_path.write_text(craft(resolution, parallel, retry))
    args.resolution_path.write_text(json.dumps(resolution, indent=2) + "\n")
