#!/usr/bin/env python3
"""Isolated, non-production PHP musl-dynamic feasibility experiment.

Always invoke with python3 -I. This is deliberately separate from the production
builder/publisher. Downloaded code and archives never share this script's directory.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

COMPOSER_SHA256 = "f446ea719708bb85fcbf4ef18def5d0515f1f9b4d703f6d820c9c1656e10a2f2"
ZIG_SHA256 = "3581863e3cbcfd0fce0efc682ce7e9e93da96668a977791d356a26d2a7e9b17a"
PHP_SHA256 = "dcf93923b0e38e0d5252b25b796520b5a47761a08b3488b6d0b5d1115764d3f0"
EXT_REFS = {"apcu": "819fce90c469a5666b1706036cd66120af419f1f",
            "redis": "df4fab2de7fc327c54c94a13af2b9542e4fbd720"}


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fetch(url, path, expected=None):
    if path.exists():
        raise FileExistsError(path)
    request = urllib.request.Request(url, headers={"User-Agent": "DevStack-musl-PoC"})
    with urllib.request.urlopen(request, timeout=120) as response, path.open("xb") as output:
        shutil.copyfileobj(response, output)
    actual = digest(path)
    if expected and actual != expected:
        raise ValueError(f"Digest mismatch for {url}: {actual}")
    return actual


def validate_archive(path):
    def valid(name):
        candidate = PurePosixPath(name)
        if candidate.is_absolute() or ".." in candidate.parts or "\\" in name:
            raise ValueError(f"Unsafe archive path: {name}")
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                valid(member.filename)
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(f"Unexpected ZIP symlink: {member.filename}")
    else:
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                valid(member.name)
                if not (member.isfile() or member.isdir()):
                    raise ValueError(f"Unexpected archive member type: {member.name}")


def prepare(repo, work, version):
    if version != "8.4.27":
        raise ValueError("This first PoC locks PHP 8.4.27; other versions need reviewed inputs")
    if not re.fullmatch(r"[1-9][0-9]?", os.environ.get("SPC_CONCURRENCY", "4")):
        raise ValueError("Invalid SPC_CONCURRENCY")
    for name in ("control", "tools", "downloads", "pkgroot", "source", "log"):
        (work / name).mkdir()
    (work / "tools/bin").mkdir()
    host_php = Path(shutil.which("php")).resolve()
    extension_dir = subprocess.check_output([str(host_php), "-n", "-r", 'echo ini_get("extension_dir");'], text=True)
    flags = ["-n"]
    for name in ("mbstring", "phar", "tokenizer", "ctype", "iconv"):
        module = Path(extension_dir) / f"{name}.so"
        if module.is_file():
            flags += ["-d", f"extension={module}"]
    subprocess.run([str(host_php), *flags, "-r", 'if(PHP_VERSION_ID<80400 || !extension_loaded("mbstring") || !extension_loaded("zlib")){exit(1);}'], check=True)
    wrapper = work / "tools/bin/php"
    wrapper.write_text("#!/bin/sh\nexec " + shlex.join([str(host_php), *flags]) + ' "$@"\n')
    wrapper.chmod(0o755)
    composer_dir = work / "tools/composer-download"
    composer_dir.mkdir()
    composer = composer_dir / "composer.phar"
    fetch("https://getcomposer.org/download/2.8.12/composer.phar", composer, COMPOSER_SHA256)
    lock = json.loads((repo / "runtimes/php/config/spc-lock.json").read_text())
    spc_download = work / "tools/spc-download"
    spc_download.mkdir()
    spc_archive = spc_download / "source.tar.gz"
    fetch(lock["source_url"], spc_archive, lock["source_sha256"])
    bootstrap = load_module("experiment_bootstrap", repo / "runtimes/php/scripts/bootstrap.py")
    spc_source = bootstrap.extract_verified(spc_archive, work / "tools/spc-source", lock["source_sha256"])
    if digest(spc_source / "composer.lock") != lock["composer_lock_sha256"]:
        raise ValueError("Unexpected upstream Composer lock")
    registry = json.loads((repo / "runtimes/php/config/spc-dependencies.json").read_text())
    packages = bootstrap.prepare_dependencies(spc_source, registry, work / "tools/spc-dependencies")
    env = {**os.environ, "COMPOSER_HOME": str(work / "tools/composer-home"),
           "COMPOSER_CACHE_DIR": str(work / "tools/composer-cache"), "PHP_INI_SCAN_DIR": ""}
    subprocess.run([str(wrapper), str(composer), f"--working-dir={spc_source}", "install",
                    "--no-dev", "--prefer-dist", "--no-interaction", "--no-scripts", "--no-plugins"],
                   check=True, cwd=work / "control", env=env)
    zig_download = work / "tools/zig-download"
    zig_download.mkdir()
    zig_archive = zig_download / "zig.tar.xz"
    cached = repo / "downloads/zig-x86_64-linux-0.17.1.tar.xz"
    if cached.is_file() and digest(cached) == ZIG_SHA256:
        shutil.copyfile(cached, zig_archive)
    else:
        fetch("https://ziglang.org/download/0.17.1/zig-x86_64-linux-0.17.1.tar.xz", zig_archive, ZIG_SHA256)
    validate_archive(zig_archive)
    extracted = work / "tools/zig-source"
    extracted.mkdir()
    with tarfile.open(zig_archive) as archive:
        archive.extractall(extracted, filter="data")
    children = list(extracted.iterdir())
    if len(children) != 1 or not (children[0] / "zig").is_file():
        raise ValueError("Unexpected Zig layout")
    shutil.move(str(children[0]), work / "pkgroot/zig")
    loader = Path("/lib/ld-musl-x86_64.so.1").resolve()
    loader_version = subprocess.run([str(loader)], text=True, capture_output=True).stderr
    save(work / "experiment.json", {"purpose": "THROWAWAY_NOT_FOR_PUBLICATION", "php_version": version,
         "spc_source": str(spc_source), "spc": lock, "composer_packages": packages,
         "composer_sha256": COMPOSER_SHA256, "zig_version": "0.17.1", "zig_sha256": ZIG_SHA256,
         "loader_source": str(loader), "loader_sha256": digest(loader), "loader_version_output": loader_version,
         "host_php": str(host_php), "host_php_flags": flags, "extensions_expected_refs": EXT_REFS})
    (work / "control/craft.yml").write_text('php-version: "8.4.27"\nextensions: "session"\nshared-extensions: "apcu,redis"\nsapi: [cli, fpm]\ndebug: false\nverbosity: 128\nbuild-options:\n  no-strip: true\ncraft-options:\n  doctor: false\n  download: false\n')
    print(f"Prepared authenticated SPC, Composer and Zig tools in {work}", flush=True)


def freeze(repo, work):
    cache = json.loads((work / "downloads/.cache.json").read_text())
    rows = []
    found_php = False
    found_ext = set()
    for name, entries in cache.items():
        binaries = entries.get("binary", {})
        if binaries == []:
            binaries = {}
        if not isinstance(binaries, dict):
            raise ValueError(f"Invalid binary cache: {name}")
        for entry in [entries.get("source"), *binaries.values()]:
            if not entry:
                continue
            if entry.get("cache_type") != "archive":
                raise ValueError(f"Only inspected archives allowed in this PoC: {name}")
            filename = entry["filename"]
            if Path(filename).name != filename:
                raise ValueError("Unsafe cache filename")
            path = work / "downloads" / filename
            actual = digest(path)
            if name == "zig" and actual != ZIG_SHA256:
                raise ValueError("Zig version or archive digest drift")
            if name == "llvm-compiler-rt" and entry.get("version") != "22.1.8":
                raise ValueError("Compiler runtime version drift")
            if name == "php-src":
                if actual != PHP_SHA256:
                    raise ValueError("PHP source differs from official PHP 8.4.27 tar.xz digest")
                found_php = True
            if name in {"ext-apcu", "ext-redis"}:
                module = name[4:]
                expected_version = {"apcu": "v5.1.28", "redis": "6.3.0"}[module]
                if entry.get("version") != expected_version:
                    raise ValueError(f"Extension version drift: {name}")
                # SPC's PIE cache omits the dist revision. Verify ZIP root and
                # GitHub's commit-bearing archive comment instead of inventing it.
                with zipfile.ZipFile(path) as archive:
                    roots = {PurePosixPath(member.filename).parts[0] for member in archive.infolist()}
                    reference = EXT_REFS[module]
                    if (len(roots) != 1 or not next(iter(roots)).endswith(reference[:7])
                            or archive.comment.decode() != reference):
                        raise ValueError(f"Extension revision drift: {name}")
                found_ext.add(module)
            validate_archive(path)
            rows.append({"artifact": name, "filename": filename, "sha256": actual, "spc_cache": entry})
    if not found_php or found_ext != {"apcu", "redis"}:
        raise ValueError("Incomplete PHP/extension source cache")
    target = work / "inputs.lock.json"
    if target.exists():
        previous = {row["filename"]: row["sha256"] for row in json.loads(target.read_text())["inputs"]}
        current = {row["filename"]: row["sha256"] for row in rows}
        if any(current.get(name) != sha for name, sha in previous.items()):
            raise ValueError("Refusing to change any previously frozen input bytes")
    save(target, {"purpose": "POC_ONLY", "inputs": rows,
         "limit": "Extension/compiler-runtime archive hashes are frozen after download and before extraction, not independent signed upstream attestations."})
    print(f"Inspected and froze {len(rows)} archives before build extraction", flush=True)


def check(repo, work):
    lock = json.loads((work / "inputs.lock.json").read_text())
    for row in lock["inputs"]:
        if digest(work / "downloads" / row["filename"]) != row["sha256"]:
            raise ValueError(f"Input bytes changed: {row['filename']}")
    print("PASS: frozen input archive hashes", flush=True)


def host_tools(repo, work):
    # SPC only looks under its private pkgroot, not the host's PATH.
    # Reuse the already-installed pkg-config without modifying host packages.
    private_bin = work / "pkgroot/bin"
    private_bin.mkdir(exist_ok=True)
    target = private_bin / "pkg-config"
    if not target.exists():
        candidates = ["/usr/bin/pkg-config", "/usr/local/bin/pkg-config"]
        executable = next((Path(p).resolve() for p in candidates if Path(p).is_file()), None)
        if executable is None:
            raise ValueError("Missing host pkg-config; no automatic package installation")
        target.write_text("#!/bin/sh\nexec " + shlex.quote(str(executable)) + ' "$@"\n')
        target.chmod(0o755)
    subprocess.run([str(target), "--version"], check=True)


def stage(repo, work):
    config = json.loads((work / "experiment.json").read_text())
    bundle = work / "bundle"
    if bundle.exists():
        raise FileExistsError("Bundle already staged; use verify instead of overwriting")
    for directory in ("bin", "libexec", "lib/musl", "etc/cli/conf.d", "etc/fpm/conf.d", "share/licenses"):
        (bundle / directory).mkdir(parents=True, exist_ok=True)
    for name in ("php", "php-fpm"):
        shutil.copy2(work / "buildroot/bin" / name, bundle / "libexec" / name)
        launcher = (repo / "runtimes/php/packaging" / name).read_text()
        original = f'exec "$ROOT/libexec/{name}"'
        replacement = f'exec "$ROOT/lib/musl/ld-musl-x86_64.so.1" --library-path "$ROOT/lib/musl" "$ROOT/libexec/{name}"'
        if launcher.count(original) != 1:
            raise ValueError("Launcher template changed")
        path = bundle / "bin" / name
        path.write_text(launcher.replace(original, replacement))
        path.chmod(0o755)
    shutil.copy2(repo / "runtimes/php/packaging/php-ext", bundle / "bin/php-ext")
    (bundle / "bin/php-ext").chmod(0o755)
    launch = (repo / "runtimes/php/packaging/launch.sh").read_text()
    (bundle / "libexec/launch.sh").write_text(launch + "\n# PoC: do not inherit external dynamic-loader injection/search settings.\nunset LD_PRELOAD LD_LIBRARY_PATH\n")
    shutil.copy2(repo / "runtimes/php/config/php.ini", bundle / "etc/php.ini")
    shutil.copy2(repo / "LICENSE", bundle / "share/licenses/builder.txt")
    loader = Path(config["loader_source"])
    if digest(loader) != config["loader_sha256"]:
        raise ValueError("Build-host musl loader changed")
    shutil.copy2(loader, bundle / "lib/musl/ld-musl-x86_64.so.1")
    # musl's loader is also libc. Use a regular file to avoid archive symlink policy changes.
    shutil.copy2(loader, bundle / "lib/musl/libc.so")
    license_file = Path("/usr/share/licenses/musl-devel/COPYRIGHT")
    if license_file.is_file():
        shutil.copy2(license_file, bundle / "share/licenses/musl.txt")
    manifest_module = load_module("experiment_manifest", repo / "runtimes/php/scripts/manifest.py")
    modules = manifest_module.stage_modules(work / "buildroot", bundle, ["apcu", "redis"])
    output = subprocess.check_output([str(bundle / "bin/php"), "-n", "-r", 'echo json_encode(["version"=>PHP_VERSION,"zts"=>PHP_ZTS,"modules"=>get_loaded_extensions()]);'], text=True)
    metadata = json.loads(output)
    if metadata["version"] != config["php_version"] or {"apcu", "redis"} & set(metadata["modules"]):
        raise ValueError("Wrong PHP version or shared modules built in")
    save(bundle / "manifest.json", {"purpose": "THROWAWAY_NOT_FOR_PUBLICATION", "runtime": "php",
         "target": "musl-dynamic", "profile": "experiment", "php": metadata,
         "shared_modules": modules, "verification": "NOT_RUN", "experiment": config})
    print(f"Staged experimental bundle: {bundle}", flush=True)


def inventory(bundle):
    rows = []
    allowed = {"libc.so"}
    for path in [bundle / "libexec/php", bundle / "libexec/php-fpm",
                 *sorted((bundle / "lib/php/extensions").glob("*.so")),
                 bundle / "lib/musl/libc.so", bundle / "lib/musl/ld-musl-x86_64.so.1"]:
        dynamic = subprocess.check_output(["readelf", "-d", str(path)], text=True)
        versions = subprocess.check_output(["readelf", "--version-info", str(path)], text=True)
        headers = subprocess.check_output(["readelf", "-l", str(path)], text=True)
        needed = re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", dynamic)
        if set(needed) - allowed or "GLIBC_" in versions:
            raise ValueError(f"Unexpected musl dependency/ABI in {path}: {needed}")
        if re.search(r"\((?:RPATH|RUNPATH)\)", dynamic):
            raise ValueError(f"Unexpected RPATH/RUNPATH: {path}")
        if path.parent.name == "libexec" and "ld-musl-x86_64.so.1" not in headers:
            raise ValueError(f"Binary does not request musl dynamic loader: {path}")
        rows.append({"path": str(path.relative_to(bundle)), "needed": needed, "sha256": digest(path)})
    return rows


def verify_without_system_musl(repo, work):
    bwrap = shutil.which("bwrap")
    if not bwrap:
        save(work / "results-no-system-musl.json", {"status": "SKIPPED", "reason": "bwrap is not installed; no auto-install"})
        print("SKIP: hidden-system-musl checks require existing bwrap", flush=True)
        return
    loader = Path("/lib/ld-musl-x86_64.so.1").resolve()
    command = [bwrap, "--ro-bind", "/", "/", "--bind", str(work), str(work),
               "--dev-bind", "/dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
               "--unshare-user", "--die-with-parent", "--setenv", "TMPDIR", "/tmp",
               "--ro-bind", "/dev/null", str(loader)]
    # Mask the host's musl development tree too, without changing any host files.
    if Path("/usr/x86_64-linux-musl").is_dir():
        command += ["--tmpfs", "/usr/x86_64-linux-musl"]
    command += [sys.executable, "-I", str(Path(__file__).resolve()), "verify",
                str(repo), str(work), "--no-system-musl"]
    subprocess.run(command, check=True, cwd=repo)


def verify(repo, work, no_system_musl=False):
    bundle = work / "bundle"
    result_path = work / ("results-no-system-musl.json" if no_system_musl else "results.json")
    result = {"purpose": "THROWAWAY_NOT_FOR_PUBLICATION", "status": "NOT_RUN",
              "limitations": ["Single Fedora glibc host; not an Alpine or multi-distro certification.",
                              "Minimal session/apcu/redis PoC, not the full PHP profile or 8.2-8.5 matrix.",
                              "Bundled loader comes from the recorded host musl package, not a separately locked portable toolchain release."]}
    save(result_path, result)
    if no_system_musl:
        if Path("/lib/ld-musl-x86_64.so.1").is_file():
            raise ValueError("System musl loader was not hidden")
        try:
            raw = subprocess.run([str(bundle / "libexec/php"), "-n", "-r", "echo PHP_VERSION;"],
                                 text=True, capture_output=True)
        except OSError as error:
            result["raw_binary_negative_control"] = {"errno": error.errno, "error": str(error)}
        else:
            if raw.returncode == 0:
                raise ValueError("Raw PHP unexpectedly ran without its system interpreter")
            result["raw_binary_negative_control"] = {"exit_code": raw.returncode, "stderr": raw.stderr}
        result["system_musl_loader_hidden"] = True
    verifier = load_module("experiment_verify", repo / "runtimes/php/scripts/verify.py")
    result["elf_inventory"] = inventory(bundle)
    names = ["apcu", "redis"]
    disabled, enabled = dict.fromkeys(names, False), dict.fromkeys(names, True)
    with tempfile.TemporaryDirectory(prefix="musl-poc-") as tmp:
        workspace = Path(tmp)
        relocated = workspace / "relocated PHP with spaces"
        shutil.copytree(bundle, relocated)
        helper = relocated / "bin/php-ext"
        def toggle(action, sapi):
            for name in names:
                subprocess.run([str(helper), action, name, sapi], check=True)
        def cli(expected, no_ini=False):
            if verifier.cli_modules(relocated, names, no_ini) != expected:
                raise ValueError("CLI module state mismatch")
        def fpm(expected):
            if verifier.fpm_probe(relocated, names, workspace)["modules"] != expected:
                raise ValueError("FPM module state mismatch")
        cli(disabled, True)
        cli(disabled)
        fpm(disabled)
        toggle("enable", "cli")
        cli(enabled)
        fpm(disabled)
        toggle("enable", "fpm")
        fpm(enabled)
        if verifier.cli_modules(bundle, names) != disabled:
            raise ValueError("Relocation leaked into original installation")
        expression = 'if(!apcu_store("musl-poc","ok")||apcu_fetch("musl-poc")!=="ok"||!class_exists("Redis")){exit(1);}echo "PASS: APCu store/fetch and Redis class\\n";'
        subprocess.run([str(relocated / "bin/php"), "-d", "apc.enable_cli=1", "-r", expression], check=True)
        loaded = subprocess.check_output([str(relocated / "bin/php"), "-r", 'echo file_get_contents("/proc/self/maps");'], text=True)
        mapped = [line for line in loaded.splitlines() if "/lib" in line and ".so" in line]
        for line in mapped:
            if str(relocated) not in line:
                raise ValueError(f"Loaded a host shared library: {line}")
        result["mapped_shared_objects"] = mapped
        toggle("disable", "all")
        cli(disabled)
        fpm(disabled)
    result["status"] = "PASSED"
    result["checks"] = ["musl_elf_needed_closure", "no_glibc_symbols", "bundle_loader_execution",
                        "cli_baseline", "cli_fpm_module_toggles", "actual_fastcgi_requests",
                        "non_root_fpm", "relocation_spaces", "installation_isolation",
                        "apcu_store_fetch", "redis_class", "no_host_shared_objects_in_php_maps"]
    if no_system_musl:
        result["checks"] += ["system_musl_loader_hidden", "raw_binary_negative_control"]
    save(result_path, result)
    print("PASS: experimental musl dynamic CLI/FPM, toggles, relocation and mapped libraries", flush=True)
    if not no_system_musl:
        verify_without_system_musl(repo, work)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "freeze", "check", "host-tools", "stage", "verify"))
    parser.add_argument("repo", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("version", nargs="?", default="8.4.27")
    parser.add_argument("--no-system-musl", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.repo.resolve(), args.workspace.resolve(), args.version)
    elif args.action == "verify":
        verify(args.repo.resolve(), args.workspace.resolve(), args.no_system_musl)
    else:
        globals()[args.action.replace("-", "_")](args.repo.resolve(), args.workspace.resolve())
