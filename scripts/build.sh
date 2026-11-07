#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  echo "DevStack Runtime Builder"
  echo "Usage: ./build.sh <runtime> [runtime-args...]"
  echo "   or: ./scripts/build.sh <runtime> [runtime-args...]"
  echo ""
  echo "Available runtimes:"
  local found=0
  for r in "$REPO_ROOT"/runtimes/*; do
    if [[ -d "$r" && -x "$r/scripts/build.sh" ]]; then
      echo "  - $(basename "$r")"
      found=1
    fi
  done
  if [[ $found -eq 0 ]]; then
    echo "  (No runtimes found in runtimes/)"
  fi
  echo ""
  echo "Examples:"
  echo "  ./build.sh php"
  echo "  ./build.sh php 8.4"
  echo "  ./build.sh redis"
  echo "  ./build.sh redis 8.10.3"
  exit 1
}

if [[ $# -lt 1 || "$1" == "-h" || "$1" == "--help" ]]; then
  usage
fi

RUNTIME="$1"
shift

RUNTIME_DIR="$REPO_ROOT/runtimes/$RUNTIME"
RUNTIME_SCRIPT="$RUNTIME_DIR/scripts/build.sh"

if [[ ! -d "$RUNTIME_DIR" ]]; then
  echo "Error: Unknown runtime '$RUNTIME'. No directory at runtimes/$RUNTIME" >&2
  exit 1
fi

if [[ ! -x "$RUNTIME_SCRIPT" ]]; then
  echo "Error: Missing executable build script at runtimes/$RUNTIME/scripts/build.sh" >&2
  exit 1
fi

exec "$RUNTIME_SCRIPT" "$@"
