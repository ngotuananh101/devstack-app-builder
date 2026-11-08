#!/usr/bin/env python3
"""Turn installed httpd and dependency prefixes into the bundle layout.

The tree keeps httpd's own shape because the shipped configuration and every
documented LoadModule line speak in terms of ServerRoot: config in conf/, modules
in modules/, documents in htdocs/. Only the executables move: the real ELFs go to
libexec/bin/, and bin/ holds launchers that start them through the bundled loader,
so nothing in the bundle is ever run with the host's libc.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import elf

RUNTIME = Path(__file__).resolve().parents[1]
CONFIG = RUNTIME / "config"
# Everything left in packaging/ is POSIX sh: the CI contract step runs `sh -n` over it.
# The httpd.conf template is configuration, so it lives in config/ instead.
PACKAGING = RUNTIME / "packaging"
REPO = RUNTIME.parents[1]
LOADER_NAME = "ld-musl-x86_64.so.1"
# Configure-time scripts. They bake absolute paths into generated files and are of
# no use inside a relocated bundle; upstream's Windows build omits them as well.
# apachectl is here rather than copied because the installed one hard-codes the
# build host's HTTPD path and sources bin/envvars; packaging/apachectl replaces it.
DROPPED = {"apachectl", "apxs", "envvars", "envvars-std", "dbmmanage"}
# Programs that are staged but get no launcher. suexec only works as a root-owned
# setuid executable that the kernel runs directly; a shell launcher cannot carry
# that, and the bundle has nothing to give it. The binary still ships, unmodified,
# next to the modules, for anyone who links it themselves.
UNLAUNCHED = {"suexec": "suexec must be a root-owned setuid binary; a launcher cannot carry that"}
# Programs from the dependency prefix that belong on the bundle's PATH.
EXTRA_PROGRAMS = ("openssl",)
# Programs whose own compiled-in server root must be overridden. httpd bakes
# HTTPD_ROOT at configure time, so without -d it reads the build host's config
# file and the bundle's own conf/ is never consulted.
SERVER_ROOT_ARGUMENT = {"httpd": '-d "$ROOT"'}
# The interpreter every shipped program must name. The build's compiler points
# PT_INTERP at the toolchain's own loader so configure can run its test programs on
# a host with no musl installed; the bundle ships no build workspace paths, so every
# program is rewritten to this name, which the launchers supply explicitly.
SYSLOADER = "/lib/" + LOADER_NAME
DBD_DRIVER = re.compile(r"apr_dbd_[a-z0-9]+-1\.so")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def is_elf(path):
    try:
        return path.read_bytes()[:4] == b"\x7fELF"
    except OSError:
        return False


def portable_interpreter(path):
    """Rewrite a staged program's PT_INTERP to the name the bundle ships.

    Nothing else reads this: the launchers exec the bundled loader with
    --library-path, so the kernel never resolves the name in a bundle run. It still
    has to be the portable one — a build workspace path inside a shipped ELF is a
    host dependency, and it would make the bundle fail on any machine but the one
    that built it.
    """
    current = elf.interpreter(path)
    if current is None:
        raise ValueError("Staged program requests no interpreter: " + path.name)
    if current == SYSLOADER:
        return
    elf.set_interpreter(path, SYSLOADER)


def copy(source, destination, mode=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if mode is not None:
        destination.chmod(mode)
    return destination


def stage_launcher(bundle, template, name):
    launcher = bundle / "bin" / name
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(template.replace("@EXTRA@", SERVER_ROOT_ARGUMENT.get(name, ""))
                        .replace("@NAME@", name))
    launcher.chmod(0o755)


def stage_binaries(install, prefix, bundle):
    """Real ELFs to libexec/bin, a generated launcher per program in bin/."""
    template = (PACKAGING / "launcher").read_text()
    if template.count("@NAME@") != 2 or template.count("@EXTRA@") != 1:
        raise ValueError("Launcher template changed")
    (bundle / "bin").mkdir(parents=True, exist_ok=True)
    (bundle / "libexec/bin").mkdir(parents=True, exist_ok=True)
    launchers, unlaunched = [], {}
    for program in sorted((install / "bin").iterdir()):
        if not program.is_file() or program.is_symlink() or program.name in DROPPED:
            continue
        if not is_elf(program):
            raise ValueError("Unexpected non-ELF program in bin/: " + program.name)
        copy(program, bundle / "libexec/bin" / program.name, 0o755)
        portable_interpreter(bundle / "libexec/bin" / program.name)
        if program.name in UNLAUNCHED:
            # 0755 is deliberate: a setuid bit inside a user-owned tarball would be
            # restored by a root extraction, and nothing in the bundle needs it.
            unlaunched[program.name] = UNLAUNCHED[program.name]
            continue
        stage_launcher(bundle, template, program.name)
        launchers.append(program.name)
    for name in EXTRA_PROGRAMS:
        program = prefix / "bin" / name
        if not program.is_file():
            raise ValueError("The dependency prefix is missing bin/" + name)
        copy(program, bundle / "libexec/bin" / name, 0o755)
        portable_interpreter(bundle / "libexec/bin" / name)
        stage_launcher(bundle, template, name)
        launchers.append(name)
    controller = bundle / "bin/apachectl"
    controller.write_text((PACKAGING / "apachectl").read_text())
    controller.chmod(0o755)
    launchers.append("apachectl")
    copy(PACKAGING / "launch.sh", bundle / "libexec/launch.sh", 0o644)
    return sorted(launchers), unlaunched


def stage_modules(install, bundle, expected):
    """DSOs go to modules/ under ServerRoot, which is what LoadModule names."""
    destination = bundle / "modules"
    destination.mkdir(parents=True, exist_ok=True)
    modules, mpms = [], []
    for module in sorted((install / "modules").glob("mod_*.so")):
        copy(module, destination / module.name, 0o755)
        record = {"name": module.name[4:-3], "path": "modules/" + module.name,
                  "sha256": digest(destination / module.name)}
        (mpms if record["name"].startswith("mpm_") else modules).append(record)
    names = {row["name"] for row in modules}
    if names != set(expected):
        raise ValueError("Staged module set differs from the census: missing %s, unexpected %s"
                         % (sorted(set(expected) - names), sorted(names - set(expected))))
    return modules, mpms


def strip_libraries(bundle):
    """Remove RPATH/RUNPATH from the staged libraries.

    libtool records the dependency prefix in every vendored library it installs,
    so libaprutil-1, libcurl, libldap, libodbccr, libpcre2-posix and libsqlite3
    all name the build host. The launcher sets the search path instead; leaving
    these in place would let a stale path win over the bundle's own libraries.
    """
    changed = []
    for path in sorted((bundle / "lib").rglob("*")):
        if path.is_symlink() or not path.is_file() or not is_elf(path):
            continue
        if elf.strip_rpath(path):
            changed.append(path.relative_to(bundle).as_posix())
    if not changed:
        raise ValueError("No vendored library carried an rpath; the link changed")
    return changed


def stage_libraries(prefix, bundle):
    """Every vendored shared library, plus APR-util's dlopen'd drivers.

    Symlinks are recreated by name rather than flattened, because a NEEDED entry
    is the soname: keeping libcrypto.so.3 -> libcrypto.so.3.0.22 visible is what
    makes the library's identity in the bundle checkable by eye.
    """
    source_dir = prefix / "lib"
    destination = bundle / "lib"
    destination.mkdir(parents=True, exist_ok=True)
    libraries = []
    for entry in sorted(source_dir.iterdir()):
        if ".so" not in entry.name:
            continue
        if entry.is_symlink():
            target = entry.readlink()
            if target.is_absolute() or "/" in str(target):
                raise ValueError("Library symlink escapes the prefix: " + entry.name)
            (destination / entry.name).symlink_to(target)
        elif entry.is_file():
            copy(entry, destination / entry.name, 0o755)
        else:
            continue
        libraries.append(entry.name)
    drivers = sorted((source_dir / "apr-util-1").glob("apr_*.so"))
    if not drivers:
        raise ValueError("APR-util shipped no loadable drivers")
    driver_dir = destination / "apr-util-1"
    driver_dir.mkdir(parents=True, exist_ok=True)
    for driver in drivers:
        if driver.is_symlink():
            (driver_dir / driver.name).symlink_to(driver.readlink())
        else:
            copy(driver, driver_dir / driver.name, 0o755)
    return sorted(libraries), [driver.name for driver in drivers], strip_libraries(bundle)


def stage_loader(bundle, record_path):
    record = json.loads(Path(record_path).read_text())
    source = Path(record["source"])
    if digest(source) != record["sha256"]:
        raise ValueError("The built musl loader changed before staging")
    destination = bundle / "lib/musl"
    destination.mkdir(parents=True, exist_ok=True)
    copy(source, destination / LOADER_NAME, 0o755)
    record["path"] = "lib/musl/" + LOADER_NAME
    record["license"] = "share/licenses/musl.txt"
    record.pop("source", None)
    return record


def stage_tree(install, bundle, request):
    """Config, documents and the data files the shipped config refers to."""
    conf = bundle / "conf"
    conf.mkdir(parents=True, exist_ok=True)
    copy(CONFIG / "httpd.conf", conf / "httpd.conf", 0o644)
    for name in ("mime.types", "magic"):
        copy(install / "conf" / name, conf / name, 0o644)
    # charset.conv is a document upstream, not a generated file: the Windows build
    # copies it out of docs/conf, and so does this one.
    copy(Path(request["sources"]["httpd"]) / "docs/conf/charset.conv", conf / "charset.conv", 0o644)
    for directory in ("extra", "original"):
        source = install / "conf" / directory
        (conf / directory).mkdir(parents=True, exist_ok=True)
        for name in sorted(source.rglob("*")):
            if name.is_file():
                copy(name, conf / directory / name.relative_to(source), 0o644)
    # The generated configs carry the build host's absolute ServerRoot. Rewrite them
    # to ${SRVROOT} so uncommenting httpd-ssl.conf in a moved bundle still works. The
    # config/httpd.conf already defines SRVROOT that way, and the pristine copy
    # under conf/original/ needs a Define of its own, so both are left out here.
    shipped = conf / "httpd.conf"
    original = conf / "original/httpd.conf"
    for name in sorted(conf.rglob("*.conf")):
        if name in (shipped, original):
            continue
        text = name.read_text()
        rewritten = text.replace(str(install), "${SRVROOT}")
        if rewritten != text:
            name.write_text(rewritten)
    rewrite_original(original, install)
    stage_documents(install, bundle)


def rewrite_original(path, install):
    """Make the pristine config backup stand on its own after relocation.

    conf/original/ is upstream's untouched install-time config, which opens with a
    literal ServerRoot and never defines SRVROOT. Substituting ${SRVROOT} there
    would leave an undefined variable, so the file first gets the same Define the
    shipped config uses, right where the directive that needs it opens; only then
    are the remaining build-host paths rewritten like every other config.
    """
    if not path.is_file():
        return
    text = path.read_text()
    literal = 'ServerRoot "%s"' % install
    if literal not in text:
        raise ValueError("conf/original/httpd.conf does not start with the install path")
    text = text.replace(
        literal,
        'Define SRVROOT "${APACHE_SERVER_ROOT}"\nServerRoot "${SRVROOT}"',
        1,
    )
    path.write_text(text.replace(str(install), "${SRVROOT}"))


def stage_documents(install, bundle):
    """The served tree, plus the directories the shipped config logs into."""
    documents = bundle / "htdocs"
    documents.mkdir(parents=True, exist_ok=True)
    for name in sorted((install / "htdocs").iterdir()):
        if name.is_file():
            copy(name, documents / name.name, 0o644)
    # Error documents, icons and the manual are what the shipped extras alias to.
    for directory in ("error", "icons", "manual"):
        if not (install / directory).is_dir():
            raise ValueError("The install tree is missing " + directory)
        shutil.copytree(install / directory, bundle / directory)
    cgi = bundle / "cgi-bin"
    cgi.mkdir(parents=True, exist_ok=True)
    for name in sorted((install / "cgi-bin").iterdir()):
        if name.is_file():
            copy(name, cgi / name.name, 0o755)
    (bundle / "logs").mkdir(parents=True, exist_ok=True)


def stage_ssl_config(bundle, request):
    """Ship openssl.cnf so the bundled openssl CLI works with no extra variables.

    `make install_sw` installs binaries and libraries but not the configuration
    file, while the CLI looks for $OPENSSLDIR/openssl.cnf and refuses to run a
    request without one. Upstream's own file is used verbatim.
    """
    copy(Path(request["sources"]["openssl"]) / "apps/openssl.cnf",
         bundle / "ssl/openssl.cnf", 0o644)


def stage_include(install, prefix, bundle):
    """Headers a consumer needs to build a module against this bundle."""
    destination = bundle / "include"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(install / "include", destination, dirs_exist_ok=True)
    for directory in ("apr-1", "openssl", "curl", "libxml2", "lua5.4", "nghttp2",
                      "brotli", "unixODBC"):
        source = prefix / "include" / directory
        if not source.is_dir():
            raise ValueError("The dependency prefix is missing include/" + directory)
        shutil.copytree(source, destination / directory, dirs_exist_ok=True)
    for header in ("expat.h", "expat_external.h", "jansson.h", "jansson_config.h",
                   "pcre2.h", "pcre2posix.h", "zlib.h", "zconf.h", "ldap.h", "lber.h",
                   "ldap_cdefs.h", "lber_types.h", "ldap_features.h",
                   "sqlite3.h", "sqlite3ext.h"):
        source = prefix / "include" / header
        if source.is_file():
            copy(source, destination / header, 0o644)


def stage_licenses(bundle, request):
    destination = bundle / "share/licenses"
    destination.mkdir(parents=True, exist_ok=True)
    copy(REPO / "LICENSE", destination / "builder.txt", 0o644)
    copy(Path(request["sources"]["httpd"]) / "LICENSE", destination / "httpd.txt", 0o644)
    copy(Path(request["sources"]["musl"]) / "COPYRIGHT", destination / "musl.txt", 0o644)
    for entry in request["dependencies"]:
        if entry["name"] == "musl":
            continue
        (destination / (entry["name"] + ".txt")).write_text(
            "%s %s\n%s\n\nLicensed under %s.\n"
            % (entry["name"], entry["version"], entry["url"], entry["license"])
        )
    # httpd also ships NOTICE, which the ASF license requires travelling with it.
    copy(Path(request["sources"]["httpd"]) / "NOTICE", destination / "httpd.NOTICE", 0o644)


def stage_sources(bundle, request):
    """Record where every byte of source came from, next to the bundle."""
    destination = bundle / "share/sources"
    destination.mkdir(parents=True, exist_ok=True)
    payload = {
        "httpd": {key: request["source"][key] for key in ("version", "url", "sha256")},
        "dependencies": [
            {key: entry[key] for key in ("name", "version", "url", "sha256", "license", "role")}
            for entry in request["dependencies"]
        ],
        "configure": {"arguments": request["configure"], "compiler": request["compiler"],
                      "kernel_headers": request.get("kernel_headers", "")},
    }
    (destination / "sources.json").write_text(json.dumps(payload, indent=2) + "\n")


def check_elf_contract(bundle):
    """No search path survives, and every NEEDED resolves inside the bundle.

    The libraries are checked along with the programs: a vendored library that
    still named the build prefix would look fine from the outside and only fail
    once the bundle moved, which is exactly the failure this contract exists to
    catch before packaging.
    """
    bundled = {path.name for path in (bundle / "lib").rglob("*")
               if path.is_file() or path.is_symlink()} | {LOADER_NAME, "libc.so"}
    binaries = {}
    candidates = (sorted((bundle / "libexec/bin").iterdir())
                  + sorted((bundle / "modules").glob("*.so"))
                  + [path for path in sorted((bundle / "lib").rglob("*.so*"))
                     if path.is_file() and not path.is_symlink()])
    for path in candidates:
        if not is_elf(path):
            raise ValueError("Staged program is not an ELF: " + path.name)
        paths = elf.rpaths(path)
        if paths:
            raise ValueError("ELF still carries a search path: %s -> %s" % (path.name, paths))
        required = elf.needed(path)
        missing = {name for name in required if name not in bundled}
        if missing:
            raise ValueError("Unbundled dependency in %s: %s" % (path.name, sorted(missing)))
        binaries[path.relative_to(bundle).as_posix()] = {
            "needed": required, "sha256": digest(path)}
    return binaries


def tree_digest(bundle):
    """A digest over the whole staged tree, symlink targets included."""
    material = hashlib.sha256()
    for path in sorted(bundle.rglob("*")):
        material.update(path.relative_to(bundle).as_posix().encode())
        if path.is_symlink():
            material.update(b"link:" + path.readlink().as_posix().encode())
        elif path.is_file():
            material.update(bytes.fromhex(digest(path)))
    return material.hexdigest()


def builder_record():
    """Identify the builder that produced the bundle.

    Publication cross-checks the commit against the workflow's own commit, so a
    bundle built from a dirty or different tree cannot be published under another
    commit. The source digest covers the builder itself: its scripts, its pins and
    its packaging, which is everything that decides what ends up in the archive.
    """
    commit = subprocess.check_output(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("The repository has no usable commit to record")
    material = hashlib.sha256()
    for path in sorted(RUNTIME.rglob("*")):
        relative = path.relative_to(REPO)
        if not path.is_file() or "__pycache__" in relative.parts:
            continue
        material.update(str(relative).encode())
        material.update(bytes.fromhex(digest(path)))
    return {"commit": commit, "source_sha256": material.hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path, help="build request JSON written by build.sh")
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    if args.bundle.exists():
        raise ValueError("Refusing to replace an existing bundle: " + str(args.bundle))
    request = json.loads(args.request.read_text())
    install, prefix = Path(request["install"]), Path(request["prefix"])
    census = json.loads(Path(request["census"]).read_text())
    args.bundle.mkdir(parents=True, exist_ok=False)
    launchers, unlaunched = stage_binaries(install, prefix, args.bundle)
    modules, mpms = stage_modules(install, args.bundle, set(census["dso"]))
    libraries, drivers, stripped = stage_libraries(prefix, args.bundle)
    loader = stage_loader(args.bundle, request["loader"])
    stage_tree(install, args.bundle, request)
    stage_ssl_config(args.bundle, request)
    stage_include(install, prefix, args.bundle)
    stage_licenses(args.bundle, request)
    stage_sources(args.bundle, request)
    binaries = check_elf_contract(args.bundle)
    manifest = {
        "schema_version": 1,
        "runtime": "apache",
        # The identity of a build is its bytes: the same request built twice gives
        # the same id only if the result is the same, which is what makes a
        # rebuilt-with-no-changes bundle visibly equal to the published one. The
        # digest is truncated to 16 hex digits, as the other runtimes do, so an
        # archive name stays readable.
        "build_id": tree_digest(args.bundle)[:16],
        "apache": {"version": request["source"]["version"]},
        "target": request["target"],
        "linkage": "dynamic",
        "architecture": request["architecture"],
        "libc": {"family": "musl", "minimum_version": None},
        "loader": loader,
        "mpm": {"default": request["mpm"],
                "shared": sorted(row["name"][4:] for row in mpms)},
        "modules": {
            "dso": sorted(row["name"] for row in modules),
            "static": census["static"],
            "excluded": census["excluded"],
            "load_order": census["load_order"],
            "files": modules,
            "mpm_files": mpms,
        },
        "dbd_drivers": sorted(name for name in drivers if DBD_DRIVER.fullmatch(name)),
        "dbm": "sdbm",
        "libraries": libraries,
        "binaries": binaries,
        "launchers": launchers,
        "unlaunched": [{"name": name, "reason": reason}
                       for name, reason in sorted(unlaunched.items())],
        "dependencies": [
            {key: entry[key] for key in ("name", "version", "sha256", "role")}
            for entry in request["dependencies"]
        ],
        "configure": {"arguments": request["configure"], "compiler": request["compiler"],
                      "kernel_headers": request.get("kernel_headers", "")},
        "builder": builder_record(),
        "build_host": {"system": platform.platform()},
        "verification": "NOT_RUN",
    }
    (args.bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({
        "build_id": manifest["build_id"],
        "launchers": len(launchers),
        "modules": len(modules),
        "mpms": manifest["mpm"]["shared"],
        "libraries": len(libraries),
        "dbd_drivers": manifest["dbd_drivers"],
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit("staging failed: %s\n" % exc)
