#!/usr/bin/env python3
"""Build every vendored dependency into one staging prefix with the pinned musl.

Each step is a small explicit recipe. Nothing is linked from the build host: the
pkg-config search path points at the staging prefix only, and every configure run
sees the staging prefix first in its include and library paths. The compiler is
the one loader.py built from the pinned musl, not the host's musl-gcc, so the
objects and the loader that ships with them come from the same release.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import uapi

TARGET = "--build=x86_64-pc-linux-gnu", "--host=x86_64-pc-linux-gnu"


def env_for(prefix, cc, kernel):
    """The environment every dependency recipe builds with.

    musl ships no kernel UAPI headers, so <linux/*.h> and the <asm/*.h> they
    include come from the host. The roots holding them are discovered by loader.py
    from the compiler's own search list — one directory on Fedora, two on Ubuntu,
    which is why they arrive as a root list rather than as a path this file
    assumes. The flags come last, so a musl header of the same name still wins.
    """
    includes = uapi.env_flags(kernel)
    return {
        **os.environ,
        "CC": cc,
        "CFLAGS": "-O2 -fPIC " + includes,
        "CXXFLAGS": "-O2 -fPIC " + includes,
        "CPPFLAGS": "-I%s/include %s" % (prefix, includes),
        "LDFLAGS": "-L%s/lib -Wl,-rpath-link,%s/lib" % (prefix, prefix),
        "PKG_CONFIG_PATH": "%s/lib/pkgconfig" % prefix,
        "PKG_CONFIG_LIBDIR": "%s/lib/pkgconfig" % prefix,
        # Keep pkg-config from falling back to a host installation.
        "PKG_CONFIG_SYSROOT_DIR": "",
    }


def run(command, cwd=None, env=None):
    print("+ " + " ".join(shlex.quote(str(part)) for part in command), flush=True)
    subprocess.run([str(part) for part in command], cwd=cwd, env=env, check=True)


def autotools(source, prefix, jobs, env, options=()):
    """Configure and install an autoconf tree out of tree."""
    build = source.parent / (source.name + "-build")
    build.mkdir(parents=True, exist_ok=False)
    run(
        [source / "configure", "--prefix=" + prefix, *TARGET,
         "--enable-shared", "--disable-static", *options],
        cwd=build, env=env,
    )
    run(["make", "-j", str(jobs)], cwd=build, env=env)
    run(["make", "install"], cwd=build, env=env)


def zlib(source, prefix, jobs, env):
    build = source.parent / "zlib-build"
    build.mkdir(parents=True, exist_ok=False)
    run([source / "configure", "--prefix", prefix, "--shared"], cwd=build, env=env)
    run(["make", "-j", str(jobs)], cwd=build, env=env)
    run(["make", "install"], cwd=build, env=env)


def openssl(source, prefix, jobs, env):
    build = source.parent / "openssl-build"
    build.mkdir(parents=True, exist_ok=False)
    run(
        ["perl", source / "Configure", "linux-x86_64", "-fPIC", "-O2",
         "--prefix=" + prefix, "--openssldir=" + prefix + "/ssl",
         "--libdir=lib", "no-tests", "no-legacy", "no-ssl3", "no-comp"],
        cwd=build, env=env,
    )
    run(["make", "-j", str(jobs)], cwd=build, env=env)
    run(["make", "install_sw"], cwd=build, env=env)


def brotli(source, prefix, jobs, env):
    build = source.parent / "brotli-build"
    build.mkdir(parents=True, exist_ok=False)
    run(
        ["cmake", source, "-DCMAKE_BUILD_TYPE=Release",
         "-DCMAKE_C_COMPILER=" + env["CC"], "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
         "-DBUILD_SHARED_LIBS=ON", "-DCMAKE_INSTALL_PREFIX=" + prefix,
         "-DCMAKE_INSTALL_LIBDIR=lib", "-DBROTLI_DISABLE_TESTS=ON"],
        cwd=build, env=env,
    )
    run(["cmake", "--build", build, "-j", str(jobs)], env=env)
    run(["cmake", "--install", build], env=env)


def lua(source, prefix, jobs, env):
    """Lua has no configure and ships no shared-library build.

    Only the library is needed (mod_lua links it), so the sources are compiled
    straight into a shared object; the standalone interpreter would drag in
    readline, which the musl toolchain does not have.
    """
    libdir = Path(prefix) / "lib"
    # httpd looks for lua.h in <prefix>/include/lua5.4 and then links -llua5.4,
    # so the headers go in that subdirectory and the pc file points there.
    includedir = Path(prefix) / "include" / "lua5.4"
    libdir.mkdir(parents=True, exist_ok=True)
    includedir.mkdir(parents=True, exist_ok=True)
    run([env["CC"], "-shared", "-O2", "-fPIC", "-DLUA_USE_LINUX",
         "-Wl,-soname,liblua5.4.so", "-o", libdir / "liblua5.4.so",
         *(source / "src" / name for name in (
             "lapi.c", "lcode.c", "lctype.c", "ldebug.c", "ldo.c", "ldump.c",
             "lfunc.c", "lgc.c", "llex.c", "lmem.c", "lobject.c", "lopcodes.c",
             "lparser.c", "lstate.c", "lstring.c", "ltable.c", "ltm.c",
             "lundump.c", "lvm.c", "lzio.c", "lauxlib.c", "lbaselib.c",
             "lcorolib.c", "ldblib.c", "liolib.c", "lmathlib.c", "loadlib.c",
             "loslib.c", "lstrlib.c", "ltablib.c", "lutf8lib.c", "linit.c",
         )), "-lm", "-ldl"], env=env)
    for header in ("lua.h", "luaconf.h", "lualib.h", "lauxlib.h", "lua.hpp"):
        run(["cp", source / "src" / header, includedir / header])
    pkgconfig = libdir / "pkgconfig"
    pkgconfig.mkdir(parents=True, exist_ok=True)
    (pkgconfig / "lua5.4.pc").write_text(
        "prefix=%s\nlibdir=${prefix}/lib\nincludedir=${prefix}/include/lua5.4\n\n"
        "Name: Lua\nDescription: Lua language\nVersion: 5.4.9\n"
        "Libs: -L${libdir} -llua5.4 -lm\nCflags: -I${includedir}\n" % prefix
    )


RECIPES = {
    "zlib": zlib,
    "openssl": openssl,
    "pcre2": lambda s, p, j, e: autotools(s, p, j, e, ["--enable-pcre2-8"]),
    "brotli": brotli,
    "nghttp2": lambda s, p, j, e: autotools(s, p, j, e, ["--enable-lib-only"]),
    "jansson": lambda s, p, j, e: autotools(s, p, j, e),
    "expat": lambda s, p, j, e: autotools(s, p, j, e, ["--without-xmlwf", "--without-docbook"]),
    "libxml2": lambda s, p, j, e: autotools(
        s, p, j, e,
        ["--without-python", "--without-lzma", "--without-zstd", "--without-icu"],
    ),
    "lua": lua,
    "openldap": lambda s, p, j, e: autotools(
        s, p, j, e,
        ["--disable-slapd", "--disable-slurpd", "--without-tls",
         "--without-cyrus-sasl"],
    ),
    "sqlite": lambda s, p, j, e: autotools(s, p, j, e, ["--disable-dependency-tracking"]),
    "unixodbc": lambda s, p, j, e: autotools(
        s, p, j, e, ["--disable-gui", "--disable-drivers", "--enable-iconv"],
    ),
    "curl": lambda s, p, j, e: autotools(
        s, p, j, e,
        ["--with-openssl=" + p, "--with-zlib=" + p, "--with-brotli=" + p,
         "--with-nghttp2=" + p, "--without-libpsl", "--without-libidn2",
         "--without-zstd", "--without-libssh2", "--disable-ldap",
         "--disable-ldaps", "--disable-manual", "--disable-ntlm",
         "--disable-rtsp", "--disable-dict", "--disable-telnet",
         "--disable-tftp", "--disable-pop3", "--disable-imap",
         "--disable-smtp", "--disable-gopher", "--disable-mqtt",
         "--disable-smb"],
    ),
    # APR's DSO support (APR_HAS_DSO) is what mod_so uses to dlopen modules and
    # what APR-util requires to build its drivers as DSOs. It is not the same
    # thing as static-vs-shared library linkage, which --disable-static covers.
    "apr": lambda s, p, j, e: autotools(s, p, j, e, ["--enable-threads"]),
    # --without-dbm would arrive as --with-dbm=no, which APR-util rejects: it
    # always names exactly one DBM type. SDBM is the default and is compiled
    # into libaprutil, so the other backends are simply not configured.
    # --with-crypto is what turns on APU_HAS_CRYPTO; without it httpd silently
    # disables mod_session_crypto. It builds apr_crypto_openssl as a DSO too.
    "apr-util": lambda s, p, j, e: autotools(
        s, p, j, e,
        ["--with-apr=" + p, "--with-expat=" + p, "--with-crypto",
         "--with-openssl=" + p, "--with-sqlite3=" + p, "--with-odbc=" + p,
         "--with-ldap", "--with-dbm=sdbm", "--without-berkeley-db",
         "--without-gdbm", "--without-ndbm"],
    ),
}

# APR-util is configured against the installed APR, so it is last.
ORDER = [
    "zlib", "openssl", "pcre2", "brotli", "nghttp2", "jansson", "expat",
    "libxml2", "lua", "openldap", "sqlite", "unixodbc", "curl", "apr",
    "apr-util",
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", type=Path,
                        help="sources.json map written by fetch.py: name -> extracted root")
    parser.add_argument("prefix", type=Path)
    parser.add_argument("--cc", type=Path, required=True,
                        help="musl-gcc built from the pinned musl by loader.py")
    parser.add_argument("--kernel-headers", required=True,
                        help="host include roots holding linux/ and asm/, colon-separated as loader.py found them")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument(
        "--from", dest="first", choices=ORDER, default=ORDER[0],
        help="start at this dependency, keeping everything before it already built",
    )
    args = parser.parse_args()
    # fetch.py extracts each archive into sources/<name>/<archive-root> and reports
    # the inner root. Resolving <name> to the wrapper directory instead would leave
    # every recipe looking for a configure script that is one level down.
    roots = json.loads(args.sources.read_text())
    missing = [name for name in ORDER if not roots.get(name)]
    if missing:
        raise ValueError("No extracted source for: " + ", ".join(missing))
    for name in ORDER:
        if not Path(roots[name]).is_dir():
            raise ValueError("Extracted source is missing for " + name)
    prefix = str(args.prefix.resolve())
    cc = str(Path(args.cc).resolve())
    if not Path(cc).is_file():
        raise ValueError("The pinned musl compiler is missing: " + cc)
    # The root list as loader.py discovered it; env_for renders the flags.
    kernel = uapi.roots(args.kernel_headers)
    env = env_for(prefix, cc, uapi.SEPARATOR.join(kernel))
    args.prefix.mkdir(parents=True, exist_ok=True)
    for name in ORDER[ORDER.index(args.first):]:
        print("=== %s ===" % name, flush=True)
        RECIPES[name](Path(roots[name]).resolve(), prefix, args.jobs, env)
    print(json.dumps({"prefix": prefix, "built": ORDER}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit("dependency build failed: %s\n" % exc)
