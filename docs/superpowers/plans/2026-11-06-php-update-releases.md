# PHP Update Releases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build only new/missing stable PHP bundles on schedule, automatically publish complete verified releases, and generate factual notes.

**Architecture:** A standard-library discovery/planning script resolves exact patches and inspects paginated public release manifests. The workflow persists a matrix plan, builds only selected rows, and passes that plan to a draft-first publisher. Manual dispatch remains a forced rebuild.

**Tech Stack:** Bash, Python >= 3.12 standard library, GitHub Actions, existing `gh` CLI.

**Spec:** `docs/superpowers/specs/2026-11-06-php-update-releases-design.md`

## Global Constraints

- Supported stable series: `8.2`, `8.3`, `8.4`, `8.5`; scheduled profile `full`; targets `glibc`, `musl`; architecture `x86_64`.
- Remote JSON is data, never executed. Python reading downloaded data runs with `-I`.
- Compare numeric versions independently for `(PHP series, target, profile, architecture)`; public `runtimes-...` history is paginated; drafts/prereleases are ignored.
- API/network/schema errors fail closed. Empty plans skip build/publication. Immutable releases are never overwritten.
- Preserve `runtime-releases`, least privileges, manual publication opt-in, pinned actions and SPC v3.
- All selected builds must pass before publication. Assets must match the persisted exact-patch plan and builder commit.
- English technical files/notes; Vietnamese conversation. Scope is builder only.
- Commit/push/merge require separate authorization; the user granted integration to `main`, push and cleanup on 2026-11-06 after review. No CI dispatch or current-session Release publication was requested.
- Preserve and document existing production blockers; no real SPC builds or publication as verification.

## Review Focus

- More than one page of releases/assets, or a newest release covering only one target: scan the complete compatible history (Task 1 tests).
- Draft/prerelease/incomplete/malformed runtime assets and API errors: never turn them into a false published baseline or false no-release condition (Task 1 tests).
- A new php.net patch appearing between planning and building: the selected exact patch must flow through craft and publication validation (Tasks 1 and 3 tests).
- Partial/mismatched uploads or a concurrent/retried run: public releases must not advertise incomplete assets or overwrite immutable tags (Task 2 tests).
- An empty include matrix and skipped dependency jobs: scheduled no-op must skip build/publish without invalid matrix expansion (Task 3 tests).

## File Structure

- Create `runtimes/php/scripts/updates.py`: upstream/public discovery, numeric selection, validated plan and Actions outputs.
- Create `tests/test_updates.py`: deterministic planning/API boundary regressions.
- Modify `runtimes/php/scripts/publish.py`: retain validator; add plan matching, factual notes, draft/upload/promote lifecycle.
- Create `tests/test_publish.py`: package fixtures and controlled `gh` boundary tests.
- Modify `.github/workflows/build.yml`: concurrency, plan artifact, conditional include matrix, scheduled publication.
- Modify `tests/test_workflow.py`: retain archive contracts; replace obsolete inline matrix assertions with new workflow integration assertions.
- Modify `README.md`, `docs/runtimes/php.md`, `runtimes/php/README.md`: current automation/notes and unchanged production caveats.

### Task 1: Exact-patch update discovery

**Files:** Create `runtimes/php/scripts/updates.py`, `tests/test_updates.py`.

**Interfaces:**
- Produces `version_tuple(version: str) -> tuple[int, int, int]` for stable supported versions.
- Produces `latest_php(series: str) -> str`, using php.net's fixed release API and validating the returned series/stable source archive checksum metadata.
- Produces `published_versions(repository: str) -> dict[tuple[str, str, str, str], str]`, keyed by series/target/profile/architecture, using paginated `gh api` metadata and bounded manifest assets.
- Produces `plan_updates(latest: dict[str, str], published: dict, target: str, profile: str, force: bool = False) -> list[dict]`; row keys: `php_series`, `php_version`, `target`, `profile`, `architecture`, `previous_version`.
- Produces `make_plan(...) -> dict` with `schema_version:1`, `event`, `repository`, `commit`, `run_id`, `run_attempt`, `release_tag`, `publish`, `has_updates`, `matrix:{include:[rows]}`; CLI writes JSON and one-line GITHUB_OUTPUT values.

- [x] Write tests: `test_numeric_comparison_and_independent_combinations` (`8.4.10 > 8.4.9`, equal/older skipped, missing musl selected); `test_manual_forces_exact_patch_matrix`; `test_public_history_uses_all_pages_and_companions`; `test_drafts_prereleases_and_unrelated_assets_do_not_count`; `test_invalid_manifest_or_api_error_fails_closed`; `test_upstream_version_and_source_metadata_are_validated`; `test_empty_plan_and_automatic_tag_outputs`.
- [x] Run `python3 -I -m unittest discover -s tests -p test_updates.py -v`; expected RED missing implementation, not fixture errors.
- [x] Implement the interfaces and CLI with validated repository/run/tag inputs, UTC date, fixed API endpoints, response bounds, stable version validation and paginated history. Manual false-publish does not need release discovery.
- [x] Run focused tests, then `python3 -I -m unittest discover -s tests -v`; expected GREEN.
- [x] Record evidence in ledger. Defer commit.

### Task 2: Complete draft-first releases and notes

**Files:** Modify `runtimes/php/scripts/publish.py`; create `tests/test_publish.py`.

**Interfaces:**
- Consumes Task 1 plan schema. Keep `validate_assets(directory)`'s existing return list and archive safeguards.
- Produces `validate_plan(plan: dict, manifests: list[dict], tag: str, repository: str, commit: str) -> None`, exact identity-set and builder-commit matching.
- Produces `release_notes(manifests: list[dict], repository: str, commit: str, plan: dict | None = None) -> str`.
- Extends `publish(directory, tag, repository, commit, plan=None)` and CLI `--plan PATH`; no-plan callers remain supported.

- [x] Write tests: `test_notes_describe_actual_bundles_and_workflow`; `test_plan_rejects_missing_extra_wrong_patch_or_commit`; `test_publish_creates_draft_uploads_then_promotes`; `test_upload_failure_leaves_draft`; `test_existing_tag_or_lookup_failure_prevents_publication`. Build fixtures through the existing packager and mock only `gh` subprocess I/O.
- [x] Run `python3 -I -m unittest discover -s tests -p test_publish.py -v`; expected RED for missing notes/plan/lifecycle behavior.
- [x] Implement plan matching and factual notes. Validate before creating anything; keep public-repository/duplicate-tag checks and inspect paginated release history including drafts. Create a draft with `gh api` REST POST, capture its returned release ID, upload assets through the fixed release-ID upload endpoint, then promote that same ID with REST PATCH; never resolve mutations by tag, clobber assets or automatically delete failed drafts.
- [x] Run focused tests and full suite; expected GREEN.
- [x] Record evidence in ledger. Defer commit.

### Task 3: Scheduled workflow and documentation

**Files:** Modify `.github/workflows/build.yml`, `tests/test_workflow.py`, `README.md`, `docs/runtimes/php.md`, `runtimes/php/README.md`.

**Interfaces:**
- Consumes Task 1 CLI JSON/GITHUB_OUTPUT: matrix, has_updates, publish, release_tag.
- Consumes Task 2 CLI `publish.py ... --plan dist/plans/php-update-plan.json`.
- Plan artifact name `runtime-update-plan`; bundle artifact pattern remains `php-*` and uses exact patch identifiers.

- [x] Replace inline-plan regression with a controlled plan invocation asserting scheduled default full, automatic publication, exact-version include matrix, manual minimal/false-publish behavior, and empty-matrix guards. Add tests for persisted plan download, least-privilege publish condition and non-canceling concurrency.
- [x] Run `python3 -I -m unittest discover -s tests -p test_workflow.py -v`; expected RED against the old workflow.
- [x] Wire checkout/discovery before matrix, serialize runs, persist plan on updates, gate builds on has_updates, download plan and verified bundles, and enable schedule or explicit manual publication via planner output. Preserve environment/pins. Update active docs and production caveats.
- [x] Run full suite; run `bash -n build.sh scripts/build.sh runtimes/php/scripts/*.sh`, packaging `sh -n`, YAML parse, `git diff --check`; expected GREEN without real network/build/publication.
- [x] Record evidence and request one fresh whole-branch read-only review. Re-grade findings by effect; fix verified blocking findings with RED→GREEN and full suite. Integrate to `main`, push and clean up only under the user's separate authorization.

## Self-Review

Coverage: all approved behavior maps to discovery, publication or workflow tasks. Exact signatures and plan fields are shared between tasks; no new source/toolchain gating is implied. External-I/O tests cover pagination, malformed responses and partial uploads. Real SPC/Release operations remain NOT RUN.

## Execution and Review Results

- All three tasks completed with focused RED→GREEN tests and full-suite verification. Final suite: `python3 -I -m unittest discover -s tests -q` — 57 tests passed.
- Bash/POSIX shell syntax, Python/PyYAML workflow parsing and `git diff --check` passed. Ruby was unavailable; no dependency was installed for YAML validation.
- One fresh whole-branch review found a blocking existing-draft collision and two input/schema boundary defects. The latter were re-graded as blocking rather than cosmetic. All three were reproduced and fixed in one RED→GREEN pass; no findings were deferred.
- Regression coverage rejects matching drafts on later history pages, binds uploads/promotion to the newly created release ID, clears unused manual release tags before Actions outputs, and rejects boolean/float schema versions at discovery/plan/archive boundaries.
- Implementation decisions: preserve legacy no-plan publication callers while requiring persisted plans in the workflow; ignore well-formed unsupported series/architecture groups because they cannot satisfy the x86_64 baseline; replace tag-resolved mutations with release-ID REST mutations. Unsupported groups are not audited, legacy callers do not get plan validation, and interrupted uploads leave drafts requiring investigation rather than automatic reuse.
- Commits were deferred until the user separately granted integration to `main`, push and cleanup. No manual CI dispatch, actual SPC build or GitHub Release mutation was performed during implementation.
