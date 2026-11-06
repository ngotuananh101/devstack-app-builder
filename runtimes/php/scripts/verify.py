#!/usr/bin/env python3
"""Real bundle validation. Runs only trusted built binaries; never ldd."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time

GLIBC_SYSTEM = {"libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1", "libresolv.so.2", "libutil.so.1", "ld-linux-x86-64.so.2"}


def parse_needed(output):
    return re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", output)


def check_glibc_versions(output):
    versions = set()
    for token in set(re.findall(r"\bGLIBC_[A-Za-z0-9_.]+\b", output)):
        match = re.fullmatch(r"GLIBC_([0-9]+(?:\.[0-9]+)+)", token)
        if not match:
            raise ValueError(f"Unsupported glibc ABI dependency: {token}")
        version = match.group(1)
        if tuple(map(int, version.split("."))) > (2, 17):
            raise ValueError(f"Required GLIBC_{version} exceeds glibc 2.17 baseline")
        versions.add(version)
    return sorted(versions, key=lambda value: tuple(map(int, value.split("."))))


def check_dependencies(needed, target):
    unexpected = set(needed) - (GLIBC_SYSTEM if target == "glibc" else set())
    if unexpected:
        raise ValueError(f"Unbundled runtime dependencies: {sorted(unexpected)}")


def elf_inventory(bundle, target):
    inventory = []
    for binary in [bundle / "libexec/php", bundle / "libexec/php-fpm", *sorted((bundle / "lib/php/extensions").glob("*.so"))]:
        dynamic = subprocess.check_output(["readelf", "-d", str(binary)], text=True)
        versions = subprocess.check_output(["readelf", "--version-info", str(binary)], text=True)
        needed = parse_needed(dynamic)
        check_dependencies(needed, target)
        if target == "glibc" and re.search(r"\((?:RELR|RELRSZ|RELRENT)\)", dynamic):
            raise ValueError("Packed RELR relocations exceed glibc 2.17 baseline")
        glibc_versions = check_glibc_versions(versions)
        # A checkout RPATH must never leak into a relocated bundle.
        for path in re.findall(r"\((?:RUNPATH|RPATH)\).*?\[([^\]]+)\]", dynamic):
            if any(part and not part.startswith("$ORIGIN") for part in path.split(":")):
                raise ValueError(f"Non-relocatable ELF RPATH: {path}")
        inventory.append({"path": str(binary.relative_to(bundle)), "needed": needed, "glibc_versions": glibc_versions})
    return inventory


def record(kind, content):
    if len(content) > 65535:
        raise ValueError("FastCGI record too large")
    return struct.pack("!BBHHBB", 1, kind, 1, len(content), 0, 0) + content


def fcgi_request(sock_path, script):
    def length(value):
        return bytes([value]) if value < 128 else struct.pack("!I", value | 0x80000000)
    parameters = {"SCRIPT_FILENAME": str(script), "SCRIPT_NAME": "/probe.php", "REQUEST_METHOD": "GET", "SERVER_PROTOCOL": "HTTP/1.1", "GATEWAY_INTERFACE": "CGI/1.1", "SERVER_SOFTWARE": "devstack-verifier", "REMOTE_ADDR": "127.0.0.1", "SERVER_NAME": "localhost", "SERVER_PORT": "80"}
    payload = b""
    for name, value in parameters.items():
        key, value = name.encode(), value.encode()
        payload += length(len(key)) + length(len(value)) + key + value
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(10)
        client.connect(str(sock_path))
        client.sendall(record(1, struct.pack("!HB5x", 1, 0)) + record(4, payload) + record(4, b"") + record(5, b""))
        def receive(size):
            data = b""
            while len(data) < size:
                chunk = client.recv(size - len(data))
                if not chunk:
                    raise ValueError("Unexpected FastCGI EOF")
                data += chunk
            return data
        stdout, stderr = b"", b""
        while True:
            version, kind, request_id, size, padding, _ = struct.unpack("!BBHHBB", receive(8))
            if version != 1 or request_id != 1:
                raise ValueError("Invalid FastCGI response")
            content = receive(size)
            receive(padding)
            if kind == 6:
                stdout += content
            elif kind == 7:
                stderr += content
            elif kind == 3:
                status, protocol = struct.unpack("!IB3x", content)
                if status or protocol:
                    raise ValueError(f"FastCGI failure: {status}/{protocol}")
                break
        if stderr:
            raise ValueError(f"FPM stderr: {stderr.decode(errors='replace')}")
    headers, body = stdout.split(b"\r\n\r\n", 1)
    if b"Status: 500" in headers:
        raise ValueError(stdout.decode(errors="replace"))
    return json.loads(body)


def fpm_probe(bundle, modules, workspace):
    if os.getuid() == 0:
        raise ValueError("Runtime FPM verification must run as non-root")
    with tempfile.TemporaryDirectory(prefix="fpm-", dir=workspace) as tmp:
        root = Path(tmp)
        sock = root / "fpm.sock"
        if len(os.fsencode(sock)) >= 104:
            raise ValueError("FPM verification socket path too long; use a short /tmp workspace")
        script = root / "probe.php"
        names = json.dumps(modules)
        script.write_text('<?php $names=json_decode(' + json.dumps(names) + ',true); $modules=[]; foreach($names as $name){$modules[$name]=extension_loaded($name);} preg_match("/^Uid:\\s+(\\d+)/m",file_get_contents("/proc/self/status"),$uid); echo json_encode(["modules"=>(object)$modules,"uid"=>(int)$uid[1],"memory_limit"=>ini_get("memory_limit"),"extension_dir"=>ini_get("extension_dir")]);')
        config = root / "fpm.conf"
        config.write_text('[global]\ndaemonize=no\nerror_log=/dev/stderr\npid="' + str(root / 'fpm.pid') + '"\n[probe]\nlisten="' + str(sock) + '"\npm=static\npm.max_children=1\nclear_env=no\ncatch_workers_output=yes\n')
        command = [str(bundle / "bin/php-fpm"), "-y", str(config)]
        subprocess.run([*command, "-t"], check=True, capture_output=True)
        process = subprocess.Popen([*command, "-F"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not sock.exists():
                if process.poll() is not None:
                    raise ValueError(process.communicate()[1].decode())
                if time.monotonic() >= deadline:
                    raise ValueError("FPM socket did not become ready")
                time.sleep(0.05)
            result = fcgi_request(sock, script)
            if result["uid"] is not None and result["uid"] != os.getuid():
                raise ValueError("FPM changed runtime identity")
            if result["extension_dir"] != str(bundle / "lib/php/extensions"):
                raise ValueError("FPM loaded another bundle's extension_dir")
            return result
        finally:
            process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


def cli_modules(bundle, names, no_ini=False):
    expression = '$names=json_decode(' + json.dumps(json.dumps(names)) + ',true);$result=[];foreach($names as $name){$result[$name]=extension_loaded($name);}echo json_encode((object)$result);'
    command = [str(bundle / "bin/php")]
    if no_ini:
        command.append("-n")
    output = subprocess.run([*command, "-r", expression], check=True, text=True, capture_output=True)
    if output.stderr:
        raise ValueError(output.stderr)
    return json.loads(output.stdout)


def verify(bundle):
    bundle = bundle.resolve()
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["verification"] = "NOT_RUN"
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    names = [row["name"] for row in manifest["shared_modules"]]
    for row in manifest["shared_modules"]:
        if not (bundle / row["path"]).is_file():
            raise ValueError(f"Missing requested shared module: {row['name']}")
    inventory = elf_inventory(bundle, manifest["target"])
    with tempfile.TemporaryDirectory(prefix="ds-verify-") as tmp:
        workspace = Path(tmp)
        relocated = workspace / "relocated PHP with spaces"
        shutil.copytree(bundle, relocated)
        helper = relocated / "bin/php-ext"
        expected_disabled = {name: False for name in names}
        expected_enabled = {name: True for name in names}
        if cli_modules(relocated, names, no_ini=True) != expected_disabled:
            raise ValueError("Shared-only module exists in php -n baseline")
        if cli_modules(relocated, names) != expected_disabled:
            raise ValueError("Shared modules must default to disabled")
        if fpm_probe(relocated, names, workspace)["modules"] not in (expected_disabled, []):
            raise ValueError("FPM shared modules unexpectedly enabled by default")
        for name in names:
            subprocess.run([str(helper), "enable", name, "cli"], check=True)
        if cli_modules(relocated, names) != expected_enabled:
            raise ValueError("CLI enable did not load requested modules")
        if fpm_probe(relocated, names, workspace)["modules"] not in (expected_disabled, []):
            raise ValueError("CLI configuration leaked into FPM")
        for name in names:
            subprocess.run([str(helper), "enable", name, "fpm"], check=True)
        if fpm_probe(relocated, names, workspace)["modules"] not in (expected_enabled, [] if not names else None):
            raise ValueError("FPM enable did not load requested modules")
        # The original and relocated installations must not cross-load INIs.
        if cli_modules(bundle, names) != expected_disabled:
            raise ValueError("Relocated installation changed original bundle")
        for name in names:
            subprocess.run([str(helper), "disable", name, "all"], check=True)
        if cli_modules(relocated, names) != expected_disabled:
            raise ValueError("CLI disable failed")
        if fpm_probe(relocated, names, workspace)["modules"] not in (expected_disabled, []):
            raise ValueError("FPM disable failed")
    manifest["elf_inventory"] = inventory
    manifest["verification"] = "PASSED"
    manifest["verification_checks"] = ["elf_dependency_closure", "glibc_symbol_baseline", "cli_baseline", "cli_toggle", "fpm_config", "fpm_request_toggle", "non_root_fpm", "relocation_spaces", "installation_isolation"]
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    verify(args.bundle)
    print("PASS: real CLI/FPM requests, module toggles, relocation and ELF contracts")
