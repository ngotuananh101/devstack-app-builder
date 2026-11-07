# PostgreSQL Runtime — Design

Date: 2026-11-07
Status: draft for approval
Research notes: `docs/superpowers/specs/2026-11-07-postgresql-runtime-research.md`

## Intent

DevStack currently ships two runtimes: PHP (`musl-dynamic`) and Redis (fully static
musl). Consumers download versioned archives rather than compiling locally.
PostgreSQL is added as a **third runtime: a database server bundle**, so a DevStack
project can run a real PostgreSQL server with no host PostgreSQL install, no Docker,
and no host libc dependency.

The `pdo_pgsql` extension in the PHP bundle is a client driver and is unrelated to
this runtime; neither bundle depends on the other.

## Goals

- A self-contained PostgreSQL **server** bundle per major series, **17.x and 18.x
  built side by side**, pinned to exact patch versions with reviewed upstream
  SHA-256.
- Runs on a host with no PostgreSQL, no glibc PostgreSQL libraries, and no
  configuration: `initdb`, start, query, stop, and relocation all work through the
  bundle's own `bin/` entry points.
- **As many extensions as can be supported**, and the support is verifiable: every
  built module is actually loaded and executed by the verifier, and the manifest
  records which modules are present, which were skipped, and why.
- Same repository conventions as PHP/Redis: no profile selector, no target/libc
  selector, no package installation on the build host, manifest +
  checksum-verified archives, and CI that plans only missing/older bundles.
- Same planner/publisher integration: `runtimes/postgresql/`, a `postgres_matrix`
  row family in the daily plan, release notes, and publish-time validation.

## Non-goals

- Not a client-only distribution, not a `libpq` packaging effort.
- Not PostgreSQL 16 or older. 16 has no `builtin` locale provider and is closer to
  end of life; supporting it would fork the locale contract for no benefit.
- Not PostGIS, pgvector, Citus, TimescaleDB, or other third-party extensions in the
  first release. They need their own source pinning, license review, and version
  matrices. The extension mechanism is left open so they can be added later.
- Not a service manager. The bundle does not install systemd units, create users,
  or run as root. DevStack starts it as the application user.
- Not a replica/cluster manager. Streaming replication and `pg_basebackup` ship as
  binaries; orchestrating HA is out of scope.
- Not a monolithic build: no icu/llvm/perl/python/tcl dependency unless it is
  strictly required by an included module (see Extension policy).

## Decisions taken (and why)

### Relocatable, no rpath, bundled loader — `linkage: dynamic`

The bundle is musl-linked and carries its own loader, exactly like PHP:
`lib/musl/ld-musl-x86_64.so.1`, with every public command a launcher that execs the
loader with `--library-path "$ROOT/lib:$ROOT/lib/musl"` and the real binary under
`libexec/`. `--disable-rpath` is used at configure time so no executable or module
holds an absolute library path — this matches the repository's existing rule that
no bundle may ship absolute `RUNPATH`.

**Both directories are required on `--library-path`** (spike-verified, see the
research note). Every musl-linked ELF carries `NEEDED: ld-musl-x86_64.so.1`, but the
loader registers itself as `ldso.name = "libc.so"`, so that name matches neither the
reserved libc names nor the loader's own name. It is therefore resolved as an
ordinary file through the search path. With only `$ROOT/lib/musl` on the path the
command works on a host that has a distro musl (found in the `/lib` fallback) and
fails on one that does not. Omitting `$ROOT/lib` is the second half of the same bug,
since `libpq.so.5` lives there. Keeping the loader in `lib/` next to the libraries
also works and is the simpler invariant, but the two-entry path is what the spike
proved and what the launcher template uses.

Why not fully static like Redis: PostgreSQL loads extension modules with `dlopen()`
at runtime, and static musl links `dlopen` to a stub that always fails. A fully
static server could not load `plpgsql` (it is a `.so`) or any contrib module, which
contradicts the "as many extensions as possible" goal. Dynamic musl still needs to
be proven: the loader's `dlopen` support was read from source, not demonstrated for
PostgreSQL, and this is the first thing the spike must falsify.

### `argv[0]` fidelity is a hard requirement of the launcher

PostgreSQL derives installation paths from the running executable's resolved path
(`find_my_exec` → `realpath(3)`), and `initdb`/`pg_ctl` **require `postgres` as a
same-directory sibling**, then execute it as a child (`"/path/postgres" -V`,
`system()`). The postmaster then `opendir()`s the sibling `lib` directory and
refuses to start if it is missing.

The obvious layout — raw ELFs together in `libexec/bin/`, `argv[0]` set to the real
binary — is **wrong**, for two independent reasons:

1. `initdb` executes the sibling *directly* (`popen`/`system`). A raw musl ELF
   carries `PT_INTERP: /lib/ld-musl-x86_64.so.1`; on a host without musl installed
   the kernel fails with `ENOENT`. It would appear to work on the build host and in
   CI (both have `musl-tools`) and break for every end user.
2. `make_relative_path()` compares the tail of the compiled-in `bindir` against the
   directory of `my_exec_path`. With the executable in `libexec/bin/`, it resolves
   `share/postgresql` to `$ROOT/libexec/share/postgresql`, not `$ROOT/share`.

The correct mechanism uses the loader's `--argv0` option, which sets `argv[0]`
after loading (`ldso/dynlink.c`: `if (replace_argv0) argv[0] = replace_argv0;`).

- Real ELFs live in a private directory, `libexec/bin/`, and are **never executed by
  the kernel**.
- `bin/<command>` is a generated launcher for every installed command, and is the
  canonical install path PostgreSQL sees. Each launcher resolves `$ROOT`, unsets
  `LD_PRELOAD`/`LD_LIBRARY_PATH`/`LD_AUDIT`, and execs:
  `"$ROOT/lib/musl/ld-musl-x86_64.so.1" --library-path "$ROOT/lib:$ROOT/lib/musl" --argv0 "$ROOT/bin/<command>" "$ROOT/libexec/bin/<command>" "$@"`
- Because `argv[0]` is the launcher path, `find_my_exec()` resolves to
  `$ROOT/bin/<command>` (a regular, readable, executable file, which passes
  `validate_exec`), so `make_relative_path()` maps the compiled-in prefix onto
  `$ROOT`: `share` → `$ROOT/share`, `lib` → `$ROOT/lib`, `etc` → `$ROOT/etc`.
  The spike confirmed this directly: `pg_config --bindir/--sharedir/--libdir/
  --pkglibdir` all reported paths under the relocated bundle.
- Sibling discovery finds `$ROOT/bin/postgres`, which is also a launcher — so the
  child spawned by `initdb`/`pg_ctl` goes through the bundled loader too, even on a
  host with no musl.
- `--argv0` is set from the generated launcher's own known path, not from `$0`, so
  it is correct whether the user invokes `bin/psql` by path or via `PATH`.
- The compiled-in prefix is therefore irrelevant to relocation and can stay the
  default (`/usr/local/pgsql`); only the requirement that `bindir` ends in `/bin`
  and the data/lib directories are its siblings matters.

`initdb` and `pg_ctl` must be launchers too, and the verifier must prove
`pg_ctl start` works, because that is the only test that exercises the whole chain.
This is the highest-risk area and gets the earliest spike.

### Locale contract: `builtin` provider, `C.UTF-8` default, ICU optional

musl 1.2.5 implements `strcoll()` as `strcmp()` and `strxfrm()` as a copy; it has no
locale collation tables. So the `libc` locale provider on a musl bundle cannot
reproduce glibc ordering, and shipping `libc`-provider clusters would silently
change sort order versus a Debian-based PostgreSQL.

Chosen contract:
- The supported, tested locale provider is **`builtin`** with **`C.UTF-8`** (18.x
  also offers `PG_UNICODE_FAST`). Ordering is deterministic code-point ordering,
  identical on every host, and does not depend on any host locale or on ICU's
  version. Documentation states this plainly so no one expects glibc ordering.
- Because `builtin` + `C.UTF-8` requires database encoding UTF-8, `initdb` in the
  verification path always passes `--encoding=UTF8 --locale-provider=builtin
  --builtin-locale=C.UTF-8`.
- **ICU is enabled by default**, and reaching it requires a C++ musl toolchain. This
  host has **no `musl-g++`, no `x86_64-linux-musl-g++`, and no musl `libstdc++`**
  (`/usr/x86_64-linux-musl/lib64/` holds only the C runtime); distro `musl-tools`
  ships `musl-gcc` only. So the build must obtain C++ another way:
  - **Chosen: Zig as the C/C++ cross-compiler** (`zig cc` / `zig c++` targeting
    `x86_64-linux-musl`), pinned to an exact Zig version with a reviewed SHA-256 in
    `config/sources.json`. Zig is already the musl toolchain in this repo's PHP
    pipeline, so it is a known, in-house dependency rather than something new.
  - If Zig cannot build ICU cleanly for the bundle, the documented fallback is a
    **C-only bundle with `--without-icu`**, which still supports every extension
    listed below (all of them are C) and keeps the `builtin`/`C.UTF-8` locale
    contract. That fallback is a release-note statement, not a user-selectable
    profile.
- **Every third-party dependency is vendored and pinned**, not taken from the build
  host: OpenSSL, zlib, libxml2, ICU and Zig itself each get an exact version, an
  HTTPS source URL and a reviewed SHA-256 in `config/sources.json`, are unpacked
  into fresh directories, and are built into a staging prefix. Nothing is linked
  from `/usr/lib`. This is the same discipline Redis applies to its source and PHP
  applies to its loader, and it is what makes the bundle's dependency set auditable.
  libuuid for `uuid-ossp` is built here too rather than borrowed from the host.
- `pg_upgrade` and dump/restore across bundles are expected to keep working because
  sorting is provider-defined and recorded per cluster; the verifier keeps a
  cross-major check out of scope but records the locale of the test cluster.

Consequence to accept: a cluster initialized with `builtin`/`C.UTF-8` does **not**
sort Vietnamese text by accent the way a glibc `vi_VN` cluster would. Users needing
that must create ICU collations (bundled) or use `PG_UNICODE_FAST`/ICU explicitly.
This is a documented property of the bundle, not a bug.

### Version policy: 17.x and 18.x in parallel, exact patch pins

`config/sources.json` pins exact patch versions for each supported series with
reviewed SHA-256 (starting with the current 18.6 and 17.11 releases) plus the
`postgresql-<version>.tar.bz2` URL. Updating a patch requires adding and reviewing
its upstream checksum first, mirroring the Redis module. The daily planner compares
only pinned exact versions — it does **not** discover upstream patches
automatically, so an unpinned new minor is never built. Dropping a series (e.g. when
14.x reaches EOL next week) is a config change plus a release-note statement.

Backwards-compatibility of data across majors is handled by `pg_dump`/`pg_upgrade`
in the bundle; a cluster directory is not portable between majors and the docs say
so.

### Extension policy: build all, verify each by kind, record skips

"Extensions" is not one category. The manifest and the verifier must distinguish
four kinds, because a single "run `CREATE EXTENSION` for each" loop would be wrong:

| Kind | Examples | Verified by |
|---|---|---|
| SQL extension (`share/extension/*.control` + `lib/*.so`) | `citext`, `hstore`, `pg_trgm`, `pgcrypto`, `uuid-ossp`, `cube`, `ltree`, ... | `CREATE EXTENSION` + a real query |
| Cluster-wide extension directory that installs several extensions | `spi` → `autoinc`, `insert_username`, `moddatetime`, `refint` | one `CREATE EXTENSION` per installed `.control` in that directory |
| `shared_preload_libraries` module (loadable `.so`, no `.control`) | `auth_delay`, `auto_explain`, `basic_archive`, `basebackup_to_shell`, `passwordcheck`, `pg_overexplain` (18 only), `pg_stat_statements` (both kinds) | process starts with the library preloaded, and its SQL objects work if it has any |
| CLI tool shipped in `contrib/` (binary, no `.so`, no SQL) | `oid2name`, `vacuumlo` | runs and reports its version / a real operation |

The spike measured the 18.6 install: **79 `.so` files in `lib/`** (including
`libpq.so.5`) and **42 `.control` files in `share/extension/`**. `config/modules.json`
is generated from that enumeration, not hand-written, and the verifier compares
against it per `(major, module)`.

The manifest therefore records, per module: `name`, `kind`, and for the excluded
set `reason`. The plan does **not** fix one list for both majors: PostgreSQL 18 has
modules 17 does not (`pg_overexplain`), so `included` is computed per major from
what `make install` actually produced and is compared against the per-major
expected set in `config/modules.json` (which also holds the exclusions and their
reasons). A publisher that rejects a 17.x bundle for lacking an 18-only module
would be a bug; the check is per `(major, module)`.

**included (plain C, no extra dependency):**
`amcheck, auth_delay, auto_explain, basebackup_to_shell, basic_archive, bloom,
btree_gin, btree_gist, citext, cube, dblink, dict_int, dict_xsyn, earthdistance,
file_fdw, fuzzystrmatch, hstore, intagg, intarray, isn, lo, ltree, oid2name,
pageinspect, passwordcheck, pg_buffercache, pg_freespacemap, pg_logicalinspect,
pg_overexplain, pg_prewarm, pg_stat_statements, pg_surgery, pg_trgm, pgrowlocks,
pgstattuple, pg_visibility, pg_walinspect, postgres_fdw, seg, spi, sslinfo,
tablefunc, tcn, tsm_system_rows, tsm_system_time, unaccent, uuid-ossp, vacuumlo,
xml2, plpgsql`

**conditional on a bundled library:**
`pgcrypto` (OpenSSL/zlib), `uuid-ossp` (choose BSD syscall implementation or
libuuid; prefer the libuuid we build rather than relying on a host header),
`xml2` (libxml2), `ssl` server TLS (OpenSSL).

**excluded, with a recorded reason:**
- `plperl`, `bool_plperl`, `hstore_plperl`, `jsonb_plperl` — Perl embedding.
- `plpython3u`, `hstore_plpython`, `jsonb_plpython`, `ltree_plpython` — Python
  embedding.
- `pl/tcl` family — Tcl embedding.
- `sepgsql` — needs SELinux policy; meaningless in a portable bundle.

`make world` (or `make -C contrib install`) installs the whole set in one step; the
verifier then enumerates what is on disk and fails on anything present-but-broken or
absent-without-a-recorded-reason. That makes "as many extensions as possible"
measurable rather than aspirational.

### Bundle layout

```text
postgresql-<major>/                 # archive root: postgresql-<version>-musl-dynamic-x86_64-<build-id>/
  bin/<command>                     # launcher (sh), for every installed command
  libexec/
    launch.sh                       # shared launcher logic (PATH resolution, loader exec)
    bin/<real executables>          # siblings together: postgres, initdb, pg_ctl, psql, ...
  lib/musl/ld-musl-x86_64.so.1      # bundled loader (hashed in the manifest)
  lib/*.so                          # dlopen target ($libdir): plpgsql + contrib modules,
                                    #   plus libpq.so.5 and the bundled dependency .so files
  share/                            # postgres.bki, *.sql, timezone/, tsearch_data/,
                                    #   extension/*.control, and the shipped *.sample
                                    #   configs (postgresql.conf, pg_hba.conf, ...)
  include/server/                   # headers, so third-party extensions can build
                                    #   against this bundle's pg_config
  share/licenses/{builder,postgresql,<deps>}.txt
  share/sources/postgresql-<version>.tar.bz2
  manifest.json
```

This mirrors what an out-of-tree `install-world-bin` actually produces (verified by
the spike): modules and `libpq` sit together in `lib/`, and cluster data sits in
`share/`. There is no `lib/postgresql/` or `share/postgresql/` under this prefix.
`$libdir` resolves relative to the executable, so modules in `lib/` load without any
`shared_preload_libraries` path configuration.

`include/server/` is staged deliberately even though the server does not need it:
it is what makes `bin/pg_config` useful to a consumer who wants to compile an
extension against the bundle. The spike built and loaded a third-party `.so` that
way. It is the one component of the install tree that is kept for consumers rather
than for runtime, and should be trimmed from the archive only if size becomes a
problem (it is small compared to the rest).

## Constraints from this repository (non-negotiable)

- Local build scripts never install packages; missing tools fail with an explicit
  message (same rule as Redis/PHP).
- Existing bundles/archives are never overwritten; remove explicitly to rebuild.
- Builds happen in fresh temp dirs under `.cache/postgresql/build.*`; downloads and
  extraction in separate new directories; any Python reading them runs with `-I`.
  Digests are verified **before** extraction.
- Everything is x86_64 Linux only, like the other runtimes.
- No Docker.

## Builder-facing requirements

### `runtimes/postgresql/scripts/`

- `source.py` — validates one exact version against `config/sources.json`, emits a
  plan for `--print-plan`, downloads + verifies + extracts into a fresh directory.
  It also resolves and verifies the pinned dependency set (OpenSSL, zlib, libxml2,
  ICU, libuuid, Zig) so a build never touches host headers or libraries.
- `build.sh` — mirrors `runtimes/redis/scripts/build.sh`: arg validation, host/tool
  checks (`zig`, `make`, `perl`, `bison`, `flex`, `readelf`, `strip`, `curl`),
  refuses to replace an existing bundle, builds the pinned dependencies into a
  staging prefix, configures and builds PostgreSQL out of tree, stages, verifies,
  packages.
- `stage.py` — copies the install tree into the bundle layout, generates launchers
  from one template, stages libs/loader/licenses/source, writes `manifest.json`.
- `verify.py` — see below; must run against the staged bundle before packaging.
- `package.py` — deterministic tar.gz + `.sha256` + manifest sidecar, mode-preserving.
- `config/sources.json` — pinned versions + URLs + SHA-256.
- `config/build-options.json` — the configure flags, ICU on/off, and the excluded
  module list with reasons.
- `packaging/launch.sh` — the shared launcher body; `bin/*` are generated.

`build.sh` accepts `./build.sh postgresql [exact-version] [--print-plan]`, matching
Redis's shape.

### Verification (must be real, not `--version` only)

1. Every executable responds to `--version` and reports the expected patch version.
2. ELF contract: no GLIBC symbols, no `NEEDED` outside the bundle's own libs plus
   `libc.so`; no absolute `RUNPATH`/`RPATH`; `lib/musl/ld-musl-x86_64.so.1` present
   and executable.
3. `initdb -D <path with spaces> --encoding=UTF8 --locale-provider=builtin
   --builtin-locale=C.UTF-8` succeeds.
4. `postgres -D ...` starts; `psql -c 'SELECT 1'` and `CREATE EXTENSION plpgsql` +
   `DO $$ ... $$` succeed over the Unix socket.
5. **Every included module is exercised by its kind** (see Extension policy): SQL
   extensions get `CREATE EXTENSION` plus a real query; the `spi` directory gets one
   per installed `.control`; preload-only modules are checked by restarting the
   server with `shared_preload_libraries` set and confirming the process starts and
   the module's SQL objects (if any) respond; CLI tools run their version query.
   The inventory in the manifest is compared against what is actually on disk.
6. `pg_ctl -w stop` succeeds, then the whole tree is moved to another directory
   (with spaces) and steps 3–6 are repeated on the moved tree, proving relocation.
   This is also the **host-independence test**: run it in an environment where
   `/lib/ld-musl-x86_64.so.1` does not exist, so a bundle that accidentally relies
   on the build host's musl fails here rather than for the user.
7. Remaining binaries that have safe, local test modes (`pg_dump`, `pg_restore`,
   `pg_config`, `pg_isready`, `createdb`, `dropdb`, `vacuumdb`) are exercised
   against the test cluster.
8. ICU (if included): `CREATE COLLATION ... (provider = icu)` plus an ordering
   assertion.
9. The server is always stopped on success and on failure; no TCP port is exposed
   (Unix socket only), matching Redis's verification posture.

### Manifest

`schema_version: 1`, `runtime: "postgresql"`, `postgresql: {version, major}`,
`target: "musl-dynamic"`, `linkage: "dynamic"`, `architecture: "x86_64"`,
`build-id`, `verification: "PASSED"`, `loader` (path/digest/version),
`modules: {included: [{name, kind}], skipped: [{name, reason}]}` computed per major,
`libraries` (bundled deps + versions), `locale: {default_provider, default_locale}`,
`provenance` (upstream URL + SHA-256 + configure flags + compiler),
`binaries` (hashes + `NEEDED`), `licenses`.

### Pipeline integration

- `scripts/updates.py`: load `runtimes/postgresql/scripts/source.py`, add
  `postgres_matrix` built from pinned versions, publish `has_postgres_updates`, and
  include it in `has_updates`/`publish`.
- `.github/workflows/build.yml`: a `build-postgresql` job (Ubuntu 24.04) that
  installs `bison flex perl build-essential binutils curl` explicitly (the pinned
  Zig and every library archive are downloaded and digest-verified by the build
  script itself, not installed as host packages), runs the build for each matrix
  row, and uploads `dist/assets/`.
- `publish.py`: accept `runtime == "postgresql"`, validate identity, require the
  executables (`bin/postgres`, `bin/initdb`, `bin/pg_ctl`, `bin/psql`,
  `bin/pg_dump`, `bin/pg_config`), require the bundled loader, and enforce the
  extension inventory shape.
- `docs/runtimes/postgresql.md`, `README.md`, `docs/architecture.md`: contracts,
  usage, and the locale statement.
- Tests: new `tests/test_postgresql.py` (source pinning, arg validation, manifest
  schema, planner rows, publish validation, launcher generation) plus updates to
  `test_updates.py`, `test_publish.py`, `test_workflow.py`, `test_runtime_updates.py`.

## Sequencing

1. ~~**Spike (throwaway, isolated dir)**~~ — **done 2026-11-07.** Both
   design-invalidating questions were answered affirmatively: `dlopen` works in a
   musl-dynamic backend (including a third-party `.so`), and the `--argv0` launcher
   chain works end to end on a host with both musl loaders masked, from a path
   containing spaces. It also corrected the `--library-path` value, the install
   layout, and the locale evidence. Results are in
   `2026-11-07-postgresql-runtime-research.md`.
2. Written plan from this design (`superpowers:writing-plans`).
3. Implement `source.py` + build/stage/verify/package for 18.6 only.
4. Add 17.11 once 18.6 passes end to end.
5. Wire the planner, workflow, publisher, docs, tests.
6. Full local verification on both majors (including a no-host-musl run), then a
   real CI matrix run.

## Open items left to the builder

- Exact static-vs-shared split for bundled dependency libraries (OpenSSL, libxml2,
  ICU, zlib, lz4, zstd) — choose per dependency based on whether it must be
  `dlopen`ed by a module.
- Tar determinism details (sorted members, fixed mtime/uid/gid) — follow whatever
  `package.py` for PHP/Redis already does.
- The precise per-command list under `bin/` (every installed command, generated
  from the install tree, not a hand-written list).
- Whether to ship `include/server/` in the archive (see Bundle layout) or treat it
  as a separate deliverable.
- The ICU/Zig question: the spike ran with `--without-icu` because the host has no
  musl C++ toolchain. Zig as the C++ provider is a design decision that still has to
  be proven; the C-only fallback is fully functional.

## Resolved by the spike

- `dlopen` in a musl-dynamic backend: **works**, including a third-party extension
  built against the bundle's `pg_config`.
- `--argv0` launcher chain on a host with no musl loader: **works**, verified with
  the host loaders masked to zero-byte files, with spaces in the path.
- `--library-path` contents: **must include both `$ROOT/lib` and `$ROOT/lib/musl`**.
- Install layout: modules in `lib/`, data in `share/`; no `lib/postgresql/`.
- `--disable-rpath` builds are clean and require the launcher, as assumed.
- Kernel UAPI headers require `-idirafter /usr/include` on top of musl headers.
- `builtin`/`C.UTF-8` and (18.x) `PG_UNICODE_FAST`: **work**.
- `libc` provider on musl: silently accepts `vi_VN.UTF-8` and collates as C, so it
  is not offered as a "normal PostgreSQL" option.
- The `lib/ld-musl-x86_64.so.1` convenience symlink in the earlier layout is
  unnecessary and was dropped.
