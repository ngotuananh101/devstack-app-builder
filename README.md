# DevStack Runtime Builder

Build Linux x86_64 standalone PHP CLI/FPM bundles for DevStack. Consumers download versioned archives rather than compiling locally. Supported PHP series: **8.5, 8.4 (default), 8.3, 8.2**.

> The new SPC-built bundle matrix has not yet passed a real CI run. Local shell contracts and preinstalled PHP tests do not establish production artifact readiness.

## Build

Build hosts require PHP >= 8.4.1 with mbstring/zlib, Composer, Python >= 3.12 and native build dependencies. Installed bundles do not require those tools.

```bash
# Digest-verified immutable SPC v3 source and Composer dependencies
./runtimes/php/scripts/download-spc.sh
# Check only; never auto-install dependencies on the development host
./runtimes/php/spc doctor --auto-fix=never --no-interaction

# Defaults: PHP 8.4, glibc, full profile
./build.sh php
./build.sh php glibc full 8.4
# Opt in to the smaller profile
./build.sh php glibc minimal 8.4
./build.sh php musl full 8.3
# Alternative argument order
./build.sh php 8.2 glibc full
```

SPC stays on pinned **v3.0.0-alpha1**, not v2 or an unverified floating nightly. CI installs build dependencies explicitly. Ordinary build scripts refuse to fix the host. Existing staged bundles/archives are not overwritten.

## Profiles and targets

| Target | Contract | Optional modules |
| --- | --- | --- |
| glibc | Hybrid, x86_64 glibc >= 2.17; checked ELF baseline | `apcu`, `redis` in **full**, shared-only and disabled by default |
| musl | Fully static Linux x86_64 | None; selected modules cannot be toggled |

- `full` (default): web/database extension suite; static except explicitly supported shared modules on glibc.
- `minimal` (explicit opt-in): lean profile, **PDO core but no database drivers**.
- Opcache is static in full; xdebug is not packaged. Not every extension can be shared.
- SPC-required dependencies may be static; actual resolved modes are included in the manifest.

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

Launchers resolve the current installed root, isolate CLI/FPM INI scan directories and module paths, and preserve user arguments. Paths with spaces and unrelated current directories are supported. Use bundled `bin/` launchers, not raw `libexec/` binaries or external symlinks. FPM refuses root and requires caller configuration; the bundled FPM config is example only.

```text
bundle/
  bin/{php,php-fpm,php-ext}
  libexec/{php,php-fpm,launch.sh}
  lib/php/extensions/*.so
  etc/php.ini
  etc/{cli,fpm}/conf.d/
  etc/shared-modules.tsv
  etc/php-fpm.conf.example
  manifest.json
  spc-build-manifest.json
  share/licenses/builder.txt
```

## Test and distribute

```bash
python3 -I -m unittest discover -s tests -v
```

Build output: `dist/php/<requested-version>/<target>/<profile>/`. Verified archives: `dist/assets/php-<actual-patch>-<target>-<profile>-<arch>-<build-id>.tar.gz` plus SHA-256 and JSON manifest sidecars. Tar preserves executable modes. Manifest includes actual patch/API/TS/modes, relative paths, SPC/source provenance and ELF inventory.

CI runs real CLI/FPM module toggles, actual FastCGI requests, relocation with spaces and dependency/baseline checks before packaging. Daily checks compare each php.net stable patch with the highest public bundle version for the same PHP series/target/profile/architecture across all runtime releases. Only newer or missing `full` bundles are built, using the exact patch selected during planning; no updates skips build/publication. API or malformed metadata failures fail closed. Manual runs always rebuild their selection (`full` by default, `minimal` available). Push triggers remain disabled.

Scheduled builds automatically publish after **all selected bundles** pass. The `runtime-releases` job has `contents: write` and retains its environment (required reviewers, if configured, delay automation). Tags are unique `runtimes-<UTC-date>-<run-id>-<attempt>`. Publication checks the persisted plan, creates a draft, uploads archive/checksum/manifest sets, and only then makes the Release public; failures leave a draft and never overwrite assets. English notes describe actual patches, previous versions, module inventories, checksums and build/workflow links. Drafts/prereleases are not a version baseline. Manual publication remains opt-in (`publish=true`, a new `runtimes-...` tag). No Release has been published during implementation. Consumers use the public Releases API/asset URLs without GitHub credentials and verify checksums before extracting into a new directory.

**Remaining production gates:** actual CI full matrix and optional apcu/redis artifacts, oldest glibc host, simultaneous distinct PHP minors, a reviewed pre-extraction version/URL/SHA-256 policy for libraries/extensions and the exact toolchain, complete prepared-toolchain provenance, and third-party legal notices. Stable PHP has upstream php.net checksum validation, not a repository-pinned exact-patch lock. SPDX/signing/attestation are not supplied. Post-build source hashing is not pre-build authentication; do not approve production builds/releases until these input gates are in place.

## Documentation

- [PHP architecture, launcher/manifest/download contracts](docs/runtimes/php.md)
- [Runtime module](runtimes/php/README.md)
- [Builder framework](docs/architecture.md)

## Adding a runtime

Create `runtimes/<name>/scripts/build.sh`, accept runtime arguments, and emit a versioned distribution with metadata. The root dispatcher discovers executable module build scripts.
