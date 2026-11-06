#!/usr/bin/env python3
"""Resolve requested modes; SPC may add REQUIRED static dependencies later."""
import argparse
import json
from pathlib import Path
import re

SUPPORTED = {"8.2", "8.3", "8.4", "8.5"}


def resolve(php_dir, target, profile, version):
    if target not in {"glibc", "musl"} or profile not in {"minimal", "full"}:
        raise ValueError("Unsupported target/profile")
    if not re.fullmatch(r"8\.[2-5](?:\.[0-9]+)?", version) or ".".join(version.split(".")[:2]) not in SUPPORTED:
        raise ValueError("Unsupported PHP version")
    def names(path):
        result = path.read_text().strip().split(",")
        if len(result) != len(set(result)) or any(not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in result):
            raise ValueError(f"Invalid extension profile: {path}")
        return result
    selected = names(Path(php_dir) / "config/profiles" / f"{profile}.txt")
    supported_shared = names(Path(php_dir) / "config/shared-glibc.txt") if target == "glibc" else []
    shared = [name for name in selected if name in supported_shared]
    static = [name for name in selected if name not in shared]
    assert not set(static) & set(shared)
    return {"version": version, "target": target, "profile": profile, "selected": selected, "static": static, "shared": shared}


def craft(resolution):
    # SPC v3 handles shared-extensions separately from extensions. No duplicated
    # build-shared override: CraftCommand constructs that option itself.
    return (f'php-version: "{resolution["version"]}"\n'
            f'extensions: "{",".join(resolution["static"])}"\n'
            f'shared-extensions: "{",".join(resolution["shared"])}"\n'
            'sapi: [cli, fpm]\ndebug: false\nbuild-options:\n  no-strip: false\n'
            'download-options:\n  parallel: 8\n  retry: 3\n'
            'craft-options:\n  doctor: false\n')


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("php_dir", type=Path)
    parser.add_argument("target")
    parser.add_argument("profile")
    parser.add_argument("version")
    parser.add_argument("craft_path", type=Path)
    parser.add_argument("resolution_path", type=Path)
    args = parser.parse_args()
    try:
        resolution = resolve(args.php_dir, args.target, args.profile, args.version)
    except ValueError as exc:
        parser.error(str(exc))
    args.craft_path.write_text(craft(resolution))
    args.resolution_path.write_text(json.dumps(resolution, indent=2) + "\n")
