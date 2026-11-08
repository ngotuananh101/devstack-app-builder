# Apache httpd source builds

## Scope

A native Linux x86_64 Apache httpd bundle that behaves like the Windows Apache Lounge
2.4.70 build. Every module Lounge ships is present under Lounge's file name, every
library those modules need is built from a pinned source in this build, and the bundle
carries its own musl loader. Target is **`musl-dynamic` only**; there is no profile and
no libc selector. This module builds, verifies and packages; it does not configure a
DevStack site, install a service, create a user or run a persistent server.

```bash
./build.sh apache                  # pinned version (2.4.70), MPM event
./build.sh apache 2.4.70 --mpm worker
./build.sh apache --print-plan
```

Arguments accept at most one pinned exact version. Unknown options, `glibc`/`musl`/`static`
target arguments, profiles, duplicate arguments and unpinned versions fail before
downloading. `config/sources.json` records the upstream httpd archive SHA-256; bumping
a version means adding the pin and its checksum first.

## Vendored dependency set

Nothing comes from the build host except a C compiler and its kernel UAPI headers.
Each dependency is downloaded over HTTPS, digest-verified **before** extraction into
its own fresh directory, built with the pinned musl toolchain into one staging
prefix, and installed into the bundle's `lib/`.

| Dependency | Version | Role | Needed by |
|---|---|---|---|
| musl | 1.2.5 | loader + toolchain | the bundle's own libc |
| zlib | 1.3.2 | link | `deflate`, curl, libxml2 |
| OpenSSL | 3.0.22 | link | `ssl`, `md`, `http2`, `session_crypto`, LDAP TLS |
| PCRE2 | 10.49 | link | httpd core (`pcre2-config` must report `10.*`) |
| brotli | 1.2.1 | link | `mod_brotli` |
| nghttp2 | 1.71.0 | link | `http2`, `proxy_http2` |
| jansson | 2.14 | link | `mod_md` |
| expat | 2.10.0 | link | APR-util XML → `dav` |
| libxml2 | 2.15.4 | link | `proxy_html`, `xml2enc` |
| Lua | 5.4.9 | link | `mod_lua` |
| OpenLDAP | 2.6.16 | link | `ldap`, `authnz_ldap`, `authz_dbd` (libraries only) |
| sqlite3 | 3.54.1 | dbd | `apr_dbd_sqlite3-1.so` |
| unixODBC | 2.3.14 | dbd | `apr_dbd_odbc-1.so` |
| curl | 8.23.0 | link | `mod_md` (built against the staged OpenSSL/zlib/brotli/nghttp2) |
| APR | 1.7.6 | core | |
| APR-util | 1.6.6 | core | `--with-expat --with-openssl --with-ldap --with-sqlite3 --with-odbc --enable-util-dso` |

**DBD/DBM.** APR-util is built with `--enable-util-dso`, so drivers are `dlopen`ed and
live in `lib/apr-util-1/`. Two ship — `apr_dbd_sqlite3-1.so` and `apr_dbd_odbc-1.so`;
`pgsql`, `mysql`, `oracle` and `freetds` are excluded because Lounge parity is `odbc`
only. DBM is **SDBM only**, compiled into `libaprutil-1.so`; no Berkeley DB, GDBM, or
extra DBM driver is vendored.

**No host libraries.** `pcre2-config`, `libcurl.pc`, `libjansson.pc` and `libnghttp2.pc`
must resolve inside the staging prefix, and `PATH`/`PKG_CONFIG_PATH` are set so a host
copy cannot be picked. The verifier rejects any `NEEDED` entry that is not a bundle file.

## Modules

`--enable-mods-shared=reallyall` with an empty `--enable-mods-static` makes every
`APACHE_MODULE` a DSO: **126 shared modules** plus the three shared MPMs
(`event`, `worker`, `prefork`). Three modules cannot be DSOs and are linked into the
binary: `core`, `http` and `so` (`httpd -l` reports exactly
`core.c http_core.c mod_so.c`).

- **126 DSOs** built and verified: the Lounge module set plus the Linux-only modules
  (`cgid`, `proxy_fdpass`, `suexec`, `unixd`), minus `isapi`.
- **`unixd` ships as a DSO** here. It is not forced static in a Linux shared build, and
  `config/modules.json` lists it in `dso`.
- **Excluded, with recorded reasons** in `config/modules.json`: `isapi` (Windows-only,
  no Linux counterpart), `socache_dc` (unmaintained distcache, not in Lounge),
  `systemd` (would link libsystemd and require a systemd host), `privileges`
  (Solaris-style, not portable).

`mod_http2` and `mod_proxy_http2` are built against the staged nghttp2; `mod_ssl`,
`mod_md`, `mod_brotli`, `mod_lua`, `mod_dav`, `mod_proxy_fcgi` and `mod_rewrite` are all
present and exercised by the verifier.

## Build dependencies

Checked, never installed: a C compiler (`gcc` or `cc`; `APACHE_CC` selects one,
default `cc`), `make`, `perl`, `pkg-config`, `cmake`, `curl`, `readelf`, `strip`,
`ar`, `ranlib`, `tar`, plus HTTPS access to the pinned upstream hosts. `BUILD_JOBS`
selects a positive job count (default 2); `APACHE_MPM` selects the default MPM.

**musl is not taken from the host.** The pinned 1.2.5 source is compiled into a
private toolchain inside the build workspace — headers, crt files, `libc.so` and
musl's own `musl-gcc` wrapper — and every dependency and httpd are built with that
wrapper. This is not an aesthetic choice: Ubuntu 24.04's `musl-tools` is 1.2.4, and
a bundle whose loader is older than the objects linked against it is precisely the
ABI mismatch the build must not ship. The host only has to provide a working C
compiler, which a probe proves before anything is downloaded.

**The toolchain names its own loader while the build runs.** `configure` compiles a
test program and runs it straight away, so the interpreter in its `PT_INTERP` has to
exist on the host — and a runner with no musl installed has no
`/lib/ld-musl-x86_64.so.1`. So the toolchain is built with
`--syslibdir=<toolchain>/lib`, every build-time program asks for that loader, and
`stage.py` rewrites each shipped program's `PT_INTERP` back to the portable
`/lib/ld-musl-x86_64.so.1` before the manifest is written. The rewrite is
length-preserving and refused if the new name cannot fit; the launchers name the
bundled loader explicitly, so nothing in a relocated bundle depends on it either way.

**Kernel UAPI headers do come from the host**, because musl deliberately ships none:
`<linux/*.h>` and the `<asm/*.h>` they include are the distribution's, and they must
match the kernel the bundle runs on. Their location is not portable — Fedora keeps
both under `/usr/include`, while Ubuntu keeps `asm/` under
`/usr/include/x86_64-linux-gnu` — so `uapi.py` reads the compiler's own include
search list and uses every directory on it that supplies `linux/` or `asm/`. Both
must be covered by some directory or the build fails at the probe, and the flags are
always `-idirafter` (never `-I`) so a musl header of the same name still wins.

The pipeline runs under fresh `.cache/apache/build.*` directories:

1. a host-compiler probe — the compiler must build and run a program, and its search
   list must supply `linux/` and `asm/`; either failing stops the run here,
2. `fetch.py` — verify each SHA-256, then extract into `sources/<name>/`,
3. `loader.py` — build the pinned musl into the toolchain, then compile, link and
   **run a probe program** with it, including a kernel header; the same `libc.so`
   becomes the shipped loader,
4. `deps.py` — build the 15 dependency pins (every pin except musl) into a staging
   prefix with that toolchain's `musl-gcc`,
5. `httpd.py` — configure, build, census the modules against `config/modules.json`,
   strip the generated rpaths and the debug symbols,
6. `request.py` / `stage.py` — assemble the bundle and its manifest,
7. `verify.py` — run the bundle,
8. `package.py` — archive + sidecars.

Archives keep their member order and mtimes: several of these trees ship a generated
`configure`/`aclocal.m4`, and extraction that stamps "now" onto every file makes
`make` try to regenerate them with the packaged `missing` script, which fails on any
autotools version mismatch with the host.

## Output contract

```text
dist/apache/<exact-version>/musl-dynamic/
  bin/{httpd,apachectl,htpasswd,htdigest,htdbm,ab,logresolve,httxt2dbm,checkgid,
       rotatelogs,htcacheclean,fcgistarter,openssl}
  libexec/launch.sh
  libexec/bin/<real ELFs>            # httpd, suexec, openssl, ... (never run directly)
  lib/musl/ld-musl-x86_64.so.1
  lib/*.so*                          # openssl, curl, xml2, expat, pcre2, brotli,
                                     #   nghttp2, jansson, lua, ldap, sqlite3, odbc,
                                     #   apr, aprutil, apriconv
  lib/apr-util-1/apr_dbd_{sqlite3,odbc}-1.so
  modules/mod_*.so                   # 126 DSOs, plus mod_mpm_{event,worker,prefork}.so
  conf/httpd.conf conf/mime.types conf/magic conf/charset.conv
  conf/extra/*.conf conf/original/*
  htdocs/index.html cgi-bin/ error/ icons/ manual/
  include/                           # httpd and APR headers for consumers
  ssl/openssl.cnf                    # the OpenSSL CLI's default configuration
  logs/                              # empty
  share/licenses/                    # builder + httpd LICENSE/NOTICE + per-dependency
  share/sources/sources.json         # exact URL/sha256 of every source archive
  manifest.json
```

`modules/` sits at the bundle root, not under `lib/`, because `ServerRoot` is the install
root and every `LoadModule` line says `modules/mod_x.so`.

### Launchers and relocation

`bin/` holds generated POSIX `sh` launchers; the real ELFs live in `libexec/bin/` and are
never executed by the kernel. A launcher resolves its own root, sources
`libexec/launch.sh`, and `exec`s the bundled loader with `--library-path` and `--argv0`:

```sh
exec "$ROOT/lib/musl/ld-musl-x86_64.so.1" \
     --library-path "$LD_LIBRARY_PATH" --argv0 "$ROOT/bin/httpd" \
     "$ROOT/libexec/bin/httpd" -d "$ROOT" "$@"
```

Three details are load-bearing:

- **`-d "$ROOT"`.** httpd bakes `HTTPD_ROOT` at configure time. Without `-d` it reads the
  build host's `conf/httpd.conf` and looks healthy while ignoring the bundle's own.
- **`APACHE_SERVER_ROOT`.** `conf/httpd.conf` starts with
  `Define SRVROOT "${APACHE_SERVER_ROOT}"`. httpd has no `${NAME:default}` syntax, so an
  unset variable would leave `ServerRoot` empty.
- **`OPENSSL_CONF`.** `make install_sw` never installs `openssl.cnf` and the compiled-in
  `OPENSSLDIR` names the build prefix, so the CLI would refuse to run a command without
  one. The launcher points it at the staged copy unless the caller set it.

`LD_PRELOAD`/`LD_AUDIT` are dropped. No ELF in the bundle carries an `RPATH`/`RUNPATH`:
the link keeps the rpath libtool insists on, and the installed ELFs have it edited out, so
resolution is the launcher's job alone.

`apachectl` is a wrapper, not upstream's configure-generated script: it resolves the root
at run time and forwards `start|stop|restart|graceful|graceful-stop` to
`bin/httpd -k`, `configtest` to `bin/httpd -t`, and anything else verbatim. It raises the
soft descriptor limit as upstream does. It is a config/control wrapper, not a service
manager.

`suexec` ships as a plain `libexec/bin/suexec` (mode 0755) with no launcher: it only works
as a root-owned setuid binary the kernel executes directly, which a shell launcher cannot
carry and which a user-owned tarball must not restore.

### Listening on a privileged port

A shell launcher cannot carry `cap_net_bind_service`: `execve` of a script re-execs the
interpreter and takes capabilities from the interpreter's file. Putting the capability on
`libexec/bin/httpd` does not work either — the kernel executes the **loader**, which maps
the program itself. The only file actually executed is the loader:

```bash
sudo setcap cap_net_bind_service=+ep "$ROOT/lib/musl/ld-musl-x86_64.so.1"
```

The shipped config still says `Listen 80`, as Lounge's does. Alternatives: run on ports
≥ 1024, or run as root and let children drop to `User daemon`. The bundle never assumes
the capability is present.

## Verification

`verify.py` proves the bundle by running it, not by reading flags. It starts the bundle's
own launchers and makes real requests:

- the loader's digest, version and exports; every ELF's `NEEDED` set and search paths;
  `httpd -l` reporting exactly the static set;
- all 126 modules load (each is exercised through the config the verifier generates);
- HTTP behaviour, TLS 1.2/1.3 with ALPN `h2` for HTTP/2, DAV `PUT`, a FastCGI round trip,
  a sqlite3-backed DBD login, a Lua hook, the deflate/brotli filters, LDAP trusted-cert
  rejection;
- relocation into a path with spaces, a hostile `LD_*` environment, and independence from
  the host's musl (`bwrap`).

The manifest records the actual httpd version, loader provenance, the module census, MPM,
DBD drivers, DBM type, every binary's `NEEDED` set and hash, builder commit/source digest
and the verification results. Only a `PASSED` bundle can be packaged.

The same pinned musl build that produces the loader is what compiles every vendored
library and httpd, so the recorded loader digest is also the ABI every object in the
bundle was linked against — not merely a loader that happens to sit beside them.

## Assets and publication

```text
dist/assets/apache-<version>-musl-dynamic-<arch>-<build-id>.tar.gz
            apache-<version>-musl-dynamic-<arch>-<build-id>.tar.gz.sha256
            apache-<version>-musl-dynamic-<arch>-<build-id>.manifest.json
```

Existing bundles and asset sets are never replaced. Archives may contain relative
symlinks (the soname aliases `stage_libraries` keeps, e.g. `lib/libz.so.1 -> libz.so.1.3.2`
and brotli's two-hop chain); the publisher follows every hop and refuses a link that is
absolute, escapes the archive root, dangles or loops.

The shared planner treats Apache like Redis's pinned siblings: it builds a pinned version
that is not yet published on `musl-dynamic`/x86_64, never discovers upstream releases, and
lets each runtime be an independent no-op. Publishing validates the archive against the
plan, the builder commit, the manifest and the required program/module set.

## Known limits

- `include/ap_config_layout.h`, `include/ap_config_auto.h` and
  `include/unixODBC/unixodbc_conf.h` still record the configure-time prefix. They are
  headers for consumers linking against the bundle; nothing in the running server reads
  the path.
- OpenSSL's compiled-in `OPENSSLDIR`/`MODULESDIR` name the build prefix. The CLI is
  redirected through `OPENSSL_CONF`; the library itself needs no config file to serve TLS.
- `suexec` is unlaunched (see above). Wiring it up is the consumer's job.
- libc and the loader are pinned and built from source, but two things are still the
  **build host's**: the C compiler that builds that toolchain, and the kernel UAPI
  headers it compiles against. The recorded compiler version and source digest are
  provenance, not a reproducible build guarantee.

## Licensing

httpd, APR and APR-util are Apache-2.0; OpenSSL 3.0 is Apache-2.0; OpenLDAP is
OpenLDAP-2.6 (permissive, with its own notice); unixODBC is LGPL-2.1 (**dynamically**
linked, which is what the LGPL requires for redistribution). Every bundled dependency's
license text is staged under `share/licenses/` (the vendored ones summarised with their
exact version, URL and license), and `share/sources/sources.json` records the exact
upstream URL and SHA-256 of every source archive so the same sources can be re-fetched.
Unlike Redis, the Apache bundle does not embed the 17 source archives: they would
dominate the bundle size. If you redistribute the bundle, keep that URL/digest record
with it so the LGPL source offer can be fulfilled. The builder's license does not
replace any of these.
