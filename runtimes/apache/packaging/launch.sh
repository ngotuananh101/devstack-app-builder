# Sourced by generated launchers (POSIX sh).
#
# httpd resolves modules through dlopen, and APR-util resolves its DBD/crypto/ldap
# drivers the same way. dlopen in musl honours exactly the same path list as the
# loader, so --library-path alone would be enough for modules; the export exists
# because APR-util looks drivers up through APR_DSOPATH, which this apr build
# defines as the literal string "LD_LIBRARY_PATH". It splits that variable's value,
# then retries each entry with "apr-util-1/" appended, which is how the drivers end
# up found in lib/apr-util-1 without a separate entry here.
unset LD_PRELOAD LD_AUDIT
export LD_LIBRARY_PATH="$ROOT/lib/musl:$ROOT/lib"
# conf/httpd.conf defines SRVROOT from this: httpd has no ${NAME:default} syntax,
# so an unset variable would leave ServerRoot as the empty string.
export APACHE_SERVER_ROOT="$ROOT"
# The openssl CLI reads $OPENSSLDIR/openssl.cnf and refuses to run a command
# without one, while `make install_sw` never installs it. OPENSSLDIR is compiled
# in and still names the build prefix, so point the CLI at the copy the bundle
# ships. A caller's own value wins: that is the documented escape hatch.
if [ -z "${OPENSSL_CONF+set}" ] && [ -f "$ROOT/ssl/openssl.cnf" ]; then
    export OPENSSL_CONF="$ROOT/ssl/openssl.cnf"
fi
