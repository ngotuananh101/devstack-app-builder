# DevStack Runtime Builder

A unified build system for creating reproducible, portable, and standalone language runtimes for DevStack environments.

Currently supports **PHP** (8.4, 8.3, 8.2, 8.1) CLI + FPM with dual glibc/musl targets via [StaticPHP CLI](https://github.com/crazywhalecc/static-php-cli), with a modular architecture ready for additional runtimes (Node.js, Python, etc.).

---

## Repository Structure

```text
devstack-runtime-builder/
├── build.sh                      # Unified build entrypoint wrapper
├── scripts/
│   └── build.sh                  # Runtime dispatcher script
├── runtimes/                     # Isolated runtime modules
│   └── php/                      # PHP runtime definition
│       ├── config/
│       │   ├── profiles/         # Extension profiles (minimal, laravel, full)
│       │   ├── php.ini           # Base PHP configuration
│       │   ├── php-fpm.conf      # Default FPM configuration
│       │   └── shared-glibc.txt  # Shared extension definitions (.so)
│       ├── scripts/
│       │   ├── build.sh          # PHP build script (supports custom versions)
│       │   ├── download-spc.sh   # StaticPHP CLI downloader
│       │   └── ext.sh            # Dynamic extension manager
│       └── README.md
├── dist/                         # Staged build artifacts (gitignored)
│   └── php/
│       └── <version>/
│           ├── glibc/<profile>/
│           └── musl/<profile>/
├── docs/                         # Architecture and detailed documentation
│   ├── architecture.md           # Builder framework architecture
│   └── runtimes/
│       └── php.md                # PHP runtime architecture & specifications
└── .github/
    └── workflows/
        └── build.yml             # GitHub Actions CI matrix build workflow
```

---

## Quick Start

### 1. Build via Unified Entrypoint

You can run builds using the root `./build.sh` (or `./scripts/build.sh`):

```bash
# View usage and available runtimes
./build.sh --help

# Build PHP default version (8.4) with GNU/glibc and Laravel profile
./build.sh php glibc laravel

# Build custom PHP versions (e.g. 8.3, 8.2, 8.1)
./build.sh php glibc laravel 8.3
./build.sh php musl minimal 8.2

# Flexible argument ordering: version first or via environment variable
./build.sh php 8.3 glibc laravel
PHP_VERSION=8.1 ./build.sh php musl full
```

---

## PHP Runtime

For complete architectural details, see [PHP Architecture Documentation](docs/runtimes/php.md).

### Prerequisites (Host Setup)

```bash
# Download StaticPHP CLI
./runtimes/php/scripts/download-spc.sh

# Run environment doctor and install dependencies
./runtimes/php/spc doctor --auto-fix
```

### Supported PHP Versions

- **8.4** (Default)
- **8.3**
- **8.2**
- **8.1**

### Targets

| Target | Libc Baseline | Linkage | Dynamic `.so` Loading | Use Case |
|---|---|---|:---:|---|
| **GNU/glibc** | glibc 2.17 | Hybrid static + dynamic | Yes | Standard Linux distros (Ubuntu, Debian, RHEL) |
| **musl** | musl | Fully static | No | Minimal containers, Alpine Linux |

- **glibc Target**: Statically compiles the core engine and builds optional extensions as shared `.so` libraries. Allows toggling extensions per SAPI without recompilation.
- **musl Target**: Compiles everything statically into self-contained `php` and `php-fpm` binaries. External dynamic `.so` loading is disabled.

### Extension Profiles

Profiles are located in `runtimes/php/config/profiles/`:

- `minimal.txt`: Lean profile for microservices and CLI tools.
- `laravel.txt`: Curated set of extensions required for modern Laravel applications.
- `full.txt`: Comprehensive extension suite for legacy or enterprise workloads.

### Dynamic Extension Management (glibc)

For GNU/glibc builds, extensions listed in `runtimes/php/config/shared-glibc.txt` are built as `.so` modules and staged into `dist/php/<version>/glibc/<profile>/lib/php/extensions/`.

Manage them using `runtimes/php/scripts/ext.sh`:

```bash
# List extensions and their active state per SAPI
./runtimes/php/scripts/ext.sh list dist/php/8.4/glibc/laravel

# Enable an extension for CLI, FPM, or both
./runtimes/php/scripts/ext.sh enable dist/php/8.4/glibc/laravel redis cli
./runtimes/php/scripts/ext.sh enable dist/php/8.4/glibc/laravel xdebug fpm
./runtimes/php/scripts/ext.sh enable dist/php/8.4/glibc/laravel opcache all

# Disable an extension
./runtimes/php/scripts/ext.sh disable dist/php/8.4/glibc/laravel redis all
```

> **Note:** Static extensions cannot be toggled dynamically. Restart or reload PHP-FPM after changing extensions.

### Artifacts Layout

Emitted under `dist/php/<version>/<target>/<profile>/`:

```text
dist/php/
└── 8.4/
    ├── glibc/laravel/
    │   ├── bin/php
    │   ├── bin/php-fpm
    │   ├── lib/php/extensions/
    │   ├── etc/php.ini
    │   ├── etc/php-fpm.conf
    │   ├── etc/cli/conf.d/
    │   ├── etc/fpm/conf.d/
    │   └── manifest.txt
    └── musl/laravel/
        ├── bin/php
        ├── bin/php-fpm
        ├── etc/php.ini
        ├── etc/php-fpm.conf
        └── manifest.txt
```

---

## Adding a New Runtime

DevStack Runtime Builder is structured to easily onboard additional runtimes:

1. Create directory `runtimes/<runtime-name>/`:
   ```bash
   mkdir -p runtimes/nodejs/config runtimes/nodejs/scripts
   ```
2. Implement `runtimes/<runtime-name>/scripts/build.sh`:
   - Accept runtime arguments (e.g., target, version).
   - Stage output files into `dist/<runtime-name>/...`.
   - Write a `manifest.txt` describing the runtime artifact.
3. Make the script executable:
   ```bash
   chmod +x runtimes/<runtime-name>/scripts/build.sh
   ```
4. The root `./build.sh` dispatcher will automatically detect and list the new runtime!

---

## GitHub Actions CI/CD

The workflow in `.github/workflows/build.yml` provides:
- Automated builds on push to `main` for changes within `runtimes/**` and `scripts/**`.
- Manual execution via `workflow_dispatch` allowing selection of:
  - **Runtime**: `php`
  - **PHP Version**: `8.4`, `8.3`, `8.2`, or `8.1`
  - **Target**: `glibc`, `musl`, or `both`
  - **Profile**: `minimal`, `laravel`, or `full`
- Artifact publishing with automated retention.

---

## Production Checklist

1. Pin upstream toolchain versions (e.g. StaticPHP releases) before tagging production releases.
2. Verify extension compatibility and ABI matches for target architectures and specific PHP versions.
3. Run test suites (`php -v`, `php -m`, `php-fpm -t`) and smoke tests on the oldest supported host distro.
4. Distribute legal notices and third-party licenses alongside runtime packages.
5. Generate checksums (SHA256) for built artifacts.

---

## References

- [StaticPHP CLI Documentation](https://static-php.dev/)
- [StaticPHP CLI GitHub Repository](https://github.com/crazywhalecc/static-php-cli)
