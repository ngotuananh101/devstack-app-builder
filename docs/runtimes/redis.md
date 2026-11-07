# Redis source builds

## Scope

Native Linux x86_64 Redis core builds, **fully static musl only**, without profiles or Docker. The libc target is not selectable. This module only builds, verifies and packages binaries; it does not configure DevStack, install a service, create a user or run a persistent server.

```bash
./build.sh redis                     # defaults: 8.10.3, musl
./build.sh redis 8.10.3
./build.sh redis --print-plan
```

Arguments accept at most one pinned exact version. Unknown options, `glibc`/`musl` target arguments, profiles, duplicate arguments and unpinned versions fail before downloading. `config/sources.json` records official Redis archive SHA-256 values for 8.10.3, 7.4.12 and 7.2.17. Updating a version requires adding and reviewing its upstream checksum first.

## Build dependencies

- Bash, Python 3, curl, GNU make and binutils (`ar`, `ranlib`, `readelf`, `strip`).
- `musl-gcc` with musl headers/static libc.
- HTTPS access to `download.redis.io`.

Dependencies are checked, never automatically installed. `REDIS_CC` selects an already installed compiler executable (default `musl-gcc`); `BUILD_JOBS` selects a positive parallel job count (default 2). Static musl builds require their installed libc copyright notice; common distro locations are detected, or `REDIS_MUSL_LICENSE` can supply the exact file. It is hashed and bundled as `share/licenses/musl.txt`. A compiled libc probe rejects glibc headers, checks ELF linkage and execution before source download. Redis and bundled dependency builds occur under fresh `.cache/redis/build.*` directories. Source downloads and extraction live in separate new directories; Python runs with `-I`.

Build flags explicitly select bundled jemalloc, `-O2`, and disable systemd, TLS and optional replication compression. These are **core binaries, not a Redis Stack/full module distribution**. No OpenSSL dependency or optional profile is introduced.

## Output contract

```text
dist/redis/<exact-version>/musl/
  bin/redis-server
  bin/redis-cli
  bin/redis-benchmark
  bin/redis-sentinel -> redis-server
  bin/redis-check-rdb -> redis-server
  bin/redis-check-aof -> redis-server
  share/licenses/builder.txt
  share/licenses/redis/...
  share/sources/redis-<exact-version>.tar.gz
  manifest.json
```

Binaries must be fully static x86_64 ELF: no program interpreter, no `NEEDED` entries and no GLIBC symbol versions. Any dynamic linkage, `NEEDED` entry or `GLIBC_*` token fails verification. `manifest.libc` is `{"family": "musl", "minimum_version": null, "static": true}`.

The manifest records actual Redis patch, allocator, libc, binary hashes/dependencies, compiler details/digest, builder revision/source digest, authenticated upstream archive and verification status. The source archive is included for distribution/license review. System toolchain packages are not reproducibly pinned by this module; a recorded compiler version is not a complete reproducible-toolchain guarantee.

Verification checks all executable versions and binary ELF contracts, relocates a copy to a path with spaces, and runs a temporary local Unix-socket server for PING, SET/GET, benchmark and RDB/AOF checker tests. It does not expose a TCP port. The server is stopped on success and failure. Sentinel's executable/version is checked; a multi-node Sentinel/cluster deployment is outside this module's scope.

Only verified bundles produce assets:

```text
dist/assets/redis-<version>-<target>-x86_64-<build-id>.tar.gz
            redis-<version>-<target>-x86_64-<build-id>.tar.gz.sha256
            redis-<version>-<target>-x86_64-<build-id>.manifest.json
```

Archives preserve executable modes and relative aliases. Existing bundle/asset sets are never replaced; remove or move a prior output explicitly before rebuilding the same version/target. Downloaded archives should be verified against their checksum before extraction into a fresh directory.

## Licensing and automation

Redis 7.2 uses BSD-3-Clause; Redis 7.4 offers RSALv2 or SSPLv1; Redis 8 offers RSALv2, SSPLv1 or AGPLv3. Retain the source distribution and upstream/dependency notices and review the chosen license's distribution obligations before publishing. The builder's license does not replace Redis licenses.

The shared workflow always covers PHP 8.2–8.5 on `musl-dynamic` and **every pinned Redis version on musl**. Every trigger, scheduled or manual, first compares against published bundles; the manual-only `bypass` input rebuilds all combinations regardless. Runtime, PHP-series, libc and profile selectors are absent. The Redis matrix uses native `musl-gcc` on Ubuntu 24.04 and explicitly installs `build-essential`, `binutils`, `curl` and `musl-tools` on the disposable CI runner. Local build scripts still never install packages.

Daily planning at 21:37 UTC / 04:37 Vietnam time checks paginated public `runtimes-...` releases. A Redis exact-version/target/architecture combination is current only when its schema-v1 `PASSED` manifest has matching uploaded archive/checksum companions. Drafts and prereleases do not count. Malformed relevant metadata/API failures stop planning. Redis builds only missing pinned combinations, independently of PHP updates; this is **not automatic upstream Redis version discovery**. The manual-only `bypass` input skips the comparison and rebuilds every pinned version. New Redis releases must first be pinned with a reviewed official SHA-256.

The persisted `runtime-update-plan.json` records both runtime matrices. Publication checks the exact PHP/Redis asset set, builder commit, checksums, manifests, executable modes and the three safe Redis aliases before creating a draft. Every selected build must succeed; empty runtime jobs may be skipped. All assets upload before promotion to a public immutable Release. Scheduled updates publish automatically; manual publication remains opt-in with `publish=true` and a new `runtimes-...` tag. Redis release notes claim only the fully static musl contract recorded in the manifest. CI run `42384737455` has since built and published Redis 7.2.17, 7.4.12 and 8.10.3 in Release `runtimes-20261107-musl-c95d643`.

```bash
python3 -I -m unittest discover -s tests -v
bash -n runtimes/redis/scripts/build.sh
```
