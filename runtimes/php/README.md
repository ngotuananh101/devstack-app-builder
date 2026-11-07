# PHP Runtime for DevStack

Build configuration and packaging for Linux x86_64 PHP **8.5, 8.4 (default), 8.3, 8.2**, one target: `musl-dynamic`.

See [PHP runtime architecture and consumer contract](../../docs/runtimes/php.md) for bootstrap integrity, installed launchers, FPM configuration, manifest fields, tests and public release downloads.

```bash
./runtimes/php/scripts/download-spc.sh
./runtimes/php/spc doctor --auto-fix=never --no-interaction
./build.sh php 8.4
./build.sh php 8.3.10
```

There is no `minimal`/`full` profile and no libc selector, so every build produces the same extension set:

- static baseline: `config/static-extensions.txt`
- shared modules: `config/shared-extensions.txt` (20 modules, packaged as `.so`, disabled by default in both SAPIs)

SPC is always invoked with `SPC_TARGET='x86_64-linux-musl -dynamic'`. The bundle ships the musl loader built from the pinned release in `config/musl.json`; the loader is also `libc.so`, so the archive needs no host libc or interpreter. `bin/php` and `bin/php-fpm` exec through that loader with `--library-path "$ROOT/lib/musl"`.

Outputs: `dist/php/<requested-version>/musl-dynamic/` and versioned tar.gz/checksum/manifest assets under `dist/assets/`. Existing outputs are never overwritten. Consumer commands live in `bin/php`, `bin/php-fpm`, `bin/php-ext`; raw binaries are in `libexec/`. FPM runs non-root with explicit caller-owned config.

Bootstrap uses a verified immutable SPC v3 archive and verified Composer dependency bytes. It then applies the reviewed exact-text patches in `config/spc-source-patches.json`, which pin the whole-file SHA-256 before and after so upstream drift fails instead of being rewritten; the current patches prevent duplicate Zig `clang_rt.crtbegin/crtend` objects and carry the resolved non-PHP static library closure onto final phpize module links. The build also exports `PHP_RPATH=no` to prevent absolute shared-module RUNPATHs. It needs build-host PHP/Composer/Python and a host `cc` for the loader, not consumer dependencies. Never use plain `spc doctor` as a read-only command: it can auto-fix with a default-yes prompt; use `--auto-fix=never`.

`SPC_DOWNLOAD_PARALLEL` (default 8) and `SPC_DOWNLOAD_RETRY` (default 3) tune only how SPC fetches archives; lower the parallelism on networks that drop concurrent connections. `DOWNLOAD_PATH` may point at an already verified download cache, which SPC re-checks against its lock before use.

Every run builds only exact stable patches newer than the compatible public `musl-dynamic` Release manifest, or combinations with no published bundle; a manual run behaves the same unless it sets the manual-only `bypass` input, which rebuilds every series regardless. Scheduled updates automatically publish a complete draft-first Release after all selected builds verify; English notes come from actual manifests. Manual runs still default to `publish=false`. The `runtime-releases` environment and immutable-tag/no-clobber policy are retained. API failures stop the check; no-update runs skip build/publication.

Native local PHP **8.4.27** passed build, full CLI/FPM verification, fresh archive extraction and functional probes for all **20 shared modules** on **2026-11-07**. CI run **42384737455** then built, verified and published **8.2.35, 8.3.36, 8.4.27 and 8.5.12** as Release `runtimes-20261107-musl-c95d643`; see [local validation evidence](../../docs/runtimes/php.md#local-validation-evidence). Production input-provenance and legal gates remain open.
