#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PHP_DIR"

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) url="https://dl.static-php.dev/v3/spc-bin/nightly/spc-linux-x86_64" ;;
  *) echo "This starter downloader currently supports Linux x86_64 build hosts only." >&2; exit 1 ;;
esac

curl -fsSL "$url" -o spc
chmod +x spc
./spc --version
