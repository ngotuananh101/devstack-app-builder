# All-Runtime Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan inline. No other agents.

**Goal:** Remove runtime, PHP-series and libc selectors; always cover all supported PHP series and pinned Redis releases on glibc and musl.

**Architecture:** A shared planner composes existing PHP planning with pinned Redis planning and records exact publication selections. The workflow runs separate PHP and native Redis jobs; one publisher validates the complete PHP/Redis selection before creating a draft Release. Scheduled runs still skip already published combinations; manual runs rebuild all combinations.

**Tech Stack:** Python standard library, Bash, GitHub Actions, native GCC/musl-gcc.

**Spec:** User request in this session: remove `Runtime to build`, `PHP minor series`, `Target libc`; always build all. Redis has no profile or Docker. Existing PHP profile/publish/tag inputs remain.

## Global Constraints

- PHP series 8.2–8.5, both targets; Redis every pinned exact version in config, both targets, no profiles.
- Keep schedule, serialization, exact-patch planning, fail-closed metadata and draft-first immutable publication.
- CI may explicitly install native build dependencies on its disposable runner; local Redis builds still never install host packages.
- Publish succeeds only if every selected job succeeds; skipped empty runtime matrices are allowed.
- No extra user inputs, unrelated agents, persistent services or Docker.

## Review Focus

- PHP-no-op with new Redis bundles must still build and publish Redis.
- Redis-no-op with new PHP bundles must still publish PHP; both-no-op skips publication.
- Missing/extra/tampered Redis archives or unsafe aliases fail before release creation.
- Redis cannot inherit a PHP profile or PHP glibc 2.17 claim.
- Removed inputs cannot narrow runtime/series/target selections.

### Task 1: Shared planner

**Files:** `scripts/updates.py`, `tests/test_runtime_updates.py`.

- [x] Test all pinned Redis versions × both targets, PHP all series, independent no-op cases, manual force and malformed public metadata; observe failure.
- [x] Compose existing PHP planner with strict Redis history/planning and named output matrices.
- [x] Run focused tests.

### Task 2: Mixed publication contract

**Files:** `runtimes/php/scripts/publish.py`, `tests/test_publish.py`, `tests/test_workflow.py`.

- [x] Test mixed asset sets, Redis aliases, exact planned selection and runtime-specific release notes; observe failure.
- [x] Extend validation and notes without loosening PHP contracts; validate both runtime matrices and builder commit.
- [x] Run focused tests and test actual locally built Redis archives without network publication.

### Task 3: Workflow and docs

**Files:** `.github/workflows/build.yml`, `tests/test_workflow.py`, `README.md`, `docs/runtimes/php.md`, `docs/runtimes/redis.md`.

- [x] Remove three selectors; retain PHP-only profile/publish/tag inputs.
- [x] Add native Redis matrix jobs, explicit CI dependencies, shared persisted plan and safe publication gating/artifact collection.
- [x] Update contract tests to execute shared planner; validate workflow syntax, full suite and unchanged Redis local build CLI.
- [ ] Commit on feature branch, fast-forward/push main per standing request and delete merged feature branch.

## Verification record

- Shared planner: 5/5 focused tests passed.
- Mixed publisher: 10/10 focused tests passed; accepted six real Redis 8.10.3 glibc/musl archive/checksum/manifest assets without publication.
- Workflow: 10/10 tests passed, including independently empty matrices, selected-build failure/cancellation and plan-artifact exclusion.
- Full suite: 85/85 passed; Bash/POSIX syntax, `git diff --check` and native actionlint 1.7.7 passed (shellcheck is not installed).
- Final review is inline, per the user's no-other-agent instruction.
- Additional native Redis 7.4.12/7.2.17 builds passed on both targets: four build/verification/package runs. No GitHub workflow dispatch or Release publication is part of this change.

## Implementation decisions

- The persisted JSON filename is `runtime-update-plan.json`; its Actions artifact is `publication-plan`, so the bundle download pattern `runtime-*` cannot include plan metadata.
- Daily Redis selection covers reviewed pinned releases only; it does not float to an unapproved upstream version.
