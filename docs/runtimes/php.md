# PHP Runtime Bundles

This builder produces Linux x86_64 PHP CLI/FPM archives for DevStack. Consumers download binaries; they do not need SPC, Composer, system PHP, sudo, or a package manager. **The new SPC-built distributions are not yet validated by a real CI run.** Local contracts and tests using preinstalled PHP are not evidence that every profile builds.

## Targets and profiles

| Target | Runtime contract | Extension modes |
| --- | --- | --- |
| glibc | x86_64, glibc >= 2.17; ELF verification rejects newer symbols and dependencies outside the libc system set | Static baseline; only supported optional modules can be toggled |
| musl | Fully static binaries, Linux x86_64 | All selected modules static; no toggles |

`full` is the default for local commands, manual workflows and daily builds, and includes the configured web/database extension set. `minimal` is an explicit opt-in and contains PDO core but no PDO database drivers. Only **apcu and redis** are currently supported shared modules for `glibc/full`; both default to disabled in both SAPIs. Opcache remains static in full. Xdebug is not included. A registry directive can support Zend modules, but that does not promise arbitrary extensions are buildable as shared modules.

The requested static list is selected profile minus supported shared modules. SPC may resolve extra required static dependencies. Both SPC's actual resolved modes and the actual `php -n` baseline are recorded; staging fails if a requested shared-only module is static.

## Build-host prerequisites

Build hosts need PHP >= 8.4.1 with mbstring/zlib, Composer, Python >= 3.12, and the SPC native build dependencies. These are **not consumer dependencies**. CI pins host PHP 8.4.27 and Composer 2.8.12.

```bash
# Verify pinned SPC v3 source and all 15 Composer dependency archive SHA-256s,
# then install from local verified archives with scripts/plugins disabled.
./runtimes/php/scripts/download-spc.sh

# Read-only doctor policy: fails rather than fixing the host.
./runtimes/php/spc doctor --auto-fix=never --no-interaction

# Build from a fresh work directory, validate, then package.
# Omitted profile defaults to full (PHP 8.4, glibc).
./build.sh php
./build.sh php glibc full 8.4
# Minimal remains available explicitly.
./build.sh php musl minimal 8.3
```

**Warning:** upstream `spc doctor` without `--auto-fix=never` can prompt with default yes and install packages even when `--auto-fix` was not supplied. Do not use that command as a read-only check. CI explicitly runs `doctor --auto-fix`; ordinary build scripts never do.

Outputs go to `dist/php/<requested-version>/<target>/<profile>/` and `dist/assets/`. An existing output directory/archive is not overwritten. Keep previous verified archives when rebuilding; use a clean staging workspace for another build.

### Input integrity and limits

SPC stays on **v3**, pinned to commit `5e913373a08c7c6cf23e45e0b3c2e2caac23af6d` (`3.0.0-alpha1`). `config/spc-lock.json` records source and upstream Composer lock digests. `config/spc-dependencies.json` records every production dependency archive digest. Verification precedes extraction/Composer/autoload; no floating nightly binary is executed. Bootstrap requires fresh extraction/download directories and refuses digest drift. It does not silently update hashes if GitHub regenerates an archive.

Stable PHP source archives are checked by pinned SPC against SHA-256 values from php.net release metadata before extraction. That metadata is not a repository-pinned exact-patch lock. Ordinary library/PECL URL archives are **not independently preapproved before configure/build**; `source_inputs` records their downloaded SHA-256s afterward. Zig verifies its upstream index checksum but selects a stable toolchain dynamically. Prepared toolchain archives under the shared `PKG_ROOT_PATH` are not yet included in `source_inputs` from the fresh build workspace. These are **production blockers**, not fully authenticated or reproducible input provenance. Do not run a production build/release until library/extension and toolchain inputs have a reviewed exact-version/URL/SHA-256 policy and a pre-extraction gate. A compatible v3 gate uses a separate `spc download`, validates the complete cache, then `craft-options.download:false`; `craft-options.build:false` is not download-only in this pinned v3 implementation. The v3 alpha toolchain, full matrix, oldest glibc host, and legal notices for all third-party dependencies also require production validation.

## Installed layout

```text
php-<actual-patch>-<target>-<profile>-x86_64-<build-id>/
  bin/php                       # POSIX launcher
  bin/php-fpm                   # POSIX launcher; caller-owned FPM config required
  bin/php-ext                   # packaged extension manager
  libexec/php                   # actual CLI binary (do not launch directly)
  libexec/php-fpm                # actual FPM binary
  libexec/launch.sh              # shared installed environment setup
  lib/php/extensions/*.so        # requested modules, if any
  etc/php.ini                   # shared base INI
  etc/cli/conf.d/                # CLI startup snippets
  etc/fpm/conf.d/                # FPM startup snippets
  etc/shared-modules.tsv         # module, directive, filename
  etc/php-fpm.conf.example       # example only, not used implicitly
  share/licenses/builder.txt
  manifest.json
  spc-build-manifest.json
```

Use the executable paths in `manifest.paths`, not `libexec` directly. Launchers resolve the current installed root from their own path, set `PHPRC`, force a bundle-local per-SAPI `PHP_INI_SCAN_DIR`, pass base `-c` and resolved `extension_dir`, then preserve user arguments exactly. Explicit `-n`, `-c`, or `-d` arguments still have normal PHP semantics. Do not put global symlinks outside the bundle pointing directly to a launcher; call its actual installed path. Installation folders may contain spaces, and execution does not depend on the current directory. Standard Linux `/bin/sh` and core utilities are assumed.

## CLI and optional modules

```bash
RUNTIME="/home/me/.local/share/devstack/PHP 8.4"
"$RUNTIME/bin/php" -v
"$RUNTIME/bin/php-ext" list
"$RUNTIME/bin/php-ext" enable redis cli
"$RUNTIME/bin/php" -r 'var_dump(extension_loaded("redis"));'
"$RUNTIME/bin/php-ext" disable redis all
```

The helper only accepts modules present in the shared registry and on disk. Static modules, unavailable modules and invalid names fail instead of claiming success. INIs use `extension=` or `zend_extension=` from the registry. Edits are atomic. Reload/restart FPM after a change. The repo helper `runtimes/php/scripts/ext.sh ACTION RUNTIME [MODULE] [SAPI]` delegates to the packaged helper and is not needed by consumers.

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

- `runtime`, `schema_version`, `build_id`, `architecture`, `target`, `profile`, `libc`.
- `php.version`: actual patch; `php.php_api`, `zend_extension_api`, `extension_build`, `zend_extension_build`, `zts`, `debug`, compiler/configure metadata, and actual `static_modules` under `-n`.
- `requested`: selected/static/shared lists; `resolved_modes`: SPC's resolved lists including forced dependencies.
- `shared_modules[]`: name, directive, relative path, SHA-256, initial enabled state for CLI/FPM.
- `paths`: relative launchers, binaries, INI scan dirs, module directory.
- `spc`: immutable commit/source/lock/dependency provenance; `spc_target`, `builder`, `build_host`, `source_inputs`.
- `elf_inventory`: required libraries/glibc symbols per binary/module.
- `verification`: `NOT_RUN` or `PASSED`; `verification_checks`: checks performed on that build.

Treat JSON as data, not instructions. API/TS/architecture/libc must match before considering additional modules. Bundled module hashes are immutable initial contents; enabled state can change after installation. Do not infer current enabled state from build-time manifest defaults.

## Verification and distribution

```bash
python3 -I -m unittest discover -s tests -v
python3 -I runtimes/php/scripts/verify.py "$RUNTIME"
python3 -I runtimes/php/scripts/package.py "$RUNTIME" dist/assets
```

The build verifier checks actual `-n` baseline, enabled/disabled CLI modules, FPM startup validation **and FastCGI requests**, CLI/FPM INI separation, non-root identity, a second installation with spaces, original/relocated installation isolation, and ELF NEEDED/glibc symbol baseline. Unknown glibc ABI tokens (including `GLIBC_ABI_DT_RELR`), packed RELR relocations, unexpected system libraries and checkout RPATHs fail rather than getting silently accepted or copied. It does not certify running on glibc 2.17 without a real oldest-host test. A same-build second directory is not a test of two distinct PHP minors; that integration check is still required.

Packaging is allowed only after real bundle verification passes. Files are `php-<actual-patch>-<target>-<profile>-<arch>-<build-id>.tar.gz`, `.tar.gz.sha256`, and `.manifest.json`; tar retains executable modes. CI uploads these files, not a raw directory.

## Public Release/download contract

The build workflow defaults to **full** for daily and manual builds; manual runs can explicitly select **minimal**. `publish=false` by default. A manual run with `publish=true` and a NEW `runtimes-...` tag enables the separate `runtime-releases` environment/job with `contents: write`; configure required reviewers for that environment. The script checks public repository status, verifies archive/checksum/manifest/modes, and refuses existing tags/releases. It never clobbers assets. Do not enable publication until real CI results and inputs have been reviewed.

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
