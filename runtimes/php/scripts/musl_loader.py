#!/usr/bin/env python3
"""Build the pinned musl loader that a musl-dynamic PHP bundle ships.

The loader is also libc, so it must be built from the exact musl release the
compiler targets. The pinned source and the toolchain's bundled musl version are
compared before anything is compiled; a mismatch fails instead of shipping a
loader that may not match the PHP ABI.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def config(php_dir):
    data = json.loads((Path(php_dir) / "config/musl.json").read_text())
    required = {"version", "url", "sha256"}
    if set(data) != required:
        raise ValueError("Invalid musl pin")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", data["version"]):
        raise ValueError("Invalid musl version")
    if not data["url"].startswith("https://musl.libc.org/releases/") or not data["url"].endswith(".tar.gz"):
        raise ValueError("Invalid musl source URL")
    if not re.fullmatch(r"[a-f0-9]{64}", data["sha256"]):
        raise ValueError("Invalid musl source digest")
    return data


def fetch(pin, downloads):
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / f"musl-{pin['version']}.tar.gz"
    if archive.exists():
        if digest(archive) != pin["sha256"]:
            raise ValueError(f"Pinned musl archive changed on disk: {archive}")
        return archive
    temporary = archive.with_name(archive.name + ".part")
    subprocess.run(["curl", "--fail", "--silent", "--show-error", "--location",
                    "--proto", "=https", "--proto-redir", "=https", "--max-time", "180",
                    "--output", str(temporary), pin["url"]], check=True)
    actual = digest(temporary)
    if actual != pin["sha256"]:
        temporary.unlink()
        raise ValueError(f"musl source digest mismatch: {actual}")
    temporary.rename(archive)
    return archive


def extract(archive, destination):
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, "r:gz") as tar:
        roots = set()
        seen = set()
        for member in tar.getmembers():
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts:
                raise ValueError("Unsafe musl archive path")
            if not (member.isfile() or member.isdir() or member.issym()):
                raise ValueError("Unsafe musl archive member")
            if member.issym():
                target = PurePosixPath(member.linkname)
                if target.is_absolute() or ".." in target.parts:
                    raise ValueError("Unsafe musl archive link")
            if str(name) in seen:
                raise ValueError("Duplicate musl archive path")
            seen.add(str(name))
            roots.add(name.parts[0])
        if len(roots) != 1:
            raise ValueError("musl archive must have one root directory")
        root = roots.pop()
        # musl's own build system needs its file modes and links; extractor filters
        # the members validated above and nothing else.
        tar.extractall(destination, filter="data", members=[
            member for member in tar.getmembers() if not member.issym()])
    return destination / root


def toolchain_musl_version(zig_dir):
    version_header = Path(zig_dir) / "lib/libc/musl/src/internal/version.h"
    if not version_header.is_file():
        raise ValueError(f"Cannot confirm the toolchain's musl version: {version_header}")
    match = re.search(r'#define VERSION "([0-9]+\.[0-9]+\.[0-9]+)"', version_header.read_text())
    if not match:
        raise ValueError(f"Unreadable toolchain musl version: {version_header}")
    return match.group(1)


def compile(source, build, compiler):
    # musl is built out of tree: configure runs from the (still empty) build dir.
    build.mkdir(parents=True, exist_ok=False)
    configure = subprocess.run([str(source / "configure"), "--prefix=/nonexistent",
                                "--disable-static", "--enable-shared"],
                               cwd=build, env={"PATH": "/usr/bin:/bin", "CC": compiler,
                                               "LC_ALL": "C", "SOURCE_DATE_EPOCH": "0"},
                               text=True, capture_output=True)
    if configure.returncode != 0:
        raise ValueError("musl configure failed:\n" + configure.stdout + configure.stderr)
    make = subprocess.run(["make", "-j", "8"], cwd=build,
                          env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "SOURCE_DATE_EPOCH": "0"},
                          text=True, capture_output=True)
    if make.returncode != 0:
        raise ValueError("musl make failed:\n" + make.stdout[-4000:] + make.stderr[-4000:])
    libc = build / "lib/libc.so"
    if not libc.is_file():
        raise ValueError("musl build did not produce lib/libc.so")
    return libc


def inspect(libc, version):
    header = subprocess.check_output(["readelf", "-hW", str(libc)], text=True)
    if "Machine:" not in header or "Advanced Micro Devices X86-64" not in header or "ELF64" not in header:
        raise ValueError("musl loader is not a Linux x86_64 ELF64 object")
    if "DYN (Shared object file)" not in header:
        raise ValueError("musl loader is not a shared object")
    dynamic = subprocess.check_output(["readelf", "-dW", str(libc)], text=True)
    if re.search(r"\((?:NEEDED|RPATH|RUNPATH)\)", dynamic):
        raise ValueError("musl loader must not have dependencies or search paths")
    # musl links its loader without -soname. The dynamic linker resolves
    # "NEEDED libc.so" (and the other reserved libc names) to itself by name in
    # ldso/dynlink.c, so a missing SONAME tag is correct, not a defect.
    soname = re.search(r"\(SONAME\).*?\[([^\]]+)\]", dynamic)
    symbols = subprocess.check_output(["readelf", "--dyn-syms", "-W", str(libc)], text=True)
    exported = {line.split()[-1] for line in symbols.splitlines()
                if len(line.split()) >= 8 and line.split()[6] != "UND"}
    missing = {"memcpy", "memset", "__stack_chk_fail", "__libc_start_main", "_dlstart"} - exported
    if missing:
        raise ValueError(f"musl loader is missing required exports: {sorted(missing)}")
    banner = subprocess.run([str(libc)], text=True, capture_output=True)
    match = re.search(r"^Version ([0-9]+\.[0-9]+\.[0-9]+)$", banner.stderr, re.MULTILINE)
    if not match or match[1] != version:
        raise ValueError(f"musl loader reports {match[1] if match else 'no version'}, pinned {version}")
    return {"version": version, "sha256": digest(libc),
            "soname": soname[1] if soname else None, "entry": "_dlstart"}


def build(php_dir, work, metadata_path, zig_dir, compiler="cc"):
    pin = config(php_dir)
    toolchain_version = toolchain_musl_version(zig_dir)
    if toolchain_version != pin["version"]:
        raise ValueError(
            f"Toolchain musl {toolchain_version} differs from the pinned loader {pin['version']}; "
            "update config/musl.json together with the SPC/Zig pin")
    if not shutil.which(compiler):
        raise ValueError(f"Missing C compiler for the musl loader: {compiler}")
    work = Path(work)
    archive = fetch(pin, work / "downloads")
    source = extract(archive, work / "source")
    libc = compile(source, work / "build", compiler)
    information = inspect(libc, pin["version"])
    notice = source / "COPYRIGHT"
    if not notice.is_file():
        raise ValueError("musl source is missing its copyright notice")
    metadata = {"pin": pin, "source": str(libc), "loader": information, "toolchain_musl_version": toolchain_version,
                "compiler": subprocess.check_output([compiler, "--version"], text=True).splitlines()[0],
                "license": {"path": str(notice), "sha256": digest(notice)}}
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    return libc, metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("php_dir", type=Path)
    parser.add_argument("work", type=Path)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("--zig-dir", type=Path, required=True)
    parser.add_argument("--compiler", default="cc")
    args = parser.parse_args()
    loader, _ = build(args.php_dir, args.work, args.metadata, args.zig_dir, args.compiler)
    print(loader)
