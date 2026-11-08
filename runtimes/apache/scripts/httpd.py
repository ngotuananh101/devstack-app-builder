#!/usr/bin/env python3
"""Configure and build httpd with the module policy, then assert the census.

httpd silently disables a module when one of its --with-* dependencies is not
honoured (mod_md, mod_http2, mod_brotli, mod_lua, mod_session_crypto all do
this), so configure succeeding proves nothing. This step therefore reads the
generated modules.mk files and compares them with config/modules.json, and it
refuses to continue on any difference.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import elf
import uapi

CONFIG = Path(__file__).resolve().parents[1] / "config"


def modules_json():
    data = json.loads((CONFIG / "modules.json").read_text())
    expected = set(data["dso"])
    excluded = {row["name"] for row in data["excluded"]}
    shared_mpms = set(data["mpm"]["shared"])
    if expected & excluded:
        raise ValueError("A module is both expected and excluded")
    return data, expected, excluded, shared_mpms


def strip_runpath(install):
    """Remove RPATH/RUNPATH from every installed ELF.

    APACHE_MODPATH_ADD writes `-rpath $(libexecdir)` into each module link line,
    and libtool additionally records the dependency prefix in the vendored
    libraries. Both are host paths that would break relocation, and neither can
    be turned off from configure: dropping the rpath argument makes libtool treat
    a module as a convenience library and emit a `.a` instead of a `.so`. So the
    link keeps the rpath and the installed ELFs have it edited out afterwards.
    """
    changed = 0
    for path in sorted(install.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.read_bytes()[:4] != b"\x7fELF":
            continue
        if elf.strip_rpath(path):
            changed += 1
    if changed == 0:
        raise ValueError("No ELF carried an rpath; the link flags changed")
    return changed


def strip_install(install):
    """Drop debug symbols from the installed programs and modules.

    httpd compiles with -g by default, and the DWARF it emits records the build
    host's dependency prefix. The symbols are of no use in a bundle, so they go.
    Modes are restored explicitly because strip rewrites the file and a setuid
    program (suexec) must not lose the bit it is installed for.
    """
    binaries = [path for path in sorted(install.rglob("*"))
                if path.is_file() and not path.is_symlink()
                and path.read_bytes()[:4] == b"\x7fELF"]
    if not binaries:
        raise ValueError("The install tree has no ELF files")
    modes = {path: path.stat().st_mode for path in binaries}
    subprocess.run(["strip", "--strip-unneeded", *(str(path) for path in binaries)], check=True)
    for path, mode in modes.items():
        path.chmod(mode & 0o7777)
    return len(binaries)


def built_modules(build):
    """Read the modules the build will produce from its own generated makefiles.

    Each shared module is a rule of the form `mod_name.la: mod_name.slo ...`,
    emitted only when configure decided the module is built as a DSO.
    """
    found = set()
    for makefile in build.rglob("modules.mk"):
        for line in makefile.read_text().splitlines():
            target = line.partition(":")[0]
            if target.startswith("mod_") and target.endswith(".la"):
                found.add(target[4:-3])
    if not found:
        raise ValueError("The build produced no shared modules")
    return found


def load_order(build):
    """The order httpd itself links the DSOs in, read from its generated makefile.

    This is not alphabetical, and it is load-bearing: mod_heartbeat resolves
    ap_hook_watchdog_need() from mod_watchdog, so a config that loads every module
    in any other order fails with an undefined symbol. The verifier replays this
    order when it proves every DSO in the bundle can be loaded.

    configure writes the makefile into <build>/build/, not <build>/ itself, and the
    installed tree has a same-named file that does not carry this variable, so the
    file is looked up inside the build tree only.
    """
    candidates = sorted(build.rglob("config_vars.mk"))
    if len(candidates) != 1:
        raise ValueError("The build recorded %d config_vars.mk files" % len(candidates))
    text = candidates[0].read_text()
    match = re.search(r"^DSO_MODULES = (.+)$", text, re.MULTILINE)
    if not match:
        raise ValueError("The build did not record its DSO module list")
    order = match[1].split()
    if len(order) < 2:
        raise ValueError("The build recorded a suspiciously short DSO list")
    return order


def census(build):
    data, expected, excluded, shared_mpms = modules_json()
    built = built_modules(build)
    mpm = {name for name in built if name.startswith("mpm_")}
    modules = built - mpm
    if mpm != {"mpm_" + name for name in shared_mpms}:
        raise ValueError("Shared MPM inventory changed: " + ", ".join(sorted(mpm)))
    missing = expected - modules
    unexpected = modules - expected
    if missing or unexpected:
        raise ValueError(
            "Module census mismatch; missing: %s; unexpected: %s"
            % (sorted(missing), sorted(unexpected))
        )
    return {
        "dso": sorted(modules),
        "static": data["static"],
        "mpm_default": data["mpm"]["default"],
        "shared_mpms": sorted(mpm),
        "excluded": data["excluded"],
        "load_order": load_order(build),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="extracted httpd source tree")
    parser.add_argument("build", type=Path, help="new, empty build directory")
    parser.add_argument("prefix", type=Path, help="staging prefix holding the dependencies")
    parser.add_argument("install", type=Path, help="httpd install prefix")
    parser.add_argument("--cc", type=Path, required=True,
                        help="musl-gcc built from the pinned musl by loader.py")
    parser.add_argument("--kernel-headers", required=True,
                        help="host include roots holding linux/ and asm/, colon-separated as loader.py found them")
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    build = args.build.resolve()
    prefix = str(args.prefix.resolve())
    install = str(args.install.resolve())
    cc = str(args.cc.resolve())
    if not Path(cc).is_file():
        raise ValueError("The pinned musl compiler is missing: " + cc)
    # musl has no kernel UAPI headers; which host directories do is a property of
    # the compiler, so it arrives with the toolchain rather than being assumed.
    kernel = uapi.env_flags(args.kernel_headers)
    if build.exists():
        raise ValueError("Refusing to reuse a build directory: " + str(build))
    build.mkdir(parents=True)
    options = json.loads((CONFIG / "build-options.json").read_text())
    arguments = [
        option.replace("${PREFIX}", prefix).replace("${INSTALL}", install)
        for option in options["arguments"]
    ]
    environment = {
        **{name: value for name, value in os.environ.items()},
        "PATH": prefix + "/bin:" + os.environ["PATH"],
        "CC": cc,
        "CFLAGS": "-O2 -fPIC " + kernel,
        "CPPFLAGS": kernel,
        "LDFLAGS": "-L%s/lib -Wl,-rpath-link,%s/lib" % (prefix, prefix),
        "PKG_CONFIG_PATH": prefix + "/lib/pkgconfig",
        "PKG_CONFIG_LIBDIR": prefix + "/lib/pkgconfig",
    }

    def run(command):
        print("+ " + " ".join(shlex.quote(str(part)) for part in command), flush=True)
        subprocess.run([str(part) for part in command], cwd=build, env=environment, check=True)

    run([args.source / "configure", *arguments])
    report = census(build)
    report["load_order"] = load_order(build)
    # The census is what the request and the manifest are built from, so the exact
    # line that configured the tree travels with it instead of being recomputed.
    report["configure"] = {"arguments": arguments, "compiler": environment["CC"],
                           "kernel_headers": kernel}
    run(["make", "-j", str(args.jobs)])
    run(["make", "install"])
    stripped = strip_runpath(Path(install))
    print("removed RUNPATH from %d ELF files" % stripped, flush=True)
    print("stripped %d installed ELF files" % strip_install(Path(install)), flush=True)
    (build / "apache-census.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit("httpd build failed: %s\n" % exc)
