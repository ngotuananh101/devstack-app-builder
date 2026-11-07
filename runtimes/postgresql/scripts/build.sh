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
      printf '%s\n' 'Usage: ./build.sh postgresql [exact-version] [--print-plan]' 'Defaults: 18.6. musl-dynamic server bundles for 17.x and 18.x; no profiles or dependency installation.'
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
  printf '%s\n' 'PostgreSQL builds currently require a native Linux x86_64 host.' >&2
  exit 1
fi
read -r version target < <(python3 -I -c 'import json,sys; p=json.load(sys.stdin); print(p["version"],p["target"])' <<< "$plan")
jobs="${BUILD_JOBS:-2}"
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || { printf '%s\n' 'BUILD_JOBS must be a positive integer.' >&2; exit 1; }
bundle="$REPO_ROOT/dist/postgresql/$version/$target"
if [[ -e "$bundle" || -L "$bundle" ]]; then
  printf 'Refusing to replace existing bundle: %s\n' "$bundle" >&2
  exit 1
fi
mkdir -p "$REPO_ROOT/.cache/postgresql"
work="$(mktemp -d "$REPO_ROOT/.cache/postgresql/build.XXXXXXXX")"
printf 'PostgreSQL build workspace: %s\n' "$work"
toolchain() { python3 -I "$RUNTIME_DIR/scripts/toolchain.py" "$@"; }
# A missing host tool must name itself before anything is downloaded or built.
toolchain require make bison flex perl curl readelf strip tar bzip2
zig_dir="$(toolchain prepare-zig --work "$work")"
source_dir="$(python3 -I "$RUNTIME_DIR/scripts/source.py" --download "$work" "$version")"
deps="$(toolchain build-dependencies --work "$work" --zig-dir "$zig_dir")"
toolchain build-loader --work "$work" --zig-dir "$zig_dir"
toolchain write-provenance --work "$work" --output "$work/toolchain.json"
mapfile -t flags < <(toolchain configure-flags)
mapfile -t environment < <(toolchain env --zig-dir "$zig_dir" --deps "$deps")
# The compiled prefix is irrelevant: relocation comes from --argv0 in the launchers,
# and the default keeps bindir ending in /bin, which launchers.install expects.
mkdir -p "$work/build"
(
  cd "$work/build"
  # The environment is scrubbed for the build commands only, never for this script:
  # CC/CXX are absolute, pkg-config sees only the vendored prefix, and Zig gets a
  # workspace-local cache so a cleaned environment still works.
  env "${environment[@]}" "$source_dir/configure" --prefix=/usr/local/pgsql "${flags[@]}" \
    --with-includes="$deps/include" --with-libraries="$deps/lib"
)
env "${environment[@]}" make -C "$work/build" -j "$jobs" world-bin
env "${environment[@]}" make -C "$work/build" DESTDIR="$work/inst" install-world-bin
printf 'PostgreSQL build workspace: %s\n' "$work"
