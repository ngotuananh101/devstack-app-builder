#!/usr/bin/env bash
# Build the Apache httpd musl-dynamic bundle.
#
# Every step is a separate script that refuses to run outside its own contract:
# fetch verifies a digest before extracting, deps builds into a fresh prefix,
# httpd refuses on a module census mismatch, stage refuses on an ELF that still
# carries a search path, verify runs the server, package refuses unverified input.
set -euo pipefail
RUNTIME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$RUNTIME_DIR/../.." && pwd)"
SCRIPTS="$RUNTIME_DIR/scripts"

usage() {
  printf '%s\n' \
    'Usage: ./build.sh apache [exact-version] [--mpm event|worker|prefork] [--print-plan]' \
    'Defaults: the version pinned in config/sources.json, MPM event.' \
    'A full build vendors every dependency, then verifies the bundle by running it.'
}

args=()
print_plan=0
mpm="${APACHE_MPM:-event}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --print-plan) print_plan=1; shift ;;
    --mpm)
      [[ $# -ge 2 ]] || { printf '%s\n' '--mpm needs a value.' >&2; exit 2; }
      mpm="$2"; shift 2 ;;
    --mpm=*) mpm="${1#--mpm=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
case "$mpm" in
  event|worker|prefork) ;;
  *) printf 'Unsupported MPM: %s\n' "$mpm" >&2; exit 2 ;;
esac

plan="$(python3 -I "$SCRIPTS/source.py" "${args[@]}")"
if [[ "$print_plan" == 1 ]]; then
  printf '%s\n' "$plan"
  exit 0
fi
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  printf '%s\n' 'Apache builds currently require a native Linux x86_64 host.' >&2
  exit 1
fi

compiler="${APACHE_CC:-cc}"
# The host provides only a C compiler. musl itself is compiled from the pinned
# source by loader.py, so the distro's musl-tools version never reaches the bundle.
# mod_md and mod_proxy_html need a working curl/perl toolchain at build time;
# pkg-config is how every child configure finds the staging prefix.
for tool in "$compiler" make perl pkg-config cmake curl readelf strip ar ranlib tar; do
  command -v "$tool" >/dev/null || {
    printf 'Missing native dependency: %s. Install it explicitly, then rerun.\n' "$tool" >&2
    exit 1
  }
done
jobs="${BUILD_JOBS:-2}"
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || { printf '%s\n' 'BUILD_JOBS must be a positive integer.' >&2; exit 1; }

read -r version target < <(python3 -I -c \
  'import json,sys; p=json.load(sys.stdin); print(p["version"],p["target"])' <<< "$plan")
bundle="$REPO_ROOT/dist/apache/$version/$target"
if [[ -e "$bundle" || -L "$bundle" ]]; then
  printf 'Refusing to replace existing bundle: %s\n' "$bundle" >&2
  exit 1
fi

mkdir -p "$REPO_ROOT/.cache/apache"
work="$(mktemp -d "$REPO_ROOT/.cache/apache/build.XXXXXXXX")"
printf 'Apache build workspace: %s\n' "$work"
printf '%s\n' "$plan" > "$work/plan.json"

# The host-compiler probe runs before anything is downloaded: a compiler that
# cannot build and run a program must fail here rather than after forty minutes.
python3 -I "$SCRIPTS/loader.py" "$work/plan.json" "$work" "$work/loader.json" \
  --compiler "$compiler" --probe-only >/dev/null
python3 -I "$SCRIPTS/fetch.py" "$work" "$work/plan.json" > "$work/sources.json"
# The pinned musl toolchain is built before the dependencies so an unusable
# compiler stops the run before the expensive part; it reuses fetch.py's verified
# musl extraction and reports the compiler path every later step must use.
toolchain="$(python3 -I "$SCRIPTS/loader.py" "$work/plan.json" "$work" "$work/loader.json" \
  --compiler "$compiler" --jobs "$jobs")"
cc="$(python3 -I -c 'import json,sys; print(json.load(sys.stdin)["cc"])' <<< "$toolchain")"
printf 'Pinned musl toolchain: %s\n' "$cc"
python3 -I "$SCRIPTS/deps.py" "$work/sources.json" "$work/prefix" --cc "$cc" --jobs "$jobs"
# fetch.py reports the archive root inside each extraction directory, so every
# later step takes the resolved tree from the map rather than the wrapper.
httpd_source="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["httpd"])' "$work/sources.json")"
python3 -I "$SCRIPTS/httpd.py" "$httpd_source" "$work/build" "$work/prefix" "$work/install" \
  --cc "$cc" --jobs "$jobs" > "$work/census.log"
python3 -I "$SCRIPTS/request.py" "$work/plan.json" "$work/sources.json" "$work/build/apache-census.json" \
  "$work/loader.json" "$work/install" "$work/prefix" "$work/request.json" --mpm "$mpm" >/dev/null
python3 -I "$SCRIPTS/stage.py" "$work/request.json" "$bundle"
python3 -I "$SCRIPTS/verify.py" "$bundle"
python3 -I "$SCRIPTS/package.py" "$bundle" "$REPO_ROOT/dist/assets"
printf 'Verified and packaged runtime: %s\n' "$bundle"
