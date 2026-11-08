# PostgreSQL Runtime for DevStack

Build configuration and packaging for Linux x86_64 PostgreSQL server bundles: **18.6 (default)** and **17.11**, one target: `musl-dynamic`.

See [PostgreSQL runtime architecture and consumer contract](../../docs/runtimes/postgresql.md) for build dependencies, launcher contracts, module taxonomy, locale behaviour, manifest fields, tests and public release downloads.

```bash
./build.sh postgresql                     # PostgreSQL 18.6, musl-dynamic
./build.sh postgresql 18.6
./build.sh postgresql 17.11
./build.sh postgresql --print-plan        # inspect inputs without building
```

There are no `minimal`/`full` profiles and no libc selectors. Every build produces a relocatable server bundle with 37 shell launchers in `bin/`, raw ELFs in `libexec/bin/`, the bundled musl loader in `lib/musl/ld-musl-x86_64.so.1`, 85 loadable extension modules in `lib/`, sample configurations in `share/` (there is no `etc/` directory), and `manifest.json`.

**Warning: Always execute through `bin/` launchers.** A raw `libexec/bin/` ELF executed directly fails because it requires a host musl dynamic linker and computes incorrect installation paths from `argv[0]`.

## Module scripts

- **`scripts/build.sh`**: Main build orchestrator. Validates the native Linux x86_64 host and `BUILD_JOBS`, creates an isolated scratch directory under `.cache/postgresql/`, calls `toolchain.py` to prepare Zig and dependencies, authenticates sources via `source.py`, configures and compiles PostgreSQL (`make world-bin`, `make install-world-bin`), stages the bundle, runs verification, and packages assets.
- **`scripts/source.py`**: Validates requested versions against `config/sources.json`, verifies upstream archive SHA-256 digests before extraction, and provides plan metadata for `--print-plan`.
- **`scripts/toolchain.py`**: Prepares the pinned Zig 0.17.1 toolchain, builds vendored dependencies (OpenSSL 3.6.5 shared, zlib 1.4 static, libxml2 2.15.4 static, ICU 79.1 static, util-linux 2.42.4 static `libuuid`), builds the musl 1.2.5 loader, prepares configure flags and build environment variables, and records toolchain provenance.
- **`scripts/launchers.py`**: Relocates raw musl ELFs from `bin/` to `libexec/bin/` and renders relocatable shell launchers into `bin/` using `packaging/launcher.in`.
- **`scripts/modules.py`**: Classifies installed modules into kinds (`sql`, `preload`, `shared`, `cli`), tracks exclusions (`exclusions` and `major_exclusions` in `config/modules.json`), and inventories installed extensions.
- **`scripts/bundle.py`**: Stages the bundle layout (`stage`), runs verification (`verify`: cluster lifecycle, Bubblewrap host loader isolation, relocation with spaces and `#`, per-kind module checks), and produces mode-preserving release archives (`package`).

## Packaging templates

- **`packaging/launcher.in`**: POSIX shell template for `bin/` launchers. Resolves `ROOT` from its own path, sources `libexec/launch.sh`, and executes the command via `$ROOT/lib/musl/ld-musl-x86_64.so.1` with `--library-path "$ROOT/lib:$ROOT/lib/musl"` and `--argv0`.
- **`packaging/launch.sh`**: Sourced by launchers to unset inherited `LD_PRELOAD`, `LD_LIBRARY_PATH` and `LD_AUDIT`.

Outputs: `dist/postgresql/<version>/musl-dynamic/` and `dist/assets/postgresql-<version>-musl-dynamic-x86_64-<build-id>.tar.gz` (plus `.sha256` and `.manifest.json`). Existing staged bundles and assets are never overwritten.
