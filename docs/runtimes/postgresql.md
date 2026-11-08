# PostgreSQL source builds

## Scope

Native Linux x86_64 PostgreSQL server bundles, **musl-dynamic only**, without profiles, Docker or libc selectors. The libc target is not selectable. This module only builds, verifies and packages server bundles; it does not configure DevStack, install a service, create a user or run a persistent server.

Supported series: **18** (default `18.6`) and **17** (`17.11`).

```bash
./build.sh postgresql                     # defaults: 18.6, musl-dynamic
./build.sh postgresql 18.6
./build.sh postgresql 17.11
./build.sh postgresql --print-plan
```

Arguments accept at most one pinned exact version. Unknown options, profiles, duplicate arguments and unpinned versions fail before downloading. `config/sources.json` records official PostgreSQL archive SHA-256 values for 18.6 and 17.11. Updating a version requires adding and reviewing its upstream checksum first.

## Build dependencies

- Host requirements: `make`, `bison`, `flex`, `perl`, `curl`, `readelf`, `strip`, `tar`, `bzip2` and Python 3 (run with `-I`).
- CI runner (`ubuntu-24.04`) installs only `bison flex perl build-essential binutils curl bubblewrap` — deliberately **no** `musl-tools`. Nothing in the build needs `/lib/ld-musl-x86_64.so.1` to exist: build-time programs are linked against the toolchain's own loader (below). `bubblewrap` is not a build dependency; it is how verification hides every host musl loader before starting a server, and publication accepts only a bundle verified that way.
- Dependencies fetched, verified and vendored (nothing is linked from `/usr/lib`, and no host package is ever installed):
  - **Zig** `0.17.1` (MIT): pinned in `config/sources.json` to provide `zig cc` and `zig c++` targeting `x86_64-linux-musl`, bundling musl `1.2.5`. Zig is a build tool and has no staged notice in `share/licenses/`.
  - **OpenSSL** `3.6.5` (Apache-2.0, shared): builds `libssl.so.3` and `libcrypto.so.3` staged into `lib/`.
  - **zlib** `1.4` (Zlib, static): `-fPIC` static archive.
  - **libxml2** `2.15.4` (MIT, static): `-fPIC` static archive.
  - **ICU** `79.1` (Unicode-3.0, static): `-fPIC` static archive.
  - **util-linux** `2.42.4` (LGPL-2.1-or-later, static `libuuid`): `-fPIC` static archive.

All pinned source archives are fetched over HTTPS (PostgreSQL source URL shape `https://ftp.postgresql.org/pub/source/v<version>/postgresql-<version>.tar.bz2`, with checksum sidecar `.sha256` at the same location). Every dependency archive is SHA-256 verified against `config/sources.json` before extraction into a fresh directory, and built into a fresh staging prefix. A download is attempted six times with cumulative backoff (15s, 45s, 1m45s, 3m45s, 6m45s), because GitHub's `releases/download` surface has answered `504` for every asset while its own status page reported all systems operational. A digest that does not match its pin is never retried — that is a changed archive, not a flaky connection. A dependency may only move to an alternative mirror that returns **exactly** the pinned SHA-256; ICU stays on GitHub because no mirror serves the same archive, and swapping in a different tarball of the same version means re-pinning it. `BUILD_JOBS` selects build parallelism (default 2).

### The toolchain names its own loader while the build runs

`configure` compiles a test program and executes it immediately, so the interpreter in
its `PT_INTERP` has to exist on the host — and a runner with no musl installed has no
`/lib/ld-musl-x86_64.so.1`. Every program `zig cc` links therefore carries
`-Wl,--dynamic-linker=<workspace>/loader/libc.so`, the loader `toolchain build-loader`
built a moment earlier, which is why `build.sh` builds the loader **before** the
dependency stack. `toolchain build-dependencies` proves the mechanism works with a
one-line probe program before it starts any dependency, so a broken link line names
itself in seconds instead of surfacing as `configure: error: cannot run C compiled
programs` inside libxml2.

The workspace path must not reach a user's archive, so `bundle.py` rewrites every
staged program's `PT_INTERP` back to the portable `/lib/ld-musl-x86_64.so.1` (before
`launchers.install` and before the manifest records the interpreter). The walk covers
the whole staged tree rather than `bin/` alone, because `install-world-bin` also puts
`pg_regress`, `pg_isolation_regress` and `isolationtester` under `lib/pgxs`, and those
run without a launcher. Shared objects carry no `PT_INTERP` and are skipped; a command
in `bin/` that names none is refused by name.
The rewrite is length-preserving — it reuses the existing `PT_INTERP` slot, NUL-padded
— and refuses a name that cannot fit rather than shipping a truncated interpreter path.
Nothing at run time reads the name back: the launchers exec the bundled loader with
`--library-path`, so the staged loader is the interpreter and never a program that asks
for one.

### Shared versus static linkage rationale

- **OpenSSL 3.6.5 is shared**: OpenSSL maintains process-global state, including its internal error queue, initialization flags and thread-local RNG state. The PostgreSQL postmaster process links OpenSSL for TLS support, while loadable extension modules such as `pgcrypto.so` and `sslinfo.so` also link OpenSSL. If OpenSSL were linked statically into both the core binaries and the extension modules, two independent static copies with separate global states would coexist in a single process address space, resulting in uncoordinated RNG seeding and initialization conflicts. OpenSSL is therefore linked as a shared library (`libssl.so`, `libcrypto.so`) in `lib/`.
- **zlib, libxml2, ICU and util-linux are static**: None of these libraries maintain process-global state across modules. Vendoring them as static archives avoids runtime DSO search overhead and isolates symbol resolution. All static archives must be compiled with `-fPIC` because their objects link into loadable PostgreSQL `.so` modules as well as executables.

### ICU collation support

Both shipped bundles record `{"decision": "with_icu", "reason": "zig c++ compiled ICU 79.1"}`. ICU is **enabled** by default. Compiling ICU for musl is made possible by `zig c++`, which compiles C++ against the bundled musl libc without requiring host `musl-g++` or host musl `libstdc++`.

The C-only `--without-icu` build exists strictly as a documented fallback option, recorded in the manifest and release notes only when taken — never as the current state. When that fallback is taken, ICU collations are unavailable and `builtin`/`C.UTF-8` becomes the sole collation provider.

### Configure flags

PostgreSQL is configured with the 20 flags recorded in `config/build-options.json`:
- Explicit features: `--disable-rpath`, `--with-ssl=openssl`, `--with-zlib`, `--with-uuid=e2fs`, `--with-libxml`, `--with-icu`.
- Explicitly disabled features: `--without-readline`, `--without-llvm`, `--without-perl`, `--without-python`, `--without-tcl`, `--without-lz4`, `--without-zstd`, `--without-pam`, `--without-systemd`, `--without-bonjour`, `--without-selinux`, `--without-gssapi`, `--without-ldap`, `--without-libcurl`.

Two flags require specific care:
- `--with-uuid=e2fs` links against the vendored static `libuuid` from util-linux. `--with-uuid=bsd` is never used because PostgreSQL's configure script requires `uuid_to_string` to exist in libc for the BSD implementation, and musl provides no `uuid_*` functions.
- `CPPFLAGS="-D_GNU_SOURCE -idirafter /usr/include"`: `-idirafter` is mandatory. It ensures that host `/usr/include` headers are consulted only after the bundled musl toolchain headers, preventing host glibc headers from shadowing musl headers.

## Output contract

```text
dist/postgresql/<version>/musl-dynamic/
  bin/                              # 37 shell launchers
  libexec/
    bin/                            # 37 raw musl ELFs (same names as bin/)
    launch.sh                       # Environment sanitizer (unsets LD_PRELOAD, LD_LIBRARY_PATH, LD_AUDIT)
  lib/
    musl/ld-musl-x86_64.so.1        # Bundled musl loader and libc (1.2.5)
    *.so                            # 85 extension DSOs, plus libpq.so.5(.18), libecpg.so.6(.18),
                                    # libecpg_compat.so.3(.18), libpgtypes.so.3(.18), libssl.so.3,
                                    # libcrypto.so.3 and unversioned aliases
    *.a                             # Static archives (libpq.a, libecpg.a, libpgtypes.a,
                                    # libpgcommon*.a, libpgport*.a), pgxs/, pkgconfig/
  include/                          # C header files
  share/
    postgres.bki                    # Bootstrap catalogue
    postgresql.conf.sample          # Sample server configuration
    pg_hba.conf.sample              # Sample client authentication configuration
    pg_ident.conf.sample            # Sample user map configuration
    pg_service.conf.sample          # Sample connection service configuration
    psqlrc.sample                   # Sample psql startup configuration
    timezone/                       # Timezone database
    timezonesets/                   # Timezone set definitions
    tsearch_data/                   # Full-text search dictionaries
    extension/                      # 216 extension SQL and control files
    sql_features.txt
    system_*.sql
    errcodes.txt
    snowball_create.sql
    information_schema.sql
    licenses/                       # Licences for builder, PostgreSQL, musl, and vendored libraries
    sources/postgresql-<version>.tar.bz2
  manifest.json                     # Machine-readable bundle manifest
```

**Configuration directory notice:** There is **no `etc/` directory** in the PostgreSQL bundle. PostgreSQL sample configuration files (`postgresql.conf.sample`, `pg_hba.conf.sample`, `pg_ident.conf.sample`, `pg_service.conf.sample` and `psqlrc.sample`) live in `share/`.

### Launcher contract

Direct execution of raw ELFs in `libexec/bin/` fails because they require a host musl dynamic linker and report incorrect installation paths. All commands MUST be invoked through the `bin/` launchers:

```bash
"$ROOT/lib/musl/ld-musl-x86_64.so.1" --library-path "$ROOT/lib:$ROOT/lib/musl" \
  --argv0 "$ROOT/bin/<command>" "$ROOT/libexec/bin/<command>" "$@"
```

- **`$ROOT/lib` comes first in `--library-path`** and is mandatory: `libpq.so.5` is a shared library of the installation tree itself. Frontend client executables (such as `clusterdb`, `createdb`, `psql`, `pg_dump`) resolve their `PQ*` symbols from `$ROOT/lib`. Omitting `$ROOT/lib` causes the loader to fail immediately with `Error loading shared library libpq.so.5`.
- **`$ROOT/lib/musl` is required second**: it provides the bundled musl libc (`libc.so`). Omitting it might accidentally pass on development hosts that happen to have a system musl loader installed, but fails on standard glibc-only hosts.
- **`--argv0 "$ROOT/bin/<command>"` is mandatory**: PostgreSQL determines its installation prefix, its `share/` data directory and its `pkglib/` extension directory from `argv[0]` via `find_my_exec()` and `make_relative_path()`. In addition, `initdb` and `pg_ctl` discover sibling binaries by looking in the directory of `argv[0]`; `initdb` explicitly runs `"<path>/postgres" -V` during cluster initialization. If a raw ELF in `libexec/bin/` were executed directly, `argv[0]` would point to `libexec/bin/`, causing PostgreSQL to search for `libexec/share/` and fail.
- **Relocation safety**: Each launcher computes `ROOT` using `$(CDPATH='' cd -P -- "$(dirname -- "$0")/.." && pwd)`, sources `libexec/launch.sh` (which unsets `LD_PRELOAD`, `LD_LIBRARY_PATH` and `LD_AUDIT`), and quotes every variable expansion. Directories containing spaces or `#` characters are fully supported.

### Module taxonomy and exclusions

`config/modules.json` is generated from verify-time inventories of the staged bundles. It catalogues modules across four kinds:
- `sql` (45 in 17.11, 46 in 18.6): extensions installed via `CREATE EXTENSION` (e.g. `pgcrypto`, `pg_stat_statements`, `uuid-ossp`, `hstore`). PostgreSQL 18 adds `pg_logicalinspect`.
- `preload` (5 in 17.11, 6 in 18.6): libraries intended for `shared_preload_libraries` (e.g. `auth_delay`, `auto_explain`, `passwordcheck`). PostgreSQL 18 adds `pg_overexplain`.
- `shared` (36 in both 17.11 and 18.6): internal loadable DSOs, encoding conversion modules and client libraries (`libpq`, `libssl`, `libcrypto`, `utf8_and_*`, `euc_*`).
- `cli` (2 in both 17.11 and 18.6): standalone client utilities (`oid2name`, `vacuumlo`).

The `exclusions` registry documents 10 modules deliberately omitted due to `--without-*` configure options:
- Perl embedding (`plperl`, `bool_plperl`, `hstore_plperl`, `jsonb_plperl`): Perl embedding is not part of this bundle (`--without-perl`).
- Python embedding (`plpython3u`, `hstore_plpython`, `jsonb_plpython`, `ltree_plpython`): Python embedding is not part of this bundle (`--without-python`).
- Tcl embedding (`pltcl`): Tcl embedding is not part of this bundle (`--without-tcl`).
- SELinux (`sepgsql`): Requires host SELinux policy; meaningless in a portable bundle (`--without-selinux`).

A `major_exclusions` mapping documents major-specific differences: `17` excludes `pg_logicalinspect` (added upstream in PostgreSQL 18; PostgreSQL 17 has no such module).

`stage()` refuses any build that lacks an expected module. The bundle `manifest.json` records all included modules under `modules.included` and all skipped modules under `modules.skipped` with their documented reasons.

### Locale contract and collation behaviour

Both manifests record `default_provider: "builtin"` with `default_locale: "C.UTF-8"`. PostgreSQL 18.6 also records `alternate: "PG_UNICODE_FAST"`, whereas PostgreSQL 17.11 records `alternate: null`.

musl 1.2.5 implements `strcoll()` as a direct call to `strcmp()`, and `strxfrm()` as a plain `strncpy()`. As a result, the `libc` locale provider on musl cannot reproduce glibc language-sensitive collation: it accepts locale names such as `vi_VN.UTF-8` or `en_US.UTF-8` but silently sorts strings in raw byte/code-point order (identical to C locale). Because offering `libc` would be misleading, it is not offered.

Collation ordering in this runtime is deterministic Unicode code-point order, identical across all host systems. It does **not** match a glibc `vi_VN` or `en_US` cluster. Users who require language-sensitive, accent-aware sorting must create ICU collations using the bundled ICU 79.1 library or use `PG_UNICODE_FAST` on PostgreSQL 18.

**Critical naming detail:** The builtin `C.UTF-8` collation is catalogued internally as `pg_c_utf8`. The SQL expression `COLLATE "C.UTF-8"` does **not** exist in a fresh database (`SELECT 'a' COLLATE "C.UTF-8"` fails with `ERROR: collation "C.UTF-8" for encoding "UTF8" does not exist`). Locales are specified by provider and locale name at `initdb` time:

```bash
"$RUNTIME/bin/initdb" -D "$PGDATA" --encoding=UTF8 --locale-provider=builtin --builtin-locale=C.UTF-8
```

Because `builtin` with `C.UTF-8` requires UTF-8 encoding, `initdb` must always be passed `--encoding=UTF8`. On PostgreSQL 18, `--builtin-locale=PG_UNICODE_FAST` can be passed instead for fast Unicode collation.

### Verification

The `verify` subcommand is the sole writer of the `verification` field in `manifest.json`. Verification performs:
1. Re-hashes every file recorded in the manifest against its recorded SHA-256.
2. Runs a complete cluster lifecycle (`initdb` -> postmaster startup -> client connection via `psql` -> clean shutdown) entirely through the `bin/` launchers.
3. Relocation test: repeats the complete cluster lifecycle from a copy relocated to a temporary path containing spaces and a `#` character.
4. Host loader masking: isolates the test environment to prove that the bundle relies on no host musl files. The primary isolation strategy uses `bwrap` (Bubblewrap) with `--dev-bind / /` and zero-byte mounts over host loader locations (`/lib`, `/lib64`, `/usr/lib`, `/usr/lib64` loader names and musl-dev trees), recorded as `verification_strategy: "bwrap"`. If user namespaces cannot be created, it falls back to a poison-file probe. If neither isolation method is available, verification fails.
5. Per-kind module exercises:
   - `sql`: executes `CREATE EXTENSION <name>;`.
   - `shared`: executes `CREATE EXTENSION <name>;` followed by a SQL query that invokes internal C functions to ensure DSO symbols resolved.
   - `preload`: starts a dedicated postmaster with `shared_preload_libraries` set to the exact list of preloaded modules.
   - `cli`: executes `<tool> --version`.
   - `icu`: verifies `icu:linked` through an ICU-dependent collation probe.

Real check counts recorded in `manifest.json`: **124 checks** for 17.11 and **130 checks** for 18.6.

**Only a `bwrap` verification is publishable.** `publish.py` rejects a PostgreSQL bundle whose `verification_strategy` is `"poison-file"`, because that probe proves strictly less: it shows the bundle's commands resolve every vendored library through the launcher's `--library-path`, but it never enters a sandbox where the host's own loader is unusable. The fallback is a build-host convenience, not publication evidence.

**A GitHub-hosted `ubuntu-24.04` runner does not produce a publishable PostgreSQL bundle.** It installs bubblewrap, but `bwrap` there does not create the user namespace the mask needs, so `verify` takes the poison fallback and prints the reason it read from `bwrap`. The CI build therefore proves the bundle builds, stages and runs a real cluster; publication has to run on a host where `bwrap` works (a self-hosted runner or a workstation), where the same `./build.sh postgresql <version>` records `verification_strategy: "bwrap"`.

### Assets and the never-overwrite rule

Verified bundles generate three immutable release assets:

```text
dist/assets/postgresql-<version>-musl-dynamic-x86_64-<build-id>.tar.gz
            postgresql-<version>-musl-dynamic-x86_64-<build-id>.tar.gz.sha256
            postgresql-<version>-musl-dynamic-x86_64-<build-id>.manifest.json
```

Archives are created using exclusive file creation (`xb`/`x`). Both staging and packaging strictly enforce the never-overwrite rule: if `dist/postgresql/<version>/musl-dynamic/` or any destination asset file already exists, the build terminates with an error. Existing outputs must be explicitly removed before rebuilding.

## Licensing and automation

PostgreSQL server and core utilities are distributed under the PostgreSQL License. Vendored dependency licences are staged in `share/licenses/`:
- `builder.txt`: DevStack Runtime Builder licence.
- `postgresql.txt`: PostgreSQL Licence.
- `musl.txt`: musl MIT licence.
- `openssl.txt`: Apache-2.0.
- `zlib.txt`: Zlib licence.
- `libxml2.txt`: MIT licence.
- `util-linux.txt`: LGPL-2.1-or-later.
- `icu.txt`: Unicode-3.0 licence.

(Zig 0.17.1, MIT, is a build-time compiler dependency pinned in `config/sources.json` and has no staged runtime notice.)

The shared CI workflow (`.github/workflows/build.yml`) runs on a daily schedule and supports manual dispatch. It plans PostgreSQL 18.6 and 17.11 alongside PHP 8.2–8.5 and Redis 7.2, 7.4, 8.10. Planning compares against public releases: existing `PASSED` PostgreSQL releases with matching archive and checksum assets are considered current. The CI runner builds only missing combinations unless `bypass=true` is supplied.

Verification commands:

```bash
python3 -I -m unittest discover -s tests -v
bash -n build.sh scripts/build.sh runtimes/postgresql/scripts/*.sh
for script in runtimes/postgresql/packaging/*; do sh -n "$script"; done
./build.sh postgresql --print-plan
```
