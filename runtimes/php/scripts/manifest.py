#!/usr/bin/env python3
"""Stage bundled musl libc/loader plus shared modules and record actual metadata."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess

LOADER_NAME = "ld-musl-x86_64.so.1"
# musl's loader is also libc: PHP requests NEEDED libc.so, satisfied by the loader
# itself. A separate libc.so file is not staged, so the bundle has one libc.
DEFAULT_ENABLED = {"cli": False, "fpm": False}


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
        registry.append({"name": name, "directive": "zend_extension" if name in {"opcache", "xdebug"} else "extension", "path": f"lib/php/extensions/{name}.so", "sha256": digest(path), "enabled_by_default": dict(DEFAULT_ENABLED)})
    (bundle / "etc").mkdir(parents=True, exist_ok=True)
    (bundle / "etc/shared-modules.tsv").write_text("".join(f'{row["name"]}\t{row["directive"]}\t{Path(row["path"]).name}\n' for row in registry))
    return registry


def stage_loader(bundle, metadata):
    loader = Path(metadata["source"])
    if digest(loader) != metadata["loader"]["sha256"]:
        raise ValueError("Built musl loader changed before staging")
    destination = bundle / "lib/musl"
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / LOADER_NAME
    shutil.copy2(loader, path)
    path.chmod(0o755)
    license_path = Path(metadata["license"]["path"])
    if digest(license_path) != metadata["license"]["sha256"]:
        raise ValueError("musl copyright notice changed before staging")
    (bundle / "share/licenses").mkdir(parents=True, exist_ok=True)
    shutil.copy2(license_path, bundle / "share/licenses/musl.txt")
    return {"path": f"lib/musl/{LOADER_NAME}", "sha256": digest(path),
            "version": metadata["loader"]["version"], "soname": metadata["loader"].get("soname"),
            # musl links its loader without -soname; the loader matches the reserved
            # libc library names (libc.so and friends) to itself in ldso/dynlink.c.
            "satisfies": ["libc.so"],
            "source": metadata["pin"], "toolchain_musl_version": metadata["toolchain_musl_version"],
            "compiler": metadata["compiler"], "license": "share/licenses/musl.txt"}


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


def launcher(php_dir, name, loader_path):
    template = (php_dir / "packaging" / name).read_text()
    original = f'exec "$ROOT/libexec/{name}"'
    replacement = f'exec "$ROOT/{loader_path}" --library-path "$ROOT/lib/musl" "$ROOT/libexec/{name}"'
    if template.count(original) != 1:
        raise ValueError(f"Launcher template changed: {name}")
    return template.replace(original, replacement)


def stage(php_dir, bundle, resolution, provenance, repo_root, buildroot=None, downloads=None, loader=None):
    if bundle.exists():
        raise FileExistsError(f"Refusing to replace existing bundle: {bundle}")
    if resolution["target"] != "musl-dynamic":
        raise ValueError("Only the musl-dynamic target is supported")
    if loader is None:
        raise ValueError("A built musl loader is required")
    buildroot = buildroot or php_dir / "buildroot"
    upstream_path = buildroot / "build-manifest.json"
    upstream = json.loads(upstream_path.read_text())
    modes = upstream["targets"]["php"]["extensions"]
    if set(modes["static"]) & set(resolution["shared"]):
        raise ValueError("SPC resolution forced a shared-only module static")
    if set(modes["shared"]) != set(resolution["shared"]):
        raise ValueError("SPC resolved shared modes differ from requested modules")
    if upstream["target"].get("libc") != "musl":
        raise ValueError("SPC did not target musl")
    for directory in ("bin", "libexec", "lib/musl", "etc/cli/conf.d", "etc/fpm/conf.d", "share/licenses"):
        (bundle / directory).mkdir(parents=True, exist_ok=True)
    for name in ("php", "php-fpm"):
        shutil.copy2(buildroot / "bin" / name, bundle / "libexec" / name)
        (bundle / "libexec" / name).chmod(0o755)
    loader_record = stage_loader(bundle, loader)
    for name in ("php", "php-fpm"):
        (bundle / "bin" / name).write_text(launcher(php_dir, name, loader_record["path"]))
        (bundle / "bin" / name).chmod(0o755)
    shutil.copy2(php_dir / "packaging/php-ext", bundle / "bin/php-ext")
    (bundle / "bin/php-ext").chmod(0o755)
    shutil.copy2(php_dir / "packaging/launch.sh", bundle / "libexec/launch.sh")
    shutil.copy2(php_dir / "config/php.ini", bundle / "etc/php.ini")
    shutil.copy2(php_dir / "config/php-fpm.conf", bundle / "etc/php-fpm.conf.example")
    shutil.copy2(repo_root / "LICENSE", bundle / "share/licenses/builder.txt")
    shutil.copy2(upstream_path, bundle / "spc-build-manifest.json")
    modules = stage_modules(buildroot, bundle, resolution["shared"])
    # Record metadata from the built binary itself: the bundle's bin/php is a
    # launcher that starts it through the bundled loader.
    metadata = php_metadata(buildroot / "bin/php")
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
    build_id = hashlib.sha256((digest(bundle / "libexec/php") + digest(bundle / "libexec/php-fpm") + loader_record["sha256"] + json.dumps(provenance, sort_keys=True) + builder_digest.hexdigest() + "".join(row["sha256"] for row in modules)).encode()).hexdigest()[:16]
    manifest = {"schema_version": 1, "runtime": "php", "build_id": build_id, "architecture": metadata["architecture"], "target": resolution["target"], "libc": {"family": "musl", "minimum_version": None}, "linkage": "dynamic", "loader": loader_record, "php": metadata, "requested": resolution, "shared_modules": modules, "paths": {"cli": "bin/php", "fpm": "bin/php-fpm", "extension_manager": "bin/php-ext", "cli_binary": "libexec/php", "fpm_binary": "libexec/php-fpm", "musl_loader": loader_record["path"], "php_ini": "etc/php.ini", "cli_scan_dir": "etc/cli/conf.d", "fpm_scan_dir": "etc/fpm/conf.d", "extension_dir": "lib/php/extensions"}, "spc": provenance, "builder": {"commit": commit, "source_sha256": builder_digest.hexdigest()}, "build_host": {"system": platform.platform()}, "source_inputs": input_files, "verification": "NOT_RUN"}
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
    parser.add_argument("--loader", type=Path, required=True)
    args = parser.parse_args()
    stage(args.php_dir, args.bundle, json.loads(args.resolution.read_text()), json.loads(args.provenance.read_text()), args.repo_root, args.buildroot, args.downloads, json.loads(args.loader.read_text()))
