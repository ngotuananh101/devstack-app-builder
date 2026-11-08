#!/usr/bin/env python3
"""Materialise the pinned toolchain, build the vendored dependencies and loader.

Every input to the build is either a pin in ``config/sources.json`` or a build
decision in ``config/build-options.json``; nothing is taken from the host except
the handful of host tools ``require`` names.  The module is driven by the
subcommands at the bottom of the file, one per stage of ``build.sh``.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import time

RUNTIME_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = RUNTIME_DIR.parents[1]
BUILD_OPTIONS = RUNTIME_DIR / "config/build-options.json"
SOURCE_MODULE = Path(__file__).with_name("source.py")
PHP_DIR = REPO_ROOT / "runtimes/php"
PHP_LOADER_MODULE = PHP_DIR / "scripts/musl_loader.py"
TARGET = "x86_64-linux-musl"
HOST_PATH = "/usr/bin:/bin"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_options(path=BUILD_OPTIONS):
    """Read and validate the build decisions; reject anything shaped differently."""
    data = json.loads(Path(path).read_text())
    if set(data) != {"zig", "configure", "locale"}:
        raise ValueError(f"{path} must hold exactly zig, configure and locale")
    zig = data["zig"]
    if set(zig) != {"version", "musl_version"}:
        raise ValueError("zig holds only the pinned version and its bundled musl version")
    for key in ("version", "musl_version"):
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", str(zig[key])):
            raise ValueError(f"Invalid zig {key}: {zig[key]!r}")
    configure = data["configure"]
    if set(configure) != {"shared", "cppflags", "make_target", "install_target"}:
        raise ValueError("configure holds only the flags, cppflags and make targets")
    flags = configure["shared"]
    if not isinstance(flags, list) or not all(isinstance(f, str) and f.startswith("--") for f in flags):
        raise ValueError("configure.shared must be a list of long options")
    if len(flags) != len(set(flags)):
        raise ValueError("configure.shared repeats an option")
    icu = [f for f in flags if "icu" in f]
    if icu not in (["--with-icu"], ["--without-icu"]):
        raise ValueError("configure.shared must state exactly one ICU decision")
    if "-idirafter /usr/include" not in configure["cppflags"]:
        raise ValueError("configure.cppflags must reach host headers only through -idirafter")
    if "-I/usr/include" in configure["cppflags"]:
        raise ValueError("configure.cppflags must never put host headers first")
    locale = data["locale"]
    if set(locale) != {"provider", "default", "alternate_18"}:
        raise ValueError("locale holds the provider, default and alternate_18 contract")
    if locale["provider"] != "builtin" or locale["default"] != "C.UTF-8":
        raise ValueError("Only the builtin C.UTF-8 locale provider is supported")
    return data


def require(tools):
    """Fail with the first host tool the build cannot do without."""
    for tool in tools:
        if shutil.which(tool) is None:
            raise ValueError(f"Missing native dependency: {tool}. Install it explicitly, then rerun.")


def _confined_extract(archive, destination):
    """Extract a digest-verified archive into a fresh directory, one root only."""
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        roots = set()
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts:
                raise ValueError(f"Unsafe archive path: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"Archive special files are not allowed: {member.name}")
            roots.add(name.parts[0])
        if len(roots) != 1:
            raise ValueError("Archive must have exactly one root directory")
        tar.extractall(destination, filter="data", members=members)
        return destination / roots.pop()


def archives(work):
    """Where the pinned dependency archives live.

    Never ``<work>/downloads``: ``source.py --download`` creates that directory
    itself and refuses to run when it already exists, so the two stages would
    break each other if they shared it.
    """
    return Path(work) / "archives"


# A build takes about an hour, so waiting minutes on a flaky CDN is cheap next
# to losing it. The delays are cumulative: 15s, 45s, 1m45s, 3m45s, 6m45s.
FETCH_RETRY_DELAYS = (15, 30, 60, 120, 180)
FETCH_ATTEMPTS = len(FETCH_RETRY_DELAYS) + 1


def _fetch(pin, downloads):
    """Download one pinned archive, retrying a transient gateway failure.

    GitHub's release CDN answers 504 for a single asset while the rest of the
    same release serves normally, and the failing assets recover at different
    moments; a build needs every one of them at once. Curl's own ``--retry``
    covers the transfer errors but a 504 arrives as an ordinary HTTP status, so
    the attempts are made here. A digest that does not match its pin is never
    retried: that is a changed archive, not a flaky connection.
    """
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / PurePosixPath(pin["url"]).name
    if archive.exists():
        if digest(archive) != pin["sha256"]:
            raise ValueError(f"Pinned archive changed on disk: {archive}")
        return archive
    temporary = archive.with_name(archive.name + ".part")
    result = None
    for attempt in range(FETCH_ATTEMPTS):
        if attempt:
            time.sleep(FETCH_RETRY_DELAYS[attempt - 1])
        result = subprocess.run(
            ["curl", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https",
             "--max-time", "600", "--output", str(temporary), pin["url"]],
            text=True, capture_output=True,
        )
        if result.returncode == 0:
            break
    if result.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise ValueError(
            f"Cannot download {pin['url']} after {FETCH_ATTEMPTS} attempts: "
            f"{result.stderr.strip()[-1000:]}")
    if digest(temporary) != pin["sha256"]:
        temporary.unlink()
        raise ValueError(f"SHA-256 mismatch for {pin['url']}")
    temporary.rename(archive)
    return archive


def prepare_zig(work, pins):
    """Fetch, verify and extract the pinned compiler; never use one from PATH."""
    # Resolved because build.sh captures this path and later hands it back in a
    # different working directory; a relative answer would not survive that.
    work = Path(work).resolve()
    archive = _fetch(pins["zig"], archives(work))
    root = _confined_extract(archive, work / "zig")
    candidates = [root] + sorted(path.parent for path in root.glob("*/zig") if path.is_file())
    for candidate in candidates:
        binary = candidate / "zig"
        if binary.is_file():
            binary.chmod(0o755)
            return candidate
    raise ValueError(f"The pinned Zig archive holds no zig executable: {root}")


def zig(zig_dir, options):
    """Return the pinned compiler after proving it is the version the build declared."""
    binary = Path(zig_dir) / "zig"
    if not binary.is_file():
        raise ValueError(f"Missing pinned compiler: {binary}")
    result = subprocess.run([str(binary), "version"], text=True, capture_output=True)
    if result.returncode != 0:
        raise ValueError(f"Cannot run {binary}: {result.stderr.strip()}")
    expected = options["zig"]["version"]
    actual = result.stdout.strip()
    if actual != expected:
        raise ValueError(f"Zig {actual} does not match the pinned {expected} in build-options.json")
    return binary


def musl_version(zig_dir):
    """Read the musl version the compiler ships; the loader must agree with it."""
    return _load(PHP_LOADER_MODULE, "php_musl_loader").toolchain_musl_version(Path(zig_dir))


def configure_flags(options):
    """The static PostgreSQL configure decisions, in declared order."""
    return list(options["configure"]["shared"])


def build_env(zig_dir, deps_prefix, loader=None):
    """A complete build environment: pinned compiler, isolated pkg-config, no host libs.

    ``loader`` is the musl loader the build was just given permission to name as
    PT_INTERP. Without it every program `zig cc` links asks the kernel for
    ``/lib/ld-musl-x86_64.so.1``, which a CI runner that never installed musl does
    not have, and `configure` dies on "cannot run C compiled programs" the moment
    it compiles its first test program. Naming the pinned loader instead makes the
    build's own programs runnable on exactly the hosts this bundle targets.

    It is not a bundle decision: the shipped ELFs are rewritten to the portable
    ``/lib/`` name when they are staged, so nothing inside a user's archive ever
    carries the workspace path this puts in a build-time program.
    """
    zig_dir = Path(zig_dir)
    deps_prefix = Path(deps_prefix)
    pkgconfig = deps_prefix / "lib/pkgconfig"
    interpreter = ""
    if loader is not None:
        # -Wl,-rpath,$ORIGIN-sibling would be simpler, but a runpath in a build
        # program is exactly what parse_elf refuses to see in a staged one.
        interpreter = f" -Wl,--dynamic-linker={Path(loader).resolve()}"
    # Everything Zig-specific rides on the compiler, never on LDFLAGS: LDFLAGS is
    # recorded in Makefile.global, returned by `pg_config --ldflags` and copied
    # into libpq.pc's Libs.private, so it must stay a value any linker accepts
    # and a path a user can still use. -dynamic is a Zig option, and ICU's static
    # archives are C++, so the C++ runtime has to be named on every link that
    # reaches them -- `postgres` is linked with CC, not CXX.
    return {
        "PATH": HOST_PATH,
        "LC_ALL": "C",
        "SOURCE_DATE_EPOCH": "0",
        "CC": f"{zig_dir}/zig cc -target {TARGET} -dynamic -lc++ -lc++abi{interpreter}",
        "CXX": f"{zig_dir}/zig c++ -target {TARGET} -dynamic -lc++ -lc++abi{interpreter}",
        "CPPFLAGS": load_options()["configure"]["cppflags"],
        "CFLAGS": "-O2 -fPIC",
        "LDFLAGS": f"-L{deps_prefix}/lib",
        "PKG_CONFIG_PATH": str(pkgconfig),
        "PKG_CONFIG_LIBDIR": str(pkgconfig),
        # Zig needs a writable cache even when the caller's environment is scrubbed;
        # the prefix's parent is the build workspace, never a shared location.
        "ZIG_GLOBAL_CACHE_DIR": str(deps_prefix.parent / ".zig-cache"),
    }


def dependency_plan(options, work=None):
    """The dependency stack in build order, with the linkage each one must have."""
    if work is not None and (Path(work) / "deps").exists():
        raise ValueError(f"Dependency prefix already exists: {Path(work) / 'deps'}; builds never reuse one")
    plan = [
        {"name": "zlib", "linkage": "static"},
        {"name": "openssl", "linkage": "shared"},
        {"name": "libxml2", "linkage": "static"},
        {"name": "icu", "linkage": "static"},
        {"name": "util-linux", "linkage": "static"},
    ]
    if "--without-icu" in configure_flags(options):
        plan = [step for step in plan if step["name"] != "icu"]
    return plan


class BuildFailure(ValueError):
    """A build step failed; the captured output decides who is to blame."""

    def __init__(self, message, output):
        super().__init__(message)
        self.output = output


# What a compiler or linker says when it cannot do its job, as opposed to what a
# broken source tree, a missing file or a full disk says.
COMPILER_DIAGNOSTICS = (
    "error: unknown type name",
    "error: use of undeclared identifier",
    "fatal error:",
    "ld.lld: error:",
    "undefined symbol",
    "cannot find -l",
    "cannot create executables",
    "unsupported option",
    "recompile with -fPIC",
)


def compiler_failure(output):
    """True when the captured output is a C/C++ compiler or linker complaint."""
    return any(marker in output for marker in COMPILER_DIAGNOSTICS)


def _call(command, cwd, env, what):
    # The dependency stack is a tens-of-minutes build with its output captured,
    # so the log needs a stage boundary on each side to stay diagnosable.
    print(f"{what}: {cwd}", file=sys.stderr, flush=True)
    started = time.monotonic()
    result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
    elapsed = time.monotonic() - started
    if result.returncode != 0:
        output = result.stdout[-4000:] + result.stderr[-4000:]
        raise BuildFailure(f"{what} failed after {elapsed:.0f}s:\n{output}", output)
    print(f"{what}: ok in {elapsed:.0f}s", file=sys.stderr, flush=True)
    return result


def _zlib(source, build, deps, env):
    _call([str(source / "configure"), f"--prefix={deps}", "--static"], build, env, "zlib configure")
    _call(["make", "-j", str(os.cpu_count() or 2)], build, env, "zlib make")
    _call(["make", "install"], build, env, "zlib install")


def _openssl(source, build, deps, env):
    # OpenSSL 3.6.5 reads _GNU_SOURCE as "glibc: strerror_r returns char *" and
    # assigns the int musl's POSIX strerror_r returns to a char *. PostgreSQL
    # needs _GNU_SOURCE; OpenSSL does not, so this one dependency drops it.
    openssl_env = dict(env)
    openssl_env["CPPFLAGS"] = " ".join(
        flag for flag in env["CPPFLAGS"].split() if flag != "-D_GNU_SOURCE")
    # The target must come first; an absolute --libdir is what puts the .so.N
    # files where staging looks for them.
    _call([str(source / "Configure"), "linux-x86_64", "shared", "no-tests", "no-apps",
           f"--prefix={deps}", f"--libdir={deps}/lib", f"--openssldir={deps}/ssl"],
          build, openssl_env, "OpenSSL Configure")
    _call(["make", "-j", str(os.cpu_count() or 2)], build, openssl_env, "OpenSSL make")
    _call(["make", "install_sw"], build, openssl_env, "OpenSSL install_sw")


def _libxml2(source, build, deps, env):
    _call([str(source / "configure"), f"--prefix={deps}", "--enable-static", "--disable-shared",
           "--without-python", "--without-readline", "--without-icu", "--without-zlib",
           "--without-lzma"], build, env, "libxml2 configure")
    _call(["make", "-j", str(os.cpu_count() or 2)], build, env, "libxml2 make")
    _call(["make", "install"], build, env, "libxml2 install")


def _util_linux(source, build, deps, env):
    _call([str(source / "configure"), f"--prefix={deps}", "--disable-all-programs",
           "--enable-libuuid", "--enable-static", "--disable-shared", "--disable-nls",
           "--without-python", "--disable-asciidoc", "--disable-poman"],
          build, env, "util-linux configure")
    _call(["make", "-j", str(os.cpu_count() or 2)], build, env, "util-linux make")
    _call(["make", "install"], build, env, "util-linux install")


def non_pic_relocations(archive):
    """Relocations that make a static archive unusable in a shared object.

    Only allocated sections matter: debug sections legitimately carry absolute
    relocations and never reach the link.
    """
    result = subprocess.run(["readelf", "-rW", str(archive)], text=True, capture_output=True)
    if result.returncode != 0:
        raise ValueError(f"Cannot read relocations from {archive}: {result.stderr.strip()}")
    found = []
    in_debug = False
    for line in result.stdout.splitlines():
        if line.startswith("Relocation section"):
            name = line.split("'")[1] if "'" in line else ""
            # Section names arrive as .rela.debug_info / .rel.debug_abbrev.
            name = name.removeprefix(".rela").removeprefix(".rel")
            in_debug = name.startswith(".debug") or name.startswith(".zdebug")
            continue
        if in_debug:
            continue
        if "R_X86_64_32S" in line or "R_X86_64_32 " in line:
            found.append(line)
    return found


def check_position_independent(*archives):
    """The plan mandates -fPIC static archives; ICU is the one that can slip."""
    for archive in archives:
        found = non_pic_relocations(archive)
        if found:
            raise ValueError(
                f"{archive} was built without -fPIC; the first offending relocation is "
                f"{found[0].strip()}. Static archives are linked into a shared object.")


def pin_version(name):
    """The exact version config/sources.json pins for a dependency."""
    pins = _load(SOURCE_MODULE, "pg_source").dependencies()
    if name not in pins:
        raise ValueError(f"No pinned {name} in config/sources.json")
    return pins[name]["version"]


def _icu(source, build, deps, env):
    # ICU keeps its autotools project one level down, under icu/source.
    configure = source / "source/configure"
    if not configure.is_file():
        raise ValueError(f"ICU source layout changed; no source/configure under {source}")
    version = pin_version("icu")
    # ICU writes its own CXXFLAGS into icudefs.mk and ignores the environment
    # variable, so -fPIC has to arrive as a configure assignment. Without it the
    # static archives carry R_X86_64_32S relocations and refuse to link into a
    # shared object; only the non-PIE `postgres` executable would hide that.
    command = [str(configure), f"--prefix={deps}", "--enable-static", "--disable-shared",
               "--with-data-packaging=static", "--disable-tests", "--disable-samples",
               "--disable-extras", "CXXFLAGS=-O2 -fPIC", "CFLAGS=-O2 -fPIC"]
    try:
        _call(command, build, env, "ICU configure")
    except BuildFailure as exc:
        # Only a compiler/linker complaint is attributable to the C++ toolchain;
        # a broken tree, a missing file or a full disk is not a reason to drop
        # ICU, and pre-emptively taking the fallback is exactly what the plan
        # forbids. Nothing here is a user-selectable profile.
        if not compiler_failure(exc.output):
            raise
        raise ValueError(
            f"ICU {version} did not compile with zig c++ -target {TARGET}; "
            "switch configure.shared to --without-icu and record the reason in "
            f"config/build-options.json. ({exc})") from exc
    _call(["make", "-j", str(os.cpu_count() or 2)], build, env, "ICU make")
    _call(["make", "install"], build, env, "ICU install")
    check_position_independent(*(Path(deps) / "lib" / name
                                 for name in ("libicuuc.a", "libicui18n.a", "libicudata.a")))


STEPS = {"zlib": _zlib, "openssl": _openssl, "libxml2": _libxml2, "icu": _icu,
         "util-linux": _util_linux}

# Each upstream ships its licence under a different name; the first match wins.
# util-linux ships a COPYING that is the GPL-2.0 text for the programs, but the
# artefact this build installs and links is libuuid, which is LGPL-2.1-or-later;
# shipping COPYING would contradict the pin's own SPDX field.
LICENSES = {
    "zlib": ("LICENSE",),
    "openssl": ("LICENSE.txt",),
    "libxml2": ("Copyright",),
    "icu": ("LICENSE",),
    "util-linux": ("Documentation/licenses/COPYING.LGPL-2.1-or-later", "README.licensing"),
}


def _license(source, name):
    for candidate in LICENSES[name]:
        path = source / candidate
        if path.is_file():
            return {"path": str(path), "sha256": digest(path)}
    raise ValueError(f"{name} source is missing its licence file: {source}")


ICU_PROBE = """\
#include <unicode/utypes.h>
#include <unicode/ustring.h>
#include <stdio.h>

int main(void) {
    UErrorCode status = U_ZERO_ERROR;
    UChar buffer[16];
    u_strToUpper(buffer, 16, buffer, 0, "", &status);
    puts(u_errorName(status));
    return status == U_ZERO_ERROR ? 0 : 1;
}
"""


def icu_link_flags(deps_prefix):
    """The ICU link line PostgreSQL's own Makefile will use, as a list."""
    return [f"-L{deps_prefix}/lib", "-licui18n", "-licuuc", "-licudata", "-lpthread", "-lm"]


RUNTIME_PROBE = """\
#include <stdio.h>

int main(void) {
    puts("build-program-ran");
    return 0;
}
"""


def check_build_programs_run(work, env):
    """Prove a program this toolchain links actually runs before the stack is built.

    `configure` compiles a test program and executes it immediately. Every program
    `zig cc` links without an explicit ``--dynamic-linker`` names
    ``/lib/ld-musl-x86_64.so.1``; on a CI runner that never installed musl the
    kernel cannot resolve that path and `configure` aborts with "cannot run C
    compiled programs" a few seconds into the first dependency. That failure names
    no cause further down, so it is checked here instead, before the tens of
    minutes the dependency build costs.

    The probe is deliberately the same shape as configure's: link with ``CC`` out
    of the scrubbed environment, then run the result with no loader indirection.
    """
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    source = work / "runtime_probe.c"
    binary = work / "runtime_probe"
    source.write_text(RUNTIME_PROBE)
    binary.unlink(missing_ok=True)
    command = env["CC"].split() + ["-O2", str(source), *env["LDFLAGS"].split(),
                                   "-o", str(binary)]
    _call(command, work, env, "toolchain runtime probe")
    try:
        result = subprocess.run([str(binary)], text=True, capture_output=True, env=env)
    except OSError as exc:
        # The kernel refuses the exec outright when the interpreter the ELF
        # names does not exist: ENOENT for a missing loader, EACCES when a
        # sandbox masks the path. Either way the cause is the same one.
        result = None
        reason = str(exc)
    else:
        reason = f"rc={result.returncode} out={result.stdout.strip()!r} err={result.stderr.strip()!r}"
    if result is None or result.returncode != 0 or result.stdout.strip() != "build-program-ran":
        raise ValueError(
            "A program this toolchain links does not run; every dependency "
            "configure would abort with 'cannot run C compiled programs'. "
            f"{reason}")


def check_icu_links(work, env, deps_prefix):
    """Prove the ICU archives link as PostgreSQL will link them, and run.

    ICU ships static C++ archives; PostgreSQL links the backend with ``CC``, so
    nothing on that command line names a C++ runtime unless ``build_env`` does.
    Configure only checks ICU through pkg-config, so without this the failure
    would surface deep inside ``make world-bin`` instead of here.
    """
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    source = work / "icu_probe.c"
    source.write_text(ICU_PROBE)
    binary = work / "icu_probe"
    binary.unlink(missing_ok=True)
    executable = env["CC"].split() + ["-O2", "-fPIC", f"-I{deps_prefix}/include",
                                      str(source), *env["LDFLAGS"].split(),
                                      *icu_link_flags(deps_prefix), "-o", str(binary)]
    _call(executable, work, env, "ICU link probe")
    run = subprocess.run([str(binary)], text=True, capture_output=True)
    if run.returncode != 0 or "U_ZERO_ERROR" not in run.stdout:
        raise ValueError(
            "The ICU link probe built but does not run; the static ICU archives are "
            f"not usable: rc={run.returncode} out={run.stdout.strip()!r} err={run.stderr.strip()!r}")


def build_dependencies(work, pins, zig_dir, loader=None):
    """Build the pinned stack into ``<work>/deps`` and return that prefix.

    ``loader`` is passed through to :func:`build_env`: without it the build's own
    programs name a host path the runner may not have, and `configure` cannot run
    what it compiles. The probe below catches that in seconds rather than at the
    first dependency.
    """
    work = Path(work).resolve()
    zig_dir = Path(zig_dir).resolve()
    plan = dependency_plan(load_options(), work)
    deps = work / "deps"
    deps.mkdir(parents=True, exist_ok=False)
    env = build_env(zig_dir, deps, loader)
    check_build_programs_run(work / "runtime-probe", env)
    licenses = {}
    for step in plan:
        name = step["name"]
        pin = pins.get(name)
        if pin is None:
            raise ValueError(f"No pinned {name} in config/sources.json")
        archive = _fetch(pin, archives(work))
        source = _confined_extract(archive, work / "sources" / name)
        licenses[name] = _license(source, name)
        build = work / "builds" / name
        build.mkdir(parents=True, exist_ok=False)
        STEPS[name](source, build, deps, env)
    if any(step["name"] == "icu" for step in plan):
        check_icu_links(work / "icu-link", env, deps)
    (work / "licenses.json").write_text(json.dumps(licenses, indent=2, sort_keys=True) + "\n")
    return deps


def build_loader(work, zig_dir):
    """Build the pinned musl loader through PHP's module so both runtimes agree."""
    work = Path(work).resolve()
    loader_module = _load(PHP_LOADER_MODULE, "php_musl_loader")
    options = load_options()
    bundled = loader_module.toolchain_musl_version(Path(zig_dir))
    if bundled != options["zig"]["musl_version"]:
        raise ValueError(
            f"Zig {options['zig']['version']} bundles musl {bundled}, but build-options.json "
            f"declares {options['zig']['musl_version']}")
    # musl's own link line pulls compiler_rt, whose memcpy/memset/strlen are
    # hidden; linking it through zig cc therefore produces a loader without the
    # exports musl_loader.inspect requires. The host C compiler reproduces the
    # loader PHP ships byte for byte, so the loader is built exactly as PHP
    # builds it rather than re-implemented here.
    libc, metadata = loader_module.build(PHP_DIR, work / "loader", work / "loader.json", Path(zig_dir), "cc")
    record = metadata["loader"]
    return {
        "path": str(libc),
        "sha256": record["sha256"],
        "musl_version": record["version"],
        "license": metadata["license"],
    }


def loader_path(work):
    """The loader :func:`build_loader` wrote for this workspace.

    Read back from the metadata rather than guessed from the layout: the loader
    module owns where it puts libc.so, and build.sh needs the exact path to hand
    the compiler as its ``--dynamic-linker``.
    """
    return json.loads((Path(work) / "loader.json").read_text())["source"]


def provenance(options, pins, deps_prefix, loader, licenses, icu=None):
    """The Task 4 -> Task 5 record: where the dependencies live and what was built."""
    flags = configure_flags(options)
    if icu is None:
        if "--with-icu" in flags:
            icu = {"decision": "with_icu", "reason": f"zig c++ compiled ICU {pins['icu']['version']}"}
        else:
            icu = {"decision": "without_icu",
                   "reason": "ICU did not compile with zig c++; recorded build-time fallback"}
    records = []
    for step in dependency_plan(options):
        name = step["name"]
        pin = pins[name]
        if name not in licenses:
            raise ValueError(f"No license file recorded for {name}")
        records.append({"name": name, "version": pin["version"], "sha256": pin["sha256"],
                        "linkage": step["linkage"], "spdx": pin["license"],
                        "license": licenses[name]})
    return {
        "zig": {"version": options["zig"]["version"],
                "musl_version": options["zig"]["musl_version"],
                "sha256": pins["zig"]["sha256"], "license": pins["zig"]["license"]},
        "deps_prefix": str(deps_prefix),
        "loader": loader,
        "dependencies": records,
        "configure_flags": flags,
        "locale": options["locale"],
        "icu": icu,
        "compiler": f"zig cc -target {TARGET}",
    }


def write_provenance(path, data):
    """Write the provenance record once; an existing record is never replaced."""
    path = Path(path)
    if path.exists():
        raise ValueError(f"Refusing to replace an existing provenance record: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    tools = sub.add_parser("require", help="Fail unless every named host tool is installed")
    tools.add_argument("tools", nargs="+")
    flags = sub.add_parser("configure-flags", help="Print one PostgreSQL configure flag per line")
    environment = sub.add_parser("env", help="Print the build environment as VARIABLE=value lines")
    environment.add_argument("--zig-dir", type=Path, required=True)
    environment.add_argument("--deps", type=Path, required=True)
    # The loader is optional so the environment can still be printed for a
    # workspace that has not built one yet; every program linked without it asks
    # for a host path this build refuses to depend on.
    environment.add_argument("--loader", type=Path)
    for name in ("prepare-zig", "build-dependencies", "build-loader"):
        command = sub.add_parser(name)
        command.add_argument("--work", type=Path, required=True)
        command.add_argument("--zig-dir", type=Path)
    dependencies = sub.choices["build-dependencies"]
    dependencies.add_argument("--loader", type=Path)
    resolve_loader = sub.add_parser(
        "loader-path", help="Print the loader build-loader recorded for this workspace")
    resolve_loader.add_argument("--work", type=Path, required=True)
    record = sub.add_parser("write-provenance")
    record.add_argument("--work", type=Path, required=True)
    record.add_argument("--output", type=Path, required=True)
    record.add_argument("--icu-reason")
    args = parser.parse_args()

    pins = _load(SOURCE_MODULE, "pg_source").dependencies()
    options = load_options()
    if args.command == "require":
        require(args.tools)
    elif args.command == "configure-flags":
        print("\n".join(configure_flags(options)))
    elif args.command == "env":
        for key, value in build_env(args.zig_dir, args.deps, args.loader).items():
            print(f"{key}={value}")
    elif args.command == "prepare-zig":
        require(["curl", "tar"])
        print(prepare_zig(args.work, pins))
    elif args.command == "build-dependencies":
        require(["make", "curl", "tar", "strip", "readelf"])
        zig_dir = args.zig_dir
        zig(zig_dir, options)
        print(build_dependencies(args.work, pins, zig_dir, args.loader))
    elif args.command == "build-loader":
        require(["make", "curl", "tar", "readelf", "cc"])
        print(json.dumps(build_loader(args.work, args.zig_dir)))
    elif args.command == "loader-path":
        print(loader_path(args.work))
    else:
        metadata = json.loads((args.work / "loader.json").read_text())
        licenses = json.loads((args.work / "licenses.json").read_text())
        record_data = provenance(
            options, pins, args.work / "deps",
            {"path": metadata["source"], "sha256": metadata["loader"]["sha256"],
             "musl_version": metadata["loader"]["version"], "license": metadata["license"]},
            licenses,
            None if args.icu_reason is None else {
                "decision": "with_icu" if "--with-icu" in configure_flags(options) else "without_icu",
                "reason": args.icu_reason})
        write_provenance(args.output, record_data)
        print(args.output)


if __name__ == "__main__":
    _main()
