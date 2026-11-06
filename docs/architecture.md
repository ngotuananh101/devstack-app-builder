# DevStack Runtime Builder Architecture

## Overview

**DevStack Runtime Builder** is an extensible build framework designed to generate isolated, portable language runtimes with recorded input provenance. Reproducibility and runtime compatibility require validation of each build's actual toolchain and sources.

Instead of bundling a single runtime, this repository is architected modularly to support multiple runtimes (PHP, Node.js, Python, etc.) under an isolated directory structure while offering a unified command-line interface.

## Repository Layout

```text
devstack-runtime-builder/
├── build.sh                  # Root build convenience wrapper
├── scripts/
│   └── build.sh              # Unified build dispatcher
├── runtimes/                 # Modular runtime definitions
│   ├── php/                  # PHP runtime builder
│   │   ├── config/           # Profiles and runtime configuration
│   │   ├── scripts/          # Build and management scripts
│   │   └── README.md
│   └── <runtime>/            # Future runtimes (e.g. nodejs, python)
├── dist/                     # Build output artifacts (gitignored)
│   ├── php/
│   │   └── <version>/
│   │       ├── glibc/<profile>/
│   │       └── musl/<profile>/
│   └── <runtime>/
├── docs/                     # Documentation and architecture guides
│   ├── architecture.md
│   └── runtimes/
│       └── php.md
└── .github/
    └── workflows/
        └── build.yml         # Matrix build CI workflow
```

## Modular Runtime Pattern

Every runtime directory under `runtimes/<name>` adheres to the following contract:

1. **`scripts/build.sh`**:
   - An executable script accepting target/profile arguments specific to that runtime.
   - Outputs compiled artifacts into `$REPO_ROOT/dist/<runtime>/...`.
   - Generates a machine-readable `manifest.json` with actual runtime version, target libc, architecture, resolved module modes and build provenance.
   - Verifies runtime behavior before generating mode-preserving tar.gz/checksum assets.
2. **`config/`**:
   - Holds configuration templates, profile definitions, and runtime INI/config files.
3. **Artifact Isolation**:
   - Build work/tool downloads use gitignored fresh directories. Installed binaries and libraries are staged under `dist/<runtime>/`, with archive assets under `dist/assets/`.

## Supported Runtimes

- [Redis](runtimes/redis.md): Native Linux x86_64 source builds, fully static musl or native glibc with an actual ELF-derived libc floor; no profiles, Docker or service configuration.
- [PHP (8.5, 8.4, 8.3, 8.2)](runtimes/php.md): Dual-target distribution (GNU/glibc hybrid with dynamic `.so` extension loading, and musl fully static binary) with independent CLI and FPM SAPI support. Custom PHP versions can be specified at build time.

## Adding New Runtimes

To introduce a new language runtime into DevStack Runtime Builder:
1. Create directory `runtimes/<runtime-name>/`.
2. Add `scripts/build.sh` (marked executable with `chmod +x`).
3. Add any necessary configuration under `runtimes/<runtime-name>/config/`.
4. Ensure outputs are staged into `dist/<runtime-name>/`.
5. Update `.github/workflows/build.yml` matrix options if CI automation is desired.
