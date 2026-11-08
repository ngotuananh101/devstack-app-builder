#!/usr/bin/env python3
"""Assemble the build request that stage.py consumes, from the plan and the tree.

build.sh runs the individual steps and each one answers for itself; this script
is only the connective tissue between them. It exists so that no shell code has to
know the shape of the request, and so that a missing artifact fails here with a
path rather than somewhere deep inside staging.
"""
import argparse
import json
from pathlib import Path
import sys


def read(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object in " + str(path))
    return value


def request(plan, sources, census, loader, install, prefix, mpm):
    source_dirs = read(sources)
    names = {entry["name"] for entry in plan["dependencies"]}
    missing = names - set(source_dirs)
    if missing or "httpd" not in source_dirs:
        raise ValueError("Source plan and extracted tree disagree: " + ", ".join(sorted(missing)))
    for name, path in source_dirs.items():
        if not Path(path).is_dir():
            raise ValueError("Extracted source is missing for " + name)
    if not Path(install).is_dir() or not Path(prefix).is_dir():
        raise ValueError("The build did not produce both the install tree and the prefix")
    # The census names shared MPMs after their source file (mpm_event); the bundle
    # and its configs name them without the prefix.
    shared = {name.partition("mpm_")[2] for name in census["shared_mpms"]}
    if mpm not in shared:
        raise ValueError("MPM %s is not built; the bundle shares %s"
                         % (mpm, ", ".join(sorted(shared))))
    return {
        "source": {key: plan["source"][key] for key in ("version", "url", "sha256")},
        "sources": source_dirs,
        "dependencies": plan["dependencies"],
        "configure": census["configure"]["arguments"],
        "compiler": census["configure"]["compiler"],
        # The host kernel UAPI include flags, recorded because they are the one
        # thing besides the compiler that the build host contributes.
        "kernel_headers": census["configure"].get("kernel_headers", ""),
        "target": plan["target"],
        "architecture": plan["architecture"],
        "mpm": mpm,
        "install": str(Path(install).resolve()),
        "prefix": str(Path(prefix).resolve()),
        "census": str(Path(census["path"]).resolve()),
        "loader": str(Path(loader).resolve()),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    parser.add_argument("sources", type=Path)
    parser.add_argument("census", type=Path)
    parser.add_argument("loader", type=Path)
    parser.add_argument("install", type=Path)
    parser.add_argument("prefix", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--mpm", default="event")
    args = parser.parse_args()
    plan = read(args.plan)
    census = read(args.census)
    census["path"] = str(args.census)
    payload = request(plan, args.sources, census, args.loader,
                      args.install, args.prefix, args.mpm)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(args.output)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError) as exc:
        sys.exit("build request failed: %s\n" % exc)
