# Sourced by installed launchers (POSIX sh).
PHPRC="$ROOT/etc/php.ini"
case ${0##*/} in
    php-fpm) PHP_INI_SCAN_DIR="$ROOT/etc/fpm/conf.d" ;;
    *) PHP_INI_SCAN_DIR="$ROOT/etc/cli/conf.d" ;;
esac
# The bundle ships its own musl loader; never inherit an external loader injection.
unset LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT
export PHPRC PHP_INI_SCAN_DIR
