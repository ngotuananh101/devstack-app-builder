# PHP Runtime for DevStack

This folder contains the build configuration, profiles, and scripts for creating standalone, reproducible PHP distributions (CLI + FPM) for DevStack.

For architecture and full documentation, refer to [PHP Architecture Doc](../../docs/runtimes/php.md).

## Supported PHP Versions

- **8.5**
- **8.4** (Default)
- **8.3**
- **8.2**

## Quick Build

```bash
# 1. Download StaticPHP CLI and install build host dependencies
./runtimes/php/scripts/download-spc.sh
./runtimes/php/spc doctor --auto-fix

# 2. Build default version (PHP 8.4, minimal profile):
./build.sh php glibc
./build.sh php musl

# 3. Build with specific profile and PHP version:
./build.sh php glibc minimal 8.4
./build.sh php glibc full 8.3
./build.sh php musl full 8.2
# Alternatively:
./build.sh php 8.3 glibc minimal
PHP_VERSION=8.2 ./build.sh php glibc full
```

## Profiles

Available profiles in `config/profiles/`:
- `minimal.txt`: Essential core extensions (default profile).
- `full.txt`: Broad extension set for comprehensive workloads (pdo, mysql, pgsql, redis, gd, intl, opcache, zip, etc.).

## Artifacts

Artifacts are emitted into `dist/php/<version>/<target>/<profile>/`, for example:
- `dist/php/8.4/glibc/full/`
- `dist/php/8.3/musl/minimal/`
