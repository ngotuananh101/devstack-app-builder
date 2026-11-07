#!/usr/bin/env bash
set -euo pipefail
RUNTIME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$RUNTIME_DIR/../.." && pwd)"
args=()
print_plan=0
for arg in "$@"; do
  case "$arg" in
    --print-plan) print_plan=1 ;;
    -h|--help)
      printf '%s\n' 'Usage: ./build.sh redis [exact-version] [--print-plan]' 'Defaults: 8.10.3. Musl-only fully static build; no profiles or dependency installation.'
      exit 0 ;;
    *) args+=("$arg") ;;
  esac
done
plan="$(python3 -I "$RUNTIME_DIR/scripts/source.py" "${args[@]}")"
if [[ "$print_plan" == 1 ]]; then
  printf '%s\n' "$plan"
  exit 0
fi
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  printf '%s\n' 'Redis builds currently require a native Linux x86_64 host.' >&2
  exit 1
fi
read -r version target < <(python3 -I -c 'import json,sys; p=json.load(sys.stdin); print(p["version"],p["target"])' <<< "$plan")
compiler="${REDIS_CC:-musl-gcc}"
ldflags='-static -no-pie'
for tool in "$compiler" make ar ranlib readelf strip curl; do
  command -v "$tool" >/dev/null || { printf 'Missing native dependency: %s. Install it explicitly, then rerun.\n' "$tool" >&2; exit 1; }
done
jobs="${BUILD_JOBS:-2}"
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || { printf '%s\n' 'BUILD_JOBS must be a positive integer.' >&2; exit 1; }
bundle="$REPO_ROOT/dist/redis/$version/$target"
if [[ -e "$bundle" || -L "$bundle" ]]; then
  printf 'Refusing to replace existing bundle: %s\n' "$bundle" >&2
  exit 1
fi
mkdir -p "$REPO_ROOT/.cache/redis"
work="$(mktemp -d "$REPO_ROOT/.cache/redis/build.XXXXXXXX")"
printf 'Redis native build workspace: %s\n' "$work"
# The libc probe must pass BEFORE any source download or build.
python3 -I "$RUNTIME_DIR/scripts/bundle.py" probe "$compiler" "$target" "$work"
source_dir="$(python3 -I "$RUNTIME_DIR/scripts/source.py" --download "$work" "$version")"
make -C "$source_dir/src" -j "$jobs" CC="$compiler" MALLOC=jemalloc \
  OPTIMIZATION=-O2 USE_SYSTEMD=no BUILD_TLS=no BUILD_COMPRESSION=no LDFLAGS='' REDIS_LDFLAGS="$ldflags" \
  redis-server redis-cli redis-benchmark
strip "$source_dir/src/redis-server" "$source_dir/src/redis-cli" "$source_dir/src/redis-benchmark"
python3 -I "$RUNTIME_DIR/scripts/bundle.py" stage "$source_dir" "$bundle" "$work/request.json" "$work/toolchain.json" "$REPO_ROOT"
python3 -I "$RUNTIME_DIR/scripts/bundle.py" verify "$bundle"
python3 -I "$RUNTIME_DIR/scripts/bundle.py" package "$bundle" "$REPO_ROOT/dist/assets"
printf 'Verified bundle: %s\n' "$bundle"
