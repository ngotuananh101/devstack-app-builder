# Portable PHP Bundles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce relocatable, versioned Linux PHP CLI/FPM bundles with a static baseline and verified optional shared modules, consumable without building locally.

**Architecture:** Keep SPC v3 and both libc targets. Stage real binaries under `libexec`, POSIX launchers under `bin`, per-SAPI INI directories and a module mode registry. CI validates real bundles before packaging; a separate, opt-in publication path creates immutable release assets.

**Tech Stack:** Bash (build host), POSIX sh (installed runtime), Python standard library (build metadata and tests), SPC v3, GitHub Actions.

**Spec:** The implementation request delivered by the DevStack peer on 2026-11-06, acceptance criteria 1–8; summarized in Global Constraints below.

## Global Constraints

- Local changes only; no commits, pushes, workflow dispatches or actual Release publication.
- Stay on SPC v3; verify an immutable upstream input before execution and record provenance.
- `glibc/full`: apcu and redis shared-only; musl: all selected modules static and non-toggleable.
- Keep minimal/full and the scheduled minimal matrix; expose full through manual inputs.
- Installed launchers require no system PHP, Python, sudo or package manager.
- FPM runs as the caller, accepts caller-owned configuration, and never silently launches as root.
- Actual PHP patch, architecture, API, TS/NTS, module modes, input provenance and relative paths belong in JSON metadata.
- Runtime tests must be clearly distinguished from mocks, config validation and source inspections.

## Review Focus

- Installation paths with spaces and unrelated current directories: quoted launch paths and relocation contract tests.
- Static/nonexistent/Zend modules: reject invalid toggles and select the correct INI directive.
- Caller environment and concurrent versions: force bundle-local INI scan/module paths while preserving explicit CLI arguments.
- Missing modules or unresolved ELF libraries: staging/verification fails before packaging/publication.
- Untrusted input and existing releases: validate profile/version/tag and refuse asset overwrite.

---

### Task 1: Installed launchers and module manager

**Files:** Create `runtimes/php/packaging/{php,php-fpm,php-ext,launch.sh}`; replace repository ext helper with delegation; add `tests/test_contracts.py`.

**Interfaces:** `bin/php [args...]`; `DEVSTACK_FPM_CONFIG=/absolute/path bin/php-fpm [args...]` or explicit `-y`; `bin/php-ext {list|enable|disable} [module] [cli|fpm|all]`. Registry `etc/shared-modules.tsv`: module, directive, filename. Real binaries `libexec/php{,-fpm}`.

- [ ] Write failing tests using recorder binaries for arguments/environment, path relocation and spaces, SAPI separation, missing/static modules, Zend directive, root refusal and explicit FPM config.
- [ ] Run `python3 -I -m unittest discover -s tests -v` and confirm the new contracts fail.
- [ ] Implement launchers and helpers; no checkout paths in installed source/config.
- [ ] Run contract tests and POSIX/Bash syntax checks.

### Task 2: Disjoint build inputs, staging, pinned SPC and manifest

**Files:** Modify `runtimes/php/scripts/{build.sh,download-spc.sh}`; create `runtimes/php/scripts/{configure.py,manifest.py}` and toolchain lock data; extend contract tests.

**Interfaces:** Profile resolution emits static/shared CSV with empty intersection; stage `buildroot/modules/<name>.so`; JSON manifest schema version 1; preserve actual binary metadata and immutable SPC provenance.

- [ ] Write failing tests for profile intersections, bad inputs, requested module absence and metadata paths.
- [ ] Confirm tests fail before implementing.
- [ ] Verify v3 reference syntax and immutable bootstrap independently; implement pinned/verified download, disjoint inputs and strict staging.
- [ ] Capture actual binary PHP version, API/TS/build metadata and source/toolchain provenance. Reject shared-only modules appearing under `php -n`.
- [ ] Run all unit/contract tests; do not represent mocked builds as runtime verification.

### Task 3: Runtime verification, archive and CI publication configuration

**Files:** Create `runtimes/php/scripts/{verify.py,package.py}`; modify `.github/workflows/build.yml`; add publication workflow and tests.

**Interfaces:** `verify.py BUNDLE` produces verifiable assertions, including FastCGI requests; `package.py BUNDLE OUTPUT_DIR` produces versioned tar.gz + SHA256. Publication is manual and refuses duplicate tags/assets.

- [ ] Write tests for tar modes, checksum, ELF inventory/baseline policy and FastCGI protocol framing.
- [ ] Run failing tests; implement archive and real-runtime checks (CLI toggles, FPM request, relocation, missing modules, ELF closure).
- [ ] CI runs contracts, pinned SPC build, real verification, archive and uploads archives rather than raw files; least privilege and pinned actions.
- [ ] Configure explicit opt-in release publication only for verified archives, refusing older asset replacement.
- [ ] Run all available tests and report missing FPM/SPC/toolchain tests as NOT RUN.

### Task 4: Documentation and independent review

**Files:** Update `README.md`, `runtimes/php/README.md`, `docs/runtimes/php.md`.

- [ ] Document installed layout, manifest schema, launch/FPM config/module contracts, public asset discovery and checksum validation.
- [ ] Correct examples that imply xdebug/opcache are currently shared; state minimal contains PDO core, no DB drivers.
- [ ] Run full local verification and independent review; address confirmed defects.
- [ ] Send the DevStack peer exact changes/results/remaining limitations; no outward-facing release actions.
