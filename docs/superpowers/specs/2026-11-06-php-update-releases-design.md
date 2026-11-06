# PHP Update Release Design

## Intent

The scheduled workflow should publish new stable PHP bundles only when the upstream patch is newer than the compatible public bundle, or no compatible bundle exists. It should automatically publish verified assets and factual English release notes. Manual runs remain forced rebuilds, with publication an explicit input.

## Approved behavior

- Check PHP stable series `8.2`, `8.3`, `8.4`, `8.5` daily through php.net release metadata. Resolve an exact stable patch before building.
- Compare numeric versions independently for `(PHP series, target, profile, architecture)`. Scheduled builds use `full`, targets `glibc` and `musl`, architecture `x86_64`.
- Inspect every public `runtimes-...` release, with pagination, not only the newest release. Ignore draft/prerelease releases. Use schema-v1 `PASSED` manifest assets with matching archive/checksum companions as the published baseline.
- Build missing combinations or newer patches; skip equal/older versions. Empty plans skip build and publication. API/network/schema errors fail closed, never masquerade as an empty release history.
- Manual dispatch resolves exact stable patches but forces the requested matrix even when already published; `publish=false` remains its default. A manual publication still requires a new `runtimes-...` tag.
- Serialize workflow runs with `cancel-in-progress: false`. Automatic tags use `runtimes-<UTC-date>-<run-id>-<attempt>`; immutable tags/releases are never overwritten.
- Publish only after all selected builds pass. Persist the exact plan as a workflow artifact, and verify that the published manifests exactly match its matrix and builder commit.
- Create a draft, upload the complete asset set, then make it public. An upload failure leaves a draft, not a public baseline. Existing drafts/tags are not reused.
- Generate English release notes from verified manifests: exact versions, previous published patches when known, target/profile/architecture/build IDs, actual static/shared extension information, checksum/CLI/FPM guidance, source commit and workflow links. Do not invent an upstream changelog or claim checks that did not run.

## Boundaries and implementation decisions

- Standard-library Python and the existing `gh` CLI; no new runtime/consumer dependencies.
- `updates.py` owns discovery, validation, numeric comparison and workflow plan outputs. `publish.py` retains archive validation and adds plan matching, factual notes and draft-first publication.
- GitHub metadata is read through fixed repository API endpoints. Manifest asset sizes are bounded and asset IDs/names validated; remote JSON is data, never executed. php.net requests use a fixed HTTPS URL, a timeout and a bounded response.
- Missing/invalid metadata in a relevant public runtime manifest fails the check. Unrelated assets/releases are ignored. This deliberately favors a visible error over duplicate publication.
- Keep the `runtime-releases` environment and least-privilege job permissions. Required reviewers configured later may delay scheduled publication.
- Existing production limitations remain explicit: no claim that source/toolchain provenance gates, the real SPC matrix, oldest-host or distinct-minor checks have been completed. This change configures automation; it does not run a build, dispatch CI or publish during implementation.
- No changes to `/mnt/Data/Ponta/dev-stack`, extension profiles, SPC v3 pinning, launcher/FPM or packaging contracts.
- Local implementation alone does not authorize commits, pushes, merges, workflow dispatches or current-session publication. The user separately authorized merge to `main`, push and cleanup on 2026-11-06 after review; no CI dispatch or current-session Release publication was requested.

## Verification

Tests use pure planning inputs and controlled external-I/O fixtures. They cover numeric comparisons, independent missing targets, public history pagination, drafts/prereleases, unrelated assets, malformed metadata/API failures, exact patch plans, manual rebuilds, empty matrices, plan/artifact mismatches, duplicate tags and upload failures. The full existing suite and shell/YAML syntax checks must remain green. Real SPC builds and real GitHub publication are not run.
