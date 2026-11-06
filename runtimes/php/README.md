# PHP Runtime for DevStack

Build configuration and packaging for Linux x86_64 PHP **8.5, 8.4 (default), 8.3, 8.2**.

See [PHP runtime architecture and consumer contract](../../docs/runtimes/php.md) for bootstrap integrity, installed launchers, FPM configuration, manifest fields, tests and public release downloads.

```bash
./runtimes/php/scripts/download-spc.sh
./runtimes/php/spc doctor --auto-fix=never --no-interaction
./build.sh php glibc full 8.4
./build.sh php musl minimal 8.3
```

`minimal` includes PDO core but no database drivers. `full` includes web/database modules. On glibc/full only `apcu` and `redis` are supported shared-only optional modules, default disabled per SAPI. Musl profiles are fully static/non-toggleable. Opcache remains static; xdebug is not included.

Outputs: `dist/php/<requested-version>/<target>/<profile>/` and versioned tar.gz/checksum/manifest assets under `dist/assets/`. Existing outputs are never overwritten. Consumer commands live in `bin/php`, `bin/php-fpm`, `bin/php-ext`; raw binaries are in `libexec/`. FPM runs non-root with explicit caller-owned config.

Bootstrap uses a verified immutable SPC v3 archive and verified Composer dependency bytes. It needs build-host PHP/Composer/Python, not consumer dependencies. Never use plain `spc doctor` as a read-only command: it can auto-fix with a default-yes prompt; use `--auto-fix=never`.

The new SPC-built CI matrix and public distribution assets have **not yet been validated/published**. Unit contracts and real tests using preinstalled PHP do not replace those checks.
