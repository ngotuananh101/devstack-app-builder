#!/usr/bin/env bash
set -euo pipefail
# Compatibility entrypoint. All installed functionality lives in the bundle.
[[ $# -ge 2 ]] || { printf '%s\n' 'Usage: ext.sh {list|enable|disable} RUNTIME_DIR [MODULE] [cli|fpm|all]' >&2; exit 2; }
ACTION="$1"; RUNTIME="$2"; shift 2
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
if [[ ! -d "$RUNTIME" && -d "$REPO_ROOT/$RUNTIME" ]]; then RUNTIME="$REPO_ROOT/$RUNTIME"; fi
RUNTIME="$(cd "$RUNTIME" && pwd)"
[[ -x "$RUNTIME/bin/php-ext" ]] || { printf '%s\n' 'Bundle lacks packaged php-ext; rebuild it.' >&2; exit 1; }
exec "$RUNTIME/bin/php-ext" "$ACTION" "$@"
