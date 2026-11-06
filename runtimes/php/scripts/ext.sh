#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$PHP_DIR/../.." && pwd)"

usage() {
  echo "Usage: $0 {list|enable|disable} RUNTIME_DIR [EXTENSION] [cli|fpm|all]" >&2
  echo "Example: $0 list dist/php/8.4/glibc/full" >&2
  echo "         $0 enable dist/php/8.4/glibc/full redis cli" >&2
  echo "         $0 disable dist/php/8.4/glibc/full redis all" >&2
  exit 2
}

[[ $# -ge 2 ]] || usage
ACTION="$1"
TARGET_ARG="$2"
EXT="${3:-}"
SAPI="${4:-all}"

# Resolve runtime directory
if [[ -d "$TARGET_ARG" ]]; then
  RUNTIME="$(cd "$TARGET_ARG" && pwd)"
elif [[ -d "$REPO_ROOT/$TARGET_ARG" ]]; then
  RUNTIME="$(cd "$REPO_ROOT/$TARGET_ARG" && pwd)"
elif [[ -d "$REPO_ROOT/dist/php/$TARGET_ARG" ]]; then
  RUNTIME="$(cd "$REPO_ROOT/dist/php/$TARGET_ARG" && pwd)"
elif [[ -d "$REPO_ROOT/dist/php/8.4/$TARGET_ARG" ]]; then
  RUNTIME="$(cd "$REPO_ROOT/dist/php/8.4/$TARGET_ARG" && pwd)"
else
  echo "Error: Directory not found: $TARGET_ARG" >&2
  exit 1
fi

case "$SAPI" in cli|fpm|all) ;; *) usage ;; esac

dirs=()
if [[ "$SAPI" == "all" || "$SAPI" == "cli" ]]; then dirs+=("$RUNTIME/etc/cli/conf.d"); fi
if [[ "$SAPI" == "all" || "$SAPI" == "fpm" ]]; then dirs+=("$RUNTIME/etc/fpm/conf.d"); fi

case "$ACTION" in
  list)
    echo "Runtime: $RUNTIME"
    for d in "$RUNTIME/etc/cli/conf.d" "$RUNTIME/etc/fpm/conf.d"; do
      echo "[$(basename "$(dirname "$d")")]"
      find "$d" -maxdepth 1 -type f -name '*.ini' -printf '%f\n' 2>/dev/null | sort || true
    done
    ;;
  enable|disable)
    [[ -n "$EXT" ]] || usage
    [[ "$EXT" =~ ^[a-zA-Z0-9_-]+$ ]] || { echo "Invalid extension name: $EXT" >&2; exit 1; }
    for d in "${dirs[@]}"; do
      mkdir -p "$d"
      f="$d/20-$EXT.ini"
      if [[ "$ACTION" == "enable" ]]; then
        printf 'extension=%s.so\n' "$EXT" > "$f"
        echo "Enabled $EXT in $d"
      else
        rm -f "$f"
        echo "Disabled $EXT in $d"
      fi
    done
    ;;
  *) usage ;;
esac
