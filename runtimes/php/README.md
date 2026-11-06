# PHP Runtime for DevStack

This folder contains the build configuration, profiles, and scripts for creating standalone, reproducible PHP distributions (CLI + FPM) for DevStack.

For architecture and full documentation, refer to [PHP Architecture Doc](../../docs/runtimes/php.md).

## Supported PHP Versions

- **8.4** (Default)
- **8.3**
- **8.2**
- **8.1**

## Quick Build

```bash
# 1. Download StaticPHP CLI and install build host dependencies
./runtimes/php/scripts/download-spc.sh
./runtimes/php/spc doctor --auto-fix

# 2. Build default version (PHP 8.4):
./build.sh php glibc laravel
./build.sh php musl minimal

# 3. Build specific PHP version (e.g. 8.3, 8.2):
./build.sh php glibc laravel 8.3
./build.sh php musl minimal 8.2
# Alternatively:
./build.sh php 8.3 glibc laravel
PHP_VERSION=8.1 ./build.sh php glibc minimal
```

## Profiles

Available profiles in `config/profiles/`:
- `minimal.txt`: Essential core extensions.
- `laravel.txt`: Extensions recommended for Laravel applications.
- `full.txt`: Broad extension set for complex enterprise workloads.

## Artifacts

Artifacts are emitted into `dist/php/<version>/<target>/<profile>/`, for example:
- `dist/php/8.4/glibc/laravel/`
- `dist/php/8.3/musl/minimal/`
