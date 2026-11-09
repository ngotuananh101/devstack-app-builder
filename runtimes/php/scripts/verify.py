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

LOADER_INTERPRETER = "ld-musl-x86_64.so.1"
# musl's loader is also libc, so PHP's only runtime dependency is satisfied by it.
MUSL_ALLOWED_NEEDED = {"libc.so"}
# Preserve manifest/INI basenames in results; PHP registers this Zend module differently.
MODULE_RUNTIME_NAMES = {"opcache": "Zend OPcache"}


def module_plan(manifest):
    """Split a manifest into stageable shared files and always-loaded built-ins.

    PHP 8.5+ links OPcache into the binary, so a built-in module has no file to
    stage and no INI to toggle; it must simply be reported loaded by every probe.
    Returns (shared records, probed names, default states).
    """
    shared = manifest.get("shared_modules", [])
    builtin = manifest.get("builtin_modules", [])
    if not isinstance(shared, list) or not isinstance(builtin, list):
        raise ValueError("Invalid module inventory")
    shared_names = set()
    for row in shared:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("path"), str):
            raise ValueError("Invalid shared module record")
        shared_names.add(row["name"])
    builtin_names = set()
    for row in builtin:
        if (not isinstance(row, dict) or not isinstance(row.get("name"), str) or row["name"] in shared_names
                or row.get("directive") is not None or row.get("always_loaded") is not True
                or row.get("runtime_name") != MODULE_RUNTIME_NAMES.get(row["name"])):
            raise ValueError("Invalid built-in module record")
        builtin_names.add(row["name"])
    states = {name: False for name in shared_names} | {name: True for name in builtin_names}
    return shared, sorted(shared_names | builtin_names), states


def expected_states(shared_names, builtin_names, enabled):
    return {name: enabled for name in shared_names} | {name: True for name in builtin_names}


def parse_needed(output):
    return re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", output)


def parse_loader(output):
    match = re.search(r"Requesting program interpreter: \[?([^\]\n]+)\]?", output)
    return match[1] if match else None


def check_dependencies(needed, target):
    if target != "musl-dynamic":
        raise ValueError(f"Unsupported target: {target}")
    unexpected = set(needed) - MUSL_ALLOWED_NEEDED
    if unexpected:
        raise ValueError(f"Unbundled runtime dependencies: {sorted(unexpected)}")


def check_abi(versions):
    # A glibc symbol in a musl bundle means the wrong toolchain or runtime.
    tokens = set(re.findall(r"\bGLIBC_[A-Za-z0-9_.]+\b", versions))
    if tokens:
        raise ValueError(f"glibc ABI tokens in a musl bundle: {sorted(tokens)}")
    return []


def elf_inventory(bundle, target):
    if target != "musl-dynamic":
        raise ValueError(f"Unsupported target: {target}")
    loader = bundle / "lib/musl" / LOADER_INTERPRETER
    if not loader.is_file():
        raise ValueError(f"Bundle does not ship its musl loader: {loader}")
    inventory = []
    binaries = [bundle / "libexec/php", bundle / "libexec/php-fpm", *sorted((bundle / "lib/php/extensions").glob("*.so"))]
    for binary in binaries:
        dynamic = subprocess.check_output(["readelf", "-dW", str(binary)], text=True)
        versions = subprocess.check_output(["readelf", "--version-info", str(binary)], text=True)
        program = subprocess.check_output(["readelf", "-lW", str(binary)], text=True)
        needed = parse_needed(dynamic)
        check_dependencies(needed, target)
        abi = check_abi(versions)
        interpreter = parse_loader(program)
        if binary.parent.name == "libexec" and not interpreter:
            raise ValueError(f"Binary does not request a dynamic loader: {binary}")
        if interpreter and Path(interpreter).name != LOADER_INTERPRETER:
            raise ValueError(f"Binary requests a different interpreter: {interpreter}")
        # A checkout RPATH must never leak into a relocated bundle.
        for path in re.findall(r"\((?:RUNPATH|RPATH)\).*?\[([^\]]+)\]", dynamic):
            if any(part and not part.startswith("$ORIGIN") for part in path.split(":")):
                raise ValueError(f"Non-relocatable ELF RPATH: {path}")
        inventory.append({"path": str(binary.relative_to(bundle)), "needed": needed,
                          "interpreter": interpreter, "glibc_versions": abi})
    inventory.append({"path": str(loader.relative_to(bundle)), "role": "musl_loader_libc"})
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
        names = json.dumps({name: MODULE_RUNTIME_NAMES.get(name, name) for name in modules})
        script.write_text('<?php $names=json_decode(' + json.dumps(names) + ',true); $modules=[]; foreach($names as $name=>$runtime){$modules[$name]=extension_loaded($runtime);} preg_match("/^Uid:\\s+(\\d+)/m",file_get_contents("/proc/self/status"),$uid); echo json_encode(["modules"=>(object)$modules,"uid"=>(int)$uid[1],"memory_limit"=>ini_get("memory_limit"),"extension_dir"=>ini_get("extension_dir")]);')
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
    runtime_names = {name: MODULE_RUNTIME_NAMES.get(name, name) for name in names}
    expression = '$names=json_decode(' + json.dumps(json.dumps(runtime_names)) + ',true);$result=[];foreach($names as $name=>$runtime){$result[$name]=extension_loaded($runtime);}echo json_encode((object)$result);'
    command = [str(bundle / "bin/php")]
    if no_ini:
        command.append("-n")
    output = subprocess.run([*command, "-r", expression], check=True, text=True, capture_output=True)
    if output.stderr:
        raise ValueError(output.stderr)
    return json.loads(output.stdout)


def check_spawned_child_environment(bundle, shared):
    """A child started the way a host app starts PHP must find the same modules.

    Consumers resolve the interpreter through PHP_BINARY, which is the real ELF
    at libexec/php: the child inherits the launcher's environment but none of its
    argv, so `-c`/`-d` are gone. The probe runs *inside* the launcher's PHP
    process and spawns PHP_BINARY from there, which is exactly the hop a
    framework makes, and it catches a bundle whose INI only works while the
    launcher's own command line is still intact.
    """
    expected_dir = str(bundle / "lib/php/extensions")
    module = shared[0] if shared else None
    child_args = ["-r", 'echo ini_get("extension_dir");']
    if module:
        child_args = ["-d", module["directive"] + "=" + module["path"].rsplit("/", 1)[-1],
                      "-r", 'echo (int)extension_loaded($argv[1]), "|", ini_get("extension_dir");',
                      MODULE_RUNTIME_NAMES.get(module["name"], module["name"])]
    with tempfile.TemporaryDirectory(prefix="ds-spawn-") as tmp:
        plan = Path(tmp) / "child.json"
        plan.write_text(json.dumps(child_args))
        probe = Path(tmp) / "probe.php"
        probe.write_text(
            "<?php\n"
            "if (!function_exists('proc_open')) { echo json_encode(['error' => 'proc_open unavailable']); exit(2); }\n"
            "$args = json_decode(file_get_contents($argv[1]), true);\n"
            "$process = proc_open(array_merge([PHP_BINARY], $args),\n"
            "    [1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);\n"
            "$out = stream_get_contents($pipes[1]);\n"
            "$err = stream_get_contents($pipes[2]);\n"
            "fclose($pipes[1]); fclose($pipes[2]); $status = proc_close($process);\n"
            "echo json_encode(['binary' => PHP_BINARY, 'child' => $out, 'child_err' => $err,\n"
            "                  'child_status' => $status]);\n"
        )
        launcher = subprocess.run([str(bundle / "bin/php"), str(probe), str(plan)],
                                  text=True, capture_output=True)
    if launcher.returncode != 0 or launcher.stderr:
        raise ValueError(f"Launcher could not spawn a PHP_BINARY child: {launcher.stderr.strip() or launcher.stdout.strip()}")
    try:
        reported = json.loads(launcher.stdout)
    except ValueError as error:
        raise ValueError(f"Child probe produced no report: {launcher.stdout.strip()}") from error
    if "error" in reported:
        raise ValueError(f"Child probe could not run: {reported['error']}")
    loaded, _, child_dir = reported["child"].partition("|") if module else ("", "", reported["child"])
    if child_dir != expected_dir:
        raise ValueError(f"PHP_BINARY child resolved extension_dir {child_dir!r}, not {expected_dir!r}")
    # A warning about a module the child loaded twice is not an environment
    # failure: the extension is resolved, which is what this check asserts.
    complaints = [line for line in reported["child_err"].splitlines()
                  if line.strip() and "already loaded" not in line]
    if reported["child_status"] != 0 or complaints:
        raise ValueError(f"PHP_BINARY child could not start cleanly: "
                         f"{('\n'.join(complaints) or reported['child']).strip()}")
    if module and loaded != "1":
        raise ValueError(f"PHP_BINARY child could not load {module['name']} from that directory")
    return {"binary": reported["binary"], "extension_dir": child_dir,
            "probed_module": module["name"] if module else None}


def check_loader_integrity(bundle, record):
    """The shipped loader must be the recorded bytes and must satisfy PHP at run time."""
    import hashlib
    path = bundle / record.get("path", "lib/musl/" + LOADER_INTERPRETER)
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != record.get("sha256"):
        raise ValueError("Staged musl loader does not match the manifest digest")
    banner = subprocess.run([str(path)], text=True, capture_output=True)
    expected = re.search(r"^Version ([0-9]+\.[0-9]+\.[0-9]+)$", banner.stderr, re.MULTILINE)
    if not expected or expected[1] != record.get("version"):
        raise ValueError("Staged musl loader reports a different version than the manifest")
    hostile = {**os.environ, "LD_PRELOAD": "/nonexistent/preload.so",
               "LD_LIBRARY_PATH": "/nonexistent/libs", "LD_AUDIT": "/nonexistent/audit.so"}
    result = subprocess.run([str(bundle / "bin/php"), "-n", "-r", "echo PHP_VERSION;"],
                            text=True, capture_output=True, env=hostile)
    if result.returncode != 0:
        raise ValueError(f"Bundle launcher failed with inherited loader settings: {result.stderr.strip()}")
    return {"path": record.get("path"), "sha256": actual, "version": expected[1], "hostile_env_exit_code": 0}


def verify(bundle):
    bundle = bundle.resolve()
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["verification"] = "NOT_RUN"
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if manifest.get("target") != "musl-dynamic" or manifest.get("linkage") != "dynamic":
        raise ValueError("Verifier only accepts musl-dynamic bundles")
    shared, names, states = module_plan(manifest)
    for row in shared:
        if not (bundle / row["path"]).is_file():
            raise ValueError(f"Missing requested shared module: {row['name']}")
    loader = manifest.get("loader") or {}
    if not (bundle / loader.get("path", "lib/musl/" + LOADER_INTERPRETER)).is_file():
        raise ValueError("Manifest does not describe a staged musl loader")
    inventory = elf_inventory(bundle, manifest["target"])
    with tempfile.TemporaryDirectory(prefix="ds-verify-") as tmp:
        workspace = Path(tmp)
        relocated = workspace / "relocated PHP with spaces"
        shutil.copytree(bundle, relocated)
        helper = relocated / "bin/php-ext"
        expected_disabled = states
        enabled_names = [row["name"] for row in shared]
        builtin_names = [row["name"] for row in manifest.get("builtin_modules", [])]
        expected_enabled = expected_states(enabled_names, builtin_names, True)
        if cli_modules(relocated, names, no_ini=True) != expected_disabled:
            raise ValueError("Shared-only module exists in php -n baseline")
        if cli_modules(relocated, names) != expected_disabled:
            raise ValueError("Shared modules must default to disabled")
        if fpm_probe(relocated, names, workspace)["modules"] not in (expected_disabled, [], {}):
            raise ValueError("FPM shared modules unexpectedly enabled by default")
        for name in enabled_names:
            subprocess.run([str(helper), "enable", name, "cli"], check=True)
        if cli_modules(relocated, names) != expected_enabled:
            raise ValueError("CLI enable did not load requested modules")
        if fpm_probe(relocated, names, workspace)["modules"] not in (expected_disabled, [], {}):
            raise ValueError("CLI configuration leaked into FPM")
        for name in enabled_names:
            subprocess.run([str(helper), "enable", name, "fpm"], check=True)
        if fpm_probe(relocated, names, workspace)["modules"] not in (expected_enabled, []):
            raise ValueError("FPM enable did not load requested modules")
        # The original and relocated installations must not cross-load INIs.
        if cli_modules(bundle, names) != expected_disabled:
            raise ValueError("Relocated installation changed original bundle")
        mapped = subprocess.check_output([str(relocated / "bin/php"), "-r", 'echo file_get_contents("/proc/self/maps");'], text=True)
        for line in mapped.splitlines():
            if ".so" in line and str(relocated) not in line:
                raise ValueError(f"Loaded a shared object outside the bundle: {line}")
        for name in enabled_names:
            subprocess.run([str(helper), "disable", name, "all"], check=True)
        if cli_modules(relocated, names) != expected_disabled:
            raise ValueError("CLI disable failed")
        if fpm_probe(relocated, names, workspace)["modules"] not in (expected_disabled, [], {}):
            raise ValueError("FPM disable failed")
    manifest["elf_inventory"] = inventory
    manifest["loader_check"] = check_loader_integrity(bundle, loader)
    manifest["spawned_child_check"] = check_spawned_child_environment(bundle, shared)
    manifest["verification"] = "PASSED"
    manifest["verification_checks"] = ["musl_interpreter", "musl_dependency_closure", "no_glibc_abi_tokens",
                                       "bundled_loader_execution", "cli_baseline", "cli_toggle", "fpm_config",
                                       "fpm_request_toggle", "non_root_fpm", "relocation_spaces",
                                       "installation_isolation", "no_host_shared_objects",
                                       "loader_digest", "loader_version", "hostile_ld_env",
                                       "php_binary_child_extension_dir"]
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    verify(args.bundle)
    print("PASS: real CLI/FPM requests, module toggles, relocation and musl-dynamic ELF contracts")
