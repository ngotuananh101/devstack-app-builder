# DevStack Runtime Builder

Build Linux x86_64 standalone PHP CLI/FPM bundles, native Redis core binaries, Apache httpd bundles and PostgreSQL server bundles for DevStack. Consumers download versioned archives rather than compiling locally. Supported PHP series: **8.5, 8.4 (default), 8.3, 8.2**; PostgreSQL series: **18 (default 18.6), 17 (17.11)**.

All runtimes are musl-only and have no profiles:

- **PHP** is a `musl-dynamic` bundle: the main binaries and the fixed static baseline link against musl, and a fixed set of 20 extensions ships as shared modules (PHP 8.5 compiles OPcache into the binary instead, leaving 19 shared). The bundle carries its own musl loader (`lib/musl/ld-musl-x86_64.so.1`), which is also `libc.so`, so it needs no host libc and no host interpreter.
- **Redis** is a fully static musl build: no interpreter, no `NEEDED` entries.
- **Apache httpd 2.4.70** is a `musl-dynamic` bundle shaped like the Windows Apache Lounge build: 126 shared modules, all three MPMs, and every dependency (OpenSSL, PCRE2, brotli, nghttp2, jansson, curl, expat, libxml2, Lua, OpenLDAP, sqlite3, unixODBC, APR, APR-util) built from a pinned source into the bundle.
- **PostgreSQL** is a `musl-dynamic` server bundle (18.x and 17.x): 37 client/server binaries and 85 extension modules link against the bundled musl loader, with OpenSSL shared and static zlib, libxml2, ICU, and libuuid. Locale collation uses the `builtin` provider with `C.UTF-8` (deterministic Unicode code-point order; alternate `PG_UNICODE_FAST` on 18.x), as musl does not provide glibc-compatible locale collation.

```bash
./build.sh php            # PHP 8.4, musl-dynamic
./build.sh php 8.3
./build.sh redis          # Redis 8.10.3, fully static musl
./build.sh redis 8.10.3
./build.sh apache         # Apache httpd 2.4.70, musl-dynamic
./build.sh apache 2.4.70 --mpm worker
./build.sh postgresql     # PostgreSQL 18.6, musl-dynamic
./build.sh postgresql 17.11
```

Native build dependencies must already be installed; the builder never installs host packages and never uses Docker. See [Redis build contracts](docs/runtimes/redis.md), [PHP build contracts](docs/runtimes/php.md), [Apache httpd build contracts](docs/runtimes/apache.md) and [PostgreSQL build contracts](docs/runtimes/postgresql.md).

> CI run [42384737455](https://github.com/ngotuananh101/devstack-app-builder/actions/runs/42384737455) built and verified PHP 8.2.35, 8.3.36, 8.4.27 and 8.5.12 plus Redis 7.2.17, 7.4.12 and 8.10.3 on 2026-11-07 and published [Release `runtimes-20261107-musl-c95d643`](https://github.com/ngotuananh101/devstack-app-builder/releases/tag/runtimes-20261107-musl-c95d643). A native local PHP 8.4.27 run also covered functional checks for all 20 shared modules. Production input/legal gates remain unvalidated; see [local evidence](docs/runtimes/php.md#local-validation-evidence).

## Build

Build hosts require PHP >= 8.4.1 with mbstring/zlib, Composer, Python >= 3.12, a host `cc`, native build dependencies and a host musl interpreter at `/lib/ld-musl-x86_64.so.1` for SPC's dynamic-musl preflight. CI explicitly installs Ubuntu's `musl` package; local builds never install it. Installed bundles use their own pinned loader and do not require those build-host tools.

```bash
# Digest-verified immutable SPC v3 source and Composer dependencies
./runtimes/php/scripts/download-spc.sh
# Check only; never auto-install dependencies on the development host
./runtimes/php/spc doctor --auto-fix=never --no-interaction

# SPC target is always x86_64-linux-musl with dynamic linkage
./build.sh php
./build.sh php 8.4
./build.sh php 8.3.10
```

SPC stays on pinned **v3.0.0-alpha1**, not v2 or an unverified floating nightly. CI installs build dependencies explicitly. Ordinary build scripts refuse to fix the host. Existing staged bundles/archives are not overwritten.

The musl loader is compiled from the pinned musl release recorded in `runtimes/php/config/musl.json` (version, HTTPS release URL, SHA-256). The build requires the Zig toolchain's bundled musl to be exactly that version, compiles musl out of tree with the host `cc`, and validates the resulting `libc.so` as a shared object with no `NEEDED`, no `RPATH`/`RUNPATH`, an `_dlstart` entry, the exports PHP needs (`memcpy`, `memset`, `__stack_chk_fail`, `__libc_start_main`) and the expected version banner. musl does not set a `SONAME` on its loader; the dynamic linker matches the reserved libc names to itself, so `libc.so` is satisfied without one. Building the loader with `zig cc` instead of the host compiler omits exported `memcpy`/`memset`/`__stack_chk_fail`, which breaks PHP at relocation time.

## Extension set

One set for every build; there is no `minimal`/`full` profile and no libc selector.

- **Static** (`config/static-extensions.txt`): `ctype, curl, dom, fileinfo, filter, iconv, libxml, mbstring, mbregex, mysqlnd, openssl, pdo, phar, readline, session, simplexml, tokenizer, xml, xmlreader, xmlwriter, zlib`.
- **Shared** (`config/shared-extensions.txt`, packaged as `.so`): `apcu, bcmath, calendar, dba, exif, gd, intl, mysqli, opcache, pdo_mysql, pdo_pgsql, pdo_sqlite, pcntl, posix, redis, sockets, sodium, sqlite3, xsl, zip`.

Shared modules are **disabled by default in both SAPIs** and are toggled per installation with `bin/php-ext`. `opcache` is a Zend extension and receives `zend_extension=`. PHP 8.5 no longer produces `opcache.so`: upstream links OPcache into every binary and removed its configure flags, so that series records `opcache` as a built-in module (always loaded, no INI, not toggleable) while 8.2–8.4 keep the shared `.so`. Not every extension can be shared: SPC forbids sharing `libxml`, `mbregex`, `readline` and `zlib`, and dependency edges force `xml`, `pdo`, `mysqlnd`, `dom` and related modules to stay static. SPC-required dependencies may also be static; both the requested and the actual resolved modes are recorded in the manifest.

## Bundle usage

```bash
RUNTIME="/home/me/.local/share/devstack/PHP 8.4"
"$RUNTIME/bin/php" -v
"$RUNTIME/bin/php-ext" list
"$RUNTIME/bin/php-ext" enable redis cli
"$RUNTIME/bin/php-ext" disable redis all

# Non-root FPM: DevStack supplies a separate absolute config/socket/pid per instance
DEVSTACK_FPM_CONFIG="/absolute/app/config/php-8.4-fpm.conf" "$RUNTIME/bin/php-fpm" -F
```

Launchers resolve the current installed root, start the real binary through the bundled musl loader (`--library-path "$ROOT/lib/musl"`), drop inherited `LD_PRELOAD`/`LD_LIBRARY_PATH`/`LD_AUDIT`, isolate CLI/FPM INI scan directories and module paths, and preserve user arguments. Paths with spaces and unrelated current directories are supported. Always launch through `bin/`; running a raw `libexec/` binary would fall back to the host interpreter. FPM refuses root and requires caller configuration; the bundled FPM config is example only.

```text
bundle/
  bin/{php,php-fpm,php-ext}
  libexec/{php,php-fpm,launch.sh}
  lib/musl/ld-musl-x86_64.so.1
  lib/php/extensions/*.so
  etc/php.ini
  etc/{cli,fpm}/conf.d/
  etc/shared-modules.tsv
  etc/php-fpm.conf.example
  manifest.json
  spc-build-manifest.json
  share/licenses/{builder,musl}.txt
# postgresql: dist/postgresql/<version>/musl-dynamic/ (sample configs live in share/, no etc/)
```

## Apache bundle usage

```bash
RUNTIME="/home/me/.local/share/devstack/Apache 2.4"
"$RUNTIME/bin/httpd" -V              # always passes -d "$ROOT": the bundle's own ServerRoot
"$RUNTIME/bin/httpd" -t              # config test
"$RUNTIME/bin/apachectl" start       # wrapper around bin/httpd -k; not a service manager
"$RUNTIME/bin/openssl" version

# Port 80 (what the shipped httpd.conf says) needs a capability on the loader,
# because the loader, not the shell launcher, is the file the kernel executes:
sudo setcap cap_net_bind_service=+ep "$RUNTIME/lib/musl/ld-musl-x86_64.so.1"
```

Real ELFs live in `libexec/bin/` and are never run directly; `bin/` holds `sh` launchers that start the bundled loader with the bundle's own library path. `conf/httpd.conf` is Lounge's shape — `Define SRVROOT "${APACHE_SERVER_ROOT}"`, `Listen 80`, `${SRVROOT}/htdocs`, `#LoadModule x_module modules/mod_x.so` — and is relocatable with the tree. `suexec` ships unlaunched in `libexec/bin/` because it must be a root-owned setuid binary. `mod_proxy_fcgi` ships and is verified but is not wired to PHP for you; configuring a DevStack site is the consumer's job.

## Test and distribute

```bash
python3 -I -m unittest discover -s tests -v
```

Build output: `dist/php/<requested-version>/musl-dynamic/` and `dist/postgresql/<version>/musl-dynamic/`. Verified archives: `dist/assets/php-<actual-patch>-musl-dynamic-<arch>-<build-id>.tar.gz` plus SHA-256 and JSON manifest sidecars. Tar preserves executable modes. Manifest includes actual patch/API/TS/modes, the static/shared split, loader provenance, relative paths, SPC/source provenance and ELF inventory.

CI runs real PHP CLI/FPM module toggles, actual FastCGI requests, a real PostgreSQL cluster lifecycle with every host musl loader masked, relocation with spaces, loader-digest/version checks, a hostile `LD_*` environment and ELF dependency checks before packaging. The workflow has no runtime, PHP-series, libc or profile input, and every trigger — daily schedule and manual alike — plans PHP **8.2–8.5** on `musl-dynamic`, every pinned Redis version (**8.10.3, 7.4.12, 7.2.17**) on `musl`, every pinned PostgreSQL version (**18.6, 17.11**) on `musl-dynamic`, and the pinned Apache httpd version on `musl-dynamic`. Push triggers remain disabled.

The Apache job builds the pinned musl toolchain, builds all 15 vendored dependencies with it, runs the server, and checks HTTP, TLS 1.2/1.3 with HTTP/2 ALPN, FastCGI, DAV, a sqlite3 DBD login, Lua and the compressors in the relocated bundle before anything is packaged. The runner supplies only a C compiler: musl is compiled from its pin (Ubuntu 24.04 packages 1.2.4, the pin is 1.2.5), and the kernel UAPI headers musl does not ship are located from that compiler's own include search list instead of an assumed `/usr/include`.

The planner always compares against public releases. PHP compares each php.net stable patch with the highest public `musl-dynamic`/x86_64 bundle for the same series across paginated runtime releases; retired static `glibc`/`musl` archives are recognised and ignored. Redis, Apache and PostgreSQL select pinned exact-version bundles that are not yet published; none discovers or approves unpinned upstream releases. PostgreSQL additionally rejects a public asset group whose manifest is unverified or whose recorded major disagrees with its version. Only newer/missing PHP bundles and missing Redis/Apache/PostgreSQL bundles build, so a manual run behaves exactly like the daily check by default. Each of the four runtimes can independently be a no-op; all being current skips all builds/publication, and a manual run over already-published versions builds nothing. The manual-only `bypass=true` input skips that comparison and rebuilds every series and every pinned Redis/PostgreSQL/Apache version. API or malformed relevant metadata failures fail closed.

Scheduled builds automatically publish after **all selected PHP/Redis/PostgreSQL/Apache bundles** pass, including when only one runtime has an update. The shared exact-patch plan is preserved as `runtime-update-plan.json`; skipped empty matrices are allowed, but a failed or cancelled required build blocks publication. The `runtime-releases` job has `contents: write` and retains its environment (required reviewers, if configured, delay automation). Tags are unique `runtimes-<UTC-date>-<run-id>-<attempt>`. Publication checks the persisted plan, creates a draft, uploads archive/checksum/manifest sets, and only then makes the Release public; failures leave a draft and never overwrite assets. English notes describe actual PHP/Redis/PostgreSQL/Apache versions, PHP module inventories, Redis static-musl requirements, the Apache module/MPM inventory, PostgreSQL module and locale inventories, checksums and build/workflow links. Drafts/prereleases are not a version baseline. Manual publication remains opt-in (`publish=true`, a new `runtimes-...` tag). The first public Release, `runtimes-20261107-musl-c95d643`, was published on 2026-11-07 from CI run 42384737455 with 21 assets. Consumers use the public Releases API/asset URLs without GitHub credentials and verify checksums before extracting into a new directory.

**Remaining production gates:** simultaneous distinct PHP minors installed side by side, a reviewed pre-extraction version/URL/SHA-256 policy for libraries/extensions and the exact toolchain, complete prepared-toolchain provenance, and third-party legal notices. The full musl-dynamic extension matrix did run in CI (PHP 8.2–8.5 plus every pinned Redis version), but that does not satisfy the input gates below. Stable PHP has upstream php.net checksum validation, not a repository-pinned exact-patch lock. SPDX/signing/attestation are not supplied. Post-build source hashing is not pre-build authentication; do not approve production builds/releases until these input gates are in place.

## Documentation

- [Redis native builds and artifact contracts](docs/runtimes/redis.md)
- [PHP architecture, launcher/manifest/download contracts](docs/runtimes/php.md)
- [Apache httpd builds, module set and bundle contract](docs/runtimes/apache.md)
- [PostgreSQL architecture, launcher/manifest/locale contracts](docs/runtimes/postgresql.md)
- [Runtime module](runtimes/php/README.md)
- [PostgreSQL runtime module](runtimes/postgresql/README.md)
- [Builder framework](docs/architecture.md)

## Adding a runtime

Create `runtimes/<name>/scripts/build.sh`, accept runtime arguments, and emit a versioned distribution with metadata. The root dispatcher discovers executable module build scripts.
