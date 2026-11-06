# Sourced by installed launchers (POSIX sh).
PHPRC="$ROOT/etc/php.ini"
case ${0##*/} in
    php-fpm) PHP_INI_SCAN_DIR="$ROOT/etc/fpm/conf.d" ;;
    *) PHP_INI_SCAN_DIR="$ROOT/etc/cli/conf.d" ;;
esac
export PHPRC PHP_INI_SCAN_DIR
