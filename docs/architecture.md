# DevStack Runtime Builder Architecture

## Overview

**DevStack Runtime Builder** is an extensible build framework designed to generate isolated, portable, and reproducible language runtimes for DevStack environments.

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
   - Generates a `manifest.txt` describing the build metadata (version, target libc, architecture, options).
2. **`config/`**:
   - Holds configuration templates, profile definitions, and runtime INI/config files.
3. **Artifact Isolation**:
   - Builds do not pollute repository source trees. All generated binaries and libraries are isolated under `dist/<runtime>/`.

## Supported Runtimes

- [PHP (8.1, 8.2, 8.3, 8.4)](runtimes/php.md): Dual-target distribution (GNU/glibc hybrid with dynamic `.so` extension loading, and musl fully static binary) with independent CLI and FPM SAPI support. Custom PHP versions can be specified at build time.

## Adding New Runtimes

To introduce a new language runtime into DevStack Runtime Builder:
1. Create directory `runtimes/<runtime-name>/`.
2. Add `scripts/build.sh` (marked executable with `chmod +x`).
3. Add any necessary configuration under `runtimes/<runtime-name>/config/`.
4. Ensure outputs are staged into `dist/<runtime-name>/`.
5. Update `.github/workflows/build.yml` matrix options if CI automation is desired.
