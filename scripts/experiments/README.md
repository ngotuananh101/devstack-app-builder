# PHP Musl Dynamic Experiment

This is a **throwaway feasibility PoC**, not a supported runtime target or a publication pipeline. It does not change the main PHP builder, workflow, planner, publisher, or existing distributions.

## Scope

- Linux x86_64, PHP **8.4.27**, CLI and FPM.
- Built-in `session`; shared **APCu 5.1.28** and **phpredis 6.3.0**.
- Pinned StaticPHP v3 source from the repository's SPC lock.
- Private Composer **2.8.12** and Zig **0.17.1**.
- An existing build-host musl loader; the staged bundle copies and records that loader.
- Four compiler jobs by default (`SPC_CONCURRENCY=4`).

The experiment downloads tools to a fresh `.cache/php-musl-dynamic.*` workspace. It never invokes sudo, a host package manager, Docker, or doctor auto-fix. It uses an isolated PHP command wrapper to load already-installed host modules without changing the system PHP configuration.

## Run

From the repository root:

```bash
bash scripts/experiments/php-musl-dynamic.sh run
```

The first line prints `EXPERIMENT_WORKSPACE=...`. Keep that exact path. Downloads, sources, output and logs are retained after failures.

After a failure, inspect `console.log`, `log/spc.output.log` and `log/spc.shell.log` before retrying:

```bash
bash scripts/experiments/php-musl-dynamic.sh resume .cache/php-musl-dynamic.EXAMPLE
```

A partially failed initial tool preparation has no `experiment.json`; start a new workspace rather than reusing incomplete downloads. `resume` is for a successfully prepared workspace. Do not run multiple builds in the same workspace simultaneously.

After a successful build/staging, run the checks again without rebuilding:

```bash
bash scripts/experiments/php-musl-dynamic.sh verify .cache/php-musl-dynamic.EXAMPLE
```

The baseline result is `results.json`; the bundle is under `bundle/`. When the existing `bwrap` tool is available, the verifier also writes `results-no-system-musl.json` after repeating the runtime checks with the host musl loader and development tree masked in a private, non-root namespace. A raw-binary negative control must fail while the bundle launchers succeed. No system files are changed. Without `bwrap`, this second check is explicitly marked `SKIPPED`; it is never silently treated as a pass.

Do not copy this bundle into `dist/assets` or publish it as a verified production runtime.

## Input policy

- SPC source, upstream Composer lock and production Composer dependencies use the repository's existing digest gates.
- Composer and Zig are SHA-256 checked before execution/extraction.
- PHP 8.4.27 source is checked against its official php.net tar.xz digest before build extraction.
- The extension ZIPs are checked for the expected version, commit-bearing ZIP root/comment and unsafe members before extraction.
- Every downloaded build archive, including compiler-rt, is inspected and SHA-256 frozen before build extraction. Already-frozen bytes cannot silently change during resume.
- Craft runs with `download: false`; a missing input stops the build rather than fetching it during compilation.

**Limit:** a digest computed after download is not independent publisher authentication. Extension ZIP commit comments do not prove authenticity by themselves. Compiler-rt comes from SPC's configured mirror, not a separately authenticated LLVM distribution. These inputs are suitable for this explicitly authorized PoC, not production certification.

## Checks

The verifier uses the staged loader and bundle-local library path, not a direct raw-binary call. It checks:

- Musl dynamic ELF interpreter and `NEEDED` closure limited to bundled libc.
- No glibc symbol requirements or RPATH/RUNPATH.
- Disabled shared modules under `php -n` and by default.
- CLI/FPM module toggles and separate INI directories.
- Actual non-root FPM FastCGI requests.
- Relocation to a directory containing spaces and isolation from the original installation.
- APCu store/fetch and availability of the Redis class.
- `/proc/self/maps` contains no host shared objects for the relocated PHP process.

With `bwrap`, it then repeats those runtime checks inside a user namespace where `/lib/ld-musl-x86_64.so.1` and `/usr/x86_64-linux-musl` are hidden. The raw `libexec/php` must fail and both bundle launchers must load the copied loader.

A pass does **not** establish: full-profile builds, PHP 8.2–8.5 compatibility, Alpine compatibility, behaviour on a host with no usable user namespaces, DNS/TLS behavior, Redis client/server interoperability, or a locked portable loader/toolchain ABI. Those require subsequent experiments.
