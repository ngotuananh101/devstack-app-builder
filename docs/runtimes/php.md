# PHP Runtime Bundles

This builder produces Linux x86_64 PHP CLI/FPM archives for DevStack. Consumers download binaries; they do not need SPC, Composer, system PHP, sudo, or a package manager. A native local PHP **8.4.27** build passed full CLI/FPM verification and extracted-archive checks on **2026-11-07**, including functional probes for all 20 shared modules. **The full musl-dynamic CI matrix remains unvalidated.** One successful local patch version does not certify every supported series or production input provenance.

## Single target: musl-dynamic

There is exactly one target, `musl-dynamic`, and no profile selector.

| Aspect | Contract |
| --- | --- |
| libc | musl; `SPC_TARGET='x86_64-linux-musl -dynamic'` |
| Linkage | Dynamic. `libexec/php` and `libexec/php-fpm` request the bundled interpreter `ld-musl-x86_64.so.1` and `NEEDED libc.so`, which the loader itself satisfies |
| Bundled loader | `lib/musl/ld-musl-x86_64.so.1`, compiled from the pinned musl release in `config/musl.json` with the host `cc` |
| Host requirements | None at run time: no host libc, no host loader, no package manager |
| Extension modes | Fixed static baseline plus 20 shared modules, toggleable per SAPI |
| ELF verification | `NEEDED` may contain only `libc.so`; any `GLIBC_*` symbol, foreign `NEEDED`, or non-`$ORIGIN` `RPATH`/`RUNPATH` fails |

The static baseline lives in `config/static-extensions.txt` and the shared set in `config/shared-extensions.txt`; they must be disjoint. Both files are the whole extension policy — there is no `minimal`/`full` choice.

The build exports `PHP_RPATH=no` for every phpize-generated shared-extension configure script. SPC's main PHP `--disable-rpath` does not propagate to those scripts; without the environment guard, modules using `PHP_ADD_LIBRARY_WITH_PATH` embed an absolute buildroot `RUNPATH`. A reviewed SPC source patch also appends the resolved non-PHP library closure to each final `<EXT>_SHARED_LIBADD`: phpize does not use the configure `LIBS` environment in that link recipe. Linux archive grouping carries transitive dependencies, including unwind/C++ runtimes and libzip's enabled compression backends, without embedding `libphp`. Dependencies other than musl remain statically linked into each module, so no module library search path is needed. The ELF verifier still rejects non-relocatable paths; no post-build ELF rewriting is used.

- Static: `ctype, curl, dom, fileinfo, filter, iconv, libxml, mbstring, mbregex, mysqlnd, openssl, pdo, phar, readline, session, simplexml, tokenizer, xml, xmlreader, xmlwriter, zlib`.
- Shared: `apcu, bcmath, calendar, dba, exif, gd, intl, mysqli, opcache, pdo_mysql, pdo_pgsql, pdo_sqlite, pcntl, posix, redis, sockets, sodium, sqlite3, xsl, zip`.

Shared modules default to **disabled in both SAPIs**. SPC forbids sharing `libxml`, `mbregex`, `readline` and `zlib`, and dependency edges force `xml`, `dom`, `simplexml`, `pdo`, `mysqlnd` and related modules to stay static. SPC may resolve extra required static dependencies. Both SPC's actual resolved modes and the actual `php -n` baseline are recorded; staging fails if a requested shared-only module is static.

### Bundled musl loader

`config/musl.json` pins the musl version, HTTPS release URL and SHA-256. During the build, `scripts/musl_loader.py`:

1. requires the Zig toolchain's bundled musl (`zig/lib/libc/musl/src/internal/version.h`) to be exactly the pinned version;
2. fetches the tarball over HTTPS (or reuses a digest-verified local copy), validates every archive member, and extracts without symlinks;
3. configures and compiles musl with `--disable-static --enable-shared` using the **host `cc`**, `SOURCE_DATE_EPOCH=0`;
4. validates the result as an ELF64 x86-64 dyn object with no `NEEDED`, no `RPATH`/`RUNPATH`, an `_dlstart` entry, the exported symbols PHP needs (`memcpy`, `memset`, `__stack_chk_fail`, `__libc_start_main`), and a `Version x.y.z` banner matching the pin;
5. records the loader digest, version, compiler and musl `COPYRIGHT` digest for the manifest and staging.

musl links its loader without a `SONAME` tag, and the dynamic linker resolves the reserved names (`libc.so`, `libpthread.so`, …) to itself in `ldso/dynlink.c`. A missing `SONAME` is therefore expected; requiring one would reject a correct build.

Compiling musl with `zig cc` produces a loader that is missing exported `memcpy`, `memset` and `__stack_chk_fail`; PHP then fails at relocation. Use a host compiler.

The staged manifest records `loader = {path, sha256, version, soname, satisfies: ["libc.so"], source, toolchain_musl_version, compiler, license}`. Verification re-checks the digest and banner, then runs the bundle under a hostile `LD_PRELOAD`/`LD_LIBRARY_PATH`/`LD_AUDIT`; the launcher drops those variables so an external loader can never be injected.

## Build-host prerequisites

Build hosts need PHP >= 8.4.1 with mbstring/zlib, Composer, Python >= 3.12, and the SPC native build dependencies. These are **not consumer dependencies**. CI pins host PHP 8.4.27 and Composer 2.8.12.

Pinned SPC also requires an executable host musl interpreter at `/lib/ld-musl-x86_64.so.1` before its dynamic-musl toolchain preflight. `doctor` does not install this prerequisite in the current workflow: PHP CI explicitly installs Ubuntu's `musl` package and checks that path before running the dependency doctor and build. Local hosts must have it prepared explicitly; build scripts never install it or skip the toolchain check. This build-host interpreter is not copied into the bundle; installed launchers still use the separately built, digest-pinned loader.

```bash
# Verify pinned SPC v3 source and all 15 Composer dependency archive SHA-256s,
# then install from local verified archives with scripts/plugins disabled.
./runtimes/php/scripts/download-spc.sh

# Read-only doctor policy: fails rather than fixing the host.
./runtimes/php/spc doctor --auto-fix=never --no-interaction

# Build from a fresh work directory, validate, then package.
# An omitted version builds PHP 8.4; there is no target or profile argument.
./build.sh php
./build.sh php 8.4
./build.sh php 8.3.10
```

`SPC_DOWNLOAD_PARALLEL` (default 8) and `SPC_DOWNLOAD_RETRY` (default 3) set SPC's fetch concurrency and retry count. They change only how archives are fetched, never the produced binaries. Lower the parallelism on networks that drop concurrent connections; a failed craft otherwise aborts on one host. `DOWNLOAD_PATH` may point at an already fetched cache directory; SPC re-checks each archive against its lock before use, and `source_inputs` records digests from that same directory.

**Warning:** upstream `spc doctor` without `--auto-fix=never` can prompt with default yes and install packages even when `--auto-fix` was not supplied. Do not use that command as a read-only check. CI explicitly runs `doctor --auto-fix`; ordinary build scripts never do.

Outputs go to `dist/php/<requested-version>/musl-dynamic/` and `dist/assets/`. An existing output directory/archive is not overwritten. Keep previous verified archives when rebuilding; use a clean staging workspace for another build.

### Input integrity and limits

SPC stays on **v3**, pinned to commit `5e913373a08c7c6cf23e45e0b3c2e2caac23af6d` (`3.0.0-alpha1`). `config/spc-lock.json` records source and upstream Composer lock digests. `config/spc-dependencies.json` records every production dependency archive digest. `config/spc-source-patches.json` records reviewed exact-text patches applied to that verified source; each patch pins the SHA-256 of the whole file before and after, so an upstream change fails instead of being silently rewritten. Currently two patches are required: SPC's `zig-cc.sh` appends `clang_rt.crtbegin.o`/`crtend.o` on every `-shared` link, but libtool's C++ tag already captured those objects into `postdep_objects` while probing the compiler, so linking `ext/intl` fails with `duplicate symbol: __dso_handle`. The first patch injects them only when the caller does not already pass them. The second patches `PhpExtensionPackage::makeForUnix()` so the resolved non-PHP dependency closure reaches the final phpize module link instead of remaining only in the configure environment. Verification precedes extraction/Composer/autoload; no floating nightly binary is executed. Bootstrap requires fresh extraction/download directories and refuses digest drift. It does not silently update hashes if GitHub regenerates an archive.

Stable PHP source archives are checked by pinned SPC against SHA-256 values from php.net release metadata before extraction. That metadata is not a repository-pinned exact-patch lock. Ordinary library/PECL URL archives are **not independently preapproved before configure/build**; `source_inputs` records their downloaded SHA-256s afterward. Zig verifies its upstream index checksum but selects a stable toolchain dynamically. Prepared toolchain archives under the shared `PKG_ROOT_PATH` are not yet included in `source_inputs` from the fresh build workspace. These are **production blockers**, not fully authenticated or reproducible input provenance. Do not run a production build/release until library/extension and toolchain inputs have a reviewed exact-version/URL/SHA-256 policy and a pre-extraction gate. A compatible v3 gate uses a separate `spc download`, validates the complete cache, then `craft-options.download:false`; `craft-options.build:false` is not download-only in this pinned v3 implementation. The v3 alpha toolchain, the full 20-module shared set, the pinned musl loader build, and legal notices for all third-party dependencies also require production validation.

## Installed layout

```text
php-<actual-patch>-musl-dynamic-x86_64-<build-id>/
  bin/php                       # POSIX launcher
  bin/php-fpm                   # POSIX launcher; caller-owned FPM config required
  bin/php-ext                   # packaged extension manager
  libexec/php                   # actual CLI binary (do not launch directly)
  libexec/php-fpm                # actual FPM binary
  libexec/launch.sh              # shared installed environment setup
  lib/musl/ld-musl-x86_64.so.1   # bundled musl loader, also libc.so
  lib/php/extensions/*.so        # 20 shared modules
  etc/php.ini                   # shared base INI
  etc/cli/conf.d/                # CLI startup snippets
  etc/fpm/conf.d/                # FPM startup snippets
  etc/shared-modules.tsv         # module, directive, filename
  etc/php-fpm.conf.example       # example only, not used implicitly
  share/licenses/builder.txt
  share/licenses/musl.txt
  manifest.json
  spc-build-manifest.json
```

Launchers `exec` the bundled loader:

```sh
exec "$ROOT/lib/musl/ld-musl-x86_64.so.1" --library-path "$ROOT/lib/musl" "$ROOT/libexec/php" "$@"
```

Use the executable paths in `manifest.paths`, not `libexec` directly. Launchers resolve the current installed root from their own path, unset inherited `LD_PRELOAD`/`LD_LIBRARY_PATH`/`LD_AUDIT`, set `PHPRC`, force a bundle-local per-SAPI `PHP_INI_SCAN_DIR`, pass base `-c` and resolved `extension_dir`, then preserve user arguments exactly. Explicit `-n`, `-c`, or `-d` arguments still have normal PHP semantics. Do not put global symlinks outside the bundle pointing directly to a launcher; call its actual installed path. Installation folders may contain spaces, and execution does not depend on the current directory. Standard Linux `/bin/sh` and core utilities are assumed.

## CLI and optional modules

```bash
RUNTIME="/home/me/.local/share/devstack/PHP 8.4"
"$RUNTIME/bin/php" -v
"$RUNTIME/bin/php-ext" list
"$RUNTIME/bin/php-ext" enable redis cli
"$RUNTIME/bin/php" -r 'var_dump(extension_loaded("redis"));'
"$RUNTIME/bin/php-ext" disable redis all
```

The helper only accepts modules present in the shared registry and on disk. Static modules, unavailable modules and invalid names fail instead of claiming success. INIs use `extension=` or `zend_extension=` from the registry. OPcache keeps the artifact/helper name `opcache` and the directive `zend_extension=opcache.so`; runtime probes map that name to PHP's registered extension name `Zend OPcache`. Edits are atomic. Reload/restart FPM after a change. The repo helper `runtimes/php/scripts/ext.sh ACTION RUNTIME [MODULE] [SAPI]` delegates to the packaged helper and is not needed by consumers.

## FPM consumer contract

DevStack writes a separate absolute config per PHP version/project. Provide a unique Unix socket or loopback port, caller-writable absolute pid/log paths, and pool settings. The launcher does not force nobody, a group, or port 9000 and refuses root/`-R`. Non-root FPM retains the application user's identity. The example shipped in `etc/` is not selected automatically.

```ini
[global]
daemonize = no
error_log = /dev/stderr
pid = "/absolute/app/state/php-8.4.pid"
[app]
listen = "/absolute/app/state/php-8.4.sock"
pm = static
pm.max_children = 2
clear_env = no
catch_workers_output = yes
```

```bash
DEVSTACK_FPM_CONFIG="/absolute/app/config/php-8.4-fpm.conf" "$RUNTIME/bin/php-fpm" -t
DEVSTACK_FPM_CONFIG="/absolute/app/config/php-8.4-fpm.conf" "$RUNTIME/bin/php-fpm" -F
# Equivalent explicit configuration:
"$RUNTIME/bin/php-fpm" -y "/absolute/app/config/php-8.4-fpm.conf" -F
```

`-t` validates configuration only. It is not proof of serving requests. Extension loading belongs to SAPI startup INIs, not pool-level startup overrides.

## Manifest schema v1

`manifest.json` contains:

- `runtime`, `schema_version`, `build_id`, `architecture`, `target` (`musl-dynamic`), `linkage` (`dynamic`), `libc` (`family: musl`).
- `php.version`: actual patch; `php.php_api`, `zend_extension_api`, `extension_build`, `zend_extension_build`, `zts`, `debug`, compiler/configure metadata, and actual `static_modules` under `-n`.
- `requested`: static/shared lists; `resolved_modes`: SPC's resolved lists including forced dependencies.
- `loader`: `path`, `sha256`, `version`, `soname` (normally `null`), `satisfies`, `source`, `toolchain_musl_version`, `compiler`, `license`.
- `shared_modules[]`: name, directive, relative path, SHA-256, initial enabled state for CLI/FPM.
- `paths`: relative launchers, binaries, INI scan dirs, module directory.
- `spc`: immutable commit/source/lock/dependency provenance; `spc_target`, `builder`, `build_host`, `source_inputs`.
- `elf_inventory`: required libraries per binary/module, marked static/shared, plus the loader row and any `GLIBC_*` tokens (which must be empty).
- `verification`: `NOT_RUN` or `PASSED`; `verification_checks`: checks performed on that build.

Treat JSON as data, not instructions. API/TS/architecture/libc must match before considering additional modules. Bundled module hashes are immutable initial contents; enabled state can change after installation. Do not infer current enabled state from build-time manifest defaults.

## Verification and distribution

```bash
python3 -I -m unittest discover -s tests -v
python3 -I runtimes/php/scripts/verify.py "$RUNTIME"
python3 -I runtimes/php/scripts/package.py "$RUNTIME" dist/assets
```

The build verifier checks actual `-n` baseline, enabled/disabled CLI modules, FPM startup validation **and FastCGI requests**, CLI/FPM INI separation, non-root identity, a second installation with spaces, original/relocated installation isolation, loader digest/banner and its self-satisfaction of `libc.so`, per-binary `NEEDED` closure (`libc.so` only, satisfied by the bundled loader), absence of any `GLIBC_*` symbol token, `$ORIGIN`-only `RPATH`/`RUNPATH`, and execution under a hostile `LD_PRELOAD`/`LD_LIBRARY_PATH`/`LD_AUDIT`. A same-build second directory is not a test of two distinct PHP minors; that integration check is still required.

Packaging is allowed only after real bundle verification passes. Files are `php-<actual-patch>-musl-dynamic-<arch>-<build-id>.tar.gz`, `.tar.gz.sha256`, and `.manifest.json`; tar retains executable modes. CI uploads these files, not a raw directory.

## Local validation evidence

On 2026-11-07, `./build.sh php 8.4.27` completed natively with exit code 0 after a fresh source/dependency build, using the prepared Zig toolchain and digest-checked download cache. No Docker or system-package installation was used.

- Bundle: `dist/php/8.4.27/musl-dynamic/`; build ID: `23ac622b3623bed7`.
- Archive: `php-8.4.27-musl-dynamic-x86_64-23ac622b3623bed7.tar.gz`.
- Archive SHA-256: `76589aa99c0e256a8243a4e42af6f8fd3b067405345b8eba15741ebcc4bbd55d`.
- Manifest: `verification=PASSED`, 20 shared modules, all initially disabled in CLI and FPM.
- The production verifier passed ELF contracts, loader integrity, actual non-root FPM FastCGI requests, all-module CLI/FPM toggles, SAPI isolation, relocation with spaces and rejection of host shared objects.
- A fresh archive extraction passed checksum, manifest, permissions, loader/module digests and the same runtime verifier. Functional checks covered all 20 shared modules, including ICU normalization/date formatting, active OPcache, SQLite queries, fork/wait, socket I/O, sodium round trips, XSL transforms and ZIP XZ compression/decompression. MySQL/PostgreSQL drivers and the Redis class were checked without external database/server connections.
- The repository suite passed 95 tests. The shared-link regression executes the reviewed PHP method against real static archives with cyclic dependencies; the OPcache regression uses actual preinstalled CLI/FPM.

This is local evidence for PHP 8.4.27, not full-series CI or production certification. SPC still warns that `llvm-compiler-rt`'s `LICENSE.TXT` was not found; complete third-party legal notices and the input gates above remain unresolved. No commit, push or release publication was performed for this validation.

## Public Release/download contract

Daily checks are scheduled at **21:37 UTC**, or **04:37 Asia/Ho_Chi_Minh on the next calendar day** (`37 21 * * *`). This gives a planned 2-hour-23-minute lead before `dev-stack`'s catalog update at 00:00 UTC / 07:00 Vietnam time. GitHub Actions schedules can be delayed; this is best-effort timing, not a cross-repository dependency or guaranteed completion order.

The daily schedule resolves stable php.net patches for `8.2`–`8.5` and compares numeric versions against the highest public bundle version for each `(PHP series, musl-dynamic, architecture)` across paginated `runtimes-...` releases. Draft/prerelease releases are ignored. A matching schema-v1 `PASSED` manifest must have uploaded archive/checksum companions, `target == "musl-dynamic"`, `linkage == "dynamic"`, no profile field, and consistent identity; malformed relevant metadata or API failures stop planning rather than pretending no bundle exists. Retired static `glibc`/`musl` archives (with or without the old profile segment) are recognised and never count as current. Missing combinations or newer patches select a `musl-dynamic`/`x86_64` build. Equal/older versions select nothing. The exact selected patch is passed to SPC, not re-resolved from a minor input. An empty PHP matrix skips PHP builds only; pinned Redis updates can still build and publish. Publication is skipped when both runtime matrices are empty.

Manual runs resolve exact patches and always rebuild **all PHP series 8.2–8.5 on `musl-dynamic`**, plus every pinned Redis version on fully static `musl`. Runtime, PHP-series, libc and profile selectors are absent. The workflow exposes only `publish` and `release_tag`; their `publish=false` default is unchanged, and `publish=true` requires a NEW `runtimes-...` tag. Scheduled runs automatically publish updates with tag `runtimes-<UTC-date>-<run-id>-<attempt>`. All runs share non-canceling workflow concurrency. GitHub concurrency is not a durable queue: superseded pending runs may be replaced, while a running publication is not canceled.

Publication requires **all selected PHP/Redis builds** to succeed and downloads the persisted `runtime-update-plan.json`. Empty runtime jobs may be skipped; selected failed or cancelled jobs block publication. Archive runtime/version/target/architecture and builder commits must exactly match both plan matrices; PHP rows must carry `target == "musl-dynamic"` and the bundled loader, Redis rows `target == "musl"` with a fully static musl bundle. Redis core is built natively; it has no glibc minimum and its manifest records `libc.static = true`. The `runtime-releases` environment/job alone has `contents: write`; required environment reviewers, if configured, can delay scheduled publication. The script verifies public repository status, archive/checksum/manifest/modes and tag absence, scans paginated releases including drafts to reject reuse, creates a draft with generated English notes, uploads all assets without clobber using the newly returned release ID, and then promotes only that ID to public. Upload failures leave a draft for investigation; no automatic deletion, replacement or resume occurs. Notes report actual PHP versions, previous public patches, actual static/shared modules, checksum/CLI/FPM guidance and source/workflow links, not an invented upstream changelog. Existing production input/validation gates above remain unresolved; do not treat this automation as production certification.

Consumers can query the public GitHub Releases API without credentials, choose a compatible asset by sidecar manifest, then download using its `browser_download_url`. Avoid guessing minor-version-only asset names; the filename contains actual patch and build ID.

```bash
curl -fsSL "https://api.github.com/repos/ngotuananh101/devstack-app-builder/releases" -o releases.json
# Select a release and exact asset names from the API/manifest in DevStack.
curl -fL "$ARCHIVE_URL" -o "$ASSET_NAME"
curl -fL "$CHECKSUM_URL" -o "$ASSET_NAME.sha256"
sha256sum -c "$ASSET_NAME.sha256"
# Extract into a new empty installation directory after checking archive paths.
tar -xzf "$ASSET_NAME" -C "$NEW_EMPTY_INSTALL_DIR"
```

No actual Release has been published by this change. GitHub Actions artifacts with 14-day retention are a CI inspection channel, not the public production download channel. A checksum fetched from the same origin protects integrity, not independent publisher authenticity; signing/attestation is outside this change.
