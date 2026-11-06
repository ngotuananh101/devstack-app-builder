#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$PHP_DIR/../.." && pwd)"
[[ $# -le 3 ]] || { printf '%s\n' 'Expected target/profile/version or version/target/profile' >&2; exit 2; }
ARG1="${1:-glibc}"; ARG2="${2:-}"; ARG3="${3:-}"
PHP_VER="${PHP_VERSION:-8.4}"
if [[ "$ARG1" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]; then
    PHP_VER="$ARG1"; TARGET="${ARG2:-glibc}"; PROFILE="${ARG3:-full}"
else
    TARGET="$ARG1"; PROFILE="${ARG2:-full}"; PHP_VER="${ARG3:-$PHP_VER}"
fi
# Validation happens before invoking SPC and before deriving user-controlled paths.
WORK="$(mktemp -d "$PHP_DIR/.build.XXXXXX")"
trap 'rm -rf -- "$WORK"' EXIT
python3 -I "$SCRIPT_DIR/configure.py" "$PHP_DIR" "$TARGET" "$PROFILE" "$PHP_VER" "$WORK/craft.yml" "$WORK/resolution.json"
SPC_BIN="${SPC_BIN:-$PHP_DIR/spc}"
[[ -x "$SPC_BIN" ]] || { printf '%s\n' 'Missing verified SPC. Run runtimes/php/scripts/download-spc.sh first.' >&2; exit 1; }
PROVENANCE="${SPC_PROVENANCE:-$PHP_DIR/.cache/spc-provenance.json}"
[[ -f "$PROVENANCE" ]] || { printf '%s\n' 'Missing verified SPC provenance.' >&2; exit 1; }
case "$TARGET" in
    glibc) export SPC_TARGET="x86_64-linux-gnu.2.17" ;;
    musl) export SPC_TARGET="x86_64-linux-musl" ;;
esac
export SPC_CMD_VAR_PHP_EMBED_TYPE=static
# Same host toolchain root as explicit preparation, independent of fresh WORK.
export PKG_ROOT_PATH="${PKG_ROOT_PATH:-$PHP_DIR/.cache/pkgroot/x86_64-linux}"
OUT="$REPO_ROOT/dist/php/$PHP_VER/$TARGET/$PROFILE"
[[ ! -e "$OUT" ]] || { printf 'Refusing to overwrite bundle: %s\n' "$OUT" >&2; exit 1; }
# A fresh, owned work directory avoids stale modules and cross-version source output.
# It is separate from the downloaded SPC source (which is only addressed by path).
cd "$WORK"
"$SPC_BIN" doctor --auto-fix=never --no-interaction
"$SPC_BIN" craft "$WORK/craft.yml" -v
# SPC source output paths are rooted in its working directory.
mkdir -p "$(dirname "$OUT")"
python3 -I "$SCRIPT_DIR/manifest.py" "$PHP_DIR" "$OUT" "$WORK/resolution.json" "$PROVENANCE" "$REPO_ROOT" --buildroot "$WORK/buildroot" --downloads "$WORK/downloads"
python3 -I "$SCRIPT_DIR/verify.py" "$OUT"
python3 -I "$SCRIPT_DIR/package.py" "$OUT" "$REPO_ROOT/dist/assets"
printf 'Verified and packaged runtime: %s\n' "$OUT"
