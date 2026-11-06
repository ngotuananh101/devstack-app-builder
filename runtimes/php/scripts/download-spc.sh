#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
export PKG_ROOT_PATH="${PKG_ROOT_PATH:-$PHP_DIR/.cache/pkgroot/x86_64-linux}"
# Build-host dependencies only; installed bundles do not need PHP/Composer.
command -v php >/dev/null
command -v composer >/dev/null
php -r 'if (PHP_VERSION_ID < 80400 || !extension_loaded("mbstring") || !extension_loaded("zlib")) { fwrite(STDERR,"SPC v3 needs host PHP >=8.4 with mbstring/zlib\n"); exit(1); }'
COMMIT="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["commit"])' "$PHP_DIR/config/spc-lock.json")"
SOURCE_PARENT="$PHP_DIR/.cache/spc/$COMMIT"
mkdir -p "$PHP_DIR/.cache/spc"
# bootstrap.py requires a fresh directory and checks the archive SHA-256 first.
SOURCE="$(python3 -I "$SCRIPT_DIR/bootstrap.py" "$PHP_DIR/config/spc-lock.json" "$SOURCE_PARENT" "$PHP_DIR/.cache/spc-provenance.json" "$PHP_DIR/config/spc-dependencies.json")"
# Never cd into extracted upstream code. Disable Composer scripts and plugins.
# Its lock now points ONLY to local archives whose SHA-256 was verified.
COMPOSER_CACHE_DIR="$(mktemp -d "$PHP_DIR/.cache/composer.XXXXXX")"
export COMPOSER_CACHE_DIR
composer --working-dir="$SOURCE" install --no-dev --prefer-dist --no-interaction --no-scripts --no-plugins
# This is our launcher, kept outside downloaded source and using an absolute path.
printf '#!/usr/bin/env bash\nset -euo pipefail\nexport PKG_ROOT_PATH="${PKG_ROOT_PATH:-%s}"\nexec php %q "$@"\n' "$PKG_ROOT_PATH" "$SOURCE/bin/spc" > "$PHP_DIR/spc"
chmod 0755 "$PHP_DIR/spc"
"$PHP_DIR/spc" --version
