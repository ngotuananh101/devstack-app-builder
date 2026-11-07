#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$PHP_DIR/../.." && pwd)"
[[ $# -le 1 ]] || { printf '%s\n' 'Expected an optional PHP version argument.' >&2; exit 2; }
PHP_VER="${1:-${PHP_VERSION:-8.4}}"
TARGET="musl-dynamic"
# Validation happens before invoking SPC and before deriving user-controlled paths.
WORK="$(mktemp -d "$PHP_DIR/.build.XXXXXX")"
trap 'rm -rf -- "$WORK"' EXIT
python3 -I "$SCRIPT_DIR/configure.py" "$PHP_DIR" "$PHP_VER" "$WORK/craft.yml" "$WORK/resolution.json"
SPC_BIN="${SPC_BIN:-$PHP_DIR/spc}"
[[ -x "$SPC_BIN" ]] || { printf '%s\n' 'Missing verified SPC. Run runtimes/php/scripts/download-spc.sh first.' >&2; exit 1; }
PROVENANCE="${SPC_PROVENANCE:-$PHP_DIR/.cache/spc-provenance.json}"
[[ -f "$PROVENANCE" ]] || { printf '%s\n' 'Missing verified SPC provenance.' >&2; exit 1; }
# The interpreter is irrelevant here: every launch goes through the bundled loader.
export SPC_TARGET="x86_64-linux-musl -dynamic"
export SPC_CMD_VAR_PHP_EMBED_TYPE=static
# Main PHP uses --disable-rpath, but phpize-generated extension configure scripts
# do not expose that option. Their PHP_RPATH guard must inherit no explicitly,
# otherwise PHP_ADD_LIBRARY_WITH_PATH embeds an absolute buildroot RUNPATH.
export PHP_RPATH=no
# Same host toolchain root as explicit preparation, independent of fresh WORK.
export PKG_ROOT_PATH="${PKG_ROOT_PATH:-$PHP_DIR/.cache/pkgroot/x86_64-linux}"
# Download cache. Defaults to the fresh workspace; a build host on a flaky network can
# point it at an already verified cache to avoid re-fetching hundreds of megabytes.
# SPC re-checks each cached archive against its lock before use, and source_inputs
# records the digests from this same directory, so the override never hides inputs.
export DOWNLOAD_PATH="${DOWNLOAD_PATH:-$WORK/downloads}"
OUT="$REPO_ROOT/dist/php/$PHP_VER/$TARGET"
[[ ! -e "$OUT" ]] || { printf 'Refusing to overwrite bundle: %s\n' "$OUT" >&2; exit 1; }
# A fresh, owned work directory avoids stale modules and cross-version source output.
# It is separate from the downloaded SPC source (which is only addressed by path).
cd "$WORK"
"$SPC_BIN" doctor --auto-fix=never --no-interaction
# The loader is also libc: build it from the pinned musl release the toolchain targets.
python3 -I "$SCRIPT_DIR/musl_loader.py" "$PHP_DIR" "$WORK/loader" "$WORK/loader.json" --zig-dir "$PKG_ROOT_PATH/zig"
"$SPC_BIN" craft "$WORK/craft.yml" -v
# SPC source output paths are rooted in its working directory.
mkdir -p "$(dirname "$OUT")"
python3 -I "$SCRIPT_DIR/manifest.py" "$PHP_DIR" "$OUT" "$WORK/resolution.json" "$PROVENANCE" "$REPO_ROOT" --buildroot "$WORK/buildroot" --downloads "$DOWNLOAD_PATH" --loader "$WORK/loader.json"
python3 -I "$SCRIPT_DIR/verify.py" "$OUT"
python3 -I "$SCRIPT_DIR/package.py" "$OUT" "$REPO_ROOT/dist/assets"
printf 'Verified and packaged runtime: %s\n' "$OUT"
