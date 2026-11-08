#!/usr/bin/env python3
"""Build the pinned musl release into the toolchain that everything else is built with.

A musl-dynamic bundle needs two things that must come from the same release: the
loader it ships and the libc every object in it was compiled against. Taking them
from the host means taking whatever release the distribution packages — Ubuntu
24.04, for one, ships musl 1.2.4 while the pin here is 1.2.5, and a bundle whose
loader is older than the objects linked against it is exactly the mismatch this
step exists to prevent. So the pinned source is compiled into a private toolchain:
headers, crt files, libc.so and musl's own `musl-gcc` wrapper. The host only has to
provide a C compiler and the kernel UAPI headers, which musl deliberately does not
ship and whose location differs per distribution; that root is discovered from the
compiler's own search list and announced to every later step.

The same build produces the shipped loader (musl's `libc.so`, which doubles as the
dynamic linker) and the compiler every dependency and httpd are built with, so the
pin is satisfied by construction rather than compared against the host.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import elf
import fetch
import uapi

LOADER_NAME = "ld-musl-x86_64.so.1"
# Where the toolchain puts its own loader while the build runs. It cannot be /lib:
# `configure` compiles a program and runs it immediately, so the interpreter its
# PT_INTERP names has to exist on a host that never installed musl — and a CI
# runner has not. The toolchain's own loader is used instead, and stage.py rewrites
# every shipped PT_INTERP back to the portable /lib name.
TOOLCHAIN_LOADER = "lib/" + LOADER_NAME


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def banner_version(path):
    result = subprocess.run([str(path)], text=True, capture_output=True)
    match = re.search(r"^Version ([0-9]+\.[0-9]+\.[0-9]+)$", result.stderr, re.MULTILINE)
    if not match:
        raise ValueError("Not a musl loader: " + str(path))
    return match[1]


def compile_and_run(command, cwd=None, env=None):
    result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
    if result.returncode != 0:
        raise ValueError("%s failed:\n%s%s" % (
            command[0], result.stdout[-4000:], result.stderr[-4000:]))
    return result


def host_compiler(compiler):
    """Prove the host's C compiler links and runs before anything is downloaded.

    musl is compiled from its pinned source, so the compiler's own libc is never in
    the bundle; it still has to work, and a broken or missing one must fail here
    rather than after the dependency build has started.
    """
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        source = directory / "probe.c"
        program = directory / "probe"
        source.write_text("#include <stdio.h>\nint main(void){puts(\"ok\");return 0;}\n")
        compile_and_run([compiler, "-O2", "-o", str(program), str(source)])
        result = subprocess.run([str(program)], text=True, capture_output=True)
        if result.returncode != 0 or result.stdout.strip() != "ok":
            raise ValueError("The host compiler builds programs that cannot run")
    return subprocess.check_output([compiler, "--version"], text=True).splitlines()[0]


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


def write_wrapper(toolchain, compiler):
    """Point musl's `musl-gcc` at the compiler that built the toolchain.

    musl's own wrapper execs a bare `gcc`, which a host that only provides `cc`
    does not have. REALGCC still overrides it, as in upstream's wrapper.

    Both paths are escaped for a double-quoted shell word rather than quoted by
    shlex: a single-quoted value inside "${REALGCC:-...}" would keep its quotes as
    characters. The toolchain lives under the build workspace, which may contain
    spaces.
    """
    real = shutil.which(compiler)
    if not real:
        raise ValueError("No such compiler: " + compiler)

    def quoted(value):
        return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")

    wrapper = toolchain / "bin/musl-gcc"
    wrapper.write_text(
        '#!/bin/sh\nexec "${REALGCC:-%s}" "$@" -specs "%s"\n'
        % (quoted(real), quoted(toolchain / "lib/musl-gcc.specs")))
    wrapper.chmod(0o755)
    return real


def build_toolchain(source, work, compiler, jobs):
    """Compile the pinned musl into a private `musl-gcc` and the libc it targets.

    musl derives the wrapper and its specs from --prefix, so the install runs
    through DESTDIR: the wrapper must name where the toolchain really lives, while
    `make install` would otherwise try to create /lib/ld-musl-x86_64.so.1 on the
    build host. The staged tree is moved into place afterwards.

    --syslibdir is the toolchain's own lib/, not /lib: every program the build
    compiles asks for that interpreter, and configure runs its test programs
    immediately. Naming the workspace loader is what lets a host with no musl
    installed at all complete the dependency build; stage.py rewrites the shipped
    PT_INTERP back to the portable /lib name when it stages the bundle.
    """
    toolchain = work / "toolchain"
    stage = work / "toolchain-stage"
    build = work / "musl-build"
    for path in (toolchain, stage, build):
        if path.exists():
            raise ValueError("Refusing to reuse " + str(path))
    build.mkdir(parents=True)
    environment = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "SOURCE_DATE_EPOCH": "0"}
    steps = (
        (["sh", str(source / "configure"), "--prefix=" + str(toolchain),
          "--syslibdir=" + str(toolchain / PurePosixPath(TOOLCHAIN_LOADER).parent),
          "--disable-static", "--enable-shared"],
         dict(environment, CC=shutil.which(compiler) or compiler)),
        (["make", "-j", str(jobs)], environment),
        (["make", "install", "DESTDIR=" + str(stage)], environment),
    )
    for command, env in steps:
        compile_and_run(command, cwd=build, env=env)
    staged = stage / toolchain.relative_to("/")
    if staged != toolchain:
        staged.rename(toolchain)
    shutil.rmtree(stage)
    for path in ("bin/musl-gcc", "lib/musl-gcc.specs", "lib/libc.so", "include/stdio.h",
                 TOOLCHAIN_LOADER):
        if not (toolchain / path).exists():
            raise ValueError("The musl toolchain is missing " + path)
    write_wrapper(toolchain, compiler)
    return {
        "cc": str(toolchain / "bin/musl-gcc"),
        "libc": str(toolchain / "lib/libc.so"),
        "prefix": str(toolchain),
        "source": str(source),
    }


def probe_toolchain(toolchain, work, version, kernel):
    """Compile, link and run a program with the toolchain before anything needs it.

    Asking the compiler for its loader (`-print-file-name`) only reports what the
    host ships, which is the dependency this step removes. A program that links,
    requests the bundle's loader name and runs under this very libc is evidence the
    toolchain works — and that it is the pinned release — forty minutes before the
    last dependency is built.

    The probe includes a kernel header because that is the one thing it takes from
    the host, and a kernel root that does not resolve <asm/*.h> is what failed the
    very first CI run of this build.

    The interpreter the program asks for must be the toolchain's own loader, and the
    program must run — that is the check a host without /lib/ld-musl-x86_64.so.1
    fails, and the one `configure` makes on every dependency.
    """
    source = work / "toolchain-probe.c"
    program = work / "toolchain-probe"
    source.write_text(
        "#include <stdio.h>\n"
        "#include <linux/mman.h>\n"
        "#include <string.h>\n"
        "int main(void){char b[8];"
        "memset(b,0,sizeof b);puts(\"toolchain-ok\");return b[0] + (MAP_PRIVATE < 0);}\n"
    )
    # No REALGCC: the wrapper must work as shipped, on the compiler path baked into
    # it, not only when the caller says which compiler to use.
    environment = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    compile_and_run([toolchain["cc"], "-O2", *kernel, "-o", str(program), str(source)],
                    env=environment)
    header = subprocess.check_output(["readelf", "-hW", str(program)], text=True)
    if "ELF64" not in header or "Advanced Micro Devices X86-64" not in header:
        raise ValueError("The musl toolchain does not produce Linux x86_64 ELF64 objects")
    dynamic = subprocess.check_output(["readelf", "-dW", str(program)], text=True)
    if re.search(r"\((?:RPATH|RUNPATH)\)", dynamic):
        raise ValueError("The musl toolchain adds a search path to every object")
    interpreter = elf.interpreter(program)
    wanted = str(Path(toolchain["prefix"]) / TOOLCHAIN_LOADER)
    if interpreter != wanted:
        raise ValueError("The toolchain requests %s instead of its own loader %s"
                         % (interpreter or "no interpreter", wanted))
    # The program is run the way configure runs its test programs: the kernel
    # resolves PT_INTERP. A host with no musl installed fails here, not 40 minutes
    # into the dependency build.
    result = subprocess.run([str(program)], text=True, capture_output=True, env=environment)
    if result.returncode != 0 or result.stdout.strip() != "toolchain-ok":
        raise ValueError("A program built with the toolchain does not run:\n" + result.stderr)
    # And once more through the loader that ships, the way the launchers run it.
    result = subprocess.run([toolchain["libc"], str(program)], text=True, capture_output=True)
    if result.returncode != 0 or result.stdout.strip() != "toolchain-ok":
        raise ValueError("The bundled loader cannot run a toolchain program:\n" + result.stderr)
    actual = banner_version(toolchain["libc"])
    if actual != version:
        raise ValueError("Toolchain musl %s is not the pinned %s" % (actual, version))


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


def build(pin, work, compiler, jobs):
    source = source_tree(dict(pin, name="musl"), work)
    # Kept as the root list rather than as flags: every later step renders the
    # flags from it itself, so there is one place that decides -idirafter.
    roots = uapi.SEPARATOR.join(uapi.discover(compiler))
    toolchain = build_toolchain(source, work, compiler, jobs)
    probe_toolchain(toolchain, work, pin["version"], uapi.flags(roots))
    libc = Path(toolchain["libc"])
    record = inspect(libc, pin["version"])
    record["source"] = str(libc)
    record["license"] = {"path": str(source / "COPYRIGHT"), "sha256": digest(source / "COPYRIGHT")}
    return libc, dict(toolchain, kernel=roots), record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path, help="plan JSON printed by source.py")
    parser.add_argument("work", type=Path, help="build workspace, already holding downloads/")
    parser.add_argument("metadata", type=Path)
    parser.add_argument("--compiler", default="cc",
                        help="host C compiler that builds the pinned musl toolchain")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--probe-only", action="store_true",
                        help="check the host compiler and stop")
    args = parser.parse_args()
    # Every build step runs with cwd set elsewhere, so a relative workspace would
    # be re-resolved from there and find nothing.
    args.work = args.work.resolve()
    request = json.loads(args.request.read_text())
    pins = [entry for entry in request["dependencies"] if entry["role"] == "loader"]
    if len(pins) != 1:
        raise ValueError("The plan must pin exactly one loader source")
    pin = dict(pins[0], name="musl")
    if args.probe_only:
        # Both of the host's contributions are proved here: the compiler, and the
        # kernel UAPI include roots musl does not ship. Running it first means an
        # unusable host stops the run before anything is downloaded.
        print(host_compiler(args.compiler))
        print(uapi.SEPARATOR.join(uapi.discover(args.compiler)))
        return
    loader, toolchain, record = build(pin, args.work, args.compiler, args.jobs)
    args.metadata.parent.mkdir(parents=True, exist_ok=True)
    args.metadata.write_text(json.dumps(record, indent=2) + "\n")
    # The compiler path and the kernel include root are announced here rather than
    # recomputed by the caller: the first lives inside this build's workspace and
    # only this step knows where, and the second is a property of the host compiler
    # that every later step must use identically.
    print(json.dumps({"loader": str(loader), "cc": toolchain["cc"],
                      "kernel": toolchain["kernel"], "version": record["version"]}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit("musl toolchain build failed: %s\n" % exc)
