#!/usr/bin/env python3
"""Stage a PostgreSQL install tree into the relocatable bundle and manifest.

``stage`` is deliberately the only writer of a bundle: it proves every command
runs through the shipped loader before it creates anything, copies the install
tree into the documented layout, wraps the raw ELFs in launcher scripts, and
records what it staged in ``manifest.json``. Tasks 6-9 read that manifest, so
its keys are a contract from this module onward.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

RUNTIME_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent

SCHEMA_VERSION = 1
# The loader the launchers exec, relative to the bundle root. Every staged ELF
# must request exactly this interpreter: a bundle that leans on the build host's
# /lib/ld-musl-x86_64.so.1 works there and fails for the user it was built for.
INTERPRETER = "/lib/ld-musl-x86_64.so.1"
# The three commands' version output differs upstream: most binaries print
# `psql (PostgreSQL) 18.6`, but `pg_config` prints the bare `PostgreSQL 18.6`
# (src/common/config_info.c stores that string verbatim). Both are the same
# patch version, so both are accepted and nothing looser is.
VERSION_PATTERN = re.compile(r"\(PostgreSQL\) ([0-9]+\.[0-9]+)|^PostgreSQL ([0-9]+\.[0-9]+)$",
                             re.MULTILINE)
# Provided by the loader itself, never by a file in lib/.
LOADER_LIBRARIES = frozenset({"libc.so", "ld-musl-x86_64.so.1"})
# Only OpenSSL is linked shared; zlib, libxml2, ICU and libuuid ride inside the
# binaries and modules as static archives, so nothing else is vendored.
SHARED_DEPENDENCIES = ("libssl.so", "libcrypto.so")
# share/doc is 24 KB of HTML that nothing at runtime reads; every other share
# subtree (bki, sql, timezone, tsearch_data, extension, the *.sample configs) is
# required.
DROPPED_SHARE = ("doc",)
COPY_TOP_LEVEL = ("bin", "lib", "share", "include")


def _load(name):
    spec = importlib.util.spec_from_file_location(f"postgresql_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launchers = _load("launchers")
modules = _load("modules")


def digest(path):
    """SHA-256 of a file, streamed so a large archive never lands in memory."""
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def run(arguments, **kwargs):
    return subprocess.check_output(arguments, text=True, timeout=60, **kwargs)


def parse_elf(header, program, dynamic, versions):
    """Apply the musl-dynamic ELF contract to one `readelf` dump.

    The contract inverts Redis's: PostgreSQL's launcher execs the bundle's own
    loader with `--library-path`, so a binary must be dynamically linked, must
    name the musl interpreter, and must not carry an rpath/runpath (which would
    let a host directory win over the bundle) or a GLIBC_* requirement.
    """
    if "ELF64" not in header or "Advanced Micro Devices X86-64" not in header:
        raise ValueError("Expected Linux x86_64 ELF64 executable")
    interpreter = re.search(r"Requesting program interpreter: ([^\]]+)", program)
    if interpreter is None:
        raise ValueError("Expected a dynamically linked executable with a program interpreter")
    if interpreter.group(1) != INTERPRETER:
        raise ValueError(f"Unexpected program interpreter {interpreter.group(1)!r}; "
                         f"the bundle ships {INTERPRETER}")
    needed = re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", dynamic)
    if not needed:
        raise ValueError("Expected at least one NEEDED shared library")
    for tag in ("RPATH", "RUNPATH"):
        match = re.search(rf"\({tag}\).*?\[([^\]]*)\]", dynamic)
        if match:
            raise ValueError(f"Refusing a hard-coded {tag.lower()}: {match.group(1)}")
    glibc_tokens = sorted(set(re.findall(r"GLIBC_[a-zA-Z0-9_.]+", versions)))
    if glibc_tokens:
        raise ValueError(f"Refusing a glibc requirement: {', '.join(glibc_tokens)}")
    machine = re.search(r"Machine:\s*(.+)", header)
    if machine is None:
        raise ValueError("ELF header names no machine")
    return {
        "interpreter": interpreter.group(1),
        "needed": needed,
        "rpath": None,
        "runpath": None,
        "glibc_required": None,
        "machine": machine.group(1).strip(),
    }


def elf(path):
    """Read one ELF and apply `parse_elf`; unreadable files are a refusal."""
    path = Path(path)
    environment = {**os.environ, "LC_ALL": "C"}
    dumps = []
    for flag in ("-hW", "-lW", "-dW", "-VW"):
        result = subprocess.run(["readelf", flag, str(path)], text=True,
                                capture_output=True, env=environment)
        if result.returncode != 0:
            raise ValueError(f"Cannot read {path} as an ELF file: {result.stderr.strip()}")
        dumps.append(result.stdout)
    return parse_elf(*dumps)


def _needed(path):
    """The NEEDED names of any ELF, executable or shared object."""
    result = subprocess.run(["readelf", "-dW", str(path)], text=True, capture_output=True,
                            env={**os.environ, "LC_ALL": "C"})
    if result.returncode != 0:
        raise ValueError(f"Cannot read {path} as an ELF file: {result.stderr.strip()}")
    return re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", result.stdout)


def binary_version(binary, loader, library_path):
    """Ask a raw bundle binary for its version through the shipped loader.

    The binary's PT_INTERP is /lib/ld-musl-x86_64.so.1, which the build host is
    not required to have, so a direct exec would fail with ENOENT. Executing
    `<loader> --library-path <install lib>:<deps>/lib:<loader dir> <binary> --version` is
    the same mechanism the launcher uses, exercised one step before it exists.
    """
    binary = Path(binary)
    command = [str(loader), "--library-path", str(library_path), str(binary), "--version"]
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=60,
                                env={**os.environ, "LC_ALL": "C"})
    except OSError as exc:
        raise ValueError(f"Cannot run {binary} through {loader}: {exc}") from exc
    if result.returncode != 0:
        raise ValueError(f"{binary} --version failed through {loader}: "
                         f"{result.stderr.strip() or result.stdout.strip()}")
    match = VERSION_PATTERN.search(result.stdout)
    if not match:
        raise ValueError(f"Cannot determine the PostgreSQL version of {binary}: "
                         f"{result.stdout.strip()!r}")
    return match.group(1) or match.group(2)


def require_version(binary, version, loader, library_path):
    """Prove a binary reports the version the request pinned, before staging."""
    actual = binary_version(binary, loader, library_path)
    if actual != version:
        raise ValueError(f"Built PostgreSQL version {actual} does not match request "
                         f"{version} ({binary})")
    return actual


def _request_identity(request):
    """Validate the fixed identity a PostgreSQL bundle is allowed to have."""
    version = request.get("version")
    major = request.get("major")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", version):
        raise ValueError(f"Request version is not an exact patch release: {version!r}")
    if major != version.split(".")[0]:
        raise ValueError(f"Request major {major!r} disagrees with version {version}")
    if request.get("target") != "musl-dynamic":
        raise ValueError("PostgreSQL bundles are musl-dynamic only")
    if request.get("architecture") != "x86_64":
        raise ValueError("PostgreSQL bundles are x86_64 only")
    source = request.get("source")
    if not isinstance(source, dict) or not re.fullmatch(r"[a-f0-9]{64}",
                                                        str(source.get("sha256", ""))):
        raise ValueError("Request carries no authenticated source archive")
    return version, major


def _archive_location(provenance, version):
    """`source.py --download` puts the archive beside the dependency prefix."""
    return Path(provenance["deps_prefix"]).parent / "downloads" / f"postgresql-{version}.tar.bz2"


def _library_path(provenance, install_root):
    """The directories the shipped launcher will search, for this workspace.

    `install_root/lib` comes first and is not optional: libpq is a shared
    library of the install tree itself, so a frontend such as clusterdb
    resolves its PQ* symbols from there. Omitting it fails with
    "Error loading shared library libpq.so.5: No such file or directory".
    """
    deps = Path(provenance["deps_prefix"])
    loader_dir = Path(provenance["loader"]["path"]).parent
    return f"{Path(install_root) / 'lib'}:{deps / 'lib'}:{loader_dir}"


def _archive_member(archive, member, destination):
    """Copy one regular file out of the authenticated source archive."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:bz2") as tar:
        try:
            info = tar.getmember(member)
        except KeyError as exc:
            raise ValueError(f"Source archive is missing {member}") from exc
        if not info.isfile():
            raise ValueError(f"Source archive member is not a regular file: {member}")
        source = tar.extractfile(info)
        with destination.open("xb") as output:
            shutil.copyfileobj(source, output)
        destination.chmod(0o644)
    return destination


def _copy_tree(install_root, output, provenance):
    """Copy bin/lib/share/include into the bundle, minus share/doc."""
    install_root = Path(install_root)
    for name in COPY_TOP_LEVEL:
        source = install_root / name
        if not source.is_dir():
            raise ValueError(f"Install tree is missing {name}/: {install_root}")
        if name == "share":
            shutil.copytree(source, output / name, symlinks=True,
                            ignore=lambda directory, entries: [
                                entry for entry in entries
                                if entry in DROPPED_SHARE
                                and Path(directory).resolve() == source.resolve()])
        else:
            shutil.copytree(source, output / name, symlinks=True)
    # The vendored shared dependency libraries, keeping both the so.N names
    # NEEDED entries reference and the unversioned .so development symlinks.
    deps_lib = Path(provenance["deps_prefix"]) / "lib"
    for prefix in SHARED_DEPENDENCIES:
        for candidate in sorted(deps_lib.glob(prefix + "*")):
            if candidate.is_symlink():
                (output / "lib" / candidate.name).symlink_to(os.readlink(candidate))
            elif candidate.is_file():
                shutil.copy2(candidate, output / "lib" / candidate.name)
            else:
                raise ValueError(f"Unexpected dependency artefact: {candidate}")


def _check_needed(output):
    """Every NEEDED name a staged ELF carries must resolve inside the bundle.

    Both the commands (before launchers.install moves them) and the loadable
    modules are checked: a module linked against a library that is not in lib/
    would dlopen-fail only when a user creates its extension.
    """
    provided = {path.name for path in (output / "lib").iterdir()}
    elfs = [output / "bin" / command for command in launchers.commands(output)]
    elfs.extend(sorted((output / "lib").glob("*.so")))
    for path in elfs:
        for needed in _needed(path):
            if needed in LOADER_LIBRARIES or needed in provided:
                continue
            raise ValueError(
                f"{path.name} needs {needed}, which is not bundled in lib/. "
                "Staging refuses a bundle that would only run on the build host.")


def builder_hash(repo_root):
    """Fold every file this runtime's scripts and config are made of, in order."""
    repo_root = Path(repo_root)
    result = hashlib.sha256()
    for path in sorted((repo_root / "runtimes/postgresql").rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(repo_root)
        # Bytecode is generated by whoever ran the tests, not shipped source; it
        # would make the build id depend on the build host.
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        result.update(str(relative).encode())
        result.update(bytes.fromhex(digest(path)))
    return result.hexdigest()


def _verify_file(record, what):
    """Refuse a licence or loader whose bytes changed after the build hashed it."""
    path = Path(record["path"])
    if not path.is_file():
        raise ValueError(f"{what} is missing: {path}")
    actual = digest(path)
    if actual != record["sha256"]:
        raise ValueError(f"{what} changed after the build: {path}")
    return path


def stage(install_root, output, request, provenance, repo_root):
    """Build the bundle at `output` and return the manifest staged with it."""
    install_root = Path(install_root)
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError(f"Refusing to replace existing bundle: {output}")
    version, major = _request_identity(request)
    pinned = request["source"].get("version")
    if pinned is not None and pinned != version:
        raise ValueError("Request version disagrees with its authenticated source pin")

    # Every key the manifest is built from, checked before output exists: a
    # truncated toolchain.json must name what it lost here, not raise KeyError
    # from the middle of staging with a half bundle on disk.
    for key in ("deps_prefix", "loader", "dependencies", "locale", "icu",
                "configure_flags", "compiler", "zig"):
        if key not in provenance:
            raise ValueError(f"Provenance record is missing {key}")
    loader_record = provenance["loader"]
    archive = _archive_location(provenance, version)

    # Everything that can refuse runs before `output` exists, so a failed stage
    # never leaves an inspectable-looking half bundle behind.
    commands_on_disk = launchers.commands(install_root)
    if not commands_on_disk:
        raise ValueError(f"No executables found under {install_root / 'bin'}")
    library_path = _library_path(provenance, install_root)
    for command in commands_on_disk:
        require_version(install_root / "bin" / command, version,
                        loader_record["path"], library_path)
    _verify_file(loader_record, "The musl loader")
    _verify_file(loader_record["license"], "The musl licence notice")
    for dependency in provenance["dependencies"]:
        _verify_file(dependency["license"], f"The {dependency['name']} licence notice")
    if not archive.is_file():
        raise ValueError(f"Authenticated source archive is missing: {archive}")
    if digest(archive) != request["source"]["sha256"]:
        raise ValueError(f"Authenticated source archive changed after build: {archive}")

    output.mkdir(parents=True, exist_ok=False)
    _copy_tree(install_root, output, provenance)

    (output / "lib/musl").mkdir()
    staged_loader = output / launchers.LOADER
    shutil.copy2(loader_record["path"], staged_loader)
    staged_loader.chmod(0o755)
    if digest(staged_loader) != loader_record["sha256"]:
        raise ValueError("The staged musl loader does not match its recorded digest")

    _check_needed(output)
    launchers.install(output)

    licenses = output / "share/licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    shutil.copy2(RUNTIME_DIR / "packaging/launch.sh", output / "libexec/launch.sh")
    shutil.copy2(Path(repo_root) / "LICENSE", licenses / "builder.txt")
    _archive_member(archive, f"postgresql-{version}/COPYRIGHT",
                    licenses / "postgresql.txt")
    shutil.copy2(loader_record["license"]["path"], licenses / "musl.txt")
    for dependency in provenance["dependencies"]:
        shutil.copy2(dependency["license"]["path"],
                     licenses / f"{dependency['name']}.txt")
    sources = output / "share/sources"
    sources.mkdir()
    staged_archive = sources / archive.name
    shutil.copy2(archive, staged_archive)
    if digest(staged_archive) != request["source"]["sha256"]:
        raise ValueError("The staged source archive changed while being copied")

    inventory = modules.inventory(output)
    expected = modules.load_expected(major)
    differences = modules.compare(inventory, expected)
    missing = [line for line in differences if line.startswith("missing ")]
    if missing:
        raise ValueError("The build did not install every expected module: "
                         + "; ".join(missing))

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "runtime": "postgresql",
        "postgresql": {"version": version, "major": major},
        "target": "musl-dynamic",
        "architecture": "x86_64",
        "linkage": "dynamic",
        "build_id": hashlib.sha256((
            json.dumps(inventory, sort_keys=True)
            + json.dumps(provenance, sort_keys=True)
            + builder_hash(repo_root)).encode()).hexdigest()[:16],
        "verification": "NOT_RUN",
        "loader": {
            "path": launchers.LOADER,
            "sha256": loader_record["sha256"],
            "musl_version": loader_record["musl_version"],
        },
        "locale": {
            "default_provider": provenance["locale"]["provider"],
            "default_locale": provenance["locale"]["default"],
            # PG_UNICODE_FAST exists from 18 on; a 17 bundle that claimed it
            # would be promising a collation it cannot create.
            "alternate": provenance["locale"]["alternate_18"] if major == "18" else None,
        },
        "libraries": [
            {"name": row["name"], "version": row["version"], "sha256": row["sha256"],
             "linkage": row["linkage"], "license": f"share/licenses/{row['name']}.txt"}
            for row in provenance["dependencies"]
        ],
        "modules": {
            "included": inventory,
            "skipped": [{"name": name, "reason": reason}
                        for name, reason in sorted(modules.EXCLUSIONS.items())],
        },
        "binaries": [
            {"path": f"libexec/bin/{path.name}", "sha256": digest(path), **elf(path)}
            for path in sorted((output / "libexec/bin").iterdir())
            if path.is_file()
        ],
        "source_archive": f"share/sources/{archive.name}",
        "licenses": {
            "builder": "share/licenses/builder.txt",
            "postgresql": "share/licenses/postgresql.txt",
            "musl": "share/licenses/musl.txt",
        },
        "provenance": {
            "upstream": {"url": request["source"].get("url"),
                         "sha256": request["source"]["sha256"]},
            "configure_flags": provenance["configure_flags"],
            "compiler": provenance["compiler"],
            "zig": provenance["zig"],
        },
        # The build decides this, not the plan: Task 7's documentation must state
        # whether ICU is actually linked rather than what was hoped for.
        "icu": provenance["icu"],
        "builder": {
            "commit": run(["git", "-C", str(repo_root), "rev-parse", "HEAD"]).strip(),
            "source_sha256": builder_hash(repo_root),
        },
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    stage_command = sub.add_parser("stage", help="Stage the install tree into a bundle")
    stage_command.add_argument("install_root", type=Path)
    stage_command.add_argument("output", type=Path)
    stage_command.add_argument("request", type=Path)
    stage_command.add_argument("provenance", type=Path)
    stage_command.add_argument("repo_root", type=Path)
    args = parser.parse_args()
    if args.command == "stage":
        stage(args.install_root, args.output,
              json.loads(args.request.read_text()),
              json.loads(args.provenance.read_text()),
              args.repo_root)


if __name__ == "__main__":
    _main()
