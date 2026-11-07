#!/usr/bin/env python3
"""Stage, verify and package native Redis builds without host modifications."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time

BINARIES = ("redis-server", "redis-cli", "redis-benchmark")
ALIASES = ("redis-sentinel", "redis-check-rdb", "redis-check-aof")


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def run(arguments, **kwargs):
    return subprocess.check_output(arguments, text=True, timeout=30, **kwargs)


def parse_elf(header, program, dynamic, versions, target):
    if "ELF64" not in header or "Advanced Micro Devices X86-64" not in header:
        raise ValueError("Expected Linux x86_64 ELF64 executable")
    if target != "musl":
        raise ValueError("Redis supports only the fully static musl target")
    needed = re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", dynamic)
    glibc_tokens = set(re.findall(r"GLIBC_[a-zA-Z0-9_.]+", versions))
    interpreter = re.search(r"Requesting program interpreter: ([^\]]+)", program)
    static = "INTERP" not in program and interpreter is None and "(NEEDED)" not in dynamic
    if not static or glibc_tokens:
        raise ValueError("musl binaries must be fully static, with no GLIBC symbols")
    return {"static": static, "interpreter": None, "needed": needed, "glibc_required": None}


def elf(binary, target):
    env = {**os.environ, "LC_ALL": "C"}
    return parse_elf(*(run(["readelf", flag, str(binary)], env=env) for flag in ("-hW", "-lW", "-dW", "-VW")), target)


def libc_notice(target, notice=None):
    if target != "musl":
        raise ValueError("Redis supports only the fully static musl target")
    if notice is None:
        candidates = [Path(os.environ["REDIS_MUSL_LICENSE"])] if os.environ.get("REDIS_MUSL_LICENSE") else [
            Path("/usr/share/licenses/musl-libc/COPYRIGHT"), Path("/usr/share/licenses/musl/COPYRIGHT"),
            Path("/usr/share/doc/musl/copyright"), Path("/usr/share/doc/musl-dev/copyright")]
        notice = next((path for path in candidates if path.is_file()), None)
    if notice is None or not notice.is_file():
        raise FileNotFoundError("musl license notice missing; set REDIS_MUSL_LICENSE to the installed toolchain's copyright file")
    return {"path": str(notice), "sha256": digest(notice)}


def probe(compiler, target, work):
    if target != "musl":
        raise ValueError("Redis supports only the fully static musl target")
    executable = shutil.which(compiler)
    if executable is None:
        raise ValueError("Native compiler is missing: " + compiler)
    macros = run([compiler, "-dM", "-E", "-include", "features.h", "-x", "c", "/dev/null"])
    if "#define __GLIBC__ " in macros:
        raise ValueError("Compiler headers are glibc; a musl-cross compiler is required")
    source = work / "libc-probe.c"
    source.write_text('#include <stdio.h>\nint main(void) { puts("native-libc-probe"); return 0; }\n')
    binary = work / "libc-probe"
    subprocess.run([compiler, "-static", "-no-pie", str(source), "-o", str(binary)], check=True, timeout=60)
    metadata = elf(binary, target)
    if run([str(binary)]).strip() != "native-libc-probe":
        raise ValueError("Native libc probe did not execute successfully")
    provenance = {"compiler": executable, "compiler_sha256": digest(Path(executable)),
                  "compiler_version": run([compiler, "--version"]).splitlines()[0],
                  "compiler_details": subprocess.run([compiler, "-v"], text=True, capture_output=True, check=True).stderr,
                  "make_version": run(["make", "--version"]).splitlines()[0],
                  "binutils_version": run(["readelf", "--version"]).splitlines()[0],
                  "libc_probe": metadata, "libc_license": libc_notice(target), "build_host": platform.platform()}
    (work / "toolchain.json").write_text(json.dumps(provenance, indent=2) + "\n")
    return provenance


def binary_version(binary):
    output = run([str(binary), "--version"])
    match = re.search(r"(?:v=|redis-cli |redis-benchmark )([0-9]+\.[0-9]+\.[0-9]+)", output)
    if not match:
        raise ValueError("Cannot determine actual Redis version: " + str(binary))
    return match.group(1)


def require_version(binary, version):
    actual = binary_version(binary)
    if actual != version:
        raise ValueError("Built Redis version " + actual + " does not match request " + version)
    return actual


def stage(source, output, request, provenance, repo_root):
    version = request["version"]
    # Validate executable identity before creating staged output.
    for name in BINARIES:
        require_version(source / "src" / name, version)
    output.mkdir(parents=True, exist_ok=False)
    (output / "bin").mkdir()
    licenses = output / "share/licenses"
    licenses.mkdir(parents=True)
    inventory = []
    for name in BINARIES:
        path = output / "bin" / name
        shutil.copy2(source / "src" / name, path)
        path.chmod(0o755)
        inventory.append({"path": "bin/" + name, "sha256": digest(path), **elf(path, request["target"])})
    for name in ALIASES:
        (output / "bin" / name).symlink_to("redis-server")
    # Preserve upstream notices for Redis and bundled static dependencies.
    for path in sorted(source.rglob("*")):
        if path.is_file() and re.match(r"^(?:LICENSE|COPYING|COPYRIGHT|NOTICE|REDISCONTRIBUTIONS)(?:[.\-_].*)?$", path.name, re.I):
            relative = path.relative_to(source)
            destination = licenses / "redis" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    if not (licenses / "redis").exists():
        raise ValueError("Redis source license notices are missing")
    shutil.copy2(repo_root / "LICENSE", licenses / "builder.txt")
    notice = provenance.get("libc_license")
    if not notice or digest(Path(notice["path"])) != notice["sha256"]:
        raise ValueError("musl license notice missing or changed")
    shutil.copy2(notice["path"], licenses / "musl.txt")
    archive = source.parent.parent / "downloads" / ("redis-" + version + ".tar.gz")
    if digest(archive) != request["source"]["sha256"]:
        raise ValueError("Authenticated source archive changed after build")
    sources = output / "share/sources"
    sources.mkdir()
    shutil.copy2(archive, sources / archive.name)
    builder_hash = hashlib.sha256()
    for path in sorted((repo_root / "runtimes/redis").rglob("*")):
        if path.is_file():
            builder_hash.update(str(path.relative_to(repo_root)).encode())
            builder_hash.update(bytes.fromhex(digest(path)))
    build_id = hashlib.sha256((json.dumps(inventory, sort_keys=True) + json.dumps(provenance, sort_keys=True) + builder_hash.hexdigest()).encode()).hexdigest()[:16]
    manifest = {"schema_version": 1, "runtime": "redis", "build_id": build_id,
                "architecture": "x86_64", "target": request["target"],
                "libc": {"family": "musl", "minimum_version": None, "static": True},
                "redis": {"version": version, "allocator": "jemalloc", "tls": False, "bundled_modules": []},
                "requested": request, "paths": {name.replace("redis-", "").replace("-", "_"): "bin/" + name for name in (*BINARIES, *ALIASES)},
                "binaries": inventory, "source_inputs": [request["source"]],
                "source_archive": "share/sources/" + archive.name,
                "builder": {"commit": run(["git", "-C", str(repo_root), "rev-parse", "HEAD"]).strip(), "source_sha256": builder_hash.hexdigest()},
                "toolchain": provenance, "verification": "NOT_RUN"}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def smoke(bundle, version):
    # A short Unix socket path avoids sun_path limits even when the repo path is long.
    with tempfile.TemporaryDirectory(prefix="redis-verify-") as temp:
        root = Path(temp)
        relocated = root / "relocated Redis bundle"
        shutil.copytree(bundle, relocated, symlinks=True)
        for name in BINARIES:
            require_version(relocated / "bin" / name, version)
        require_version(relocated / "bin/redis-sentinel", version)
        socket = root / "redis.sock"
        with (root / "server.log").open("w+") as log:
            process = subprocess.Popen([str(relocated / "bin/redis-server"), "--port", "0", "--unixsocket", str(socket), "--unixsocketperm", "700", "--save", "", "--appendonly", "no", "--dir", str(root)], cwd=root, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 15
                while not socket.exists():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        log.seek(0)
                        raise ValueError("Redis smoke server did not start: " + log.read())
                    time.sleep(0.05)
                cli = [str(relocated / "bin/redis-cli"), "-s", str(socket), "--raw"]
                for command, expected in [("PING", "PONG"), ("SET builder-check native", "OK"), ("GET builder-check", "native")]:
                    if run([*cli, *command.split()], cwd=root).strip() != expected:
                        raise ValueError("Redis request smoke failed: " + command)
                benchmark = run([str(relocated / "bin/redis-benchmark"), "-s", str(socket), "-n", "2", "-c", "1", "-t", "ping", "-q"], cwd=root)
                if "PING" not in benchmark:
                    raise ValueError("Redis benchmark smoke did not run")
                # Exercise both checker aliases against files produced by this build.
                run([*cli, "SAVE"], cwd=root)
                subprocess.run([str(relocated / "bin/redis-check-rdb"), str(root / "dump.rdb")], check=True, capture_output=True, timeout=30)
                aof = root / "empty.aof"
                aof.touch()
                subprocess.run([str(relocated / "bin/redis-check-aof"), str(aof)], check=True, capture_output=True, timeout=30)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
    return ["relocation-with-spaces", "version", "PING", "SET/GET", "benchmark", "RDB-check", "AOF-check"]


def verify(bundle):
    path = bundle / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["verification"] = "NOT_RUN"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    for row in manifest["binaries"]:
        relative = Path(row["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe binary manifest path")
        binary = bundle / relative
        if binary.is_symlink() or digest(binary) != row["sha256"]:
            raise ValueError("Staged binary checksum mismatch")
        if elf(binary, manifest["target"]) != {key: row[key] for key in ("static", "interpreter", "needed", "glibc_required")}:
            raise ValueError("ELF manifest mismatch")
        require_version(binary, manifest["redis"]["version"])
    for name in ALIASES:
        if not (bundle / "bin" / name).is_symlink() or os.readlink(bundle / "bin" / name) != "redis-server":
            raise ValueError("Missing or unsafe Redis alias: " + name)
    manifest["checks"] = smoke(bundle, manifest["redis"]["version"])
    manifest["verification"] = "PASSED"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def package(bundle, output):
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest.get("runtime") != "redis" or manifest.get("verification") != "PASSED":
        raise ValueError("Real Redis verification must pass before packaging")
    parts = [manifest["redis"]["version"], manifest["target"], manifest["architecture"], manifest["build_id"]]
    if any(not re.fullmatch(r"[a-zA-Z0-9_.-]+", part) for part in parts):
        raise ValueError("Invalid Redis archive identity")
    name = "redis-" + "-".join(parts)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / (name + ".tar.gz")
    checksum = output / (name + ".tar.gz.sha256")
    sidecar = output / (name + ".manifest.json")
    paths = (archive, checksum, sidecar)
    if any(path.exists() or path.is_symlink() for path in paths):
        raise FileExistsError("Refusing to replace Redis assets: " + name)
    created = []
    try:
        # Reserve the entire asset set exclusively; unwind only paths we created.
        with archive.open("xb") as stream:
            created.append(archive)
            with checksum.open("x") as sums:
                created.append(checksum)
                with sidecar.open("x") as metadata:
                    created.append(sidecar)
                    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
                        tar.add(bundle, arcname=name)
                    stream.flush()
                    sums.write(digest(archive) + "  " + archive.name + "\n")
                    metadata.write(json.dumps(manifest, indent=2) + "\n")
    except Exception:
        for path in created:
            path.unlink()
        raise
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("probe")
    p.add_argument("compiler")
    p.add_argument("target", choices=("musl",))
    p.add_argument("work", type=Path)
    p = sub.add_parser("stage")
    for name in ("source", "output", "request", "provenance", "repo_root"):
        p.add_argument(name, type=Path)
    p = sub.add_parser("verify")
    p.add_argument("bundle", type=Path)
    p = sub.add_parser("package")
    p.add_argument("bundle", type=Path)
    p.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.action == "probe":
        probe(args.compiler, args.target, args.work)
    elif args.action == "stage":
        stage(args.source, args.output, json.loads(args.request.read_text()), json.loads(args.provenance.read_text()), args.repo_root)
    elif args.action == "verify":
        print(json.dumps(verify(args.bundle)["checks"]))
    else:
        print(package(args.bundle, args.output))
