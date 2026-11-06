#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$PHP_DIR/../.." && pwd)"

cd "$PHP_DIR"

ARG1="${1:-}"
ARG2="${2:-}"
ARG3="${3:-}"

DEFAULT_PHP_VERSION="8.4"
PHP_VER="${PHP_VERSION:-$DEFAULT_PHP_VERSION}"
TARGET="glibc"
PROFILE="laravel"

# Support flexible argument ordering:
# 1) [glibc|musl] [profile] [php_version]
# 2) [php_version] [glibc|musl] [profile]
if [[ "$ARG1" =~ ^[789]\.[0-9]+(\.[0-9]+)?$ ]]; then
  PHP_VER="$ARG1"
  TARGET="${ARG2:-glibc}"
  PROFILE="${ARG3:-laravel}"
elif [[ "$ARG1" == "glibc" || "$ARG1" == "musl" ]]; then
  TARGET="$ARG1"
  PROFILE="${ARG2:-laravel}"
  if [[ -n "$ARG3" ]]; then
    PHP_VER="$ARG3"
  fi
elif [[ -z "$ARG1" ]]; then
  TARGET="glibc"
  PROFILE="laravel"
else
  echo "Usage: $0 [glibc|musl] [profile] [php_version]" >&2
  echo "   or: $0 [php_version] [glibc|musl] [profile]" >&2
  echo "Example: $0 glibc laravel 8.3" >&2
  echo "         $0 8.4 musl minimal" >&2
  exit 2
fi

SPC_BIN="${SPC_BIN:-$PHP_DIR/spc}"
if [[ ! -x "$SPC_BIN" && -x "$REPO_ROOT/spc" ]]; then
  SPC_BIN="$REPO_ROOT/spc"
fi

if [[ ! -x "$SPC_BIN" ]]; then
  echo "Missing executable SPC: $SPC_BIN (run runtimes/php/scripts/download-spc.sh)" >&2
  exit 1
fi

PROFILE_FILE="$PHP_DIR/config/profiles/$PROFILE.txt"
if [[ ! -f "$PROFILE_FILE" ]]; then
  echo "Unknown profile: $PROFILE (expected $PROFILE_FILE)" >&2
  exit 1
fi

EXTENSIONS="$(tr -d ' \n\r\t' < "$PROFILE_FILE")"
[[ -n "$EXTENSIONS" ]] || { echo "Empty extension profile" >&2; exit 1; }

case "$TARGET" in
  glibc)
    export SPC_TARGET="native-native-gnu.2.17"
    SHARED="$(tr -d ' \n\r\t' < "$PHP_DIR/config/shared-glibc.txt")"
    ;;
  musl)
    export SPC_TARGET="native-native-musl"
    SHARED=""
    ;;
  *)
    echo "Unsupported target libc: $TARGET (must be glibc or musl)" >&2
    exit 2
    ;;
esac

# Ensure shared extensions are included in the selected extension set.
if [[ -n "$SHARED" ]]; then
  IFS=',' read -ra shared_items <<< "$SHARED"
  for ext in "${shared_items[@]}"; do
    case ",$EXTENSIONS," in *,"$ext",*) ;; *)
      echo "Shared extension '$ext' is not present in profile '$PROFILE'." >&2
      exit 1
    esac
  done
fi

cat > "$PHP_DIR/craft.yml" <<EOF
php-version: "$PHP_VER"
extensions: "$EXTENSIONS"
sapi:
  - cli
  - fpm
debug: true
build-options:
  no-strip: false
  build-shared: "$SHARED"
download-options:
  parallel: 8
  retry: 3
EOF

echo "Building PHP runtime: version=$PHP_VER target=$TARGET profile=$PROFILE SPC_TARGET=$SPC_TARGET"
"$SPC_BIN" doctor
"$SPC_BIN" craft -v

OUT="$REPO_ROOT/dist/php/$PHP_VER/$TARGET/$PROFILE"
rm -rf "$OUT"
mkdir -p "$OUT/bin" "$OUT/etc/cli/conf.d" "$OUT/etc/fpm/conf.d" "$OUT/lib"

install -m 0755 "$PHP_DIR/buildroot/bin/php" "$OUT/bin/php"
install -m 0755 "$PHP_DIR/buildroot/bin/php-fpm" "$OUT/bin/php-fpm"
install -m 0644 "$PHP_DIR/config/php.ini" "$OUT/etc/php.ini"
install -m 0644 "$PHP_DIR/config/php-fpm.conf" "$OUT/etc/php-fpm.conf"

# Locate extension directory from the build output, if SPC emitted shared modules.
EXT_DIR="$(find "$PHP_DIR/buildroot" -type d -path '*/extensions' -print -quit || true)"
if [[ -n "$EXT_DIR" ]]; then
  mkdir -p "$OUT/lib/php/extensions"
  cp -a "$EXT_DIR/." "$OUT/lib/php/extensions/"
fi

cat > "$OUT/manifest.txt" <<EOF
runtime=php
php_version=$PHP_VER
architecture=x86_64
target=$TARGET
profile=$PROFILE
spc_target=$SPC_TARGET
extensions=$EXTENSIONS
shared_extensions=$SHARED
EOF

# Point PHP at staged shared modules; static extensions are unaffected.
if [[ -d "$OUT/lib/php/extensions" ]]; then
  printf '\nextension_dir=%s\n' "$OUT/lib/php/extensions" >> "$OUT/etc/php.ini"
fi

echo "Staged runtime: $OUT"
"$OUT/bin/php" -c "$OUT/etc/php.ini" -m
"$OUT/bin/php-fpm" -c "$OUT/etc/php.ini" -y "$OUT/etc/php-fpm.conf" -t
