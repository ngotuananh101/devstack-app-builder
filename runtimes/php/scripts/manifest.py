#!/usr/bin/env python3
"""Stage strict module output and record metadata from the actual built PHP."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def stage_modules(buildroot, bundle, requested):
    destination = bundle / "lib/php/extensions"
    destination.mkdir(parents=True, exist_ok=True)
    registry = []
    for name in requested:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("Invalid module name")
        source = buildroot / "modules" / f"{name}.so"
        if not source.is_file():
            raise ValueError(f"SPC did not emit requested shared module: {name} ({source})")
        path = destination / source.name
        shutil.copy2(source, path)
        path.chmod(0o644)
        registry.append({"name": name, "directive": "zend_extension" if name in {"opcache", "xdebug"} else "extension", "path": f"lib/php/extensions/{name}.so", "sha256": digest(path), "enabled_by_default": {"cli": False, "fpm": False}})
    (bundle / "etc").mkdir(parents=True, exist_ok=True)
    (bundle / "etc/shared-modules.tsv").write_text("".join(f'{row["name"]}\t{row["directive"]}\t{Path(row["path"]).name}\n' for row in registry))
    return registry


def php_metadata(binary):
    expression = 'echo json_encode(["version"=>PHP_VERSION,"zts"=>PHP_ZTS,"debug"=>PHP_DEBUG,"architecture"=>php_uname("m"),"static_modules"=>get_loaded_extensions()]);'
    metadata = json.loads(subprocess.check_output([str(binary), "-n", "-r", expression], text=True))
    info = subprocess.check_output([str(binary), "-n", "-i"], text=True)
    for label, key in [("PHP API", "php_api"), ("Zend Extension", "zend_extension_api"), ("PHP Extension Build", "extension_build"), ("Zend Extension Build", "zend_extension_build"), ("Compiler", "compiler"), ("Configure Command", "configure_command")]:
        match = re.search(r"^" + re.escape(label) + r" => (.*)$", info, re.MULTILINE)
        if not match and key in {"php_api", "extension_build", "zend_extension_api", "zend_extension_build"}:
            raise ValueError(f"Missing built PHP metadata: {label}")
        metadata[key] = match.group(1) if match else None
    return metadata


def stage(php_dir, bundle, resolution, provenance, repo_root, buildroot=None, downloads=None):
    if bundle.exists():
        raise FileExistsError(f"Refusing to replace existing bundle: {bundle}")
    buildroot = buildroot or php_dir / "buildroot"
    upstream_path = buildroot / "build-manifest.json"
    upstream = json.loads(upstream_path.read_text())
    modes = upstream["targets"]["php"]["extensions"]
    if set(modes["static"]) & set(resolution["shared"]):
        raise ValueError("SPC resolution forced a shared-only module static")
    if set(modes["shared"]) != set(resolution["shared"]):
        raise ValueError("SPC resolved shared modes differ from requested modules")
    for directory in ("bin", "libexec", "etc/cli/conf.d", "etc/fpm/conf.d", "share/licenses"):
        (bundle / directory).mkdir(parents=True, exist_ok=True)
    for name in ("php", "php-fpm"):
        shutil.copy2(buildroot / "bin" / name, bundle / "libexec" / name)
        (bundle / "libexec" / name).chmod(0o755)
    for name in ("php", "php-fpm", "php-ext"):
        shutil.copy2(php_dir / "packaging" / name, bundle / "bin" / name)
        (bundle / "bin" / name).chmod(0o755)
    shutil.copy2(php_dir / "packaging/launch.sh", bundle / "libexec/launch.sh")
    shutil.copy2(php_dir / "config/php.ini", bundle / "etc/php.ini")
    shutil.copy2(php_dir / "config/php-fpm.conf", bundle / "etc/php-fpm.conf.example")
    shutil.copy2(repo_root / "LICENSE", bundle / "share/licenses/builder.txt")
    shutil.copy2(upstream_path, bundle / "spc-build-manifest.json")
    modules = stage_modules(buildroot, bundle, resolution["shared"])
    metadata = php_metadata(bundle / "libexec/php")
    requested = resolution["version"]
    if metadata["version"] != requested and not metadata["version"].startswith(requested + "."):
        raise ValueError(f"Built version {metadata['version']} does not match request {requested}")
    baseline = {name.lower() for name in metadata["static_modules"]}
    overlap = baseline & set(resolution["shared"])
    if overlap:
        raise ValueError(f"Shared-only modules were built in: {sorted(overlap)}")
    commit = subprocess.check_output(["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True).strip()
    input_files = []
    # SPC's downloaded source/toolchain archives are captured by name+digest.
    download = downloads or php_dir / "downloads"
    if download.is_dir():
        for path in sorted(download.rglob("*")):
            if path.is_file():
                input_files.append({"path": str(path.relative_to(download)), "sha256": digest(path)})
    builder_digest = hashlib.sha256()
    for path in sorted((repo_root / "runtimes/php").rglob("*")):
        if path.is_file() and any(part in {"scripts", "config", "packaging"} for part in path.relative_to(repo_root / "runtimes/php").parts):
            builder_digest.update(str(path.relative_to(repo_root)).encode())
            builder_digest.update(bytes.fromhex(digest(path)))
    build_id = hashlib.sha256((digest(bundle / "libexec/php") + digest(bundle / "libexec/php-fpm") + json.dumps(provenance, sort_keys=True) + builder_digest.hexdigest() + "".join(row["sha256"] for row in modules)).encode()).hexdigest()[:16]
    manifest = {"schema_version": 1, "runtime": "php", "build_id": build_id, "architecture": metadata["architecture"], "target": resolution["target"], "libc": {"family": "glibc" if resolution["target"] == "glibc" else "musl", "minimum_version": "2.17" if resolution["target"] == "glibc" else None}, "profile": resolution["profile"], "php": metadata, "requested": resolution, "shared_modules": modules, "paths": {"cli": "bin/php", "fpm": "bin/php-fpm", "extension_manager": "bin/php-ext", "cli_binary": "libexec/php", "fpm_binary": "libexec/php-fpm", "php_ini": "etc/php.ini", "cli_scan_dir": "etc/cli/conf.d", "fpm_scan_dir": "etc/fpm/conf.d", "extension_dir": "lib/php/extensions"}, "spc": provenance, "builder": {"commit": commit, "source_sha256": builder_digest.hexdigest()}, "build_host": {"system": platform.platform()}, "source_inputs": input_files, "verification": "NOT_RUN"}
    manifest["resolved_modes"] = modes
    manifest["spc_target"] = upstream["target"]
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("php_dir", "bundle", "resolution", "provenance", "repo_root"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--buildroot", type=Path)
    parser.add_argument("--downloads", type=Path)
    args = parser.parse_args()
    stage(args.php_dir, args.bundle, json.loads(args.resolution.read_text()), json.loads(args.provenance.read_text()), args.repo_root, args.buildroot, args.downloads)
