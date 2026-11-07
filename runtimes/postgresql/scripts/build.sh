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
printf 'PostgreSQL build pipeline not implemented yet\n' >&2
exit 1
