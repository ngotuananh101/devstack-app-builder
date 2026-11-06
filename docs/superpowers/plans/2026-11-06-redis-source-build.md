# Redis Native Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build all Redis core executables from authenticated source with native glibc/musl toolchains, then verify and package them.

**Architecture:** The existing runtime dispatcher invokes a native Bash builder. Pinned Redis sources are authenticated and extracted into a fresh workspace. GNU make uses GCC for glibc or musl-gcc for static musl; Python stages, checks and packages the outputs. No Docker, profiles, dependency installation, or DevStack service configuration.

**Tech Stack:** Bash, Python standard library, GNU make, musl-gcc, binutils.

**Spec:** User explicitly requires native builds, support for glibc and musl (latest correction), no Docker and no profiles. They authorized push to main and cleanup of redundant worktrees/branches and excluded coordination with other agents.

## Global Constraints

- Native Linux x86_64; musl executables fully static, glibc executables report their actual required glibc version.
- Support both glibc and musl (latest user correction); musl remains the default. No profile arguments.
- Default pinned Redis 8.10.3, SHA-256 authenticated before extraction; no extra features/toolchains without approval.
- Include server, CLI, benchmark, Sentinel, RDB/AOF checkers and upstream/dependency license notices.
- Check dependencies only; never auto-install. Never overwrite existing bundles/assets.
- Do not alter PHP builds or add Redis release/schedule automation.

## Review Focus

- Invalid/unpinned versions and removed Docker/glibc/profile options fail before download/build.
- Modified archives fail SHA-256 verification before extraction.
- Archive paths cannot escape the fresh directory; links and special members are rejected.
- Every executable is static x86_64 ELF with no interpreter/NEEDED/GLIBC symbols; musl toolchain is probed before download.
- Relocated binaries must answer actual PING and SET/GET requests through a temporary local socket; test processes are always stopped.

---

### Task 1: Source and native CLI

**Files:** `runtimes/redis/config/sources.json`, `runtimes/redis/scripts/source.py`, `runtimes/redis/scripts/build.sh`, `tests/test_redis.py`.

**Interfaces:** `load_source(version)`, `verify_digest(archive, expected)`, `extract_source(archive, destination)`, `plan(arguments)`; CLI `./build.sh redis [musl|glibc] [exact-version] [--print-plan]`.

- [x] Write strict CLI, checksum, fresh extraction and traversal tests; observe failure.
- [x] Implement source authentication and native dependency/toolchain checks.
- [x] Run focused tests and shell syntax checks.

### Task 2: Verified static Redis bundle

**Files:** `runtimes/redis/scripts/bundle.py`, `tests/test_redis.py`.

**Interfaces:** `stage(source, output, request, provenance, repo_root)`, `verify(bundle)`, `package(bundle, output)`; common manifest identity with `runtime=redis`, `redis.version`, `target=musl|glibc`, no profile.

- [x] Write tests for ELF validation, version mismatch and exclusive packaging; observe failure.
- [x] Compile core with native GCC/musl-gcc, static final links for musl, bundled jemalloc and systemd disabled.
- [x] Stage all six executable entry points, licenses and authenticated build provenance.
- [x] Check versions and ELF contracts; relocate into a path with spaces and run local Redis request smoke tests.
- [x] Run focused tests and complete native source builds/packages for both targets.

### Task 3: Documentation and integration

**Files:** `README.md`, `docs/architecture.md`, `docs/runtimes/redis.md`, `runtimes/redis/README.md`, `scripts/build.sh`, `.gitignore`.

- [x] Leave existing CI and release automation unchanged; no additional CI environment or dependency installation was requested.
- [x] Document exact dependencies, output layout, core/no-TLS scope and licensing.
- [x] Run full tests, syntax and workflow validation; inspect actual archive/checksum.
- [ ] Review inline, commit on the feature branch, fast-forward main and push.
- [ ] Inspect/remove merged clean worktrees/branches and accidental tracked worktree gitlinks; verify main matches origin/main.
