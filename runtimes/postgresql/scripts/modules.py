#!/usr/bin/env python3
"""Classify and verify PostgreSQL loadable modules, client libraries, and tools."""
import argparse
import json
from pathlib import Path
import re

CONFIG = Path(__file__).resolve().parents[1] / "config/modules.json"

KINDS = ("sql", "preload", "cli", "shared")
PRELOAD_ONLY = frozenset({
    "auth_delay",
    "auto_explain",
    "basebackup_to_shell",
    "basic_archive",
    "passwordcheck",
    "pg_overexplain",
})
CLI_ONLY = frozenset({"oid2name", "vacuumlo"})
MAJORS = ("17", "18")


def _load_exclusions() -> dict[str, str]:
    if CONFIG.is_file():
        data = json.loads(CONFIG.read_text())
        return dict(data.get("exclusions", {}))
    return {}


EXCLUSIONS: dict[str, str] = _load_exclusions()


def _load_major_exclusions() -> dict[str, dict[str, str]]:
    if CONFIG.is_file():
        data = json.loads(CONFIG.read_text())
        return {major: dict(reasons)
                for major, reasons in data.get("major_exclusions", {}).items()}
    return {}


def exclusions_for(major: str) -> dict[str, str]:
    """The exclusions that apply to one major, flat ones first.

    Some modules exist in one major only, so a single flat map cannot describe
    what a bundle of a given major lacks: recording such a module flatly would
    make the other major's manifest claim it was excluded while also including
    it. `pg_logicalinspect` (contrib, added in 18) is the one such module here.
    `pg_overexplain` is 18-only too, but it needs no entry: 17's expected list
    simply does not name it, and the flat map never mentioned it either.
    """
    if not isinstance(major, str) or major not in MAJORS:
        raise ValueError(f"Unsupported major {major!r}; supported majors are {MAJORS}")
    return {**EXCLUSIONS, **_load_major_exclusions().get(major, {})}


def inventory(install_root: Path) -> list[dict]:
    """Inventory installed extensions and tools for either built or staged bundles."""
    install_root = Path(install_root)
    results = []
    control_names = set()

    # 1. SQL extensions discovered from .control files
    control_dirs = [install_root / "share/extension", install_root / "extension"]
    for cdir in control_dirs:
        if cdir.is_dir():
            for cpath in sorted(cdir.glob("*.control")):
                name = cpath.stem
                if name not in control_names:
                    control_names.add(name)
                    results.append({"name": name, "kind": "sql"})

    # 2. Preload and shared libraries from lib/*.so (excluding those with .control)
    lib_dir = install_root / "lib"
    if lib_dir.is_dir():
        for so_path in sorted(lib_dir.glob("*.so")):
            name = so_path.stem
            if name in control_names:
                continue
            if name in PRELOAD_ONLY:
                results.append({"name": name, "kind": "preload"})
            else:
                results.append({"name": name, "kind": "shared"})

    # 3. Contrib CLI tools in bin or libexec/bin
    for name in sorted(CLI_ONLY):
        if (install_root / "bin" / name).is_file() or (install_root / "libexec/bin" / name).is_file():
            results.append({"name": name, "kind": "cli"})

    return sorted(results, key=lambda item: (item["kind"], item["name"]))


def _to_name_kind_map(data) -> dict[str, str]:
    if isinstance(data, list):
        return {item["name"]: item["kind"] for item in data}
    if isinstance(data, dict):
        if any(isinstance(v, (list, tuple, set, frozenset)) for v in data.values()):
            return {name: kind for kind, names in data.items() for name in names}
        return dict(data)
    raise ValueError(f"Unsupported inventory/expected structure: {type(data)}")


def compare(inventory_data, expected_data) -> list[str]:
    """Return human-readable discrepancies between inventory and expected modules."""
    inv_map = _to_name_kind_map(inventory_data)
    exp_map = _to_name_kind_map(expected_data)

    missing = []
    unexpected = []
    wrong_kind = []

    for name in sorted(exp_map.keys()):
        if name not in inv_map:
            missing.append(f"missing {exp_map[name]}: {name}")
        elif inv_map[name] != exp_map[name]:
            wrong_kind.append(f"wrong kind: {name} is {inv_map[name]}, expected {exp_map[name]}")

    for name in sorted(inv_map.keys()):
        if name not in exp_map:
            unexpected.append(f"unexpected {inv_map[name]}: {name}")

    return missing + unexpected + wrong_kind


def load_expected(major: str) -> dict:
    """Load and validate the expected module inventory for a supported major version."""
    if not isinstance(major, str) or major not in MAJORS:
        raise ValueError(f"Unsupported major {major!r}; supported majors are {MAJORS}")
    if not CONFIG.is_file():
        raise FileNotFoundError(f"Configuration file {CONFIG} is missing")
    data = json.loads(CONFIG.read_text())
    if major not in data:
        raise ValueError(f"Major {major!r} not found in {CONFIG}")

    expected = data[major]
    if not isinstance(expected, dict):
        raise ValueError(f"Expected modules for major {major!r} must be an object")
    if set(expected.keys()) != set(KINDS):
        raise ValueError(f"Expected module kinds must match {KINDS}")

    name_pattern = re.compile(r"^[a-z_][a-z0-9_-]*$")
    seen = set()

    for kind in KINDS:
        names = expected[kind]
        if not isinstance(names, list):
            raise ValueError(f"Kind {kind!r} in expected modules for {major} must be a list")
        if names != sorted(names):
            raise ValueError(f"Kind {kind!r} in expected modules for {major} must be sorted")
        for name in names:
            if not isinstance(name, str) or not name_pattern.fullmatch(name):
                raise ValueError(f"Invalid module name {name!r} in {kind} for major {major}")
            if name in seen:
                raise ValueError(f"Duplicate module name {name!r} in expected modules for major {major}")
            seen.add(name)

    return expected


def generate(install_root: Path, major: str) -> dict:
    """The expected-module object for one real install tree or staged bundle.

    `shared` cannot be written by hand: it is whatever staging put in `lib/`,
    including the vendored shared OpenSSL libraries the staging step copies
    there. Hand-writing it is wrong in both directions, and only one of them is
    loud. An over-guess (a module the tree does not have) is a `missing` line,
    and `stage()` refuses to build. An under-guess (a module the tree does have)
    is an `unexpected` line, and both `stage()` and `_check_module_inventory`
    drop everything that is not `missing` — so it is reported to nobody. That
    silence is why `shared: []` survived against a tree with 36 shared
    libraries, and why this makes the config a generated artefact of the build
    it describes instead.

    `major` is validated even though nothing here reads it: emitting a block
    for a major `load_expected` would reject only moves the failure to the
    paste-in step, and the exclusions this file carries are per-runtime.

    A path that is not a directory, or a tree with nothing recognisable in it,
    is refused rather than returned as an empty object: an empty block is
    trivially sorted, `load_expected` accepts it, and every module guard would
    then pass while checking nothing.
    """
    if not isinstance(major, str) or major not in MAJORS:
        raise ValueError(f"Unsupported major {major!r}; supported majors are {MAJORS}")
    install_root = Path(install_root)
    if not install_root.is_dir():
        raise ValueError(f"Install tree {str(install_root)!r} is not a directory")
    generated: dict[str, list[str]] = {kind: [] for kind in KINDS}
    for row in inventory(install_root):
        generated[row["kind"]].append(row["name"])
    if not any(generated.values()):
        raise ValueError(f"Install tree {str(install_root)!r} has no modules; "
                         "refusing to generate an empty expected-module list")
    # Sorted because load_expected rejects an unsorted list; KINDS order because
    # that is the order the config file carries the blocks in.
    return {kind: sorted(generated[kind]) for kind in KINDS}


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generate", nargs=2, metavar=("INSTALL_ROOT", "MAJOR"),
                        help="Print the expected-module JSON for an installed tree")
    args = parser.parse_args()
    if args.generate is None:
        parser.error("nothing to do; pass --generate <install_root> <major>")
    install_root, major = args.generate
    print(json.dumps(generate(Path(install_root), major), indent=2))


if __name__ == "__main__":
    _main()
