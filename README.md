# PHP Runtime Builder

Build reproducible PHP 8.4 x86_64 CLI + FPM distributions with [StaticPHP / SPC](https://github.com/crazywhalecc/static-php-cli).

Two libc targets are produced independently:

- **GNU/glibc** (`native-native-gnu.2.17`): static core + optional shared extensions (`.so`) that can be enabled per SAPI using INI scan directories.
- **musl** (`native-native-musl`): fully static binary. Extensions are compiled into the binary and cannot be toggled dynamically. Change the extension set and rebuild to change the feature set.

> StaticPHP v3 is currently documented as alpha. Pin the SPC version/commit before treating artifacts as production releases. This repository is a starter build system; CI smoke tests are the first compatibility gate, not a claim that every third-party extension is supported by every SPC revision.

## Quick start

```bash
git clone <your-repository-url> php-runtime-builder
cd php-runtime-builder

# Linux x86_64 build host
./scripts/download-spc.sh
./spc doctor --auto-fix

# Build GNU/glibc hybrid (default extension profile)
./scripts/build.sh glibc laravel

# Build fully static musl (same profile)
./scripts/build.sh musl laravel
```

Artifacts are emitted under `dist/<target>/<profile>/`:

```text
dist/
  glibc/laravel/
    bin/php
    bin/php-fpm
    lib/php/extensions/...
    etc/php.ini
    etc/cli/conf.d/
    etc/fpm/conf.d/
  musl/laravel/
    bin/php
    bin/php-fpm
    etc/php.ini
```

The exact SPC output layout can vary by SPC version; `build.sh` stages the documented `buildroot/bin/php` and `buildroot/bin/php-fpm` outputs and copies the buildroot extension directory when present. Review the generated manifest after a build.

## Build profiles

Profiles live in `config/profiles/`:

- `minimal.txt`
- `laravel.txt`
- `full.txt`

Each file is a comma-separated list of PHP extensions. Edit these lists to suit your workload. Extension availability and extra system-library requirements are defined by the SPC registry; check [Supported Extensions](https://static-php.dev/en/guide/extensions.html) and [Extension Notes](https://static-php.dev/en/guide/extension-notes.html).

The GNU target uses `config/shared-glibc.txt` as the list of extensions to build as shared modules. Every shared extension must also appear in the selected profile. Extensions not in that file are compiled into PHP.

The musl target ignores `shared-glibc.txt`: all selected extensions are compiled in, because musl's fully static target cannot load external `.so` modules at runtime.

## Extension management (GNU/glibc)

```bash
# List shared extensions and current per-SAPI state
./scripts/ext.sh list glibc/laravel

# Enable / disable a shared extension for CLI, FPM, or both
./scripts/ext.sh enable glibc/laravel redis cli
./scripts/ext.sh enable glibc/laravel xdebug fpm
./scripts/ext.sh disable glibc/laravel redis all
```

The manager writes/removes INI snippets in the separate CLI and FPM scan directories. It does not restart FPM; restart/reload the service after changing FPM extensions.

Static extensions cannot be disabled using this manager. For musl, switch to a different build profile (or rebuild with a changed extension list).

Example invocation:

```bash
PHP_INI_SCAN_DIR="$PWD/dist/glibc/laravel/etc/cli/conf.d" \
  dist/glibc/laravel/bin/php \
  -c "$PWD/dist/glibc/laravel/etc/php.ini" -m
```

For FPM, set `PHP_INI_SCAN_DIR` in the systemd service environment or wrapper and use:

```bash
dist/glibc/laravel/bin/php-fpm \
  -c "$PWD/dist/glibc/laravel/etc/php.ini" \
  -y "$PWD/dist/glibc/laravel/etc/php-fpm.conf" -t
```

## Build targets and limitations

| Target | Linkage | Runtime `.so` loading | Dynamic enable/disable |
|---|---|---:|---:|
| GNU/glibc | partially static, glibc-linked | Yes | Yes, for shared extensions |
| musl | fully static | No | No; rebuild/profile selection |

GNU target baseline is glibc 2.17. It will not run on Alpine/musl. The musl build is the Alpine-oriented variant.

## GitHub Actions

The workflow `.github/workflows/build.yml` supports manual dispatch with `target` (`glibc`, `musl`, `both`) and `profile` (`minimal`, `laravel`, `full`). It builds on Ubuntu x86_64 and uploads artifacts.

## Important production checklist

1. Pin an SPC release or commit; avoid consuming `nightly` for production without validation.
2. Review the exact extension list and verify each extension in the SPC registry.
3. Verify shared modules exist in the output and that `php --ri <extension>` works after enabling.
4. Run `php -v`, `php -m`, `php-fpm -v`, `php-fpm -t`, Laravel smoke tests, and tests on the oldest supported target distro.
5. Run `spc dump-license` and ship third-party notices with the distribution.
6. Sign/checksum release artifacts and keep GNU and musl artifacts separate.

## References

- https://static-php.dev/en/guide/first-build.html
- https://static-php.dev/en/develop/craft-yml.html
- https://static-php.dev/en/guide/sapi-reference.html
- https://github.com/crazywhalecc/static-php-cli
