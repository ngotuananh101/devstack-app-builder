# Sourced by installed launchers (POSIX sh).
PHPRC="$ROOT/etc/php.ini"
case ${0##*/} in
    php-fpm) PHP_INI_SCAN_DIR="$ROOT/etc/fpm/conf.d" ;;
    *) PHP_INI_SCAN_DIR="$ROOT/etc/cli/conf.d" ;;
esac
# The bundle root has to be in the environment, not only on this launcher's
# command line. A consumer that runs PHP as a library resolves the interpreter
# through PHP_BINARY, which is always the real ELF at libexec/php; the children
# it spawns inherit this process's environment and nothing of its argv, so a
# root passed only as `-d extension_dir=...` is lost one level down. etc/php.ini
# expands this variable so those children find the same extensions.
DEVSTACK_PHP_ROOT="$ROOT"
# The bundle ships its own musl loader; never inherit an external loader injection.
unset LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT
export PHPRC PHP_INI_SCAN_DIR DEVSTACK_PHP_ROOT
