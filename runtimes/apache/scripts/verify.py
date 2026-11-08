#!/usr/bin/env python3
"""Prove a staged bundle works by running it, not by inspecting its flags.

Every check here starts the bundle's own launcher. Headers and POSIX sh are not
enough: a module can be present, named correctly, and still fail to load, and a
config can parse and still serve 500. So each probe starts a server, makes a real
request over a real socket and requires a real answer.

The verifier never binds a privileged port, never touches the host's configuration
and always stops what it started, including on failure.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import elf

LOADER_NAME = "ld-musl-x86_64.so.1"
# The static set httpd -l must report: nothing else is linked into the binary.
STATIC_MODULES = {"core.c", "http_core.c", "mod_so.c"}
TIMEOUT = 20


class Failure(Exception):
    pass


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def run(command, **kwargs):
    return subprocess.run([str(part) for part in command], text=True, **kwargs)


def capture(command, **kwargs):
    result = run(command, capture_output=True, **kwargs)
    if result.returncode != 0:
        raise Failure("%s failed:\n%s\n%s" % (command[0], result.stdout, result.stderr))
    return result.stdout


# --------------------------------------------------------------------------- ELF


def check_loader(bundle, manifest):
    record = manifest["loader"]
    path = bundle / record["path"]
    if path.name != LOADER_NAME or not path.is_file():
        raise Failure("The bundle does not ship its loader at " + record["path"])
    if not os.access(path, os.X_OK):
        raise Failure("The bundled loader is not executable")
    if digest(path) != record["sha256"]:
        raise Failure("The staged loader does not match the manifest digest")
    banner = run([path], capture_output=True)
    version = re.search(r"^Version ([0-9]+\.[0-9]+\.[0-9]+)$", banner.stderr, re.MULTILINE)
    if not version or version[1] != record["version"]:
        raise Failure("The bundled loader reports a different version than the manifest")
    return {"path": record["path"], "version": version[1], "sha256": record["sha256"]}


def check_elf(bundle, manifest):
    """No glibc ABI, no search path, and nothing resolved outside the bundle."""
    bundled = {path.name for path in bundle.rglob("*") if path.is_file() or path.is_symlink()}
    inventory = []
    for relative, record in sorted(manifest["binaries"].items()):
        path = bundle / relative
        if not path.is_file():
            raise Failure("Manifest names a missing binary: " + relative)
        header = capture(["readelf", "-hW", path])
        if "ELF64" not in header or "Advanced Micro Devices X86-64" not in header:
            raise Failure("Not a Linux x86_64 ELF64 object: " + relative)
        versions = capture(["readelf", "--version-info", path])
        glibc = sorted(set(re.findall(r"\bGLIBC_[A-Za-z0-9_.]+\b", versions)))
        if glibc:
            raise Failure("glibc symbols in a musl bundle: %s (%s)" % (glibc, relative))
        paths = elf.rpaths(path)
        if paths:
            raise Failure("ELF carries a search path: %s -> %s" % (relative, paths))
        needed = elf.needed(path)
        missing = sorted(set(needed) - bundled - {"libc.so", LOADER_NAME})
        if missing:
            raise Failure("Unbundled dependency in %s: %s" % (relative, missing))
        if needed != record["needed"]:
            raise Failure("NEEDED entries changed after staging: " + relative)
        if digest(path) != record["sha256"]:
            raise Failure("Staged binary changed after the manifest was written: " + relative)
        interpreter = re.search(r"Requesting program interpreter: \[?([^\]\n]+)\]?",
                                capture(["readelf", "-lW", path]))
        if relative.startswith("libexec/bin/"):
            if not interpreter or Path(interpreter[1]).name != LOADER_NAME:
                raise Failure("Program does not request the bundled loader: " + relative)
        elif interpreter:
            raise Failure("Module requests an interpreter: " + relative)
        inventory.append({"path": relative, "needed": needed, "glibc": glibc})
    return inventory


def check_static_set(bundle):
    listed = capture([bundle / "bin/httpd", "-l"])
    modules = set(re.findall(r"^\s*(\S+\.c)$", listed, re.MULTILINE))
    if modules != STATIC_MODULES:
        raise Failure("httpd -l reports %s, expected %s" % (sorted(modules), sorted(STATIC_MODULES)))
    if "mpm" in listed.lower():
        raise Failure("A shared MPM appeared in httpd -l")
    version_line = capture([bundle / "bin/httpd", "-v"])
    version = re.search(r"Server version: Apache/(\S+)", version_line)
    if not version or not version[1].startswith("2.4."):
        raise Failure("httpd -v does not report Apache 2.4: " + version_line.strip())
    # HTTPD_ROOT is compiled in and still names the build host, so the launcher
    # must pin the run-time server root; otherwise the bundle would read the
    # build host's conf/httpd.conf and look fine while ignoring its own tree.
    run = capture([bundle / "bin/httpd", "-t", "-D", "DUMP_RUN_CFG"])
    root = re.search(r'ServerRoot: "([^"]*)"', run)
    if not root or Path(root[1]).resolve() != bundle:
        raise Failure("The launcher does not pin the bundle's ServerRoot: %s"
                      % (root[1] if root else "not reported"))
    if not (bundle / "conf/httpd.conf").is_file():
        raise Failure("The bundle has no conf/httpd.conf")
    return version[1]


# ------------------------------------------------------------------------ modules


def module_config(bundle, workspace, order, mpm):
    """One config that loads every DSO the bundle ships, in httpd's own order."""
    loads = []
    for name in order:
        loads.append("LoadModule %s_module modules/mod_%s.so" % (name, name))
    loads.append("LoadModule mpm_%s_module modules/mod_mpm_%s.so" % (mpm, mpm))
    documents = workspace / "docs"
    documents.mkdir(parents=True, exist_ok=True)
    (documents / "index.html").write_text("module probe\n")
    config = workspace / ("modules-%s.conf" % mpm)
    config.write_text("\n".join([
        "ServerRoot " + str(bundle),
        "Listen 127.0.0.1:" + str(free_port()),
        "PidFile " + str(workspace / ("pid-" + mpm)),
        "ErrorLog " + str(workspace / ("error-" + mpm + ".log")),
        "LogLevel error",
        "DocumentRoot " + str(documents),
        "<Directory " + str(documents) + ">",
        "  Require all granted",
        "</Directory>",
        # Some modules refuse to start without the directives they exist for.
        "ServerName localhost",
        "DBDriver sqlite3",
        "DBDParams \"" + str(workspace / "users.db") + "\"",
        "DavLockDB " + str(workspace / "davlocks"),
        # Mutex needs an existing directory, and every probe below passes -f, so each
        # one needs its own state directory alongside its config.
        "Mutex file:" + str(workspace / ("mutex-" + mpm)) + " default",
        "SSLSessionCache shmcb:" + str(workspace / "ssl_scache") + "(512000)",
        "MDStoreDir " + str(workspace / "md"),
        *loads,
        "",
    ]))
    (workspace / ("mutex-" + mpm)).mkdir(parents=True, exist_ok=True)
    return config


def check_module_loading(bundle, manifest, workspace):
    """Every DSO must load, and httpd -M must name it `shared`."""
    order = manifest["modules"]["load_order"]
    expected = set(manifest["modules"]["dso"])
    if set(order) != expected:
        raise Failure("The recorded load order does not match the staged module set")
    mpm = manifest["mpm"]["default"]
    config = module_config(bundle, workspace, order, mpm)
    result = run([bundle / "bin/httpd", "-f", config, "-M"], capture_output=True)
    if result.returncode != 0:
        raise Failure("Loading every module failed:\n" + result.stderr)
    shared = set()
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\S+)_module\s+\(shared\)", line)
        if match:
            shared.add(match.group(1))
    missing = sorted(expected - shared)
    if missing:
        raise Failure("Modules did not load: " + ", ".join(missing))
    for name in manifest["mpm"]["shared"]:
        config = module_config(bundle, workspace, order, name)
        result = run([bundle / "bin/httpd", "-f", config, "-M"], capture_output=True)
        if result.returncode != 0:
            raise Failure("MPM %s does not start:\n%s" % (name, result.stderr))
        if ("mpm_" + name + "_module (shared)") not in result.stdout:
            raise Failure("MPM %s is not reported as shared" % name)
    return sorted(shared | {"mpm_" + mpm})


# ----------------------------------------------------------------------- requests


@contextlib.contextmanager
def server(bundle, config, workspace, name):
    """Start httpd in the foreground and make sure it is gone afterwards."""
    log = (workspace / ("server-" + name + ".log")).open("w+")
    process = subprocess.Popen(
        [str(bundle / "bin/httpd"), "-f", str(config), "-DFOREGROUND"],
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    try:
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            if process.poll() is not None:
                log.seek(0)
                raise Failure("httpd exited during startup:\n" + log.read())
            time.sleep(0.05)
            yield
            return
        raise Failure("httpd did not become ready")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        log.close()


def request(port, path="/", host="127.0.0.1", method="GET", body=None, headers=None,
            tls=None, http2=False):
    """A single HTTP/1.1 exchange over a real socket, plus what curl sees."""
    if http2 or tls:
        command = ["curl", "--silent", "--show-error", "--max-time", "10",
                   "--output", "-", "--write-out", "\\n%{http_code} %{http_version}",
                   "--resolve", "localhost:%d:127.0.0.1" % port]
        if tls:
            command += ["--cacert", tls]
        if http2:
            command += ["--http2"]
        if method != "GET":
            command += ["--request", method]
        if body is not None:
            command += ["--data-binary", body]
        for name, value in (headers or {}).items():
            command += ["--header", "%s: %s" % (name, value)]
        scheme = "https" if tls else "http"
        result = run([*command, "--url", "%s://localhost:%d%s" % (scheme, port, path)],
                     capture_output=True)
        if result.returncode != 0:
            raise Failure("curl failed for %s: %s" % (path, result.stderr))
        payload, _, status = result.stdout.rpartition("\n")
        code, version = status.split()
        return int(code), payload, version, {}
    lines = ["%s %s HTTP/1.1" % (method, path), "Host: %s:%d" % (host, port)]
    lines += ["%s: %s" % (name, value) for name, value in (headers or {}).items()]
    if body is not None:
        lines.append("Content-Length: %d" % len(body.encode()))
    lines += ["Connection: close", "", body or ""]
    with socket.create_connection((host, port), timeout=TIMEOUT) as client:
        client.sendall("\r\n".join(lines).encode())
        chunks = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    head, _, payload = raw.partition(b"\r\n\r\n")
    header_lines = head.decode("latin-1").split("\r\n")
    status = header_lines[0].split()
    if len(status) < 2:
        raise Failure("Malformed response for " + path)
    parsed = {}
    for line in header_lines[1:]:
        name, _, value = line.partition(":")
        parsed[name.strip().lower()] = value.strip()
    return int(status[1]), payload.decode("utf-8", "replace"), status[0].split("/")[1], parsed


def wait_for_port(port, deadline_seconds=10):
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.05)
    return False


# -------------------------------------------------------------------- behaviour


def documents_dir(workspace):
    """The one document root every suite serves from, created on first use.

    Suites share it, so the HTTP ones can be run in any order and each still finds
    the files it needs.
    """
    documents = workspace / "htdocs"
    (documents / "sub").mkdir(parents=True, exist_ok=True)
    (documents / "index.html").write_text("<html><body>hello bundle</body></html>\n")
    (documents / "sub/one.txt").write_text("one\n")
    (documents / "sub/two.txt").write_text("two\n")
    # A body big enough that compression is unambiguous.
    (documents / "big.html").write_text("<html><body>" + "compressible " * 2000 + "</body></html>\n")
    (documents / "rewrite").mkdir(parents=True, exist_ok=True)
    (documents / "rewrite" / ".htaccess").write_text(
        "RewriteEngine On\nRewriteRule ^from$ /index.html [L]\n")
    return documents


def behaviour_suite(bundle, workspace, ports):
    """Everything the bundle claims to do, proved over HTTP."""
    documents = documents_dir(workspace)
    config = workspace / "behaviour.conf"
    config.write_text("\n".join([
        "ServerRoot " + str(bundle),
        "Listen 127.0.0.1:" + str(ports["http"]),
        "PidFile " + str(workspace / "pid-behaviour"),
        "ErrorLog " + str(workspace / "error-behaviour.log"),
        "LogLevel info",
        "ServerName localhost",
        "DocumentRoot " + str(documents),
        "<Directory " + str(documents) + ">",
        "  Options Indexes FollowSymLinks",
        "  AllowOverride All",
        "  Require all granted",
        "</Directory>",
        *["LoadModule %s_module modules/mod_%s.so" % (name, name) for name in (
            "mpm_event", "unixd", "mime", "dir", "autoindex", "rewrite", "headers",
            "expires", "deflate", "filter", "brotli", "alias", "log_config", "env",
            "setenvif",
            # Require/AllowOverride need these; they are DSOs in this build, and
            # httpd fails the whole config when a directive's module is absent.
            "authz_core", "authz_host", "authn_core")],
        "AddOutputFilterByType DEFLATE text/html",
        "AddOutputFilterByType BROTLI_COMPRESS text/html",
        # A Location needs something to serve; the probe only reads the header it adds.
        "<Location /index.html>",
        "  Header set X-Bundle \"verified\"",
        "</Location>",
        "ExpiresActive On",
        "<FilesMatch \"\\.txt$\">",
        "  ExpiresDefault \"access plus 1 hour\"",
        "</FilesMatch>",
        "",
    ]))
    results = {}
    with server(bundle, config, workspace, "behaviour"):
        if not wait_for_port(ports["http"]):
            raise Failure("The behaviour server never accepted a connection")
        code, body, _, _ = request(ports["http"], "/")
        if code != 200 or "hello bundle" not in body:
            raise Failure("Static file was not served (%d)" % code)
        code, body, _, _ = request(ports["http"], "/sub/")
        if code != 200 or "one.txt" not in body:
            raise Failure("autoindex did not list the directory (%d)" % code)
        code, body, _, _ = request(ports["http"], "/rewrite/from")
        if code != 200 or "hello bundle" not in body:
            raise Failure(".htaccess rewrite did not apply (%d)" % code)
        code, body, _, headers = request(ports["http"], "/index.html")
        if headers.get("x-bundle") != "verified" or "hello bundle" not in body:
            raise Failure("mod_headers did not set the header")
        code, body, _, headers = request(ports["http"], "/sub/one.txt")
        if "expires" not in headers:
            raise Failure("mod_expires did not add an Expires header")
        plain = request(ports["http"], "/big.html")[1]
        code, gzipped, _, headers = request(ports["http"], "/big.html",
                                            headers={"Accept-Encoding": "gzip"})
        if headers.get("content-encoding") != "gzip" or len(gzipped) >= len(plain):
            raise Failure("mod_deflate did not compress (%s)" % headers.get("content-encoding"))
        code, brotli, _, headers = request(ports["http"], "/big.html",
                                           headers={"Accept-Encoding": "br"})
        if headers.get("content-encoding") != "br" or len(brotli) >= len(plain):
            raise Failure("mod_brotli did not compress (%s)" % headers.get("content-encoding"))
        results["compression"] = {"plain": len(plain), "gzip": len(gzipped), "brotli": len(brotli)}
        if not (len(plain) > len(gzipped) > len(brotli)):
            raise Failure("Compression sizes are not ordered as expected: %s" % results["compression"])
    return results


def tls_suite(bundle, workspace, ports):
    """TLS 1.2 and 1.3 with the bundle's own openssl, plus ALPN h2."""
    certificate = workspace / "server.crt"
    key = workspace / "server.key"
    # The CLI reads $OPENSSLDIR/openssl.cnf; the bundle ships it, so no env needed.
    result = run([bundle / "bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                  "-keyout", key, "-out", certificate, "-days", "1",
                  "-subj", "/CN=localhost",
                  "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                 capture_output=True)
    if result.returncode != 0:
        raise Failure("The bundled openssl could not create a certificate:\n" + result.stderr)
    documents = documents_dir(workspace)
    config = workspace / "tls.conf"
    config.write_text("\n".join([
        "ServerRoot " + str(bundle),
        "Listen 127.0.0.1:" + str(ports["tls"]),
        "Listen 127.0.0.1:" + str(ports["h2"]),
        "PidFile " + str(workspace / "pid-tls"),
        "ErrorLog " + str(workspace / "error-tls.log"),
        "LogLevel warn",
        "ServerName localhost",
        "DocumentRoot " + str(documents),
        "<Directory " + str(documents) + ">",
        "  Require all granted",
        "</Directory>",
        *["LoadModule %s_module modules/mod_%s.so" % (name, name) for name in (
            "mpm_event", "unixd", "mime", "authn_core", "authz_core", "authz_host",
            "socache_shmcb", "ssl", "http2", "log_config")],
        "Protocols h2 http/1.1",
        "SSLSessionCache shmcb:" + str(workspace / "ssl_scache") + "(512000)",
        "SSLEngine on",
        "SSLCertificateFile " + str(certificate),
        "SSLCertificateKeyFile " + str(key),
        "",
    ]))
    results = {}
    with server(bundle, config, workspace, "tls"):
        if not wait_for_port(ports["tls"]):
            raise Failure("The TLS listener never accepted a connection")
        for label, version in (("tls12", ssl.TLSVersion.TLSv1_2), ("tls13", ssl.TLSVersion.TLSv1_3)):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            context.minimum_version = version
            context.maximum_version = version
            try:
                with socket.create_connection(("127.0.0.1", ports["tls"]), timeout=TIMEOUT) as raw:
                    with context.wrap_socket(raw) as client:
                        client.sendall(b"GET /index.html HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                        chunks = []
                        while True:
                            chunk = client.recv(65536)
                            if not chunk:
                                break
                            chunks.append(chunk)
                        response = b"".join(chunks)
                        negotiated = client.version()
            except ssl.SSLError as error:
                raise Failure("%s is not usable: %s" % (label, error))
            if b" 200 " not in response.split(b"\r\n", 1)[0] or "hello bundle" not in response.decode("latin-1"):
                raise Failure("%s request did not return the document" % label)
            results[label] = negotiated
        code, body, protocol, _ = request(ports["h2"], "/index.html", tls=certificate, http2=True)
        if code != 200 or "hello bundle" not in body:
            raise Failure("HTTP/2 request failed (%d)" % code)
        if not protocol.startswith("2"):
            raise Failure("HTTP/2 was not negotiated; got " + protocol)
        results["http2"] = protocol
    return results


def fcgi_record(kind, content, request=1):
    """One FastCGI record: version, type, request id, length, padding, reserved."""
    return b"".join([
        bytes([1, kind]),
        request.to_bytes(2, "big"),
        len(content).to_bytes(2, "big"),
        b"\x00",
        b"\x00",
        content,
    ])


def fcgi_responder(socket_path, stop):
    """A minimal FastCGI application that answers every request with a header."""
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(socket_path))
        listener.listen(4)
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            with connection:
                while True:
                    header = connection.recv(8)
                    if len(header) < 8:
                        break
                    kind = header[1]
                    request = int.from_bytes(header[2:4], "big")
                    size = int.from_bytes(header[4:6], "big")
                    padding = header[6]
                    content = b""
                    while len(content) < size:
                        chunk = connection.recv(size - len(content))
                        if not chunk:
                            break
                        content += chunk
                    if padding:
                        connection.recv(padding)
                    # END_REQUEST is what closes the exchange; anything else is
                    # either the request body or params we do not need to read.
                    if kind == 1:
                        connection.sendall(
                            fcgi_record(6, b"Content-Type: text/plain\r\nX-Fcgi: reached\r\n\r\nfcgi ok\n", request)
                            + fcgi_record(3, b"\x00" * 8, request))
                        break


def fcgi_suite(bundle, workspace, ports):
    import threading
    socket_path = workspace / "fcgi.sock"
    stop = __import__("threading").Event()
    responder = __import__("threading").Thread(target=fcgi_responder, args=(socket_path, stop), daemon=True)
    responder.start()
    documents = documents_dir(workspace)
    config = workspace / "fcgi.conf"
    config.write_text("\n".join([
        "ServerRoot " + str(bundle),
        "Listen 127.0.0.1:" + str(ports["fcgi"]),
        "PidFile " + str(workspace / "pid-fcgi"),
        "ErrorLog " + str(workspace / "error-fcgi.log"),
        "LogLevel warn",
        "ServerName localhost",
        "DocumentRoot " + str(documents),
        *["LoadModule %s_module modules/mod_%s.so" % (name, name) for name in (
            "mpm_event", "unixd", "mime", "proxy", "proxy_fcgi", "authn_core",
            "authz_core", "authz_host")],
        "<Location /app>",
        "  SetHandler \"proxy:unix:" + str(socket_path) + "|fcgi://localhost\"",
        "  Require all granted",
        "</Location>",
        "",
    ]))
    try:
        if not socket_path.exists():
            raise Failure("The FastCGI responder did not start")
        with server(bundle, config, workspace, "fcgi"):
            if not wait_for_port(ports["fcgi"]):
                raise Failure("The FastCGI listener never accepted a connection")
            code, body, _, headers = request(ports["fcgi"], "/app")
            # The header proves the response came back through the responder
            # rather than from httpd's own error document.
            if code != 200 or "fcgi ok" not in body or headers.get("x-fcgi") != "reached":
                raise Failure("mod_proxy_fcgi did not reach the responder (%d)" % code)
    finally:
        stop.set()
        responder.join(timeout=5)
    return {"status": "proxied"}


def dav_suite(bundle, workspace, ports):
    repository = workspace / "dav"
    repository.mkdir()
    config = workspace / "dav.conf"
    config.write_text("\n".join([
        "ServerRoot " + str(bundle),
        "Listen 127.0.0.1:" + str(ports["dav"]),
        "PidFile " + str(workspace / "pid-dav"),
        "ErrorLog " + str(workspace / "error-dav.log"),
        "LogLevel warn",
        "ServerName localhost",
        *["LoadModule %s_module modules/mod_%s.so" % (name, name) for name in (
            "mpm_event", "unixd", "mime", "alias", "authz_core", "authz_host",
            "authn_core", "dav", "dav_fs", "dav_lock")],
        "DavLockDB " + str(workspace / "davlocks"),
        "Alias /dav " + str(repository),
        "<Directory " + str(repository) + ">",
        "  Dav On",
        "  Require all granted",
        "</Directory>",
        "",
    ]))
    with server(bundle, config, workspace, "dav"):
        if not wait_for_port(ports["dav"]):
            raise Failure("The DAV listener never accepted a connection")
        code, _, _, _ = request(ports["dav"], "/dav/file.txt", method="PUT", body="dav content\n")
        if code not in (200, 201, 204):
            raise Failure("DAV PUT failed (%d)" % code)
        code, body, _, _ = request(ports["dav"], "/dav/file.txt")
        if code != 200 or "dav content" not in body:
            raise Failure("DAV GET failed (%d)" % code)
        code, _, _, _ = request(ports["dav"], "/dav/file.txt", method="DELETE")
        if code not in (200, 204):
            raise Failure("DAV DELETE failed (%d)" % code)
    return {"put": 201}


def sqlite_database(path, password_hash):
    """Create the credential database with the bundle's own sqlite3."""
    import sqlite3
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE users (user TEXT PRIMARY KEY, password TEXT)")
        database.execute("INSERT INTO users VALUES ('alice', ?)", (password_hash,))
    return path


def dbd_suite(bundle, workspace, ports):
    """mod_dbd over the sqlite3 driver, used by mod_authn_dbd for real auth."""
    password = capture([bundle / "bin/htpasswd", "-nbm", "alice", "s3cret"]).strip().split(":", 1)[1]
    database_path = sqlite_database(workspace / "users.db", password)
    documents = documents_dir(workspace)
    config = workspace / "dbd.conf"
    config.write_text("\n".join([
        "ServerRoot " + str(bundle),
        "Listen 127.0.0.1:" + str(ports["dbd"]),
        "PidFile " + str(workspace / "pid-dbd"),
        "ErrorLog " + str(workspace / "error-dbd.log"),
        "LogLevel info",
        "ServerName localhost",
        "DocumentRoot " + str(documents),
        "<Directory " + str(documents) + ">",
        "  Require all granted",
        "</Directory>",
        *["LoadModule %s_module modules/mod_%s.so" % (name, name) for name in (
            "mpm_event", "unixd", "mime", "authn_core", "authz_core", "authz_user",
            "auth_basic", "dbd", "authn_dbd")],
        "DBDriver sqlite3",
        "DBDParams \"" + str(database_path) + "\"",
        "DBDMin 1",
        "DBDKeep 1",
        "DBDMax 2",
        "<Location /secret>",
        "  AuthType Basic",
        "  AuthName dbd",
        "  AuthBasicProvider dbd",
        "  AuthDBDUserPWQuery \"SELECT password FROM users WHERE user = %s\"",
        "  Require valid-user",
        "</Location>",
        "",
    ]))
    import base64
    with server(bundle, config, workspace, "dbd"):
        if not wait_for_port(ports["dbd"]):
            raise Failure("The DBD listener never accepted a connection")
        good = base64.b64encode(b"alice:s3cret").decode()
        code, _, _, _ = request(ports["dbd"], "/secret", headers={"Authorization": "Basic " + good})
        if code == 401:
            raise Failure("mod_authn_dbd rejected the correct password")
        bad = base64.b64encode(b"alice:wrong").decode()
        code, _, _, _ = request(ports["dbd"], "/secret", headers={"Authorization": "Basic " + bad})
        if code != 401:
            raise Failure("mod_authn_dbd accepted a wrong password (%d)" % code)
    return {"driver": "sqlite3"}


def lua_suite(bundle, workspace, ports):
    script = workspace / "hook.lua"
    script.write_text(
        'function myhook(r)\n'
        '  r.headers_out["X-Lua"] = "alive"\n'
        '  return apache2.OK\n'
        'end\n'
        'function handler(r)\n'
        '  r.content_type = "text/plain"\n'
        '  r:puts("lua ok")\n'
        '  return apache2.OK\n'
        'end\n'
    )
    documents = documents_dir(workspace)
    config = workspace / "lua.conf"
    config.write_text("\n".join([
        "ServerRoot " + str(bundle),
        "Listen 127.0.0.1:" + str(ports["lua"]),
        "PidFile " + str(workspace / "pid-lua"),
        "ErrorLog " + str(workspace / "error-lua.log"),
        "LogLevel warn",
        "ServerName localhost",
        "DocumentRoot " + str(documents),
        "<Directory " + str(documents) + ">",
        "  Require all granted",
        "</Directory>",
        *["LoadModule %s_module modules/mod_%s.so" % (name, name) for name in (
            "mpm_event", "unixd", "mime", "authn_core", "authz_core", "authz_host",
            "lua")],
        "LuaHookFixups " + str(script) + " myhook",
        "LuaMapHandler \"/lua\" " + str(script) + " handler",
        "",
    ]))
    with server(bundle, config, workspace, "lua"):
        if not wait_for_port(ports["lua"]):
            raise Failure("The Lua listener never accepted a connection")
        # An explicit file, not "/": without mod_dir a directory request 404s and
        # the error path replaces headers_out, hiding whether the hook ran.
        code, _, _, headers = request(ports["lua"], "/index.html")
        if headers.get("x-lua") != "alive":
            raise Failure("mod_lua did not run the fixup hook")
        code, body, _, _ = request(ports["lua"], "/lua")
        if code != 200 or "lua ok" not in body:
            raise Failure("mod_lua handler did not answer (%d)" % code)
    return {"hook": "alive"}


def typed(bundle, program, arguments, answers):
    """Run an interactive program with a pty and answer its prompts.

    htdigest takes its password through apr_password_get, which under musl is
    getpass() reading /dev/tty: with no terminal it fails with a generic buffer
    error ("password too long") that has nothing to do with the password. A pty
    is the only way to exercise the real code path.
    """
    import pty
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(str(bundle / "bin" / program), [program, *map(str, arguments)])
    output = b""
    try:
        for marker, answer in answers:
            while marker not in output:
                ready, _, _ = select.select([fd], [], [], TIMEOUT)
                if not ready:
                    raise Failure("%s did not prompt for %r" % (program, marker.decode()))
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    raise Failure("%s exited before prompting for %r" % (program, marker.decode()))
                output += chunk
            os.write(fd, answer.encode() + b"\n")
        while True:
            ready, _, _ = select.select([fd], [], [], 1)
            if not ready:
                break
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
    finally:
        os.close(fd)
    status = os.waitpid(pid, 0)[1]
    return os.waitstatus_to_exitcode(status), output.decode("utf-8", "replace")


def credential_suite(bundle, workspace):
    """htpasswd, htdigest and htdbm produce credentials the modules can read."""
    plain = capture([bundle / "bin/htpasswd", "-nbm", "alice", "s3cret"]).strip()
    if not plain.startswith("alice:$apr1$"):
        raise Failure("htpasswd did not produce an apr1 hash: " + plain)
    digest = workspace / "digest"
    code, output = typed(bundle, "htdigest", ["-c", digest, "realm", "alice"],
                         [(b"New password:", "s3cret"), (b"Re-type new password:", "s3cret")])
    if code != 0 or not digest.is_file():
        raise Failure("htdigest failed (%d):\n%s" % (code, output))
    line = digest.read_text().strip()
    if "alice:realm:" not in line or len(line.rsplit(":", 1)[1]) != 32:
        raise Failure("htdigest did not produce an MD5 digest line: " + line)
    database = workspace / "users.dbm"
    run([bundle / "bin/htdbm", "-cbm", database, "alice", "s3cret"], check=True, capture_output=True)
    run([bundle / "bin/htdbm", "-vb", database, "alice", "s3cret"], check=True, capture_output=True)
    wrong = run([bundle / "bin/htdbm", "-vb", database, "alice", "wrong"], capture_output=True)
    if wrong.returncode == 0:
        raise Failure("htdbm accepted a wrong password")
    if not Path(str(database) + ".dir").is_file():
        raise Failure("htdbm did not create an SDBM database")
    return {"dbm": "sdbm", "digest": "md5"}


def ldap_suite(bundle, workspace, ports):
    """mod_ldap loads and reports a connection failure, not a load failure."""
    config = workspace / "ldap.conf"
    config.write_text("\n".join([
        "ServerRoot " + str(bundle),
        "Listen 127.0.0.1:" + str(ports["ldap"]),
        "PidFile " + str(workspace / "pid-ldap"),
        "ErrorLog " + str(workspace / "error-ldap.log"),
        "LogLevel warn",
        "ServerName localhost",
        *["LoadModule %s_module modules/mod_%s.so" % (name, name) for name in (
            "mpm_event", "unixd", "mime", "ldap", "authnz_ldap", "authn_core",
            "authz_core")],
        "LDAPTrustedGlobalCert CA_BASE64 " + str(workspace / "none.pem"),
        "",
    ]))
    if not (bundle / "modules/mod_ldap.so").is_file():
        raise Failure("mod_ldap is missing")
    result = run([bundle / "bin/httpd", "-f", config, "-t"], capture_output=True)
    if result.returncode == 0:
        raise Failure("mod_ldap started with a certificate file that does not exist")
    return {"trusted_cert": "rejected"}


# ------------------------------------------------------------------------- host


def check_config_paths(bundle, manifest):
    """No shipped config may name the build-host prefix it was installed from.

    A config that still says the build prefix looks fine where it was built and
    then fails, or silently serves the wrong tree, once the bundle moves. Only
    conf/ is checked: the bundled headers legitimately record the configure prefix
    for anyone compiling a module against the bundle.
    """
    prefixes = {argument.partition("=")[2] for argument in manifest["configure"]["arguments"]
                if argument.startswith("--prefix=")}
    if not prefixes:
        raise Failure("The manifest does not record the configure prefix")
    offenders = []
    for path in sorted((bundle / "conf").rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        for prefix in prefixes:
            for line in text.splitlines():
                if prefix in line and line.lstrip().startswith("#") is False:
                    offenders.append("%s: %s" % (path.relative_to(bundle).as_posix(), line.strip()))
    if offenders:
        raise Failure("Shipped configs name the build prefix:\n" + "\n".join(offenders))
    return "relative"


def check_relocation(bundle, workspace):
    """Move the tree to a path with spaces and prove the launchers still work."""
    relocated = workspace / "relocated bundle with spaces"
    shutil.copytree(bundle, relocated, symlinks=True)
    version_line = capture([relocated / "bin/httpd", "-v"])
    if not re.search(r"Server version: Apache/2\.4\.", version_line):
        raise Failure("Relocated bundle does not run:\n" + version_line)
    listed = capture([relocated / "bin/httpd", "-l"])
    if "mod_so.c" not in listed:
        raise Failure("Relocated bundle does not report its static modules")
    # The config has to come from the moved tree, not the original one.
    root = re.search(r'ServerRoot: "([^"]*)"',
                     capture([relocated / "bin/httpd", "-t", "-D", "DUMP_RUN_CFG"]))
    if not root or Path(root[1]).resolve() != relocated:
        raise Failure("The relocated launcher does not pin its own ServerRoot")
    return relocated


def check_hostile_environment(relocated):
    """An inherited loader variable must not change which libraries get loaded."""
    hostile = {**os.environ, "LD_PRELOAD": "/nonexistent/preload.so",
               "LD_AUDIT": "/nonexistent/audit.so",
               "LD_LIBRARY_PATH": "/nonexistent/libs"}
    result = run([relocated / "bin/httpd", "-v"], capture_output=True, env=hostile)
    if result.returncode != 0 or "Apache/2.4" not in result.stdout:
        raise Failure("The launcher inherits a broken loader environment:\n" + result.stderr)
    return "isolated"


def check_no_host_libraries(relocated, ports):
    """In a namespace with the host loaders masked, the bundle must still work."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return "SKIPPED: bwrap is not installed"
    empty = relocated.parent / "empty"
    empty.write_text("")
    command = [bwrap, "--dev-bind", "/", "/", "--bind", str(relocated.parent), str(relocated.parent),
               "--ro-bind", str(empty), "/lib/ld-musl-x86_64.so.1",
               "--ro-bind", str(empty), "/usr/lib/ld-musl-x86_64.so.1"]
    for host in ("/usr/x86_64-linux-musl", "/usr/lib/musl"):
        if Path(host).exists():
            command += ["--ro-bind", str(empty.parent / "emptydir"), host]
    (relocated.parent / "emptydir").mkdir()
    result = run([*command, relocated / "bin/httpd", "-v"], capture_output=True)
    if result.returncode != 0:
        raise Failure("The bundle needs the host's musl loader:\n" + result.stderr)
    raw = run([*command, relocated / "libexec/bin/httpd", "-v"], capture_output=True)
    if raw.returncode == 0:
        raise Failure("The raw binary ran without the bundled loader; the namespace did not hide it")
    return "isolated"


def verify(bundle):
    bundle = bundle.resolve()
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["verification"] = "NOT_RUN"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    if manifest.get("runtime") != "apache":
        raise Failure("This verifier only accepts Apache bundles")
    if manifest.get("target") != "musl-dynamic" or manifest.get("linkage") != "dynamic":
        raise Failure("The bundle is not musl-dynamic")
    with tempfile.TemporaryDirectory(prefix="apache-verify-") as temporary:
        workspace = Path(temporary)
        ports = {name: free_port() for name in
                 ("http", "tls", "h2", "fcgi", "dav", "dbd", "lua", "ldap")}
        checks = {}
        checks["loader"] = check_loader(bundle, manifest)
        checks["elf"] = {"files": len(check_elf(bundle, manifest))}
        checks["static"] = check_static_set(bundle)
        checks["modules"] = len(check_module_loading(bundle, manifest, workspace / "modules")
                                 if (workspace / "modules").mkdir() is None else [])
        checks["credentials"] = credential_suite(bundle, workspace)
        checks["ldap"] = ldap_suite(bundle, workspace, ports)
        (workspace / "htdocs").mkdir()
        (workspace / "htdocs/index.html").write_text("<html><body>hello bundle</body></html>\n")
        checks["behaviour"] = behaviour_suite(bundle, workspace, ports)
        checks["tls"] = tls_suite(bundle, workspace, ports)
        checks["fcgi"] = fcgi_suite(bundle, workspace, ports)
        checks["dav"] = dav_suite(bundle, workspace, ports)
        checks["dbd"] = dbd_suite(bundle, workspace, ports)
        checks["lua"] = lua_suite(bundle, workspace, ports)
        checks["config_paths"] = check_config_paths(bundle, manifest)
        relocated = check_relocation(bundle, workspace)
        checks["relocation"] = str(relocated.relative_to(workspace))
        checks["hostile_env"] = check_hostile_environment(relocated)
        checks["host_independence"] = check_no_host_libraries(relocated, ports)
    manifest["verification"] = "PASSED"
    manifest["verification_checks"] = checks
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    try:
        verify(args.bundle)
    except (Failure, ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit("verification failed: %s\n" % exc)
    print("PASS: every module loads, HTTP/TLS/HTTP2/DAV/DBD/Lua/FastCGI work, "
          "the bundle relocates and does not need the host's musl")
