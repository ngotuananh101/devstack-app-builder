#!/usr/bin/env python3
"""Build the pinned musl loader that a musl-dynamic httpd bundle ships.

The loader is also libc, so it must be built from the exact musl release the
compiler targets. The pinned source's version and the version the toolchain's own
loader reports are compared before anything is compiled; a mismatch fails instead
of shipping a loader whose ABI may not match every object built with musl-gcc.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch

LOADER_NAME = "ld-musl-x86_64.so.1"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def banner_version(path):
    result = subprocess.run([str(path)], text=True, capture_output=True)
    match = re.search(r"^Version ([0-9]+\.[0-9]+\.[0-9]+)$", result.stderr, re.MULTILINE)
    if not match:
        raise ValueError("Not a musl loader: " + str(path))
    return match[1]


def toolchain_loader(compiler):
    """Ask the compiler for the loader it links every binary against."""
    path = subprocess.check_output(
        [compiler, "-print-file-name=" + LOADER_NAME], text=True
    ).strip()
    if not Path(path).is_file():
        raise ValueError("The " + compiler + " toolchain ships no loader: " + path)
    return Path(path)


def compile_loader(source, build, compiler):
    build.mkdir(parents=True, exist_ok=False)
    environment = {"PATH": "/usr/bin:/bin", "CC": compiler, "LC_ALL": "C", "SOURCE_DATE_EPOCH": "0"}
    for command in (["sh", str(source / "configure"), "--prefix=/nonexistent",
                     "--disable-static", "--enable-shared"], ["make", "-j", "2"]):
        result = subprocess.run(command, cwd=build, env=environment,
                                text=True, capture_output=True)
        if result.returncode != 0:
            raise ValueError("%s failed:\n%s%s" % (
                command[0], result.stdout[-4000:], result.stderr[-4000:]))
    libc = build / "lib/libc.so"
    if not libc.is_file():
        raise ValueError("musl build did not produce lib/libc.so")
    return libc


def inspect(libc, version):
    header = subprocess.check_output(["readelf", "-hW", str(libc)], text=True)
    if "ELF64" not in header or "Advanced Micro Devices X86-64" not in header:
        raise ValueError("musl loader is not a Linux x86_64 ELF64 object")
    if "DYN (Shared object file)" not in header:
        raise ValueError("musl loader is not a shared object")
    dynamic = subprocess.check_output(["readelf", "-dW", str(libc)], text=True)
    if re.search(r"\((?:NEEDED|RPATH|RUNPATH)\)", dynamic):
        raise ValueError("musl loader must not have dependencies or search paths")
    symbols = subprocess.check_output(["readelf", "--dyn-syms", "-W", str(libc)], text=True)
    exported = {line.split()[-1] for line in symbols.splitlines()
                if len(line.split()) >= 8 and line.split()[6] != "UND"}
    missing = {"memcpy", "memset", "__stack_chk_fail", "__libc_start_main", "_dlstart"} - exported
    if missing:
        raise ValueError("musl loader is missing required exports: " + ", ".join(sorted(missing)))
    actual = banner_version(libc)
    if actual != version:
        raise ValueError("musl loader reports %s, pinned %s" % (actual, version))
    # musl links its loader without -soname: the loader matches the reserved libc
    # names (libc.so and friends) to itself in ldso/dynlink.c.
    return {"path": "lib/musl/" + LOADER_NAME, "sha256": digest(libc), "version": version,
            "satisfies": ["libc.so"], "entry": "_dlstart", "soname": None}


def source_tree(pin, work):
    """Return the verified musl source tree, reusing fetch.py's extraction.

    fetch.py verifies every archive before extracting, so when it has already
    unpacked the loader's source there is nothing left to check and re-extracting
    would fail on the existing directory. The archive digest is still compared, so
    a tree that fetch.py did not produce cannot slip in.
    """
    download = work / "downloads" / (pin["name"] + ".tar.gz")
    extracted = work / "sources" / pin["name"]
    if download.is_file():
        actual = digest(download)
        if actual != pin["sha256"]:
            raise ValueError("musl source digest mismatch: " + actual)
    else:
        fetch.download(pin["url"], download)
        actual = digest(download)
        if actual != pin["sha256"]:
            raise ValueError("musl source digest mismatch: " + actual)
    if extracted.is_dir():
        # fetch.py extracts into sources/<name>/<archive-root>, so the tree the
        # build needs is the single directory inside it, not the wrapper.
        roots = [path for path in extracted.iterdir() if path.is_dir()]
        if len(roots) != 1:
            raise ValueError("musl extraction does not hold exactly one root: " + str(extracted))
        return roots[0]
    return fetch.extract(download, extracted)


def build(pin, work, compiler):
    toolchain = banner_version(toolchain_loader(compiler))
    if toolchain != pin["version"]:
        raise ValueError(
            "Toolchain musl %s differs from the pinned loader %s; the bundle's loader "
            "and its compiled objects must come from the same release"
            % (toolchain, pin["version"]))
    source = source_tree(dict(pin, name="musl"), work)
    libc = compile_loader(source, work / "musl-build", compiler)
    record = inspect(libc, pin["version"])
    record["source"] = str(libc)
    record["license"] = {"path": str(source / "COPYRIGHT"), "sha256": digest(source / "COPYRIGHT")}
    return libc, record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path, help="plan JSON printed by source.py")
    parser.add_argument("work", type=Path, help="build workspace, already holding downloads/")
    parser.add_argument("metadata", type=Path)
    parser.add_argument("--compiler", default="cc")
    parser.add_argument("--probe-only", action="store_true",
                        help="check the toolchain against the pin and stop")
    args = parser.parse_args()
    # compile_loader runs configure with cwd set to the build directory, so a
    # relative workspace would be re-resolved from there and find nothing.
    args.work = args.work.resolve()
    request = json.loads(args.request.read_text())
    pins = [entry for entry in request["dependencies"] if entry["role"] == "loader"]
    if len(pins) != 1:
        raise ValueError("The plan must pin exactly one loader source")
    pin = dict(pins[0], name="musl")
    if args.probe_only:
        actual = banner_version(toolchain_loader(args.compiler))
        if actual != pin["version"]:
            raise ValueError("Toolchain musl %s is not the pinned %s" % (actual, pin["version"]))
        print(actual)
        return
    loader, record = build(pin, args.work, args.compiler)
    args.metadata.parent.mkdir(parents=True, exist_ok=True)
    args.metadata.write_text(json.dumps(record, indent=2) + "\n")
    print(loader)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit("musl loader build failed: %s\n" % exc)
