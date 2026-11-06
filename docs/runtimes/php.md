# PHP Runtime Architecture & Specifications

Build reproducible PHP (8.5, 8.4, 8.3, 8.2) x86_64 CLI + FPM distributions using [StaticPHP / SPC](https://github.com/crazywhalecc/static-php-cli).

## Target Libc & Linkage

StaticPHP produces two distinct target variants:

| Target | Linkage | SPC Target | Runtime `.so` Loading | Dynamic Module Toggling |
|---|---|---|:---:|:---:|
| **GNU/glibc** | Partially static, glibc 2.17 baseline | `native-native-gnu.2.17` | Yes | Yes (for shared extensions) |
| **musl** | Fully static | `native-native-musl` | No | No (requires profile rebuild) |

### GNU/glibc
- Uses glibc 2.17 baseline for high compatibility across modern and legacy Linux distributions.
- Core and common extensions are compiled statically into PHP binaries.
- Configured shared extensions (`config/shared-glibc.txt`) are compiled as dynamic `.so` modules and staged into `lib/php/extensions/`.
- Dynamic extensions can be toggled independently for CLI and FPM via INI scan directories without recompiling.
- Incompatible with Alpine / musl-based environments.

### musl
- Fully static binaries targeted for Alpine Linux or minimal container environments.
- All extensions selected in the profile are compiled directly into the binaries.
- Dynamic `.so` module loading is disabled by musl design; changing extensions requires rebuilding with a different profile.

## Directory Structure

```text
runtimes/php/
├── config/
│   ├── profiles/
│   │   ├── minimal.txt       # Lean CLI/worker profile (default)
│   │   └── full.txt          # Comprehensive extension set
│   ├── php.ini               # Base PHP configuration
│   ├── php-fpm.conf          # Default FPM configuration
│   └── shared-glibc.txt      # List of extensions built as shared .so in glibc
├── scripts/
│   ├── build.sh              # Runtime build script (generates craft.yml and runs spc)
│   ├── download-spc.sh       # Downloads StaticPHP CLI binary
│   └── ext.sh                # Helper script to list/enable/disable shared extensions
└── README.md
```

## Staged Output Structure

Artifacts are placed in `dist/php/<version>/<target>/<profile>/`:

```text
dist/php/
└── 8.4/
    ├── glibc/<profile>/
    │   ├── bin/
    │   │   ├── php
    │   │   └── php-fpm
    │   ├── lib/
    │   │   └── php/extensions/   # Shared .so modules
    │   ├── etc/
    │   │   ├── php.ini
    │   │   ├── php-fpm.conf
    │   │   ├── cli/conf.d/       # CLI-specific dynamic INI snippets
    │   │   └── fpm/conf.d/       # FPM-specific dynamic INI snippets
    │   └── manifest.txt
    └── musl/<profile>/
        ├── bin/
        │   ├── php
        │   └── php-fpm
        ├── etc/
        │   ├── php.ini
        │   └── php-fpm.conf
        └── manifest.txt
```

## Customizing PHP Version

You can pass the PHP version as an argument or via the `PHP_VERSION` environment variable:

```bash
# Form 1: [glibc|musl] [profile] [version]
./build.sh php glibc minimal 8.4
./build.sh php glibc full 8.3

# Form 2: [version] [glibc|musl] [profile]
./build.sh php 8.2 musl full

# Form 3: Environment variable
PHP_VERSION=8.3 ./build.sh php glibc minimal
```

## Extension Management (GNU/glibc)

Use `scripts/ext.sh` to manage dynamic extensions:

```bash
# List extensions
./runtimes/php/scripts/ext.sh list dist/php/8.4/glibc/full

# Enable an extension
./runtimes/php/scripts/ext.sh enable dist/php/8.4/glibc/full redis cli
./runtimes/php/scripts/ext.sh enable dist/php/8.4/glibc/full xdebug fpm

# Disable an extension
./runtimes/php/scripts/ext.sh disable dist/php/8.4/glibc/full redis all
```

## ABI & Dependency Rules

1. Every `.so` module is compiled against the exact PHP API version and target libc. Do not attempt to load extensions built from distribution package managers.
2. Static extensions cannot be toggled dynamically.
3. Reload or restart PHP-FPM after enabling or disabling extensions in `fpm/conf.d/`.
