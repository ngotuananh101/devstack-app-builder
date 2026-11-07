# PostgreSQL Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `runtimes/postgresql/` so DevStack ships self-contained PostgreSQL 17.x and 18.x **server** bundles — musl-dynamic, relocatable, with the largest extension set that the build actually produces — and wire them into the existing planner, workflow and publisher.

**Architecture:** A new runtime module following the Redis shape: `source.py` authenticates pinned upstream archives, `build.sh` orchestrates, and one `bundle.py` stages/verifies/packages. The bundle is a musl-dynamic install tree in which every public command in `bin/` is a shell launcher that execs the bundled musl loader with `--library-path "$ROOT/lib:$ROOT/lib/musl"` and `--argv0 "$ROOT/bin/<command>"` around a raw ELF kept in `libexec/bin/`. Verification runs a real cluster lifecycle on a relocated copy and exercises every installed module by its kind.

**Tech Stack:** Bash, Python 3 standard library, GNU make, Zig 0.17.1 (`zig cc` and `zig c++` for a musl C/C++ toolchain), bison, flex, perl, binutils; pinned OpenSSL, zlib, libxml2, util-linux `libuuid` and ICU sources.

**Spec:** `docs/superpowers/specs/2026-11-07-postgresql-runtime-design.md` — read it first; it carries the rationale and the spike evidence. Evidence and measurements: `docs/superpowers/specs/2026-11-07-postgresql-runtime-research.md`.

## Global Constraints

- Native Linux x86_64 only. No Docker. Local build scripts never install host packages; missing tools fail with an explicit message.
- musl-dynamic only. `--disable-rpath` at configure time; no absolute `RPATH`/`RUNPATH` in any ELF the bundle ships.
- Launcher invocation is exactly `"$ROOT/lib/musl/ld-musl-x86_64.so.1" --library-path "$ROOT/lib:$ROOT/lib/musl" --argv0 "$ROOT/bin/<command>" "$ROOT/libexec/bin/<command>" "$@"`. Both `--library-path` entries are required; dropping `$ROOT/lib/musl` silently passes on hosts that have a distro musl.
- Compile flags include `CPPFLAGS="-D_GNU_SOURCE -idirafter /usr/include"`. `-idirafter`, never `-I`, so musl headers keep priority.
- Every source archive (PostgreSQL 17.11/18.6, Zig, OpenSSL, zlib, libxml2, ICU, util-linux for `libuuid`) is exact-version pinned, HTTPS only, SHA-256 verified **before** extraction, extracted into a fresh directory that must not already exist. All pins live in `config/sources.json`, one file, as in Redis; `config/build-options.json` holds only configure flags and the locale contract. The musl loader's pin is not duplicated — it is reused from `runtimes/php/config/musl.json` (musl 1.2.5).
- OpenSSL is built **shared** and staged into `lib/` as `libssl.so.3` / `libcrypto.so.3`; zlib, libxml2 and util-linux's `libuuid` are built **static** with `-fPIC`. OpenSSL is shared because it keeps global state (error queue, init flags, RNG) that must not be duplicated: the postmaster for TLS and `pgcrypto.so` for digests both link it, and two independent static copies in the same process would each have their own. The others have no such state and cost less as static archives. `-fPIC` on the static ones is required because they are linked into loadable `.so` modules, not only into executables.
- `--with-uuid=e2fs` with a vendored static `libuuid`, never `--with-uuid=bsd`: `configure` requires `uuid_to_string` in libc for `bsd`, and musl provides no `uuid_*` functions at all.
- Existing bundles and assets are never overwritten; remove explicitly to rebuild.
- No Python is ever run with its working directory inside an extracted archive; all module invocations use `python3 -I`.
- Locale contract: `builtin` provider, `C.UTF-8` default (18.x also `PG_UNICODE_FAST`). The `libc` provider is not offered — musl accepts `vi_VN.UTF-8` and collates as C, silently.
- ICU **is** built, with `zig c++` as the C++ provider. The host has no `musl-g++` and no musl `libstdc++`; Zig supplies both its own libc++ and musl headers, so ICU's C++ sources compile. `zig c++` targeting `x86_64-linux-musl` is what makes this possible, and it is why the toolchain pin matters. The C-only `--without-icu` build is the documented fallback if ICU fails to compile — a release decision recorded in the notes, never a silent option. Task 4 builds ICU first and only then decides; the `--with-icu` / `--without-icu` flag is the single place that decision lands.
- Shipped config samples live in `share/` (`postgresql.conf.sample`, `pg_hba.conf.sample`, `pg_ident.conf.sample`, `pg_service.conf.sample`, `psqlrc.sample`). There is no `etc/` directory in an `install-world-bin` tree; do not create one.
- Manifest `schema_version` is the integer `1`, `runtime` is the string `"postgresql"`, and there is no `profile` key anywhere.
- Supported majors in this plan: `17` and `18`. Pinned patches: `17.11` (`dd27f2b3c59e73ed14aa3324901242bf69a032a6347805f274e6260322d42979`) and `18.6` (`555610c24d53e4316da5b7d3fc25c279d96856d5e0e23ee308c328c5fa881d9f`) — both re-verify against `https://ftp.postgresql.org/pub/source/v<version>/postgresql-<version>.tar.bz2.sha256` before pinning.

## Review Focus

Inputs and conditions the spec implies but no task's happy-path test covers. Each line has a test in the named task.

1. **A host with no musl loader at all.** A bundle that resolves `ld-musl-x86_64.so.1` from `/lib` works on the build host and fails for the user. Task 6 masks every host loader and re-runs the lifecycle.
2. **A path containing spaces and `#`.** DevStack installs under paths with spaces on some hosts; `initdb` must accept `-D "…/PostgreSQL 18.6 data"`, and a `#` in the bundle path must not start a shell comment. Task 2 asserts the launcher resolves `$ROOT` from its own path rather than the caller's and that every expansion is quoted; Task 6 runs the whole lifecycle from a relocated copy whose path contains both a space and a `#`.
3. **A module present on one major only.** `pg_overexplain` exists in 18 and not 17. A publisher that rejects 17 for lacking it, or a manifest that claims it, is a bug. Tasks 3 and 9.
4. **A module built but unloadable** (`dlopen` failure, missing dependency, wrong `$libdir`). The verifier must load and *execute* every module, not list it. Task 6.
5. **A cluster initialized with an unsupported locale name.** `--locale-provider=libc --locale=vi_VN.UTF-8` succeeds and silently collates as C, so the verifier must reject `libc` as a requestable provider rather than pass it through. Task 6.
6. **A module the design calls included but the configure flags quietly drop.** `xml2` is built only under `--with-libxml`, `uuid-ossp` only when `with_uuid != no`, `pgcrypto`/`sslinfo` only under `--with-ssl=openssl`. A bundle that omits them still builds and verifies cleanly against a `modules.json` written from the same omission — a self-confirming hole. Task 3 writes `modules.json` from the design's expectation (not from the build), Task 4 asserts the enabling flags, and Task 5's `compare` fails on any missing module. Task 7 then reconciles the generated list against the expectation and requires an explicit reason for every difference.

---

### Task 1: Pinned sources and the `--print-plan` CLI

**Files:**
- Create: `runtimes/postgresql/config/sources.json`
- Create: `runtimes/postgresql/scripts/source.py`
- Create: `runtimes/postgresql/scripts/build.sh`
- Create: `tests/test_postgresql.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `source.load_source(version) -> dict` with keys `version`, `url`, `sha256`, `license`, `series`; `source.verify_digest(archive: Path, expected: str) -> None`; `source.extract_source(archive: Path, destination: Path) -> Path` (returns the single extracted root, raises `FileExistsError` if `destination` exists); `source.plan(arguments: list[str]) -> dict` with keys `version`, `major`, `target` (`"musl-dynamic"`), `architecture` (`"x86_64"`), `source`; `source.dependencies() -> dict` returning the vendored pins (`zig`, `openssl`, `zlib`, `libxml2`, `icu`, `util-linux`); `source.CONFIG` (the `Path` to `config/sources.json`); `source.DEFAULT` (`"18.6"`), `source.SERIES` (`("18", "17")`). CLI: `python3 -I runtimes/postgresql/scripts/source.py [--download <work>] [version]`.
- Later tasks import `source` and call `plan`/`load_source`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_postgresql.py`, follow the `tests/test_redis.py` loading pattern exactly — a module-level `ROOT`, `SCRIPTS = ROOT / "runtimes/postgresql/scripts"`, and a `module(name)` helper that loads `runtimes/postgresql/scripts/<name>.py` with `importlib.util.spec_from_file_location`, then a `setUp` that asserts the file exists before importing it so a missing module fails with the message rather than an `ImportError`. Every test class below uses `self.source`/`self.launchers`/… from its own `setUp`.

```python
def test_pinned_sources_select_exact_upstream_archives(self):
    row = self.source.load_source("18.6")
    self.assertEqual(row["url"], "https://ftp.postgresql.org/pub/source/v18.6/postgresql-18.6.tar.bz2")
    self.assertEqual(row["sha256"], "555610c24d53e4316da5b7d3fc25c279d96856d5e0e23ee308c328c5fa881d9f")
    self.assertEqual(row["series"], "18")
    self.assertEqual(self.source.load_source("17.11")["series"], "17")

def test_unpinned_and_wrong_shaped_versions_are_rejected(self):
    for value in ("18", "18.6.1", "latest", "17", "16.9", "../tmp", "18.6\n", "19.0"):
        with self.subTest(value=value), self.assertRaises(ValueError):
            self.source.load_source(value)

def test_digest_mismatch_is_rejected(self):
    # sanity: a real digest passes, a tampered file fails
    ...

def test_extraction_is_fresh_and_confined(self):
    # reuse the Redis cases: escape paths, absolute paths, symlink escape,
    # hard links, device nodes, duplicate members, two roots
    ...

def test_plan_rejects_target_and_profile_arguments(self):
    for args in (["glibc"], ["musl"], ["musl-dynamic"], ["full"], ["minimal"], ["--profile", "full"], ["18.6", "17.11"]):
        with self.subTest(args=args), self.assertRaises(ValueError):
            self.source.plan(args)

def test_plan_defaults_to_18_6_and_reports_no_profile(self):
    plan = self.source.plan([])
    self.assertEqual((plan["version"], plan["major"], plan["target"], plan["architecture"]),
                     ("18.6", "18", "musl-dynamic", "x86_64"))
    self.assertNotIn("profile", plan)
    self.assertEqual(self.source.plan(["17.11"])["major"], "17")

def test_every_vendored_dependency_is_pinned_before_a_build_starts(self):
    pins = self.source.dependencies()
    self.assertEqual(set(pins), {"zig", "openssl", "zlib", "libxml2", "icu", "util-linux"})
    for name, pin in pins.items():
        with self.subTest(name=name):
            self.assertTrue(pin["url"].startswith("https://"), name)
            self.assertRegex(pin["sha256"], r"^[a-f0-9]{64}$", name)
            self.assertNotIn("<", pin["version"] + pin["sha256"], name)
```

The two elided bodies copy `tests/test_redis.py::test_source_digest_rejects_modified_archive` and `::test_extraction_rejects_escape_links_and_special_members` verbatim, renaming only the module under test — the archive-safety rules are the same and must not drift between runtimes.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `python3 -I -m unittest tests.test_postgresql -v` (or `discover -s tests -p 'test_postgresql.py'`)
Expected: FAIL — `runtimes/postgresql/scripts/source.py` does not exist, so the helper raises `FileNotFoundError`.

- [ ] **Step 3: Write `config/sources.json`**

```json
{
  "default": "18.6",
  "series": ["18", "17"],
  "checksum_source": "https://ftp.postgresql.org/pub/source/v<version>/postgresql-<version>.tar.bz2.sha256",
  "versions": {
    "18.6": {"series": "18", "sha256": "555610c24d53e4316da5b7d3fc25c279d96856d5e0e23ee308c328c5fa881d9f", "license": "PostgreSQL"},
    "17.11": {"series": "17", "sha256": "dd27f2b3c59e73ed14aa3324901242bf69a032a6347805f274e6260322d42979", "license": "PostgreSQL"}
  },
  "dependencies": {
    "zig": {"version": "0.17.1", "url": "https://ziglang.org/download/0.17.1/zig-x86_64-linux-0.17.1.tar.xz", "sha256": "<verify upstream>", "license": "MIT"},
    "openssl": {"version": "<pin>", "url": "https://www.openssl.org/source/openssl-<version>.tar.gz", "sha256": "<verify upstream>", "license": "Apache-2.0"},
    "zlib": {"version": "<pin>", "url": "https://zlib.net/zlib-<version>.tar.gz", "sha256": "<verify upstream>", "license": "Zlib"},
    "libxml2": {"version": "<pin>", "url": "https://download.gnome.org/sources/libxml2/<minor>/libxml2-<version>.tar.xz", "sha256": "<verify upstream>", "license": "MIT"},
    "icu": {"version": "<pin>", "url": "https://github.com/unicode-org/icu/releases/download/release-<v>/icu4c-<v>-src.tgz", "sha256": "<verify upstream>", "license": "Unicode-3.0"},
    "util-linux": {"version": "<pin>", "url": "https://www.kernel.org/pub/linux/utils/util-linux/v<minor>/util-linux-<version>.tar.xz", "sha256": "<verify upstream>", "license": "LGPL-2.1-or-later"}
  }
}
```

The `<verify upstream>` placeholders are deliberate: their checksums must be read from the upstream `.sha256` / `.sign` / release index at implementation time and never copied from a plan. Zig is the one exception with a starting point already in this repository — `scripts/experiments/php-musl-dynamic.py` records `ZIG_SHA256 = "3581863e3cbcfd0fce0efc682ce7e9e93da96668a977791d356a26d2a7e9b17a"` and `downloads/zig-x86_64-linux-0.17.1.tar.xz` on disk matches it — so re-verify that value against `https://ziglang.org/download/index.json` rather than re-deriving it. `zig`, `util-linux` and `libxml2` are named here because the build genuinely needs them (`zig cc`, `libuuid` for `uuid-ossp`, libxml2 for `xml2`), not because the design lists them.

The musl loader's pin is **not** duplicated here. `runtimes/php/config/musl.json` already pins musl 1.2.5 (`a9a118bbe84d8764da0ea0d28b3ab3fae8477fc7e4085d90102b8596fc7c75e4`) and the PHP runtime already builds a loader from it; PostgreSQL reuses that pin through `toolchain.build_loader` (Task 4) so the two runtimes cannot ship loaders built from different sources. Duplicating the checksum would let them drift.

URL construction for PostgreSQL itself is `https://ftp.postgresql.org/pub/source/v{version}/postgresql-{version}.tar.bz2`, built in `load_source` exactly as the Redis module builds its URL, so the URL is never stored twice. `load_source` must reject a `series` that is not in `SERIES`, a version whose leading component disagrees with `series`, a `sha256` that is not 64 lowercase hex characters, and any extra key in the pin object. `dependencies()` returns the `dependencies` object and rejects any pin whose `version`/`sha256` still holds a `<` placeholder, so a forgotten pin fails at plan time rather than after a 20-minute build.

- [ ] **Step 4: Write `scripts/source.py`**

Mirror `runtimes/redis/scripts/source.py` function-for-function (`digest`, `load_source`, `verify_digest`, `extract_source`, `plan`, and the `if __name__ == "__main__":` argparse block), with these differences:

- `load_source` validates `re.fullmatch(r"[0-9]+\.[0-9]+", version)` first, then membership in `config["versions"]`.
- `extract_source` opens with `tarfile.open(archive, "r:bz2")` and requires `src/include/postgres.h` and `contrib/Makefile` to exist in the returned root, in place of Redis's `src/Makefile` check.
- `plan` raises `ValueError("PostgreSQL is musl-dynamic only; the libc target is not selectable.")` for all three target words — `glibc`, `musl` **and** `musl-dynamic` — because none of them is a selectable argument here; the plan always reports `musl-dynamic` on its own. And `ValueError("PostgreSQL builds all supported majors together; pass no profile.")` for `full` and `minimal`.
- `--download` fetches with the same `curl --fail --location --proto =https --proto-redir =https` invocation, verifies the digest **before** `extract_source`, and writes `request.json` into the work directory.

- [ ] **Step 5: Write `scripts/build.sh`**

Model it on `runtimes/redis/scripts/build.sh`: parse `--print-plan`/`-h`, collect the remaining arguments, call `python3 -I "$RUNTIME_DIR/scripts/source.py" "${args[@]}"`, print and exit when `--print-plan`, otherwise validate `uname -s` = `Linux` and `uname -m` = `x86_64` and continue. For this task the body after the platform check is `printf 'PostgreSQL build pipeline not implemented yet\n' >&2; exit 1` — Tasks 4–7 replace it.

Usage string: `Usage: ./build.sh postgresql [exact-version] [--print-plan]` with `Defaults: 18.6. musl-dynamic server bundles for 17.x and 18.x; no profiles or dependency installation.`

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `python3 -I -m unittest tests.test_postgresql -v` and `bash -n runtimes/postgresql/scripts/build.sh`
Expected: all pass; `bash -n` exits 0 with no output.

- [ ] **Step 7: Commit**

```bash
git add runtimes/postgresql/config/sources.json runtimes/postgresql/scripts/source.py runtimes/postgresql/scripts/build.sh tests/test_postgresql.py
git commit -m "feat(postgresql): pin 17.11/18.6 sources and add a refusal-first CLI"
```

---

### Task 2: Launcher generation

**Files:**
- Create: `runtimes/postgresql/packaging/launch.sh`
- Create: `runtimes/postgresql/packaging/launcher.in`
- Create: `runtimes/postgresql/scripts/launchers.py`
- Test: `tests/test_postgresql.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `launchers.commands(bundle_root: Path) -> list[str]` (basenames of the ELFs in `<bundle_root>/bin`, sorted); `launchers.render(command: str) -> str` (the launcher shell text, ending in exactly one newline); `launchers.install(bundle_root: Path) -> list[str]` which moves every ELF from `<bundle_root>/bin/<cmd>` to `<bundle_root>/libexec/bin/<cmd>` and writes the launcher at `<bundle_root>/bin/<cmd>`, returning the command list; `launchers.LOADER = "lib/musl/ld-musl-x86_64.so.1"`, `launchers.LIBRARY_PATH = "lib:lib/musl"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_launcher_sets_argv0_to_its_own_path(self):
    text = self.launchers.render("psql")
    self.assertIn('--library-path "$ROOT/lib:$ROOT/lib/musl"', text)
    self.assertIn('--argv0 "$ROOT/bin/psql"', text)
    self.assertIn('"$ROOT/libexec/bin/psql"', text)
    self.assertIn('"$ROOT/lib/musl/ld-musl-x86_64.so.1"', text)
    self.assertIn('exec ', text)
    self.assertTrue(text.endswith('"$@"\n'))
    self.assertNotIn("$@\"", text.replace('"$@"', ""))  # no other positional expansion

def test_launcher_never_drops_a_library_path_entry(self):
    # Both entries must be present: dropping lib/musl passes on hosts whose
    # distro ships a musl loader and fails for everyone else.
    text = self.launchers.render("initdb")
    self.assertNotIn('--library-path "$ROOT/lib/musl"', text)
    self.assertEqual(text.count('lib/musl'), 2)

def test_launcher_isolates_the_environment_and_resolves_spaces(self):
    text = self.launchers.render("pg_ctl")
    self.assertIn('ROOT=$(CDPATH=\'\' cd -P -- "$(dirname -- "$0")/.." && pwd)', text)
    self.assertIn('. "$ROOT/libexec/launch.sh"', text)
    self.assertIn("#!/bin/sh\nset -eu\n", text)
    self.assertIn("$(dirname -- \"$0\")/..", text, "ROOT must come from the launcher's own path")
    # The environment scrub lives in the sourced file, exactly as PHP's does.
    shared = (SCRIPTS.parent / "packaging/launch.sh").read_text()
    for variable in ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT"):
        self.assertIn(variable, shared)

def test_install_moves_every_elf_and_leaves_no_raw_binary_in_bin(self):
    # build a fixture install tree with a real ELF-identifying first word,
    # a non-ELF data file in bin/, and a subdirectory
    ...

def test_installation_is_executable_and_idempotent_per_command(self):
    ...
```

The elided bodies create a temporary install tree (`bin/postgres` written as `b"\x7fELF" + b"\0"*60`, `bin/README` written as text, `bin/` also containing a directory), call `launchers.install`, and assert that `bin/postgres` is now a `0o755` shell script whose text equals `render("postgres")`, that `libexec/bin/postgres` holds the original bytes, and that `bin/README` and the subdirectory were left untouched.

- [ ] **Step 2: Run the tests and confirm they fail**

Expected: FAIL with `ModuleNotFoundError`/`FileNotFoundError` for `launchers.py`.

- [ ] **Step 3: Write `packaging/launcher.in` and `packaging/launch.sh`**

`launcher.in` is the template with one `@COMMAND@` placeholder:

```sh
#!/bin/sh
set -eu
# Resolve from the executable, never from the caller's current directory.
ROOT=$(CDPATH='' cd -P -- "$(dirname -- "$0")/.." && pwd)
. "$ROOT/libexec/launch.sh"
exec "$ROOT/lib/musl/ld-musl-x86_64.so.1" --library-path "$ROOT/lib:$ROOT/lib/musl" \
  --argv0 "$ROOT/bin/@COMMAND@" "$ROOT/libexec/bin/@COMMAND@" "$@"
```

`packaging/launch.sh` mirrors PHP's: a header comment saying it is sourced by installed launchers, then `unset LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT` — and nothing else yet. It exists so a future launcher that needs prepared environment (for example a default `PGDATA` root) has one place to add it; `stage` copies it to `$ROOT/libexec/launch.sh`, and `ROOT` is already set by the time it is sourced, exactly as PHP's `packaging/php` does it. Keeping the scrub in the sourced file rather than in the template means the generated launchers stay identical to each other, which is what `install`'s idempotence check relies on.

`render` reads the template from `Path(__file__).resolve().parents[1] / "packaging/launcher.in"` and `.replace("@COMMAND@", command)`.

- [ ] **Step 4: Implement `scripts/launchers.py`**

`commands(bundle_root)` returns `sorted(p.name for p in (bundle_root / "bin").iterdir() if p.is_file() and not p.is_symlink() and p.read_bytes()[:4] == b"\x7fELF")`. Reading four bytes per file is the cheap identity check the spike used; do not use `readelf` for 37 files when a magic test suffices. `install` refuses to run twice on the same tree by raising `ValueError` if `libexec/bin` already exists, and `chmod`s each launcher `0o755`.

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `python3 -I -m unittest tests.test_postgresql -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add runtimes/postgresql/packaging runtimes/postgresql/scripts/launchers.py tests/test_postgresql.py
git commit -m "feat(postgresql): generate loader launchers with argv0 fidelity"
```

---

### Task 3: Extension inventory computed per major

**Files:**
- Create: `runtimes/postgresql/config/modules.json`
- Create: `runtimes/postgresql/scripts/modules.py`
- Test: `tests/test_postgresql.py`

**Interfaces:**
- Consumes: nothing (operates on an install tree).
- Produces: `modules.KINDS = ("sql", "preload", "cli", "shared")`; `modules.inventory(install_root: Path) -> list[dict]` where each dict is `{"name": str, "kind": str}` sorted by `(kind, name)`; `modules.PRELOAD_ONLY: frozenset[str]`; `modules.CLI_ONLY: frozenset[str]`; `modules.EXCLUSIONS: dict[str, str]` mapping an excluded module name to the reason; `modules.compare(inventory, expected) -> list[str]` returning human-readable discrepancies (empty list means agreement); `modules.load_expected(major: str) -> dict` reading `config/modules.json`.

`inventory(install_root)` accepts **either** tree shape: the built install tree (`bin/<cmd>` are ELFs) or the staged bundle (`bin/<cmd>` are launcher scripts, the ELFs are in `libexec/bin/<cmd>`). It only ever asks "is `lib/<name>.so` there", "is `share/extension/<name>.control` there", and "is `<name>` one of the two contrib CLI names" — never "is this file an ELF in `bin/`" — so it gives the same answer on both.

Kinds, derived from what `make install-world-bin` produced on 18.6 (79 `.so`, 42 `.control`). The classification is closed-form, not heuristic — enumerate once and it stays right:

- `sql` — `share/extension/<name>.control` exists. 42 on the spike's 18.6 build, which had no OpenSSL/libxml2/uuid (`--without-ssl`, `--without-libxml`, `with_uuid=no`); with Task 4's flags it grows by `pgcrypto`, `sslinfo`, `xml2` and `uuid-ossp`.
- `preload` — `lib/<name>.so` exists, no `.control`, and the name is in `modules.PRELOAD_ONLY`, an explicit six-name set: `auth_delay auto_explain basebackup_to_shell basic_archive passwordcheck pg_overexplain`. These are loadable by `shared_preload_libraries` only.
- `shared` — `lib/<name>.so` with no `.control` and not in `PRELOAD_ONLY`: the `utf8_and_*` / `*_and_mic` encoding conversion objects, `_int`, `dict_snowball`, `libpqwalreceiver`, `pgoutput`, `test_decoding`, and the client libraries `libpq`, `libecpg`, `libecpg_compat`, `libpgtypes`. Loaded by another module or by a client, never by the server directly; recorded and hashed, not independently exercised.
- `cli` — the name is in `modules.CLI_ONLY`, an explicit two-name set: `oid2name vacuumlo`. The other 35 `bin/` entries are core PostgreSQL commands, not modules; they are covered by the launcher contract and Task 6's tool round-trip, so enumerating them here would only duplicate that.

Two explicit closed sets rather than a `bin/`-shape heuristic, because the staged bundle no longer has ELFs in `bin/`: a rule keyed on "an ELF in `bin/` with no `.so`" would classify all 37 launchers as `cli` once staged, and one keyed on ELF-ness would classify none. Anything with a `.control` or a `.so` that fits none of these four rules is an `unexpected` entry and `compare` reports it — that is how a future PostgreSQL release adding a new module shape gets noticed instead of silently misclassified.

- [ ] **Step 1: Write the failing tests**

```python
def test_inventory_separates_kinds_by_what_is_on_disk(self):
    # fixture install tree: extension/hstore.control + lib/hstore.so  -> sql
    #                      lib/auto_explain.so only              -> preload
    #                      lib/utf8_and_euc_jp.so only            -> shared
    #                      bin/oid2name                           -> cli
    #                      bin/pg_dump                            -> not a module
    ...

def test_inventory_reads_the_staged_tree_the_same_way(self):
    # the same fixture, restaged: bin/<cmd> are shell launchers and the ELFs
    # live in libexec/bin/. inventory() must return the identical list, which a
    # rule keyed on "an ELF in bin/" would not.
    ...

def test_preload_list_matches_the_servers_own_support_objects(self):
    # The preload set is closed and explicit; anything else with a .so and no
    # .control must fall into "shared", never "preload".
    self.assertEqual(self.modules.PRELOAD_ONLY,
                     frozenset({"auth_delay", "auto_explain", "basebackup_to_shell",
                                "basic_archive", "passwordcheck", "pg_overexplain"}))
    self.assertEqual(self.modules.CLI_ONLY, frozenset({"oid2name", "vacuumlo"}))

def test_eighteen_expects_the_modules_the_configure_flags_enable(self):
    expected = self.modules.load_expected("18")
    # Gated on configure flags, so their absence would be a silent capability loss.
    for name in ("pgcrypto", "sslinfo", "uuid-ossp", "xml2", "plpgsql", "hstore", "citext"):
        with self.subTest(name=name), self.assertIn(name, expected["sql"])
    # Preload-only modules have no .control and exist in 18 only.
    self.assertIn("pg_overexplain", expected["preload"])
    self.assertNotIn("pg_overexplain", expected["sql"])

def test_compare_reports_both_missing_and_unexpected(self):
    # compare() must name each discrepancy; an empty list only when equal
    ...

def test_exclusions_carry_a_reason(self):
    for name in ("plperl", "plpython3u", "sepgsql"):
        self.assertIn(name, self.modules.EXCLUSIONS)
        self.assertTrue(self.modules.EXCLUSIONS[name].strip())
```

- [ ] **Step 2: Run the tests and confirm they fail**

Expected: FAIL — `modules.py` missing.

- [ ] **Step 3: Write `config/modules.json`**

This task's file is written **from the design's expectations**, because no build with the final configure flags exists yet. That is deliberate and safe: Task 5 fails the build if a module it names is absent from the install tree, so the file is a specification that the build must meet, not a description of whatever the build happens to produce. Task 7 Step 5 replaces it with a generated version and reconciles any difference.

Expected content for both majors, with the `spi` directory's four extensions (`autoinc`, `insert_username`, `moddatetime`, `refint`) listed as ordinary `sql` members — 18.6 installs one `.control` per member, so the design's separate "cluster-wide extension directory" kind collapses into `sql`:

- `sql`, both majors: `amcheck autoinc bloom btree_gin btree_gist citext cube dblink dict_int dict_xsyn earthdistance file_fdw fuzzystrmatch hstore insert_username intagg intarray isn lo ltree moddatetime pageinspect pg_buffercache pg_freespacemap pg_logicalinspect pg_prewarm pgrowlocks pg_stat_statements pgstattuple pg_surgery pg_trgm pg_visibility pg_walinspect pgcrypto plpgsql postgres_fdw refint seg sslinfo tablefunc tcn tsm_system_rows tsm_system_time unaccent uuid-ossp xml2`. These are exactly the modules Task 4's flags enable: `pgcrypto` and `sslinfo` need `--with-ssl=openssl`, `uuid-ossp` needs `with_uuid != no`, `xml2` needs `--with-libxml`.
- `preload`, both majors unless noted: `auth_delay auto_explain basebackup_to_shell basic_archive passwordcheck`, plus `pg_overexplain` on 18 only.
- `shared`, both majors, generated by enumeration in Task 7 rather than typed here.
- `cli`, both majors: `oid2name vacuumlo`.

Record in `exclusions` at minimum:

```json
{
  "plperl": "Perl embedding is not part of this bundle.",
  "bool_plperl": "Perl embedding is not part of this bundle.",
  "hstore_plperl": "Perl embedding is not part of this bundle.",
  "jsonb_plperl": "Perl embedding is not part of this bundle.",
  "plpython3u": "Python embedding is not part of this bundle.",
  "hstore_plpython": "Python embedding is not part of this bundle.",
  "jsonb_plpython": "Python embedding is not part of this bundle.",
  "ltree_plpython": "Python embedding is not part of this bundle.",
  "pltcl": "Tcl embedding is not part of this bundle.",
  "sepgsql": "Requires a SELinux policy; meaningless in a portable bundle."
}
```

Add a top-level `"generated_from"` string recording the versions the lists were written against (`"18.6/17.11 design contract"` initially, then the real versions once generated).

- [ ] **Step 4: Implement `scripts/modules.py`**

`inventory(install_root)` enumerates `share/extension/*.control` and `lib/*.so` and classifies per the rules above, consulting `CLI_ONLY` for the two contrib tools and never `bin/`'s file type. `compare(inventory, expected)` returns `[f"missing {kind}: {name}" ...] + [f"unexpected {kind}: {name}" ...] + [f"wrong kind: {name} is {got}, expected {want}" ...]`, empty only when the two agree on every `(kind, name)`. `load_expected(major)` validates that the major is in `("17", "18")` and that every list is a sorted list of `[a-z][a-z0-9_]*` strings. `PRELOAD_ONLY` and `CLI_ONLY` are module-level frozensets, exported so the tests can assert against them.

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `python3 -I -m unittest tests.test_postgresql -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add runtimes/postgresql/config/modules.json runtimes/postgresql/scripts/modules.py tests/test_postgresql.py
git commit -m "feat(postgresql): classify installed modules per major"
```

---

### Task 4: Build pipeline — pinned dependencies, configure, make

**Files:**
- Create: `runtimes/postgresql/config/build-options.json`
- Create: `runtimes/postgresql/scripts/toolchain.py`
- Modify: `runtimes/postgresql/scripts/build.sh`
- Test: `tests/test_postgresql.py`

**Interfaces:**
- Consumes: `source.plan`, `source.verify_digest`, `source.extract_source`, `source.dependencies` (Task 1).
- Produces: `toolchain.require(tools: Sequence[str]) -> None` raising `ValueError` naming the first missing tool; `toolchain.prepare_zig(work: Path, pins: dict) -> Path` fetching, digest-verifying and extracting the pinned Zig archive and returning the directory holding the `zig` executable; `toolchain.zig(zig_dir: Path, options: dict) -> Path` returning `<zig_dir>/zig` after asserting `zig version` matches `build-options.json` `zig.version`; `toolchain.musl_version(zig_dir: Path) -> str` reading `lib/libc/musl/src/internal/version.h`; `toolchain.configure_flags(options: dict) -> list[str]`; `toolchain.build_env(zig_dir: Path, deps_prefix: Path) -> dict`; `toolchain.build_dependencies(work: Path, pins: dict, zig_dir: Path) -> Path` returning the dependency prefix; `toolchain.build_loader(work: Path, zig_dir: Path) -> dict` returning `{"path", "sha256", "musl_version", "license"}`, the built `ld-musl-x86_64.so.1`; `toolchain.write_provenance(path: Path, data: dict) -> None`.

`build-options.json` holds only what the build decides — configure flags, the make targets, the locale contract, and the Zig version/musl-version pair `toolchain.zig` cross-checks. Every dependency pin lives in `config/sources.json`. The musl loader has no pin in either file: it is read from `runtimes/php/config/musl.json`, which is the one place musl is pinned in this repository.

```json
{
  "zig": {"version": "0.17.1", "musl_version": "1.2.5"},
  "configure": {
    "shared": ["--disable-rpath", "--with-ssl=openssl", "--with-zlib", "--with-uuid=e2fs",
               "--with-libxml", "--with-icu", "--without-readline", "--without-llvm",
               "--without-perl", "--without-python", "--without-tcl", "--without-lz4",
               "--without-zstd", "--without-pam", "--without-systemd", "--without-bonjour",
               "--without-selinux", "--without-gssapi", "--without-ldap", "--without-libcurl"],
    "cppflags": "-D_GNU_SOURCE -idirafter /usr/include",
    "make_target": "world-bin",
    "install_target": "install-world-bin"
  },
  "locale": {"provider": "builtin", "default": "C.UTF-8", "alternate_18": "PG_UNICODE_FAST"}
}
```

Four details this encodes, each verified against the 18.6 `configure` rather than assumed:

- **`--with-icu`, with `zig c++` as the C++ provider.** The design enables ICU by default so that `CREATE COLLATION ... (provider = icu)` works and accent-sensitive ordering is available without a glibc host. The host has neither `musl-g++` nor a musl `libstdc++`, so `zig c++ -target x86_64-linux-musl` is what compiles ICU's C++ sources; Zig bundles libc++ and the musl headers. ICU is built as a static `-fPIC` archive and linked into the server, so nothing extra appears in `lib/`.
- **`--with-uuid=e2fs`, never `bsd`.** `configure` accepts `bsd` only when `uuid_to_string` is already in libc; musl exports no `uuid_*` symbol at all, so `bsd` fails the `ac_fn_c_check_func` and aborts. `e2fs` links `-luuid`, which the vendored util-linux provides.
- **`--with-libxml` with a vendored libxml2.** `contrib/Makefile` builds `xml2` only when `with_libxml=yes`. Leaving it off would drop a module the design lists as included; the vendored static libxml2 is what makes it reachable without a host header.
- **`--without-libcurl` is explicit.** It already defaults to `no`, but naming it means a future default change cannot silently add a host dependency. `configure` spells this option `--with-libcurl`, not `--with-curl`.

**ICU is the one decision that can still go wrong at build time.** Step 5 attempts it and, only if ICU's C++ compilation fails, falls back to `--without-icu` — recording the fallback in `config/build-options.json`, in `modules.json` (ICU-dependent collations become a documented limitation, not a module exclusion) and in the release notes. The fallback is not a user-selectable profile; it is a build-time outcome with a recorded reason. Do not reach for it pre-emptively: whether `zig c++` compiles ICU is exactly what the build is there to find out.

Every dependency pin in `config/sources.json` (`openssl`, `zlib`, `libxml2`, `icu`, `util-linux` for `libuuid`, `zig`) must be a real value re-verified from its upstream checksum or signature at implementation time; do not copy a checksum from this plan and do not invent one. A dependency that cannot be pinned is dropped from the build with the affected modules moved to `modules.json` exclusions and a reason — never satisfied from the host.

`build_loader(work, zig_dir)` does not copy a loader from the host. The compatibility trap is real: the spike found the distro loader happens to work, but it is not the artefact this repository ships and its digest differs from the one PHP builds. Build it from `runtimes/php/config/musl.json` directly — that file is the pin, which is why `config/sources.json` has no `musl_loader` key at all: musl 1.2.5, `a9a118bbe84d8764da0ea0d28b3ab3fae8477fc7e4085d90102b8596fc7c75e4` — by calling `runtimes/php/scripts/musl_loader.py`'s `toolchain_musl_version()` and `build()`: compile musl as a shared loader with `zig cc` and record its digest and version. Load the PHP module by path with `importlib.util.spec_from_file_location` rather than reimplementing it, so the two runtimes ship a loader built the same way. `musl_loader.build` takes the PHP runtime directory as its first argument because it reads `<php_dir>/config/musl.json`; pass `REPO_ROOT/runtimes/php`.

One requirement follows from that choice and must be asserted in `build_loader`: the loader's musl version must equal Zig's bundled musl version (the PHP module already checks this with `toolchain_musl_version`). A mismatch means the loader and the compiler disagree about libc ABI, which produces a bundle that runs until the first `dlopen`.

- [ ] **Step 1: Write the failing tests**

```python
def test_require_names_the_first_missing_tool(self):
    with self.assertRaisesRegex(ValueError, "definitely-not-installed"):
        self.toolchain.require(["definitely-not-installed"])

def test_configure_flags_always_disable_rpath_and_select_musl_safe_options(self):
    flags = self.toolchain.configure_flags(self.options)
    self.assertIn("--disable-rpath", flags)
    self.assertIn("--with-ssl=openssl", flags)
    self.assertNotIn("--enable-rpath", flags)
    # Exactly one ICU decision, and it is `--with-icu` unless a recorded
    # fallback changed the config file itself.
    self.assertEqual(len([f for f in flags if "icu" in f]), 1)
    self.assertEqual(len(flags), len(set(flags)))

def test_build_environment_keeps_musl_headers_first(self):
    env = self.toolchain.build_env(Path("/zig"), Path("/deps"))
    self.assertEqual(env["CC"], "/zig/zig cc -target x86_64-linux-musl")
    self.assertEqual(env["CXX"], "/zig/zig c++ -target x86_64-linux-musl")
    self.assertIn("-idirafter /usr/include", env["CPPFLAGS"])
    self.assertNotIn("-I/usr/include", env["CPPFLAGS"])
    self.assertTrue(env["PKG_CONFIG_PATH"].startswith("/deps"))
    self.assertEqual(env["PKG_CONFIG_LIBDIR"], env["PKG_CONFIG_PATH"])

def test_zig_pin_is_checked_before_any_configure(self):
    # mismatched declared version must raise, not warn
    ...

def test_uuid_and_libxml_select_the_vendored_libraries(self):
    flags = self.toolchain.configure_flags(self.options)
    self.assertIn("--with-uuid=e2fs", flags)
    self.assertIn("--with-libxml", flags)
    self.assertNotIn("--with-uuid=bsd", flags)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Expected: FAIL — `toolchain.py` missing.

- [ ] **Step 3: Implement `scripts/toolchain.py`**

`prepare_zig(work, pins)` is what makes the toolchain fresh rather than ambient: it fetches `pins["zig"]` with the same verified `curl` invocation, checks the digest, extracts into `$work/zig`, and returns the single directory inside that holds the `zig` executable (`zig-x86_64-linux-0.17.1/`). Never use a `zig` found on `PATH`; the whole point of the pin is that the compiler is an input to the build, not a host property. `zig(zig_dir, options)` then runs `<zig>/zig version` and raises `ValueError` unless it equals `options["zig"]["version"]`, and `musl_version` must equal `options["zig"]["musl_version"]` — a Zig upgrade that changes its bundled musl is exactly the event the loader comparison in `build_loader` exists to catch.

`build_env` returns a complete environment, not a partial one: `PATH`, `LC_ALL=C`, `SOURCE_DATE_EPOCH=0`, `CC`, `CXX`, `CPPFLAGS`, `CFLAGS=-O2 -fPIC`, `LDFLAGS`, `PKG_CONFIG_PATH`, `PKG_CONFIG_LIBDIR`. `LDFLAGS` carries `-L<deps>/lib` so the link step resolves the vendored OpenSSL, and `PKG_CONFIG_LIBDIR` points at `<deps>/lib/pkgconfig` with `PKG_CONFIG_PATH` set to the same only, so `pkg-config` cannot fall back to `/usr`. There is no `-Wl,-rpath-link`: OpenSSL at runtime is found through the launcher's `--library-path`, which is set at exec time, not at link time. `CC` and `CXX` are absolute paths to `zig` followed by `cc`/`c++ -target x86_64-linux-musl`, so a build that runs from a cleaned environment still finds the pinned compiler.

`build_dependencies(work, pins, zig_dir)` builds, in dependency order (zlib → OpenSSL → libxml2 → ICU → util-linux): zlib, libxml2 and ICU with `--enable-static --disable-shared` (ICU via its own `runConfigureICU`/`configure` under `source/`); OpenSSL with `shared` and `no-tests no-apps`; util-linux with `--disable-all-programs --enable-libuuid --enable-static --disable-shared`. ICU is the only dependency whose build may fail for toolchain reasons; catch that failure specifically and surface it as a one-line, actionable message naming `zig c++` — Step 5 needs it to make the fallback decision. Every archive is fetched with the standard `curl --fail --location --proto =https --proto-redir =https`, digest-verified before extraction, and built into `<work>/deps`; an already-populated prefix is a failure, not a reuse. OpenSSL is the one dependency configured with an explicit `--libdir=<deps>/lib` so its `.so.N` files land where Task 5 can find them; the others install through their own `--prefix`.

`configure_flags(options)` returns the static list above; `build.sh` appends `--with-includes="$deps/include" --with-libraries="$deps/lib"` at configure time, since the dependency prefix is only known then. Without those two flags the vendored OpenSSL/zlib/libxml2/libuuid/ICU are invisible to `configure` and it either fails or silently falls back to a host copy — there is no PATH-based fallback that would be correct. ICU also needs `PKG_CONFIG_PATH` to find `icu-i18n.pc`, which its own build installs into `<deps>/lib/pkgconfig`.

`write_provenance(path, data)` writes `$work/toolchain.json`, which is the `provenance` argument Task 5's `stage` consumes. It records the Zig version and digest, the loader record from `build_loader`, and for each dependency its pinned version, digest, whether it was built shared or static, and its license file path — plus, for ICU, the `with_icu`/`without_icu` decision and the reason. Task 5 stages these into the manifest as `libraries`, and Task 9's release notes report them.

- [ ] **Step 4: Replace the stub body of `scripts/build.sh`**

Keep the Task 1 argument handling and platform check. Then, in order: resolve `RUNTIME_DIR`/`REPO_ROOT`; refuse an existing `$REPO_ROOT/dist/postgresql/$version/musl-dynamic`; create `.cache/postgresql` and a fresh `mktemp -d "$REPO_ROOT/.cache/postgresql/build.XXXXXXXX"`; call `toolchain.require` with `make bison flex perl curl readelf strip tar bzip2`; call `toolchain.prepare_zig "$work"` to materialise the pinned compiler; call `source.py --download "$work" "$version"`; call `toolchain.build_dependencies`; call `toolchain.build_loader`; write `$work/toolchain.json` with `toolchain.write_provenance`; configure out of tree into `$work/build` with `--prefix=/usr/local/pgsql` (the compiled prefix is irrelevant — relocation comes from `--argv0`, and keeping the default means `bindir` ends in `/bin`); `make -j "$jobs" world-bin`; `make install-world-bin DESTDIR="$work/inst"`. `jobs` honours `BUILD_JOBS` (default 2) with the same positive-integer validation Redis uses.

The `toolchain` calls are `python3 -I` subcommand invocations of `scripts/toolchain.py`, the way `bundle.py` is called in Redis's `build.sh`, so each returns its own result on stdout and a failure exits non-zero before the next stage. Give `toolchain.py` an `argparse` with `prepare-zig`, `build-dependencies`, `build-loader` and `write-provenance` subcommands, plus `--work`, `--zig-dir` and `--output` as needed. `require` is a plain function the subcommands call first (`build-dependencies` asserts the C/C++ toolchain, `build-loader` asserts the loader's own needs), so a missing host tool fails in the same non-zero way.

Print `PostgreSQL build workspace: <work>` before doing work, exactly like Redis, so a failure is diagnosable.

- [ ] **Step 5: Run the tests and the first real build**

Run: `python3 -I -m unittest tests.test_postgresql -v` — PASS.
Then run `./build.sh postgresql 18.6` on a host with Zig and the listed tools. Expected: every dependency builds, the build reaches `make install-world-bin`, and it produces `$work/inst/usr/local/pgsql/bin/postgres`.

Two outcomes are both acceptable here, and the difference must be recorded:

- ICU builds with `zig c++` → keep `--with-icu`.
- ICU fails → change `configure.shared` to `--without-icu`, note the reason in `build-options.json`, and re-run. Modules are unaffected (none of the listed modules needs ICU); only ICU collations become unavailable, and the docs say so.

The script still exits non-zero at the staging step, which Task 5 adds; record the exact failure line rather than working around it.

- [ ] **Step 6: Commit**

```bash
git add runtimes/postgresql/config/build-options.json runtimes/postgresql/scripts/toolchain.py runtimes/postgresql/scripts/build.sh tests/test_postgresql.py
git commit -m "feat(postgresql): build pinned dependencies and the server with zig cc"
```

---

### Task 5: Staging and the manifest

**Files:**
- Create: `runtimes/postgresql/scripts/bundle.py`
- Modify: `runtimes/postgresql/scripts/build.sh`
- Test: `tests/test_postgresql.py`

**Interfaces:**
- Consumes: `launchers.install`/`launchers.LOADER` (Task 2), `modules.inventory`/`modules.load_expected` (Task 3), `source` (Task 1), `toolchain` (Task 4).
- Produces: `bundle.stage(install_root: Path, output: Path, request: dict, provenance: dict, repo_root: Path) -> dict` returning the manifest; `bundle.digest(path) -> str`; `bundle.parse_elf(header: str, program: str, dynamic: str, versions: str) -> dict`; `bundle.elf(path: Path) -> dict` (runs `readelf` and delegates to `parse_elf`) with keys `interpreter`, `needed`, `rpath`, `runpath`, `glibc_required`, `machine`; `bundle.binary_version(binary: Path, loader: Path, library_path: str) -> str`; `bundle.require_version(binary: Path, version: str, loader: Path, library_path: str) -> None`. `provenance` is the parsed `$work/toolchain.json` from Task 4. `stage` reads `provenance["deps_prefix"]` (the dependency install prefix), `provenance["loader"]` (`path`, `sha256`, `musl_version`) and `provenance["dependencies"]` (the per-dependency records); those three keys are the Task 4 → Task 5 contract. Later tasks call `bundle.verify(bundle) -> dict` and `bundle.package(bundle, output) -> Path`.

Manifest identity fields (later consumed verbatim by the publisher): `schema_version: 1`, `runtime: "postgresql"`, `target: "musl-dynamic"`, `architecture: "x86_64"`, `linkage: "dynamic"`, `build_id`, `postgresql: {version, major}`, `verification: "NOT_RUN"`, and `loader: {"path": "lib/musl/ld-musl-x86_64.so.1", "sha256": ..., "musl_version": ...}`. The publisher asserts `manifest["loader"]["path"]` and `manifest["linkage"]`, so both keys are part of the contract from this task onward.

- [ ] **Step 1: Write the failing tests**

```python
def test_elf_contract_requires_dynamic_musl_without_rpath(self):
    row = self.bundle.parse_elf(
        "Class: ELF64\nMachine: Advanced Micro Devices X86-64\n",
        "[Requesting program interpreter: /lib/ld-musl-x86_64.so.1]",
        " (NEEDED)  Shared library: [libpq.so.5]\n (NEEDED)  Shared library: [ld-musl-x86_64.so.1]",
        "")
    self.assertEqual(row["needed"], ["libpq.so.5", "ld-musl-x86_64.so.1"])
    self.assertIsNone(row["rpath"])
    self.assertIsNone(row["runpath"])
    self.assertIsNone(row["glibc_required"])

def test_elf_contract_rejects_rpath_glibc_and_static(self):
    for header, program, dynamic, versions in [
        (elf64, progs, " (RPATH)  Library rpath: [/usr/lib]", ""),
        (elf64, progs, " (RUNPATH)  Library runpath: [/opt/lib]", ""),
        (elf64, progs, "", "GLIBC_2.34"),
        (elf64, "", "", ""),                       # static: no interpreter
        ("Class: ELF64\nMachine: AArch64\n", progs, "", ""),
    ]:
        with self.subTest(dynamic=dynamic), self.assertRaises(ValueError):
            self.bundle.parse_elf(header, program, dynamic, versions)

def test_binary_version_runs_through_the_loader(self):
    # a stub script printing "postgres (PostgreSQL) 18.6"; run via a stub loader
    # that execs its last-permitted argument, so this test does not need musl
    self.assertEqual(self.bundle.binary_version(stub, loader, "/deps/lib"), "18.6")
    with self.assertRaises(ValueError):
        self.bundle.require_version(stub, "17.11", loader, "/deps/lib")

def test_stage_builds_the_documented_layout(self):
    manifest = self.bundle.stage(install, output, request, provenance, repo_root)
    for relative in ("bin/postgres", "bin/initdb", "bin/pg_ctl", "bin/psql",
                     "libexec/bin/postgres", "lib/musl/ld-musl-x86_64.so.1",
                     "share/postgres.bki", "share/licenses/builder.txt",
                     "share/sources/postgresql-18.6.tar.bz2", "manifest.json"):
        self.assertTrue((output / relative).exists(), relative)
    self.assertEqual(manifest["runtime"], "postgresql")
    self.assertEqual(manifest["postgresql"], {"version": "18.6", "major": "18"})
    self.assertEqual(manifest["target"], "musl-dynamic")
    self.assertNotIn("profile", manifest)
    self.assertEqual(manifest["verification"], "NOT_RUN")

def test_stage_refuses_an_existing_output_and_a_wrong_patch(self):
    ...

def test_manifest_records_modules_and_locale_contract(self):
    manifest = self.bundle.stage(...)
    kinds = {row["kind"] for row in manifest["modules"]["included"]}
    self.assertEqual(kinds, {"sql", "preload", "shared", "cli"})
    self.assertEqual(manifest["locale"],
                     {"default_provider": "builtin", "default_locale": "C.UTF-8",
                      "alternate": "PG_UNICODE_FAST"})
    self.assertIn("pg_overexplain", [r["name"] for r in manifest["modules"]["included"]])
```

The last assertion holds for `major == "18"` only; write the test to read the manifest's major and compare against `modules.load_expected(major)` instead of hard-coding a module name for both majors.

- [ ] **Step 2: Run the tests and confirm they fail**

Expected: FAIL — `bundle.py` missing.

- [ ] **Step 3: Implement `parse_elf`, `elf`, `binary_version`, `require_version`**

Follow `runtimes/redis/scripts/bundle.py`: run `readelf -hW -lW -dW -VW` with `LC_ALL=C`, then parse. The PostgreSQL contract inverts Redis's static requirement — require an `INTERP` of `/lib/ld-musl-x86_64.so.1`, require at least one `NEEDED`, and require **no** `RPATH`/`RUNPATH` and no `GLIBC_*` token. `binary_version` matches `r"\(PostgreSQL\) ([0-9]+\.[0-9]+)"` from `<binary> --version` output, so `postgres --version`, `psql --version` and `pg_config --version` all parse.

**These binaries cannot be executed directly at staging time.** They are raw musl ELFs whose `PT_INTERP` is `/lib/ld-musl-x86_64.so.1`, and the build host is not required to have one — a direct `exec` fails with `ENOENT` on exactly the host this bundle is meant for. So `binary_version(binary, loader, library_path)` and `require_version(binary, version, loader, library_path)` run `<loader> --library-path <library_path> <binary> --version`, with `library_path` as `<deps>/lib:<loader's directory>`. That is the same mechanism the launcher will use, exercised one step earlier, and it is why version checking belongs before `launchers.install` rather than after.

- [ ] **Step 4: Implement `stage`**

Order matters, and each early exit avoids creating `output`:

1. Run `require_version(install_root / "bin/<cmd>", request["version"], provenance["loader"]["path"], <deps>/lib:<loader dir>)` for every command `launchers.commands(install_root)` returns — before `output` exists, and through the loader, per the note above.
2. `output.mkdir(parents=True, exist_ok=False)`.
3. Copy the install tree (`bin/`, `lib/`, `share/`, `include/`) preserving modes. Do not expect or create `etc/`: `install-world-bin` places `postgresql.conf.sample`, `pg_hba.conf.sample`, `pg_ident.conf.sample`, `pg_service.conf.sample` and `psqlrc.sample` in `share/`, and `initdb` reads them from `$sharedir`. `share/doc/` (24 KB of HTML) is the one subtree dropped, to keep the archive lean; everything else in `share/` is required at runtime (`postgres.bki`, `*.sql`, `timezone/`, `timezonesets/`, `tsearch_data/`, `extension/`).
4. Copy the vendored shared dependency libraries into `lib/`: every `libssl.so*` and `libcrypto.so*` from the dependency prefix that `provenance["deps_prefix"]` names (Task 4's `write_provenance` records it, because `stage`'s signature has no other way to learn it), preserving the `so.N` names and the `.so` symlinks that `NEEDED` entries reference (`libcrypto.so.3`, `libssl.so.3`). zlib, libxml2 and libuuid were built static and are already inside the binaries and modules, so nothing else is copied. A `NEEDED` entry that names a library not present in `lib/` after this step is a staging failure — check it here rather than at verify time.
5. Copy the loader that `provenance["loader"]["path"]` names, produced by `toolchain.build_loader`, to `lib/musl/ld-musl-x86_64.so.1` and `chmod 0o755`; assert its digest still equals `provenance["loader"]["sha256"]`. Never copy `/lib/ld-musl-x86_64.so.1` from the host: the distro loader happens to work, but the artefact that ships must be the one this repository builds, and its digest goes in the manifest's `loader` record.
6. `launchers.install(output)`.
7. Stage `packaging/launch.sh` to `libexec/launch.sh`; stage `LICENSE` to `share/licenses/builder.txt`; stage upstream `COPYRIGHT` to `share/licenses/postgresql.txt`; stage the musl `COPYRIGHT` to `share/licenses/musl.txt`; stage the per-dependency licence files to `share/licenses/<dependency>.txt`; stage the authenticated PostgreSQL archive to `share/sources/`.
8. Re-verify each staged archive digest against the request (an archive that changed after the build is a failure, exactly as Redis does).
9. Compute `build_id` as the first 16 hex characters of SHA-256 over `json.dumps(inventory, sort_keys=True) + json.dumps(provenance, sort_keys=True) + builder_hash`, where `builder_hash` folds every file under `runtimes/postgresql/` — the same construction Redis uses.
10. Write the manifest. Its keys are fixed from this task onward, because Tasks 6, 8 and 9 read them:

```json
{
  "schema_version": 1, "runtime": "postgresql",
  "postgresql": {"version": "18.6", "major": "18"},
  "target": "musl-dynamic", "architecture": "x86_64", "linkage": "dynamic",
  "build_id": "<16 hex>", "verification": "NOT_RUN",
  "loader": {"path": "lib/musl/ld-musl-x86_64.so.1", "sha256": "...", "musl_version": "1.2.5"},
  "locale": {"default_provider": "builtin", "default_locale": "C.UTF-8", "alternate": "PG_UNICODE_FAST"},
  "libraries": [{"name": "openssl", "version": "...", "sha256": "...", "linkage": "shared", "license": "share/licenses/openssl.txt"}, ...],
  "modules": {"included": [{"name": "hstore", "kind": "sql"}], "skipped": [{"name": "plperl", "reason": "..."}]},
  "binaries": [{"path": "libexec/bin/postgres", "sha256": "...", "interpreter": "...", "needed": ["..."], "rpath": null, "runpath": null, "glibc_required": null, "machine": "..."}],
  "source_archive": "share/sources/postgresql-18.6.tar.bz2",
  "licenses": {"builder": "share/licenses/builder.txt", "postgresql": "share/licenses/postgresql.txt", "musl": "share/licenses/musl.txt"},
  "provenance": {"upstream": {"url": "...", "sha256": "..."}, "configure_flags": ["..."], "compiler": "zig cc ...", "zig": {...}},
  "builder": {"commit": "<git rev-parse HEAD>", "source_sha256": "<builder_hash>"}
}
```

Fill `modules.included` from `modules.inventory(output)` and `skipped` from `modules.EXCLUSIONS`, and fail if `modules.compare` reports a **missing** expected module (an absent module is a build failure; an unexpected extra module is recorded in `included`, not fatal). `libraries` is `provenance["dependencies"]` reshaped, and `binaries` covers every file under `libexec/bin/` after `launchers.install` — the raw ELFs, not the launcher scripts, which are covered by `require_version` and by Task 6's executable checks.

`lib/musl` doubling as the library search directory is not repeated here: the loader lives at `lib/musl/ld-musl-x86_64.so.1` and the launcher's `--library-path` lists `lib:lib/musl`.

- [ ] **Step 5: Wire `stage` into `build.sh`**

After `install-world-bin`, call `python3 -I "$RUNTIME_DIR/scripts/bundle.py" stage "$work/inst/usr/local/pgsql" "$OUT" "$work/request.json" "$work/toolchain.json" "$REPO_ROOT"` and print `Staged bundle: $OUT`. `toolchain.json` is written by `toolchain.write_provenance` in Task 4; assert it exists here before staging, so a Task 4 that skipped the write fails with a clear message rather than a `FileNotFoundError` from deep inside `stage`.

- [ ] **Step 6: Run the tests and a staged build**

Run: `python3 -I -m unittest tests.test_postgresql -v` — PASS.
Then `./build.sh postgresql 18.6` on the build host. Expected: `dist/postgresql/18.6/musl-dynamic/` exists, `bin/psql --version` prints `psql (PostgreSQL) 18.6` through the launcher, and `pg_config --sharedir` reports a path inside that bundle.

- [ ] **Step 7: Commit**

```bash
git add runtimes/postgresql/scripts/bundle.py runtimes/postgresql/scripts/build.sh tests/test_postgresql.py
git commit -m "feat(postgresql): stage a launcher-wrapped bundle with a per-major manifest"
```

---

### Task 6: Verification — real cluster lifecycle, every module, no host musl

**Files:**
- Modify: `runtimes/postgresql/scripts/bundle.py`
- Modify: `runtimes/postgresql/scripts/build.sh`
- Test: `tests/test_postgresql.py`

**Interfaces:**
- Consumes: `bundle.stage`, `modules` (Task 3).
- Produces: `bundle.verify(bundle: Path) -> dict` returning `{"checks": list[str], "strategy": str}` and leaving `verification: "PASSED"` plus `verification_strategy: <strategy>` in `manifest.json` only on success; `bundle.checks(manifest: dict) -> list[dict]` (pure) returning the planned checks as `{"kind", "name", "statement"}` rows; `bundle.smoke(root: Path, manifest: dict) -> list[str]` executing those plans against a running cluster and returning one check name per exercised module and tool; `bundle.lifecycle(root: Path, manifest: dict, preload: list[str]) -> None`; `bundle.assert_no_host_loaders() -> str` returning the masking strategy it used.

`checks` is separate from `smoke` so the plan is testable without a server: it is a pure function of the manifest's module list.

- [ ] **Step 1: Write the failing tests**

```python
def test_verify_rejects_a_tampered_binary_before_smoking(self):
    # flip a byte in bin/libexec/bin/postgres; verify() must raise and must
    # reset verification to NOT_RUN in the manifest
    ...

def test_verify_refuses_to_report_passed_for_an_unsupported_locale_request(self):
    # manifest locale provider must be "builtin"; a manifest declaring "libc"
    # is rejected, because musl accepts vi_VN.UTF-8 and collates as C
    ...

def test_host_loader_masking_is_detected_or_reported(self):
    # assert_no_host_loaders() must return a named strategy and must never
    # silently fall back to "no isolation available"
    ...

def test_check_plan_covers_every_included_kind(self):
    checks = self.bundle.checks(manifest)      # a fixture manifest, no cluster
    for kind in ("sql", "preload", "cli"):
        self.assertTrue(any(row["kind"] == kind for row in checks), kind)
    self.assertEqual({row["name"] for row in checks if row["kind"] == "sql"},
                     {row["name"] for row in manifest["modules"]["included"] if row["kind"] == "sql"})

def test_a_sql_module_without_a_statement_is_an_error_not_a_silent_skip(self):
    manifest["modules"]["included"].append({"name": "brand_new", "kind": "sql"})
    with self.assertRaisesRegex(ValueError, "brand_new"):
        self.bundle.checks(manifest)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Expected: FAIL on the new symbols.

- [ ] **Step 3: Implement `lifecycle` and `smoke`**

The whole point of this task: the smoke test must be a real server, not `--version` calls. `lifecycle(root, manifest, preload)` does items 1, 2 and 8–9 and calls `smoke` for items 3–7; `smoke(root, manifest)` returns the list of check names. `checks(manifest)` is the pure half: a module name → SQL statement table in the module, plus a `ValueError` naming any `sql` module it has no statement for, which is what stops "as many extensions as possible" from quietly becoming "as many as the table remembered". `PRELOAD` and `CLI` rows carry their type as the statement. Order:

1. `initdb -D "$root/PostgreSQL <version> data" -U verify --encoding=UTF8 --locale-provider=builtin --builtin-locale=C.UTF-8 --no-instructions` — the version comes from the manifest, and the directory name contains a space on purpose.
2. `pg_ctl -D <data> -l <log> -w -o "-c listen_addresses='' -c unix_socket_directories=<sock> -c shared_preload_libraries=<preload list>" start`. The preload list is every `preload` module in the manifest, comma-separated; a failure here is exactly the "built but unloadable" case Review Focus item 4 is about, so the failure message must quote the server log tail.
3. `psql -h <sock> -U verify -d postgres -tAc "SELECT version()"` and assert the output contains `PostgreSQL <version>`.
4. `DO $$ BEGIN RAISE NOTICE 'ok'; END $$` — proves `plpgsql` loads.
5. One `CREATE EXTENSION` plus one real query per `sql` module, from the `checks` table. Write each statement from the module's own `share/extension/<name>--*.sql` — its first exported function, type or operator. Keep these as the named proof-of-linkage cases, because each one also proves a vendored library actually linked:
   - `hstore`: `'a=>1'::hstore->'a'` returns `1`
   - `cube`: `cube_distance('(0,0)'::cube, '(3,4)'::cube)` returns `5`
   - `citext`: `'ABC'::citext = 'abc'` returns true
   - `pg_trgm`: `similarity('abc','abd') > 0` returns true
   - `ltree`: `'a.b'::ltree <@ 'a'::ltree` returns true
   - `pgcrypto`: `encode(digest('x','sha256'),'hex')` — proves the **shared** OpenSSL links through the launcher
   - `uuid-ossp`: `uuid_generate_v4()` returns a UUID — proves the **static** libuuid linked
   - `xml2`: `xpath('//a', '<a>1</a>')` returns `{1}` — proves the **static** libxml2 linked
   - `amcheck`, `pageinspect`, `pgstattuple`, `pg_visibility`: create one small table, then call the module's function on it
   - `pg_stat_statements`: a row count from its view, after the extension loads

   Every `sql` module in the manifest must have an entry, and a missing entry is a `ValueError` naming it. There is no "`CREATE EXTENSION` alone" fallback, because that fallback is precisely how a module with a broken function gets recorded as verified.
6. Round-trip the remaining local tools against the running cluster: `createdb`, `pg_dump`, `dropdb`, `createdb` again, `pg_restore` the dump, `vacuumdb --analyze`, `pg_isready`, `reindexdb`, `clusterdb`. Every one of them must exit 0 through its launcher. `pg_upgrade` and `pg_basebackup` are **not** run — they need a second cluster and their own lifecycle; the design keeps cross-major migration out of scope.
7. If ICU is enabled (the manifest's `libraries` records whether it is), `CREATE COLLATION verify_icu (provider = icu, locale = 'en-US')` then assert `SELECT 'B' < 'a' COLLATE verify_icu` is true while the same comparison under the database's `C.UTF-8` collation is false. That single assertion distinguishes "ICU is linked and working" from "ICU is linked but silently falls back". If ICU is not enabled, assert the `CREATE COLLATION ... provider = icu` statement **fails** with a clear message, so the absence is a checked fact rather than an untested possibility.
8. `pg_ctl -w stop -m fast`.
9. Assert the server log contains no `could not load` and no `incomplete installation`.

Always stop the server in a `finally`, on success and on failure, exactly as Redis does. Use a Unix socket only; never bind TCP. Note in a comment that `background worker "logical replication launcher" ... exited with exit code 1` is normal upstream behaviour and is not an error — the spike confirmed the distro glibc build logs it too, so a future reader does not "fix" it.

- [ ] **Step 4: Implement `assert_no_host_loaders` and the relocation check**

Resolve host-independence without requiring root. Try, in order:

1. `bwrap` — `--dev-bind / / --ro-bind <empty> /lib/ld-musl-x86_64.so.1 --ro-bind <empty> /usr/lib/ld-musl-x86_64.so.1 --ro-bind <emptydir> /usr/x86_64-linux-musl/lib64`. This is the strategy the spike validated: it keeps `uid != 0` (so `initdb` accepts it) while making every host loader a zero-byte file.
2. If `bwrap` is absent, run the lifecycle normally but additionally assert that the bundle's own loader is reachable through the launcher's `--library-path` by pointing `--library-path` at a directory containing a zero-byte `ld-musl-x86_64.so.1` **first** and requiring the command to fail — the poison-file probe. Record which strategy ran in the returned string and in the manifest `verification_strategy`.
3. If neither can run, **fail**. Never report `PASSED` without one of these; that is the whole reason this task exists.

Relocation: `copytree` the bundle to a sibling directory named `PostgreSQL <version> #relocated`, then repeat `lifecycle` there. The `#` is not decoration: an unquoted expansion that reaches a shell would truncate the path at it, and the launcher's `"$ROOT/..."` quoting is what this test measures. `pg_config --bindir`, `--sharedir`, `--libdir`, `--pkglibdir` must all report paths under the relocated copy.

- [ ] **Step 5: Implement `verify`**

Reset `verification` to `NOT_RUN` and rewrite the manifest first, exactly as Redis does, so a crash mid-verification cannot leave `PASSED` on disk. Then, in order: manifest schema and identity; every `binaries` row's digest and ELF contract; every `bin/<command>` is a regular executable file and not a symlink; the loader is present, executable and its digest matches the manifest; `assert_no_host_loaders`; `lifecycle` in the staged location; relocation plus `lifecycle` and `pg_config`; per-kind module checks; module inventory compared against `modules.load_expected(major)` with zero missing. Set `verification: "PASSED"` and write the manifest only after all of it returns.

- [ ] **Step 6: Wire `verify` into `build.sh`**

`python3 -I "$RUNTIME_DIR/scripts/bundle.py" verify "$OUT"` after staging. A non-zero exit must leave the staged tree in place for inspection and must not package.

- [ ] **Step 7: Run the tests and the first verified build**

Run: `python3 -I -m unittest tests.test_postgresql -v` — PASS.
Then `./build.sh postgresql 18.6`. Expected: the build prints the check list and `manifest.json` ends at `"verification": "PASSED"`. Manually confirm the bwrap path was used (`verification_strategy` in the manifest names it), because that is the failure mode this task exists to catch.

- [ ] **Step 8: Commit**

```bash
git add runtimes/postgresql/scripts/bundle.py runtimes/postgresql/scripts/build.sh tests/test_postgresql.py
git commit -m "feat(postgresql): verify a real cluster with no host musl loader"
```

---

### Task 7: Package, then prove 17.11

**Files:**
- Modify: `runtimes/postgresql/scripts/bundle.py`
- Modify: `runtimes/postgresql/scripts/build.sh`
- Test: `tests/test_postgresql.py`

**Interfaces:**
- Consumes: `bundle.verify`.
- Produces: `bundle.package(bundle: Path, output: Path) -> Path`, naming assets `postgresql-<version>-musl-dynamic-x86_64-<build_id>.tar.gz` plus `.sha256` and `.manifest.json`, refusing to replace any of the three.

- [ ] **Step 1: Write the failing tests**

```python
def test_packaging_requires_verified_manifest_and_names_the_asset(self):
    # verification != PASSED -> ValueError; after PASSED -> archive
    #   postgresql-18.6-musl-dynamic-x86_64-<id>.tar.gz
    ...

def test_packaging_preserves_executable_modes_and_launchers(self):
    with tarfile.open(archive) as tar:
        psql = tar.getmember(root + "/bin/psql")
        self.assertEqual(psql.mode & 0o777, 0o755)
        loader = tar.getmember(root + "/lib/musl/ld-musl-x86_64.so.1")
        self.assertEqual(loader.mode & 0o777, 0o755)

def test_packaging_never_overwrites_a_sidecar_or_archive(self):
    ...

def test_archive_identity_parts_are_filesystem_safe(self):
    for part in ("18.6", "musl-dynamic", "x86_64", "abc123"):
        self.assertRegex(part, r"^[a-zA-Z0-9_.-]+$")
```

- [ ] **Step 2: Run the tests and confirm they fail**

Expected: FAIL — `package` missing.

- [ ] **Step 3: Implement `package`**

Copy `runtimes/redis/scripts/bundle.py::package` and adapt the identity: require `runtime == "postgresql"` and `verification == "PASSED"`, build the name from `postgresql["version"]`, `target`, `architecture`, `build_id`, reserve all three paths with `xb`/`x` opens and unwind only what was created. Do not change the tar call — the existing `tarfile.open(fileobj=stream, mode="w:gz"); tar.add(bundle, arcname=name)` behaviour preserves modes and relative paths.

- [ ] **Step 4: Wire `package` into `build.sh` and run the full pipeline for 18.6**

Append the `package` call and print `Verified bundle: $OUT` like Redis. Then run `./build.sh postgresql 18.6` and inspect the produced archive: extract it into a fresh directory and confirm `bin/psql --version` works from the extracted copy after deleting nothing on the host.

- [ ] **Step 5: Run the full pipeline for 17.11 and reconcile the module lists**

Run `./build.sh postgresql 17.11`. Expected: a second bundle under `dist/postgresql/17.11/musl-dynamic/` whose manifest has `major: "17"` and whose inventory has no `pg_overexplain`.

Then reconcile. Add `modules.py --generate <install_root> <major>` and run it against both staged install trees, diffing the result against the committed `config/modules.json`. `shared` is expected to differ freely (it is generated); `sql`, `preload` and `cli` must match, and **every** difference must be either (a) recorded as an exclusion with a reason, or (b) accompanied by a change to Task 4's configure flags that explains it. Update `config/modules.json` from the reconciliation and re-run both builds. A difference that is neither explained nor excluded is a defect in the build, not in the file.

- [ ] **Step 6: Commit**

```bash
git add runtimes/postgresql tests/test_postgresql.py
git commit -m "feat(postgresql): package verified bundles for 17.11 and 18.6"
```

---

### Task 8: Planner integration

**Files:**
- Modify: `scripts/updates.py`
- Modify: `tests/test_runtime_updates.py`

**Interfaces:**
- Consumes: `source.load_source`, `source.CONFIG` (Task 1); `php.validate_repository`, `php.pages`, `php.positive_id`, `php.gh_json` (existing).
- Produces: `published_postgresql(repository) -> dict[tuple[str, str, str], str]` keyed `(version, target, architecture)`; `make_plan` returns an additional `postgres_matrix` key shaped `{"include": [{"postgresql_version", "target", "architecture", "previous_version"}]}`; `write_outputs` emits `postgres_matrix`, `has_postgres_updates`, and includes it in `has_updates`/`publish`.

- [ ] **Step 1: Write the failing tests**

```python
def test_manual_bypass_builds_every_pinned_major(self):
    plan = self.planner.make_plan(event="workflow_dispatch", ..., bypass=True)
    versions = {row["postgresql_version"] for row in plan["postgres_matrix"]["include"]}
    self.assertEqual(versions, {"18.6", "17.11"})
    self.assertTrue(all(row["target"] == "musl-dynamic" for row in plan["postgres_matrix"]["include"]))

def test_scheduled_run_builds_only_missing_pinned_versions(self):
    # history already has 18.6 -> only 17.11 is selected
    ...

def test_postgres_history_rejects_incomplete_or_unverified_asset_groups(self):
    # mirror test_redis_history_rejects_incomplete_or_unverified_asset_groups
    ...

def test_postgres_manifest_from_another_major_is_not_trusted(self):
    # a 17.x manifest must not satisfy an 18.x plan row
    ...

def test_outputs_expose_three_matrices_and_gates(self):
    self.assertIn("postgres_matrix=", text)
    self.assertIn("has_postgres_updates=", text)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `python3 -I -m unittest tests.test_runtime_updates -v`
Expected: FAIL — `postgres_matrix` missing from the plan.

- [ ] **Step 3: Implement `published_postgresql`**

Model on `published_redis`, with these differences: asset roots match `r"postgresql-([0-9]+\.[0-9]+)-musl-dynamic-x86_64-([A-Za-z0-9_.-]+)"`; the manifest must be `schema_version == 1`, `runtime == "postgresql"`, `verification == "PASSED"`, `linkage == "dynamic"`, and must carry `postgresql.version` equal to the version in the asset name and `target == "musl-dynamic"`. A manifest whose `postgresql.major` disagrees with the version's leading component is an error, not a skip.

- [ ] **Step 4: Extend `make_plan` and `write_outputs`**

Load the module with the same `importlib` helper at the top of `scripts/updates.py` (`postgresql = load(ROOT / "runtimes/postgresql/scripts/source.py", "postgresql_source")`). Build rows from `postgresql.CONFIG`'s pinned versions with the **same selection rule Redis uses**, not a new one: `previous = history.get((version, target, "x86_64"))` and append the row `if bypass or previous is None`. So a scheduled or default manual run selects only pins that are missing or not yet published, and only an explicit bypass rebuilds every pinned version — that is what `test_scheduled_run_builds_only_missing_pinned_versions` pins. Call `postgresql.load_source(version)` for every pinned version first, so an unpinned source is never planned. Then set `policy["postgres_matrix"]`, extend `has_updates` to a three-way `or`, keep the `publish = has_updates and (scheduled or publish)` line, update the closing `print` to report all three counts, and add the new keys to `write_outputs`'s `fields` dict.

Then update the two existing tests this breaks, and treat their failures as the signal that the wiring is real:

- `tests/test_runtime_updates.py`'s `make()` helper patches `published_redis`; without a matching `patch.object(self.module, "published_postgresql", return_value=...)` every existing case in that file would call `gh` for real. Add the patch and a `postgres_current` parameter alongside `php_current`/`redis_current`.
- The counts in `test_scheduled_noops_are_independent_per_runtime` gain a third column; the assertion becomes `bool(php_count + redis_count + postgres_count)`.
- `test_outputs_always_expose_both_matrices_and_runtime_gates` becomes `..._three_matrices_...` and asserts the third key.

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `python3 -I -m unittest tests.test_runtime_updates tests.test_updates -v`
Expected: PASS, including the pre-existing PHP and Redis tests.

- [ ] **Step 6: Commit**

```bash
git add scripts/updates.py tests/test_runtime_updates.py
git commit -m "feat: plan pinned postgresql bundles alongside php and redis"
```

---

### Task 9: Publisher and workflow

**Files:**
- Modify: `runtimes/php/scripts/publish.py`
- Modify: `.github/workflows/build.yml`
- Modify: `tests/test_publish.py`
- Modify: `tests/test_workflow.py`

**Interfaces:**
- Consumes: the manifest identity from Task 5 and the plan shape from Task 8.
- Produces: `validate_assets` accepts `runtime == "postgresql"`; `validate_plan` accepts `postgres_matrix`; `release_notes` emits a PostgreSQL section; the workflow gains `build-postgresql` and extends the `publish` gate.

- [ ] **Step 1: Write the failing tests**

```python
def test_assets_accept_only_postgresql_identity_and_required_commands(self):
    # runtime=postgresql, target=musl-dynamic, linkage=dynamic, no profile,
    # loader at lib/musl/ld-musl-x86_64.so.1, and these files executable:
    # bin/{postgres,initdb,pg_ctl,psql,pg_dump,pg_config}
    ...

def test_assets_reject_a_postgresql_bundle_without_the_loader(self):
    ...

def test_plan_accepts_three_runtime_matrices_and_requires_exact_coverage(self):
    ...

def test_notes_describe_the_locale_contract_and_every_built_module(self):
    # notes must state builtin/C.UTF-8 and must not claim glibc ordering
    ...

def test_workflow_builds_postgresql_with_explicit_ci_dependencies(self):
    text = WORKFLOW.read_text()
    job = job_block(text, "build-postgresql")   # helper: from "  build-postgresql:" to the next top-level job
    for package in ("bison", "flex", "perl", "build-essential", "binutils", "curl"):
        self.assertIn(package, job, package)
    # Zig and every library archive come from the build script, digest-verified.
    self.assertNotIn("musl-tools", job)
    self.assertIn("${{ matrix.postgresql_version }}", job)
    self.assertIn("if-no-files-found: error", job)

def test_publish_gate_requires_every_selected_runtime_to_succeed(self):
    gate = job_block(WORKFLOW.read_text(), "publish")
    for runtime in ("php", "redis", "postgresql"):
        self.assertIn(f"has_{runtime}_updates", gate, runtime)
    self.assertIn("build-postgresql", gate, "publish must depend on the postgresql build")
```

`job_block` is a five-line helper that slices the workflow at the indented job key and stops at the next line matching `^  [a-z][a-z0-9-]*:$`. Slicing by `split("build-", 1)` would cut on `build-essential` and assert against the wrong region.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `python3 -I -m unittest tests.test_publish tests.test_workflow -v`
Expected: FAIL on the new runtime.

- [ ] **Step 3: Extend `validate_assets`**

Add an `elif runtime == "postgresql":` branch beside the `php`/`redis` ones: `parts = [manifest["postgresql"]["version"], manifest["target"], manifest["architecture"], manifest["build_id"]]`; `valid = re.fullmatch(r"1[78]\.[0-9]+", parts[0]) and parts[2] == "x86_64" and manifest.get("linkage") == "dynamic" and "profile" not in manifest and isinstance(manifest.get("loader"), dict) and manifest["loader"].get("path") == "lib/musl/ld-musl-x86_64.so.1"`; `executables = ("bin/postgres", "bin/initdb", "bin/pg_ctl", "bin/psql", "bin/pg_dump", "bin/pg_config")`; and require the loader member present, non-empty and executable, reusing the PHP loader check. The existing `runtime + "-" + "-".join(parts) != root` assertion then enforces the asset name for free.

- [ ] **Step 4: Extend `validate_plan` and `release_notes`**

`validate_plan` reads `plan.get("postgres_matrix", {"include": []})`, validates each row (`postgresql_version` matching `1[78]\.[0-9]+` and present in the pinned config, `target == "musl-dynamic"`, `architecture == "x86_64"`, no `profile`, `previous_version` equal or absent), adds `("postgresql", version, target, architecture)` to `expected`, and accepts the same identity from manifests. The `matrix["include"] or redis_matrix["include"]` emptiness check becomes a three-way or.

`release_notes` gains a PostgreSQL section: a version/target/architecture/build-id table, the module inventory grouped by kind with the excluded set and its reasons, and a short **Locale** paragraph stating that the bundle uses the `builtin` provider with `C.UTF-8` (18.x also `PG_UNICODE_FAST`) and therefore gives deterministic code-point ordering rather than glibc ordering. That paragraph is not optional documentation — it is the disclosure that makes the silent-musl-collation finding a stated property instead of a hidden defect.

- [ ] **Step 5: Add the `build-postgresql` job**

Mirror `build-redis`: `if: needs.plan.outputs.has_postgres_updates == 'true'`, `needs: [plan, contracts]`, `runs-on: ubuntu-24.04`, `matrix: ${{ fromJson(needs.plan.outputs.postgres_matrix) }}`. Install `bison flex perl build-essential binutils curl` explicitly and nothing else from apt — Zig, OpenSSL, zlib, libxml2 and util-linux are fetched and digest-verified by the build script itself. Run `./build.sh postgresql "$BUILD_VERSION"` with `BUILD_VERSION: ${{ matrix.postgresql_version }}`, upload `dist/assets/` as `runtime-postgresql-${{ matrix.postgresql_version }}-musl-dynamic-x86_64` with `if-no-files-found: error`. Extend `publish`'s `if:` with the same success-or-skipped clause used for PHP and Redis — `((needs.plan.outputs.has_postgres_updates == 'true' && needs.build-postgresql.result == 'success') || (needs.plan.outputs.has_postgres_updates == 'false' && needs.build-postgresql.result == 'skipped'))` — and add `build-postgresql` to its `needs:`.

`publish.py`'s `validate_assets` reads every `dist/assets/` file, and the `publish` job downloads `pattern: runtime-*` with `merge-multiple: true`, so no workflow change is needed there. Note that `validate_plan`'s existing guard `not (matrix["include"] or redis_matrix["include"])` sits on the *same* line as the `redis_matrix` type check; split it so a `postgres_matrix`-only plan is valid — otherwise a run with only a PostgreSQL update is rejected as an empty matrix.

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `python3 -I -m unittest tests.test_publish tests.test_workflow -v`
Expected: PASS, including every existing PHP and Redis assertion.

- [ ] **Step 7: Commit**

```bash
git add runtimes/php/scripts/publish.py .github/workflows/build.yml tests/test_publish.py tests/test_workflow.py
git commit -m "feat: publish and build postgresql bundles in the shared pipeline"
```

---

### Task 10: Documentation and the contract sweep

**Files:**
- Create: `docs/runtimes/postgresql.md`
- Create: `runtimes/postgresql/README.md`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `.github/workflows/build.yml` (the `contracts` job's `bash -n` line)

**Interfaces:**
- Consumes: everything above.
- Produces: no code.

- [ ] **Step 1: Write `docs/runtimes/postgresql.md`**

Cover, in the shape of `docs/runtimes/redis.md`: scope (server bundles, 17.x and 18.x, musl-dynamic, no profiles, no libc selector); build dependencies and the exact pinned inputs; **which dependency is shared and which is static, and why** (OpenSSL shared for its process-global state, zlib/libxml2/libuuid/ICU static); **ICU's status as recorded in the manifest** — enabled by default via `zig c++`, and if the build recorded the `--without-icu` fallback, state that ICU collations are unavailable and `builtin`/`C.UTF-8` is the only option, rather than writing either outcome unconditionally; the output layout **including where the `*.sample` configs live (`share/`, not `etc/`)**; the launcher contract **including both `--library-path` entries and the `--argv0` reason**; the module taxonomy and where `config/modules.json` comes from; the locale contract with the musl `setlocale` finding stated plainly; verification (cluster lifecycle, relocation with spaces, no-host-musl strategy, per-kind module exercise); the asset names and never-overwrite rule; and the licensing note (the PostgreSQL License, plus OpenSSL's Apache-2.0, zlib's Zlib, libxml2's MIT, util-linux's LGPL-2.1-or-later, ICU's Unicode-3.0 and musl's MIT).

- [ ] **Step 2: Write `runtimes/postgresql/README.md`**

The module-level view: which scripts exist, what each does, the `./build.sh postgresql [version] [--print-plan]` interface, and the one-line warning that `bin/` launchers are mandatory because a raw `libexec/` ELF needs a host musl interpreter.

- [ ] **Step 3: Update `README.md` and `docs/architecture.md`**

`README.md`: add PostgreSQL to the runtime list and the usage block, add a `postgresql` line to the bundle-layout example, state the locale contract in one sentence, and correct the "two runtimes" phrasing where it appears. `docs/architecture.md`: add `runtimes/postgresql` to the repository-layout tree, add a bullet under Supported Runtimes, and extend the "Adding New Runtimes" note only if this work changed the pattern (it did not — say nothing rather than restating).

- [ ] **Step 4: Extend the contracts job's syntax checks**

In `.github/workflows/build.yml`'s `contracts` job, add `runtimes/postgresql/scripts/*.sh` to the `bash -n` list and `runtimes/postgresql/packaging/*` to the `sh -n` loop, matching how PHP's packaging scripts are checked.

- [ ] **Step 5: Run the complete suite and every syntax check**

```bash
python3 -I -m unittest discover -s tests -v
bash -n build.sh scripts/build.sh runtimes/php/scripts/*.sh runtimes/redis/scripts/*.sh runtimes/postgresql/scripts/*.sh
for script in runtimes/php/packaging/* runtimes/postgresql/packaging/*; do sh -n "$script"; done
```
Expected: all pass, zero failures.

- [ ] **Step 6: Confirm the two bundles still build end to end**

Run `./build.sh postgresql --print-plan`, then a clean `./build.sh postgresql 18.6` and `./build.sh postgresql 17.11` with the previous `dist/postgresql` entries removed. Expected: both reach `verification: PASSED` and produce an archive plus both sidecars.

- [ ] **Step 7: Commit**

```bash
git add README.md docs/architecture.md docs/runtimes/postgresql.md runtimes/postgresql/README.md .github/workflows/build.yml
git commit -m "docs: document the postgresql runtime contract and locale behaviour"
```

---

## Notes for the implementer

- The cached `.cache/repro/work/source/postgresql/` tree is an SPC-patched 18.6 that deletes the shared-library build rules for PHP's static `libpq`. Never build the server from it; always start from the freshly verified archive.
- `background worker "logical replication launcher" ... exited with exit code 1` in the server log is normal. The distro glibc 18.6 build logs it identically.
- If OpenSSL pinning turns out to be expensive, dropping `--with-ssl` is a legitimate release decision, but it must be a recorded exclusion in `config/modules.json` and in the release notes — not a silent build option. It would also cost `pgcrypto` and `sslinfo`, and server-side TLS.
- Task 9 touches `runtimes/php/scripts/publish.py` and the shared workflow. That file is shared with the PHP runtime by design; keep the PHP and Redis branches passing unchanged.
- The spike directory `.cache/pg-spike/` (about 534 MB) is throwaway. Delete it once the real build passes; nothing in this plan reads it.
