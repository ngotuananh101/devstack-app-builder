# PostgreSQL Runtime Feasibility — Research Notes

Status: research only. No PostgreSQL module exists in this repository yet, and no
build or packaging behaviour has been changed. This note records what was verified
on 2026-11-07 and the questions a spike must answer before any design is written.

## Intent

The repository builds portable PHP (`musl-dynamic`) and Redis (fully static musl)
bundles for DevStack consumers who download versioned archives. The open question
is whether PostgreSQL should join them, and on which contract.

The prior working assumption (recorded in session memory) was
**musl-dynamic, keeping PL/pgSQL and loadable `.so` modules**, rather than requiring
the fully static musl contract that Redis enforces. Nothing in the current tree
implements or contradicts that assumption; it is still unvalidated.

## Verified today

### Upstream identity and provenance

- PostgreSQL 18.6 is the current 18.x minor and is supported; 17.x, 16.x, 15.x and
  14.x are also supported. 14.x is the next branch to reach end of life
  (final release 2026-11-12). Source: postgresql.org/support/versioning.
- Official source lives at `https://ftp.postgresql.org/pub/source/v18.6/` with
  `postgresql-18.6.tar.gz` / `.tar.bz2` and per-file `.sha256` companions.
  The 18.6 bzip2 archive checksum published today is
  `555610c24d53e4316da5b7d3fc25c279d96856d5e0e23ee308c328c5fa881d9f`.
  Quoted here for orientation only: pin the value from the signed source at
  design time, not from this note.
- Licence: the PostgreSQL License (BSD/MIT-like). Retain the copyright notice and
  licence text in any distributed bundle, as the licence requires.

### The local source tree is not a clean upstream tree

`runtimes/php/.cache/.../static-php-cli/.../src/Package/Library/postgresql.php`
builds PostgreSQL only to obtain a **static `libpq`** for PHP's `pdo_pgsql`. Its
`patchBeforeBuild()` rewrites `src/Makefile.shlib` to delete the shared-library
link and install rules, and it installs only `pg_config`, headers, `libpgcommon`,
`libpgport` and `libpq`.

`.cache/repro/work/source/postgresql/` is that already-patched tree (18.6). A
server build must start from a freshly verified upstream archive; reusing this
tree would silently change the build. Treat the cached tree as evidence about
toolchain behaviour, not as an input.

### Relocation is decided by `argv[0]`, not by the dynamic loader

PostgreSQL derives everything from the running executable's own path:

- `find_my_exec()` validates `argv[0]`, resolves it with `realpath(3)`, and only
  falls back to `PATH` when `argv[0]` has no directory separator.
- `get_share_path()`, `get_pkglib_path()`, `get_etc_path()` and friends rewrite the
  compiled-in prefixes relative to that path (`make_relative_path`), which is what
  makes an install tree relocatable.
- `initdb` locates `postgres` with `find_other_exec(argv0, "postgres", ...)`,
  requires it in the **same directory** as `initdb`, and then runs
  `"<path>" -V` through a shell and compares the version string.
- `pg_ctl` does the same for both `initdb` and `postgres`, and shells out with
  `system()`.
- The postmaster calls `get_pkglib_path()` and then `opendir()`s it, failing with
  "incomplete PostgreSQL installation" if the sibling directory is missing.
  Extension modules are loaded with `dlopen()` from `$libdir` /
  `dynamic_library_path`.

Consequence for packaging: the launcher must make each command behave as if it were
the real binary at its real path. Wrapping only `postgres` is insufficient.
`initdb` and `pg_ctl` must see their siblings in the same directory, and the sibling
check runs a child process. `scripts/experiments/php-musl-dynamic.sh` shows the
current launcher pattern (`ld-musl-x86_64.so.1 --library-path "$ROOT/lib/musl"`),
and the pinned loader implements `--argv0`, which is the mechanism to keep the
child's view of its own path intact. Whether every entry point survives that
wrapper is an open spike question, not an established fact.

### Dynamic linking assumptions

- `src/makefiles/Makefile.linux` adds `-Wl,-rpath,'$(rpathdir)',--enable-new-dtags`
  by default. Upstream documents `--disable-rpath` as the way to keep an
  installation relocatable, and warns that without an rpath the binaries need
  another way to find the shared libraries (typically the system dynamic linker).
- Current PHP documentation (README) established a repository rule that bundles
  must not keep absolute `RUNPATH` entries. The pinned musl loader searches
  `LD_LIBRARY_PATH`, then each object's rpath/runpath, then `/etc/ld-musl-<arch>.path`,
  then `/lib:/usr/local/lib:/usr/lib`. A PostgreSQL bundle must therefore be
  explicit about where `libpq`, `libssl`, `libcrypto`, `libz`, ICU, XML and other
  dependencies are found, and the verifier must reject host paths.
- `musl-gcc` on this host is GCC 16.2.1 with specs pointing at
  `/usr/x86_64-linux-musl/lib64`, and the installed musl is 1.2.5 — the same
  version the PHP bundle pins. The specs inject
  `-dynamic-linker /lib/ld-musl-x86_64.so.1` for non-static links, so a
  self-contained bundle still needs its own loader (as PHP does) or a documented
  host-interpreter requirement.
- A local non-static PCM build of musl exposes `dlopen` through the shared
  library implementation; the `stub_dlopen` in `src/ldso/dlopen.c` is only the
  static-link fallback. So `dlopen` support is compatible with the
  "musl-dynamic" direction, but this was read from source, not demonstrated by a
  PostgreSQL module load.

### Locale is the sharpest functional trade-off

- musl's `strcoll()` is implemented as `strcmp()`, and `strxfrm()` copies the
  string; musl does not provide glibc-style locale collation data.
- PostgreSQL 18's `builtin` provider supports only `C`, `C.UTF-8` and
  `PG_UNICODE_FAST`. PostgreSQL 17's `builtin` provider supports only `C` and
  `C.UTF-8` (`PG_UNICODE_FAST` arrived in 18). PostgreSQL 16 has no `builtin`
  provider at all; it offers `libc` and `icu` only.
- Upstream documents that ICU results can depend on the ICU library version, so
  bundling ICU makes the bundle's behaviour depend on which ICU release is
  shipped.

Consequences to decide, not to assume:

1. If the contract is "collation behaves like Debian glibc", musl + `libc`
   provider cannot satisfy it. Vietnamese and other accent-sensitive sort orders
   would silently change.
2. `builtin` with `C.UTF-8` (or `PG_UNICODE_FAST` on 18) gives deterministic,
   portable code-point ordering, at the cost of not matching glibc ordering.
3. Bundling ICU restores rich collation but adds a large dependency and a version
   sensitivity; `--without-icu` is the smaller, more predictable bundle.
4. The PHP runtime already ships ICU for `ext-intl`; sharing it was not verified
   and would couple two bundles' lifecycles.

### Versioning constraints

- `src/pl/plpgsql/src/Makefile` builds `plpgsql` as a module with a `PG_MODULE_MAGIC`
  block, which the backend validates against the major version. A `.so` built for
  18.x is rejected by a 17.x server, and vice versa.
- `pg_upgrade` shells out to `pg_ctl`, `pg_dump`, `pg_dumpall`, `pg_restore` and
  `psql`, and checks versions by running them. That makes it a strong end-to-end
  probe of a relocatable bundle, but also a requirement that those commands all
  resolve correctly through the launcher.
- Redis pins exact versions with reviewed upstream SHA-256 values; PHP resolves
  stable patches and validates php.net checksums. PostgreSQL would follow the same
  "pinned exact version + reviewed checksum" discipline, with a per-major series
  decision (for example 18.x only, or 18.x plus 17.x while both are supported).

### CI and planner integration costs

- `.github/workflows/build.yml` installs only `build-essential binutils curl
  musl-tools` for Redis. A PostgreSQL job also needs `bison`, `flex`, `perl`
  (build-time code generation), and whatever libraries the chosen feature set
  uses. The repo rule is that local scripts never install host packages; CI may
  install explicitly.
- `scripts/updates.py` plans PHP and Redis matrices from
  `runtime-update-plan.json`. `runtimes/php/scripts/publish.py` validates the
  exact expected asset set per runtime and refuses unknown shapes. Adding
  PostgreSQL requires new `postgres_matrix` planning, manifest/Magic identity
  checks, release-note wording, and publish validation — not just a new runtime
  directory.
- Tests are contract tests keyed to the existing runtimes; they encode the
  Redis "fully static, no `NEEDED`" and PHP "musl-dynamic with bundled loader"
  contracts. A PostgreSQL contract test has to be written from the agreed
  design, not adapted from either.

## Spike results (run 2026-11-07)

The spike was executed in a throwaway directory (`.cache/pg-spike/`, not tracked)
against a fresh, checksum-verified upstream 18.6 archive. It overturned or
sharpened several assumptions, and it confirmed the two questions that could have
invalidated the design.

### Confirmed: `dlopen` works in a musl-dynamic PostgreSQL backend

Built with `musl-gcc`, the backend loads and executes `.so` modules normally:
`plpgsql`, `hstore`, `citext`, `cube`, `ltree`, `pg_trgm`, `unaccent`, `intarray`
and `btree_gist` all loaded and ran real queries. A C extension compiled
separately against the bundle's own `pg_config --includedir-server` was then
installed into `lib/` and loaded via `CREATE EXTENSION`, returning its value. This
settles the "as many extensions as possible" requirement: the dynamic-musl contract
is not just required, it works.

Ruby-style fully static musl remains ruled out — `stub_dlopen` would fail every one
of these loads.

### Confirmed: the launcher chain works with no host musl loader at all

`initdb` → `pg_ctl start` → `psql` → `CREATE EXTENSION` → `pg_ctl stop` completed
end to end from a tree relocated under a directory name containing spaces, inside a
sandbox where both `/lib/ld-musl-x86_64.so.1` and `/usr/lib/ld-musl-x86_64.so.1`
were replaced by zero-byte files. In that same sandbox a raw `libexec/bin/postgres`
was refused by the kernel (`Permission denied`) while `bin/postgres` ran and
reported `PostgreSQL 18.6 on x86_64-pc-linux-musl`. `pg_config --bindir/--sharedir/
--libdir/--pkglibdir` all resolved inside the relocated bundle, proving the
`--argv0` mechanism makes PostgreSQL see `$ROOT/bin/<command>` as its own path.

### Corrected: the loader must find itself *by name*, not by `PT_INTERP`

The design initially assumed `--library-path "$ROOT/lib/musl"`. That fails, and the
reason is subtle enough to be worth recording:

- Every musl-linked ELF the build produces carries
  `NEEDED: ld-musl-x86_64.so.1` (as well as `NEEDED: libpq.so.5` etc.).
- The loader registers *itself* as `ldso.name = "libc.so"` (`ldso/dynlink.c:1731`),
  and `load_library()` short-circuits only when a `NEEDED` name matches `ldso.name`
  or one of the reserved names (`libc`, `libpthread`, `librt`, `libm`, `libdl`,
  `libutil`, `libxnet`).
- `ld-musl-x86_64.so.1` matches **neither**, so the loader does not recognise the
  entry as itself. It opens the name through `--library-path`, then each object's
  rpath, then `/etc/ld-musl-<arch>.path`, then `/lib:/usr/local/lib:/usr/lib`.
- Demonstrated with a poison file: a zero-byte `ld-musl-x86_64.so.1` placed *first*
  in `--library-path` produces `Exec format error (needed by ...)` — the search
  really does resolve this name from the path list.

Consequence: the launcher must put **both** directories on the path —
`--library-path "$ROOT/lib:$ROOT/lib/musl"` — or simply keep the loader in the same
directory as the libraries (`lib/`), which was also verified to work. Without that,
a bundle silently succeeds on any host that has a distro musl and fails on one that
does not, because `/lib` is the last-resort search path.

This also means the `lib/ld-musl-x86_64.so.1` convenience symlink in the earlier
layout sketch is unnecessary: the kernel only interprets `PT_INTERP` if it executes
a raw ELF, and the launcher guarantees that never happens.

### Corrected: the built tree's own layout is `lib/` and `share/`

An out-of-tree `make install-world-bin` with a `/usr/local/pgsql` prefix produces:

```text
bin/         initdb, postgres, pg_ctl, psql, pg_dump, pg_config, ...
lib/*.so     plpgsql.so and the whole contrib module set (79 files), plus libpq.so.5
share/       postgres.bki, *.sql, timezone/, tsearch_data/, extension/*.control
include/     server headers (needed only by third-party extension builds)
```

There is no `lib/postgresql/` or `share/postgresql/` split under this prefix — the
earlier design sketch was wrong about that, and `$libdir` (`pg_config --pkglibdir`)
is simply `<prefix>/lib`.

### Confirmed: kernel UAPI headers need a separate include root

`pg_combinebackup` includes `<linux/fs.h>`. `/usr/x86_64-linux-musl/include` ships
no `linux/` or `asm/` tree, so the build fails with
`fatal error: linux/fs.h: No such file or directory` until the compile adds a
secondary root:

```text
CPPFLAGS="-D_GNU_SOURCE -idirafter /usr/include"
```

`-idirafter` (not `-I`) is required so musl's own headers keep priority. A CI job
must therefore provide kernel UAPI headers as a separate, non-musl include root.

### Confirmed: `--disable-rpath` builds are clean, and need the launcher

`readelf -d` on the built binaries shows no `RPATH`/`RUNPATH` and `NEEDED` of only
`libpq.so.5` and `ld-musl-x86_64.so.1`. Running `psql` without an explicit library
path fails with `Error loading shared library libpq.so.5` — expected, and exactly
why every entry point must be a launcher rather than a bare exec.

### Measured: the locale trade-off is worse than "different ordering"

musl `setlocale()` **accepts any locale name** it is given:

```text
vi_VN.UTF-8    -> vi_VN.UTF-8
en_US.UTF-8    -> en_US.UTF-8
```

but the collation tables behind those names are the C ones. Consequences measured
on a running bundle:

- `initdb --locale-provider=libc --locale=vi_VN.UTF-8` **succeeds**, stores
  `datcollate=vi_VN.UTF-8`, `datctype=vi_VN.UTF-8`, `datlocprovider=c` and an empty
  `datlocale`. There is no error and no warning — a cluster that claims Vietnamese
  conventions while collating as C.
- Sorting eight Vietnamese strings gave `an,anh,binh,bình,da,Ánh,ăn,đa`, which is
  byte-identical to glibc `LC_ALL=C`, and differs from glibc `LC_ALL=vi_VN.UTF-8`
  (`an,anh,Ánh,ăn,binh,bình,da,đa`).

That last point is the design-relevant one: the failure mode is **silent**, so the
bundle cannot offer `libc`-provider clusters as a "works like normal PostgreSQL"
option. `builtin` with `C.UTF-8` and `PG_UNICODE_FAST` both work (18.6
`PG_UNICODE_FAST` cluster verified; ordering is code-point, e.g.
`A,B,a,b,z,á`), and they are honest about what they do.

### Non-finding: "logical replication launcher exited with exit code 1"

Every shutdown logs `background worker "logical replication launcher" ... exited
with exit code 1`. This looked like a portability defect. It is not: the host's own
glibc PostgreSQL 18.6, built by the distro, logs the identical line on the identical
`pg_ctl -w stop -m fast`. `ApplyLauncherMain` registers `pqsignal(SIGTERM, die)`
and `die` calls `proc_exit(1)`, so exit code 1 on a fast shutdown is upstream
behaviour. Recorded so it is not re-investigated.

### Bundle size observed

A complete 18.6 server install (79 module `.so` files, all CLI tools, headers
excluded from the archive) is roughly 40 MB unstripped in the staged tree, before
tar.gz. That fits the existing archive conventions comfortably.

### Not covered by this spike

ICU was built **without** (`--without-icu`), because the host has no musl C++
toolchain (`musl-gcc` only, no `musl-g++`, no musl `libstdc++`). The Zig-based C++
route is a design decision, still unproven. Everything else listed above was run.

## Proposed spike (original scope, kept for the record)

One question only: **can a self-contained PostgreSQL 18.6 bundle be started,
queried, stopped and relocated on a host with no PostgreSQL and no glibc?**

Shape:

1. Fresh upstream `postgresql-18.6.tar.bz2` in a new empty directory; verify SHA-256
   against the published value; extract with `python3 -I` / plain `tar`.
2. Configure with `--disable-rpath`, a prefix that matches the final bundle layout,
   `--without-icu` (smaller first spike; ICU as a follow-up), explicit
   `--with-ssl=openssl --with-zlib` using a bundled static dependency set, and
   whichever of `--without-readline`/`--without-lz4`/`--without-zstd` keeps the
   dependency graph closed. Build `world` with `musl-gcc`.
3. Stage a bundle whose launcher mirrors `bin/php`: every public command is a
   launcher that execs the pinned musl loader with `--library-path` and the real
   binary, preserving `argv[0]` semantics.
4. Verify, on a machine without PostgreSQL installed:
   - `initdb` (builtin `C.UTF-8` provider) in a path containing spaces;
   - `postgres -D ...` starts, `psql -c 'select 1'` connects over a Unix socket;
   - `CREATE EXTENSION plpgsql` and a `DO $$ ... $$` block run;
   - a trivial C extension built against the bundle's `pg_config` loads via
     `dlopen`, proving the `.so` module path;
   - `pg_ctl stop`, then move the whole tree and repeat start/query/stop;
   - `readelf` on all ELFs shows no absolute `RUNPATH`/`RPATH`, no host library
     paths, and only bundle-relative resolution.
5. Record what failed. The locale question (glibc-compatible ordering vs.
   deterministic code-point ordering vs. bundled ICU) is a product decision that
   should come out of the spike with measured evidence, not a guess.

## Open questions for the user

1. Is PostgreSQL meant to be a **server bundle** (like Redis) that DevStack starts
   per project, or only enough for drivers/clients?
2. Which majors matter? 18.x only, or 17.x and 18.x in parallel while both are
   supported?
3. Collation: accept musl-native `C.UTF-8`/`PG_UNICODE_FAST` deterministic
   ordering, or require glibc-compatible ordering (which forces ICU)?
4. Are extra server extensions required (e.g. PostGIS, pgvector, uuid-ossp,
   hstore) or is core + PL/pgSQL enough for the first bundle?
5. Should PostgreSQL live in this repository as `runtimes/postgresql/`, sharing
   the planner, or as a separate builder?

These change what gets built, so they belong here rather than in a builder's
discretion.

## References

- https://www.postgresql.org/support/versioning/
- https://www.postgresql.org/docs/18/install-make.html (including `--disable-rpath`)
- https://www.postgresql.org/docs/18/locale.html and .../17/locale.html
- https://www.postgresql.org/docs/18/xfunc-c.html (`$libdir`, magic block)
- https://www.postgresql.org/about/licence/
- https://ftp.postgresql.org/pub/source/v18.6/
- https://wiki.musl-libc.org/functional-differences-from-glibc.html
- Local: `.cache/repro/work/source/postgresql/src/common/exec.c`,
  `src/port/path.c`, `src/bin/initdb/initdb.c`, `src/bin/pg_ctl/pg_ctl.c`,
  `src/backend/postmaster/postmaster.c`, `src/makefiles/Makefile.linux`,
  `.cache/loader-try/out/source/musl-1.2.5/src/locale/strcoll.c`,
  `.cache/loader-try/out/source/musl-1.2.5/ldso/dynlink.c`
