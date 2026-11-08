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
# Command substitution first: a failing subcommand inside `< <(...)` never
# reaches `set -e`, and the arrays would silently stay empty — configure would
# then run with the caller's PATH, no CC, and no --disable-rpath.
flag_list="$(toolchain configure-flags)"
mapfile -t flags <<< "$flag_list"
environment_list="$(toolchain env --zig-dir "$zig_dir" --deps "$deps")"
mapfile -t environment <<< "$environment_list"
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
# Task 4 writes this record; a build that skipped the write must say so here
# rather than fail deep inside staging with a bare FileNotFoundError.
[[ -f "$work/toolchain.json" ]] || { printf 'Missing provenance record: %s\n' "$work/toolchain.json" >&2; exit 1; }
python3 -I "$RUNTIME_DIR/scripts/bundle.py" stage \
  "$work/inst/usr/local/pgsql" "$bundle" "$work/request.json" "$work/toolchain.json" "$REPO_ROOT"
printf 'Staged bundle: %s\n' "$bundle"
# Verification runs a real cluster out of the staged tree with the host's own
# musl loader masked away. A failure here leaves the tree in place — it is the
# only evidence of what went wrong — and must never reach packaging.
python3 -I "$RUNTIME_DIR/scripts/bundle.py" verify "$bundle"
# Packaging names the assets after the manifest's own identity and refuses to
# replace any of them, so a rerun of a version that already shipped stops here
# rather than silently overwriting a published archive.
python3 -I "$RUNTIME_DIR/scripts/bundle.py" package "$bundle" "$REPO_ROOT/dist/assets"
printf 'Verified bundle: %s\n' "$bundle"
printf 'PostgreSQL build workspace: %s\n' "$work"
