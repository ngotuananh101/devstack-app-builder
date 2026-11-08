#!/usr/bin/env bash
# Throwaway feasibility experiment. Never publishes or changes supported targets.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HELPER="$SCRIPT_DIR/php-musl-dynamic.py"
PHP_VERSION="${PHP_VERSION:-8.4.27}"
SPC_CONCURRENCY="${SPC_CONCURRENCY:-4}"
export SPC_CONCURRENCY
case "${1:-run}" in
  run)
    mkdir -p "$REPO_ROOT/.cache"
    WORK="$(mktemp -d "$REPO_ROOT/.cache/php-musl-dynamic.XXXXXX")"
    ;;
  resume|verify)
    [[ $# == 2 ]] || { printf 'Usage: bash %s [run | resume WORKSPACE | verify WORKSPACE]\n' "$0" >&2; exit 2; }
    WORK="$(realpath "$2")"
    [[ -f "$WORK/experiment.json" ]] || { printf '%s\n' 'Not an experiment workspace' >&2; exit 2; }
    ;;
  *) printf 'Usage: bash %s [run | resume WORKSPACE | verify WORKSPACE]\n' "$0" >&2; exit 2 ;;
esac
printf 'EXPERIMENT_WORKSPACE=%s\n' "$WORK"
# Retain every log and downloaded archive, including on failure. No host fixes.
exec > >(tee -a "$WORK/console.log") 2>&1
if [[ "${1:-run}" == verify ]]; then
    python3 -I "$HELPER" verify "$REPO_ROOT" "$WORK"
    exit
fi
for tool in php python3 make cc cmake pkg-config readelf curl tar xz; do
    command -v "$tool" >/dev/null || { printf 'Missing host tool: %s (no auto-install)\n' "$tool" >&2; exit 1; }
done
[[ $(id -u) != 0 ]] || { printf '%s\n' 'Run this experiment as a non-root user.' >&2; exit 1; }
[[ $(uname -m) == x86_64 && -x /lib/ld-musl-x86_64.so.1 ]] || {
    printf '%s\n' 'This PoC requires an existing x86_64 musl loader on the build host.' >&2; exit 1;
}
if [[ ! -f "$WORK/experiment.json" ]]; then
    python3 -I "$HELPER" prepare "$REPO_ROOT" "$WORK" "$PHP_VERSION"
fi
export PATH="$WORK/tools/bin:$PATH"
export PHP_INI_SCAN_DIR=''
export SPC_TARGET='x86_64-linux-musl -dynamic'
export SPC_CMD_VAR_PHP_EMBED_TYPE=static
export PKG_ROOT_PATH="$WORK/pkgroot"
export PATH="$PKG_ROOT_PATH/bin:$PATH"
export PKG_CONFIG_LIBDIR="$WORK/buildroot/lib/pkgconfig"
export PKG_CONFIG_PATH=''
export BUILD_ROOT_PATH="$WORK/buildroot"
export SOURCE_PATH="$WORK/source"
export DOWNLOAD_PATH="$WORK/downloads"
export SPC_LOGS_DIR="$WORK/log"
export SPC_ENABLE_LOG_FILE=1
export SPC_SKIP_MOTD=1
SPC_SOURCE="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["spc_source"])' "$WORK/experiment.json")"
# CWD is the owned control directory, never an extracted/download directory.
cd "$WORK/control"
php "$SPC_SOURCE/bin/spc" --version
if [[ ! -f "$WORK/inputs.lock.json" ]]; then
    # Download-only. No source extraction or compiler execution here.
    php "$SPC_SOURCE/bin/spc" download --for-packages=php,ext-session,ext-apcu,ext-redis,zig \
        --without-suggests --with-php="$PHP_VERSION" --parallel=4 --retry=2 --no-interaction -vv
    python3 -I "$HELPER" freeze "$REPO_ROOT" "$WORK"
fi
python3 -I "$HELPER" check "$REPO_ROOT" "$WORK"
# The compiler-runtime artifact is keyed on the compiler version, which SPC only
# resolves for a tool package it has itself installed and recorded.
php "$SPC_SOURCE/bin/spc" install-pkg zig --no-interaction -vv
if [[ ! -f "$WORK/downloads/compiler-rt-22.1.8.src.tar.xz" ]]; then
    php "$SPC_SOURCE/bin/spc" download llvm-compiler-rt --retry=2 --no-interaction -vv
fi
# Include compiler-rt even when an earlier download completed before freezing.
python3 -I "$HELPER" freeze "$REPO_ROOT" "$WORK"
python3 -I "$HELPER" check "$REPO_ROOT" "$WORK"
python3 -I "$HELPER" host-tools "$REPO_ROOT" "$WORK"
# Doctor is check-only. The build cannot silently fetch more archives.
php "$SPC_SOURCE/bin/spc" doctor --auto-fix=never --no-interaction
php "$SPC_SOURCE/bin/spc" craft "$WORK/control/craft.yml" --no-interaction -vv
python3 -I "$HELPER" stage "$REPO_ROOT" "$WORK"
python3 -I "$HELPER" verify "$REPO_ROOT" "$WORK"
printf 'PoC finished. Results: %s/results.json\n' "$WORK"
