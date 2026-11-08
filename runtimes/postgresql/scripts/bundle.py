#!/usr/bin/env python3
"""Stage and verify a relocatable PostgreSQL bundle.

`stage` is the only writer of a bundle: it proves every command runs through
the shipped loader before it creates anything, copies the install tree into the
documented layout, wraps the raw ELFs in launcher scripts, and records what it
staged in `manifest.json`. Tasks 6-9 read that manifest, so its keys are a
contract from this module onward.

`verify` is the only writer of `verification`: it re-proves every byte the
manifest records, runs a real cluster through the launchers with the host's own
musl loader masked away, repeats that lifecycle from a relocated copy, and
exercises every included module with a query that fails if the module did not
link. It reports PASSED only after all of that returns.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

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

# Every place a Linux host keeps a musl loader. A bundle that resolved one of
# these instead of its own would pass on the build host and fail everywhere
# else, so verification makes each of them unusable before it starts a server.
HOST_LOADERS = ("/lib/ld-musl-x86_64.so.1", "/lib64/ld-musl-x86_64.so.1",
                "/usr/lib/ld-musl-x86_64.so.1", "/usr/lib64/ld-musl-x86_64.so.1",
                # `make install` for musl defaults to --prefix=/usr/local, so a
                # source install leaves the loader here, where neither the
                # distro paths above nor the musl-dev tree below would find it.
                "/usr/local/lib/ld-musl-x86_64.so.1",
                "/usr/local/lib64/ld-musl-x86_64.so.1")
# A musl-dev tree ships the loader again, and `cc -print-file-name=` style
# probes find it there. Masking the directory hides every copy at once.
HOST_LOADER_TREES = ("/usr/x86_64-linux-musl/lib64",)
# The bwrap prefix travels in the child's environment so that every helper
# handed only an env — smoke, the round-trip tools, the ICU check — still
# starts its commands inside the same sandbox as initdb and the postmaster.
MASK_VARIABLE = "PG_BUNDLE_VERIFY_MASK"

# How each contrib cli module is exercised against the running cluster. A cli
# module with no entry here is a ValueError naming it, never a bare --version.
# Which modules run is read from checks(), never from a list beside it, so a
# cli module added to the manifest is executed rather than silently skipped.
CLI_PROOFS = {
    "oid2name": ("-d", "postgres", "-q"),
    "vacuumlo": ("-n", "postgres"),
}

# Every SQL module the manifest may include, with the statement that proves it
# linked. Each statement is written from the module's own
# share/extension/<name>--*.sql and calls something that only exists in the
# module's shared object: a bare CREATE EXTENSION proves the SQL script ran,
# not that the library loads or that its vendored dependency linked. The unit
# test `test_every_sql_module_in_the_real_manifest_has_a_statement` fails when
# a module is added here without a proof, so "as many extensions as possible"
# cannot quietly become "as many as the table remembered".
SQL_PROOFS = {
    "amcheck": "CREATE TABLE amcheck_probe (i int);\n"
               "INSERT INTO amcheck_probe SELECT generate_series(1, 100);\n"
               "CREATE INDEX amcheck_probe_i ON amcheck_probe (i);\n"
               # bt_index_check returns void, so the proof is that it ran at all.
               "SELECT count(*) = 1 FROM (SELECT bt_index_check('amcheck_probe_i')) AS called;",
    "autoinc": "CREATE TABLE autoinc_probe (id int);\n"
               "CREATE SEQUENCE autoinc_probe_seq;\n"
               "CREATE TRIGGER autoinc_probe_t BEFORE INSERT ON autoinc_probe FOR EACH ROW"
               " EXECUTE FUNCTION autoinc('id', 'autoinc_probe_seq');\n"
               "INSERT INTO autoinc_probe VALUES (0);\n"
               "SELECT id = 1 FROM autoinc_probe;",
    "bloom": "CREATE TABLE bloom_probe (i int);\n"
             "INSERT INTO bloom_probe SELECT generate_series(1, 100);\n"
             "CREATE INDEX bloom_probe_i ON bloom_probe USING bloom (i);\n"
             "SET enable_seqscan = off;\n"
             "SELECT count(*) = 1 FROM bloom_probe WHERE i = 42;",
    "btree_gin": "CREATE TABLE btree_gin_probe (i int);\n"
                 "INSERT INTO btree_gin_probe SELECT generate_series(1, 10);\n"
                 "CREATE INDEX btree_gin_probe_i ON btree_gin_probe USING gin (i);\n"
                 "SET enable_seqscan = off;\n"
                 "SELECT count(*) = 1 FROM btree_gin_probe WHERE i = 7;",
    "btree_gist": "CREATE TABLE btree_gist_probe (i int, span int4range,"
                  " EXCLUDE USING gist (i WITH =, span WITH &&));\n"
                  "INSERT INTO btree_gist_probe VALUES (1, '[1,5)');\n"
                  "SELECT count(*) = 1 FROM btree_gist_probe WHERE i = 1;",
    "citext": "SELECT 'ABC'::citext = 'abc';",
    "cube": "SELECT cube_distance('(0,0)'::cube, '(3,4)'::cube) = 5;",
    "dblink": "SELECT dblink_connect('proof', '{dsn}');\n"
              "SELECT x = 1 FROM dblink('proof', 'SELECT 1') AS t(x int);\n"
              "SELECT dblink_disconnect('proof') = 'OK';",
    "dict_int": "CREATE TEXT SEARCH DICTIONARY proof_intdict (TEMPLATE = intdict_template);\n"
                "SELECT ts_lexize('proof_intdict', '123') = '{123}';",
    "dict_xsyn": "CREATE TEXT SEARCH DICTIONARY proof_xsyn"
                 " (TEMPLATE = xsyn_template, rules = 'xsyn_sample');\n"
                 "SELECT array_to_string(ts_lexize('proof_xsyn', 'supernova'), ',') LIKE '%1987a%';",
    "earthdistance": "SELECT earth_distance(ll_to_earth(0, 0), ll_to_earth(0, 1)) > 0;",
    "file_fdw": "CREATE SERVER proof_file FOREIGN DATA WRAPPER file_fdw;\n"
                "CREATE FOREIGN TABLE proof_file_t (line text) SERVER proof_file"
                " OPTIONS (filename '/proc/version');\n"
                "SELECT count(*) >= 1 FROM proof_file_t;",
    "fuzzystrmatch": "SELECT levenshtein('kitten', 'sitting') = 3;",
    "hstore": "SELECT 'a=>1'::hstore->'a' = '1';",
    "insert_username": "CREATE TABLE insert_username_probe (name text, changed_by text);\n"
                       "CREATE TRIGGER insert_username_probe_t BEFORE INSERT"
                       " ON insert_username_probe FOR EACH ROW"
                       " EXECUTE FUNCTION insert_username('changed_by');\n"
                       "INSERT INTO insert_username_probe (name) VALUES ('a');\n"
                       "SELECT changed_by = 'verify' FROM insert_username_probe;",
    "intagg": "SELECT int_array_aggregate(i) = '{1,2,3}' FROM generate_series(1, 3) AS g(i);",
    "intarray": "SELECT icount('{1,2,3}'::int[]) = 3;",
    # The ISBN input is normalised to its 10-digit form: the proof is the
    # module's input function, not the string in the manifest.
    "isn": "SELECT isbn('978-0-393-04002-9') = '0-393-04002-X'::isbn;",
    "lo": "CREATE TABLE lo_probe (id oid);\n"
          "INSERT INTO lo_probe VALUES (lo_from_bytea(0, 'abc'::bytea));\n"
          # lo_manage is the module's C function; deleting through it unlinks
          # the large object, which pg_largeobject_metadata can then confirm.
          "CREATE TRIGGER lo_probe_t BEFORE UPDATE OR DELETE ON lo_probe"
          " FOR EACH ROW EXECUTE FUNCTION lo_manage(id);\n"
          "DELETE FROM lo_probe;\n"
          "SELECT count(*) = 0 FROM pg_largeobject_metadata;",
    "ltree": "SELECT 'a.b'::ltree <@ 'a'::ltree;",
    "moddatetime": "CREATE TABLE moddatetime_probe (id int, touched timestamptz DEFAULT now());\n"
                   "CREATE TRIGGER moddatetime_probe_t BEFORE UPDATE ON moddatetime_probe"
                   " FOR EACH ROW EXECUTE FUNCTION moddatetime(touched);\n"
                   "INSERT INTO moddatetime_probe VALUES (1, now() - interval '1 day');\n"
                   "UPDATE moddatetime_probe SET id = 2;\n"
                   "SELECT touched > now() - interval '1 minute' FROM moddatetime_probe;",
    "pageinspect": "SELECT (page_header(get_raw_page('pg_class', 0))).lsn IS NOT NULL;",
    "pg_buffercache": "SELECT count(*) >= 1 FROM pg_buffercache WHERE relfilenode IS NOT NULL;",
    "pg_freespacemap": "SELECT pg_freespace('pg_class', 0) >= 0;",
    # A fresh cluster has no serialized snapshots, so the probe cannot read a
    # real one. What it can do is prove the C function linked: it must reject
    # the probe's file name with its own message rather than a missing symbol.
    "pg_logicalinspect": "DO $$ BEGIN\n"
                         "  PERFORM pg_get_logical_snapshot_meta('verification-probe.snap');\n"
                         "  RAISE EXCEPTION 'pg_logicalinspect accepted a snapshot name it should reject';\n"
                         "EXCEPTION WHEN OTHERS THEN\n"
                         "  IF SQLERRM NOT LIKE 'invalid snapshot file name%' THEN\n"
                         "    RAISE EXCEPTION 'pg_logicalinspect did not link: %', SQLERRM;\n"
                         "  END IF;\n"
                         "END $$;\n"
                         "SELECT 1 = 1;",
    "pg_prewarm": "SELECT pg_prewarm('pg_class') >= 0;",
    # pg_stat_statements is created into its own schema so the view exists even
    # when --schema is not set, and its count proves the shared object's hooks
    # are installed. Without the preload it is not loadable at all, which is a
    # failure this task must report rather than paper over.
    "pg_stat_statements": "SELECT count(*) >= 1 FROM verify_stat_statements.pg_stat_statements;",
    "pg_surgery": "CREATE TABLE pg_surgery_probe (i int);\n"
                  "INSERT INTO pg_surgery_probe VALUES (1);\n"
                  "SELECT count(*) = 1 FROM (SELECT heap_force_freeze("
                  "'pg_surgery_probe'::regclass, ARRAY['(0,1)'::tid])) AS called;",
    "pg_trgm": "SELECT similarity('abc', 'abd') > 0;",
    "pg_visibility": "CREATE TABLE pg_visibility_probe (i int);\n"
                     "INSERT INTO pg_visibility_probe VALUES (1);\n"
                     "SELECT bool_and(all_visible = false)"
                     " FROM pg_visibility_map('pg_visibility_probe');",
    "pg_walinspect": "SELECT count(*) >= 0 FROM pg_get_wal_stats("
                     "pg_current_wal_flush_lsn() - 4096, pg_current_wal_flush_lsn() + 1);",
    # Proves the *shared* OpenSSL the bundle vendors links through the launcher.
    "pgcrypto": "SELECT encode(digest('x', 'sha256'), 'hex') ="
                " '2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881';",
    "pgrowlocks": "CREATE TABLE pgrowlocks_probe (i int);\n"
                  "INSERT INTO pgrowlocks_probe VALUES (1);\n"
                  "SELECT count(*) >= 0 FROM pgrowlocks('pgrowlocks_probe');",
    "pgstattuple": "SELECT table_len > 0 FROM pgstattuple('pg_class');",
    "plpgsql": "DO $$ BEGIN RAISE NOTICE 'ok'; END $$;\nSELECT 1 = 1;",
    "postgres_fdw": "CREATE SERVER proof_pg FOREIGN DATA WRAPPER postgres_fdw"
                    " OPTIONS (host '{host}', port '{port}', dbname '{dsn_db}');\n"
                    "CREATE USER MAPPING FOR verify SERVER proof_pg OPTIONS (user 'verify');\n"
                    "CREATE FOREIGN TABLE postgres_fdw_probe (relname text) SERVER proof_pg"
                    " OPTIONS (schema_name 'pg_catalog', table_name 'pg_class');\n"
                    "SELECT count(*) > 0 FROM postgres_fdw_probe;",
    "refint": "CREATE TABLE refint_parent (id int PRIMARY KEY);\n"
              "CREATE TABLE refint_child (id int, parent_id int);\n"
              "CREATE CONSTRAINT TRIGGER refint_child_t AFTER INSERT OR UPDATE ON refint_child"
              " FOR EACH ROW EXECUTE FUNCTION"
              " check_primary_key('parent_id', 'refint_parent', 'id');\n"
              "INSERT INTO refint_parent VALUES (1);\n"
              "INSERT INTO refint_child VALUES (1, 1);\n"
              "SELECT count(*) = 1 FROM refint_child;",
    "seg": "SELECT '10'::seg << '11'::seg;",
    "sslinfo": "SELECT ssl_is_used() = false;",
    "tablefunc": "SELECT count(*) = 5 FROM normal_rand(5, 0, 1);",
    "tcn": "CREATE TABLE tcn_probe (i int PRIMARY KEY);\n"
           "CREATE TRIGGER tcn_probe_t AFTER INSERT ON tcn_probe FOR EACH ROW"
           " EXECUTE FUNCTION triggered_change_notification();\n"
           "INSERT INTO tcn_probe VALUES (1);\nSELECT count(*) = 1 FROM tcn_probe;",
    "tsm_system_rows": "CREATE TABLE tsm_rows_probe AS SELECT generate_series(1, 100) AS g;\n"
                       "SELECT count(*) <= 100 FROM tsm_rows_probe TABLESAMPLE system_rows(10);",
    "tsm_system_time": "CREATE TABLE tsm_time_probe AS SELECT generate_series(1, 1000) AS g;\n"
                       "SELECT count(*) <= 1000 FROM tsm_time_probe TABLESAMPLE system_time(10);",
    "unaccent": "CREATE TEXT SEARCH CONFIGURATION proof_unaccent (COPY = pg_catalog.english);\n"
                "ALTER TEXT SEARCH CONFIGURATION proof_unaccent"
                " ALTER MAPPING FOR hword, hword_part, word WITH unaccent, english_stem;\n"
                "SELECT to_tsvector('proof_unaccent', 'Hôtel')"
                " @@ to_tsquery('proof_unaccent', 'hotel');",
    # Proves the *static* libuuid that util-linux vendored actually linked.
    "uuid-ossp": "SELECT uuid_generate_v4()::text ~ '^[0-9a-f]{8}-';",
    # Proves the *static* libxml2 linked.
    "xml2": "SELECT array_to_string(xpath('//a', '<a>1</a>'), ',') = '<a>1</a>';",
}


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
    # Every difference is fatal, not just a missing module. The config is
    # generated from the staged trees themselves, so a module the tree has and
    # the config does not means the build installed something nobody reviewed —
    # and an `unexpected` line dropped on the floor is how `shared: []` once
    # read as a verified empty set. `stage()` reports it; it does not skip it.
    differences = modules.compare(inventory, expected)
    if differences:
        raise ValueError("The build did not install exactly the expected modules: "
                         + "; ".join(differences))

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
                        for name, reason in sorted(modules.exclusions_for(major).items())],
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


# -- verification ---------------------------------------------------------
#
# verify() answers one question: is this staged tree a working PostgreSQL
# server for a host that has no musl of its own? Every check below exists
# because a cheaper version of it would answer "yes" for a bundle that only
# works on this build host. The host really does carry
# /lib/ld-musl-x86_64.so.1, so a server started without masking could resolve
# it and report PASSED for a bundle that needs the host — that is the failure
# mode this whole section is built to make impossible.


def checks(manifest):
    """Plan the proof-of-linkage checks for every included module and tool.

    Pure: the plan is a function of the manifest's module list and nothing
    else, so it is testable without a server, a bundle, or a build. A `sql`
    module with no statement is a ValueError naming it — never a silent skip,
    because a skipped module is exactly how "verified" comes to mean "mostly
    verified".
    """
    plan = []
    for row in manifest["modules"]["included"]:
        kind, name = row["kind"], row["name"]
        if kind == "sql":
            if name not in SQL_PROOFS:
                raise ValueError(
                    f"No proof statement for sql module {name!r}; every included "
                    "extension needs one, written from its own share/extension SQL")
            plan.append({"kind": "sql", "name": name, "statement": SQL_PROOFS[name]})
        elif kind == "preload":
            plan.append({"kind": "preload", "name": name, "statement": name})
        elif kind == "cli":
            # The statement is the command name; smoke() runs it against the
            # cluster. A tool the bundle does not ship fails there, by name.
            plan.append({"kind": "cli", "name": name, "statement": name})
        elif kind == "shared":
            # Shared libraries are loaded by something else — libpq by a
            # launcher, pgoutput by the server — so they have no standalone
            # proof; the binaries that need them are proven by using them.
            continue
        else:
            raise ValueError(f"Unknown module kind {kind!r} for {name!r}")
    return plan


def preload_list(manifest):
    """Every preload module in manifest order, ready for shared_preload_libraries."""
    return [row["name"] for row in manifest["modules"]["included"]
            if row["kind"] == "preload"]


def _workspace():
    """A short, private directory for one verification run.

    The path stays short on purpose: PostgreSQL socket paths are limited by
    sun_path, and the repo's own dist path is already long.
    """
    return Path(tempfile.mkdtemp(prefix="pg-verify-"))


def _mask_arguments(workspace):
    """bwrap arguments that replace every host loader with a zero-byte file.

    A zero-byte file is not a valid ELF, so any binary that somehow resolved
    one of these fails to execute. `--dev-bind / /` keeps the caller's uid, so
    initdb still accepts the data directory; the bind mounts are read-only so
    a run cannot modify the host.

    Only real files are bind-mounted: on a merged-/usr host /lib and /lib64 are
    symlinks (here /lib64/ld-musl-x86_64.so.1 -> ../lib/...), and bwrap refuses
    a mount on a symlink destination. Masking the resolved target covers every
    alias of it, so resolving first both avoids the refusal and hides the
    loader under all of its names.
    """
    workspace = Path(workspace)
    empty = workspace / "mask/empty"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.touch()
    empty_tree = workspace / "mask/empty-tree"
    empty_tree.mkdir(exist_ok=True)
    arguments = ["--dev-bind", "/", "/"]
    masked = set()
    for loader in HOST_LOADERS:
        path = Path(loader)
        if not path.exists() or path.is_symlink():
            continue
        target = path.resolve()
        if target in masked:
            continue
        masked.add(target)
        arguments += ["--ro-bind", str(empty), str(target)]
    for tree in HOST_LOADER_TREES:
        path = Path(tree)
        if not path.exists() or path.is_symlink() or path.resolve() in masked:
            continue
        masked.add(path.resolve())
        arguments += ["--ro-bind", str(empty_tree), str(path)]
    return arguments


def _bwrap_usable():
    """Whether bwrap can create the namespaces the masking needs, right now."""
    executable = shutil.which("bwrap")
    if executable is None:
        return False
    workspace = _workspace()
    try:
        result = subprocess.run(
            [executable, *_mask_arguments(workspace), "--", "/bin/sh", "-c", "exit 0"],
            text=True, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return result.returncode == 0


def _masked_host_loaders_probe():
    """Enter the mask and assert the host loaders really are unusable."""
    executable = shutil.which("bwrap")
    workspace = _workspace()
    try:
        # Every alias of a host loader must be missing or zero bytes inside the
        # sandbox, and every musl-dev tree must be empty, or the mask is not
        # the mask that was asked for. A tree left populated is a second way to
        # resolve a host loader, so it is asserted exactly like the files.
        checks = [
            f"test ! -s {loader} || {{ echo '{loader} is still a live loader' >&2; exit 1; }}"
            for loader in HOST_LOADERS]
        checks += [
            f"test -z \"$(ls -A {tree} 2>/dev/null)\" || "
            f"{{ echo '{tree} still lists entries' >&2; exit 1; }}"
            for tree in HOST_LOADER_TREES]
        script = "; ".join(checks)
        result = subprocess.run(
            [executable, *_mask_arguments(workspace), "--", "/bin/sh", "-c", script],
            text=True, capture_output=True, timeout=60)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    if result.returncode != 0:
        raise ValueError("Host loader masking did not take effect: "
                         + (result.stderr.strip() or result.stdout.strip()))
    return "bwrap"


def _poison_probe(bundle):
    """Fallback: prove every staged command resolves its libraries only from the bundle.

    Used only when bwrap genuinely cannot run, and it proves strictly less than
    the mask, so it must prove as much as it can: every launcher, not one
    hand-picked command. Each staged command resolves its NEEDED libraries
    through the launcher's `--library-path`, never from a default host
    directory. For each command the probe prepends a directory holding a
    zero-byte file named after a library that command NEEDS and that the bundle
    carries: the loader must then fail with "Exec format error" rather than
    quietly falling back to a host copy. A command that still succeeds resolved
    that library from somewhere the launcher does not control, and a command
    that fails without the poison is not resolvable at all.

    A command with no bundle-resident NEEDED library (pg_config links only
    libc, which is the loader itself) has nothing to prove here and is skipped;
    the mask, which is the strategy that actually gets published, still covers
    it. The probe returns False unless at least one command proved the point,
    so "no command had anything to prove" can never read as success.
    """
    bundle = Path(bundle)
    library_dir = bundle / "lib"
    if not library_dir.is_dir():
        return False
    proved = 0
    for raw in sorted((bundle / "libexec/bin").iterdir()):
        launcher = bundle / "bin" / raw.name
        if not launcher.is_file() or not raw.is_file():
            continue
        needed = [name for name in _needed(raw)
                  if name not in LOADER_LIBRARIES and (library_dir / name).exists()]
        if not needed:
            continue
        try:
            _run([launcher, "--version"], env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        except (ValueError, OSError):
            return False
        workspace = _workspace()
        try:
            poison = workspace / "poison"
            poison.mkdir()
            for name in needed:
                (poison / name).touch()
            library_path = f"{poison}:{bundle / 'lib'}:{bundle / 'lib/musl'}"
            result = subprocess.run(
                [str(bundle / launchers.LOADER), "--library-path", library_path,
                 "--argv0", str(launcher), str(raw), "--version"],
                text=True, capture_output=True, timeout=300,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        except (OSError, subprocess.TimeoutExpired):
            return False
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        if result.returncode == 0:
            return False
        proved += 1
    return proved > 0


def assert_no_host_loaders(bundle=None):
    """Decide, and prove, that the checks below cannot lean on a host musl loader.

    Returns the strategy that must be used for the lifecycle itself: "bwrap"
    means every staged command runs inside the mask, "poison-file" means bwrap
    cannot create namespaces here and the loader is proven from the bundle's
    own --library-path instead. Raises when neither can run: reporting PASSED
    with no isolation is the false pass this task exists to prevent, so
    "nothing was available" is a failure, never a quieter PASSED.
    """
    if _bwrap_usable():
        return _masked_host_loaders_probe()
    if bundle is not None and _poison_probe(bundle):
        return "poison-file"
    raise ValueError(
        "Cannot prove host loader independence: bwrap cannot create namespaces and "
        "the poison-file probe did not run. A PASSED without one of them could be "
        "a pass produced by the host's own loader.")


def _isolation(workspace, strategy):
    """The bwrap prefix for one staged command, or none under the poison strategy.

    Under "poison-file" the lifecycle deliberately runs unmasked: the proof
    there is that the loader resolves everything through --library-path, which
    the probe has already established.
    """
    if strategy != "bwrap":
        return None
    executable = shutil.which("bwrap")
    if executable is None:
        # The probe chose bwrap a moment ago, so a missing binary here is a
        # changed host, not a fallback opportunity: an unmasked lifecycle would
        # silently stop proving the one thing this task exists to prove.
        raise ValueError("bwrap disappeared between the isolation probe and the lifecycle")
    return [executable, *_mask_arguments(workspace)]


def _run(arguments, env=None, timeout=600, mask=None):
    """Run a staged command; a non-zero exit is a ValueError carrying its output.

    `mask` is the bwrap argument list that hides the host's loaders. It defaults
    to the prefix carried by `env`, and is applied per command rather than
    around the whole run because initdb, the postmaster and every client are
    separate processes: each one has to be started inside the sandbox for the
    proof to mean anything.
    """
    mask = mask if mask is not None else _mask_of(env)
    prefix = [*(str(argument) for argument in mask), "--"] if mask else []
    child = None if env is None else {key: value for key, value in env.items()
                                      if key != MASK_VARIABLE}
    result = subprocess.run([*prefix, *(str(argument) for argument in arguments)],
                            text=True, capture_output=True, timeout=timeout, env=child)
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
        raise ValueError(f"{Path(str(arguments[0])).name} failed with exit code "
                         f"{result.returncode}: {' | '.join(detail[-5:])}")
    return result.stdout


def _environment(workspace, data, socket, mask=None):
    """The environment every staged command in one lifecycle sees.

    PostgreSQL's own client tools read these, so the smoke steps do not have to
    thread -h/-U/-d through forty invocations. LC_ALL keeps the messages this
    module greps for in English.

    The bwrap prefix rides along in the environment under a private key so that
    smoke(), the round-trip tools and the ICU check — which are handed an env,
    not a mask — still start every one of their commands inside the sandbox.
    """
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(workspace),
        "TMPDIR": str(workspace),
        "LC_ALL": "C",
        "PGHOST": str(socket),
        "PGUSER": "verify",
        "PGDATABASE": "postgres",
        "PGDATA": str(data),
    }
    if mask:
        environment[MASK_VARIABLE] = json.dumps([str(argument) for argument in mask])
    return environment


def _mask_of(env):
    """The bwrap prefix carried by an environment, or None outside the sandbox."""
    value = (env or {}).get(MASK_VARIABLE)
    return json.loads(value) if value else None


def _server_log_tail(log, lines=20):
    """The last lines of a server log, for a failure message that names a cause."""
    try:
        return "\n".join(Path(log).read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(the server wrote no log)"


def lifecycle(root, manifest, preload, strategy=None):
    """Create, start, exercise and stop one real cluster under `root`.

    Items 1, 2 and 8-9 of the brief: initdb, pg_ctl start with every preload
    module, smoke, pg_ctl stop, and the log assertion. The server is always
    stopped in a `finally`, on success and failure alike.

    `strategy` is the one assert_no_host_loaders returned. "bwrap" runs every
    command below inside the mask, so the whole lifecycle — not just a probe —
    is what proves the bundle needs no host loader. "poison-file" leaves the
    commands unmasked; the probe has already proven they resolve everything
    from the bundle's own --library-path.

    A Unix socket only — never TCP — so this cannot collide with whatever the
    host already runs, and nothing outside the workspace can connect.

    Every path this writes lives in the private workspace, never in `root`.
    Two reasons: `root` is the staged tree that gets packaged, and a data
    directory left in it would ship; and the staged socket path is already
    longer than sun_path's 108 bytes, so a cluster created there could not even
    start. The workspace is removed on the way out, so a rerun starts clean.
    """
    root = Path(root).resolve()
    version = manifest["postgresql"]["version"]
    workspace = _workspace()
    data = workspace / f"PostgreSQL {version} data"
    socket = workspace / "socket"
    log = workspace / "server.log"
    socket.mkdir(parents=True, exist_ok=True)
    started = False
    try:
        # Inside the try: _isolation can raise (bwrap disappearing between the
        # probe and here), and the workspace must not leak when it does.
        mask = _isolation(workspace, strategy)
        env = _environment(workspace, data, socket, mask)
        _run([root / "bin/initdb", "-D", data, "-U", "verify", "--encoding=UTF8",
              "--locale-provider=builtin", "--builtin-locale=C.UTF-8", "--no-instructions"],
             env=env)
        options = ("-c listen_addresses='' "
                   f"-c unix_socket_directories={socket} "
                   f"-c shared_preload_libraries={','.join(preload)}")
        try:
            _run([root / "bin/pg_ctl", "-D", data, "-l", log, "-w", "-o", options, "start"],
                 env=env)
        except ValueError as exc:
            # This is the "built but unloadable" case: the server knows which
            # library it could not load and says so in its own log.
            raise ValueError(
                f"Server would not start with {','.join(preload)} preloaded: {exc}\n"
                f"{_server_log_tail(log)}") from exc
        started = True
        names = smoke(root, manifest, env=env)
        _stop(root, data, env)
        started = False
        log_text = log.read_text(errors="replace") if log.is_file() else ""
        for marker in ("could not load", "incomplete installation"):
            if marker in log_text:
                raise ValueError(f"Server log reports {marker!r}:\n{_server_log_tail(log)}")
        # Note: `background worker "logical replication launcher" ... exited with
        # exit code 1` is normal upstream behaviour on a cluster with no slots —
        # the distro glibc build logs it too. It is not one of the markers above;
        # do not add it, and do not treat it as a smoke failure.
        return names
    finally:
        if started:
            _stop(root, data, env)
        shutil.rmtree(workspace, ignore_errors=True)


def _stop(root, data, env):
    """Stop the cluster; a stop that leaves the postmaster running is a failure."""
    _run([root / "bin/pg_ctl", "-D", data, "-w", "-m", "fast", "stop"], env=env)


def _psql(psql, arguments, env, stdin=None, timeout=300):
    """Run psql with a statement list on stdin, refusing any error."""
    mask = _mask_of(env)
    prefix = [*(str(argument) for argument in mask), "--"] if mask else []
    child = {key: value for key, value in env.items() if key != MASK_VARIABLE}
    result = subprocess.run(
        [*prefix, str(psql), "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1", *arguments],
        input=stdin, text=True, capture_output=True, timeout=timeout, env=child)
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
        raise ValueError(f"psql failed with exit code {result.returncode}: "
                         f"{' | '.join(detail[-5:])}")
    return result.stdout


def smoke(root, manifest, env=None):
    """Exercise every planned check against a running cluster.

    Returns one check name per module and tool exercised. Every sql module's
    statement must end by printing `t`; anything else — an error, a false, an
    empty result — aborts the run with the module named.

    `env` is the one lifecycle built: it names the running cluster and, under
    the bwrap strategy, carries the mask every command must be started with.
    """
    root = Path(root)
    version = manifest["postgresql"]["version"]
    owned = env is None
    workspace = _workspace() if owned else Path(env["TMPDIR"])
    data = Path(env["PGDATA"]) if env else workspace / f"PostgreSQL {version} data"
    socket = Path(env["PGHOST"]) if env else workspace / "socket"
    env = env or _environment(workspace, data, socket)
    psql = root / "bin/psql"
    exercised = []
    try:
        # Step 3: the server itself says which version it is, via the launcher.
        banner = _psql(psql, ["-c", "SELECT version()"], env)
        if f"PostgreSQL {version}" not in banner:
            raise ValueError(f"Server reports {banner.strip()!r}, not PostgreSQL {version}")
        exercised.append("server-version")

        # Step 4: plpgsql must load in the plain postgres database.
        _psql(psql, ["-c", "DO $$ BEGIN RAISE NOTICE 'ok'; END $$"], env)
        exercised.append("plpgsql")

        # Step 5: one CREATE EXTENSION plus one proof query per sql module.
        for row in checks(manifest):
            if row["kind"] != "sql":
                continue
            _create_extension(psql, row, env)
            statement = _substitute(row["statement"], socket)
            output = _psql(psql, ["-f", "-"], env, stdin=statement + "\n")
            lines = [line for line in output.splitlines() if line.strip()]
            last = lines[-1].strip() if lines else "nothing"
            if last != "t":
                raise ValueError(f"{row['name']} did not prove itself: its proof statement "
                                 f"returned {last!r}, not true")
            exercised.append(f"sql:{row['name']}")

        # Step 6: the client tools, derived from the plan — never a list beside
        # it — so a cli module the plan names is executed or fails by name.
        plan = checks(manifest)
        exercised.extend(_round_trip(root, env, plan))
        # Step 7: ICU ordering, or its absence as a checked fact.
        exercised.append(_icu(root, manifest, env))
        # Step 7b: the alternate builtin collation, when the manifest promises
        # one. 18 records PG_UNICODE_FAST; 17 records null, so this contributes
        # no row at all and the check count stays flat.
        alternate = _alternate_locale(root, manifest, env)
        if alternate is not None:
            exercised.append(alternate)
        # Step 8: every planned preload row. Under bwrap the server already
        # started with exactly this list in shared_preload_libraries, and a
        # bogus entry makes start-up fail — measured on this bundle — so a
        # cluster that came up is the proof that each one loaded.
        for row in plan:
            if row["kind"] == "preload":
                exercised.append(f"preload:{row['name']}")
        return exercised
    finally:
        if owned:
            shutil.rmtree(workspace, ignore_errors=True)


def _substitute(statement, socket):
    """Fill the connection placeholders a proof may use.

    A plain replace, never str.format: several proofs contain braces — array
    literals like '{1,2,3}', JSONB documents, regex quantifiers — and format
    would read those as fields and raise. A '%' in a LIKE pattern must survive
    too, which rules out the %-formatting operator for the same reason.
    """
    values = {
        "dsn": f"host={socket} port=5432 dbname=postgres user=verify",
        "host": str(socket),
        "port": "5432",
        "dsn_db": "postgres",
        "dbname": "postgres",
    }
    for key, value in values.items():
        statement = statement.replace("{" + key + "}", value)
    return statement


def _create_extension(psql, row, env):
    """Install one extension, in its own schema where the module insists."""
    name = row["name"]
    statement = f'CREATE EXTENSION IF NOT EXISTS "{name}" CASCADE'
    if name == "pg_stat_statements":
        # The module only loads through shared_preload_libraries, which
        # lifecycle sets; its own schema keeps the view out of search_path.
        statement = ('CREATE SCHEMA IF NOT EXISTS verify_stat_statements; '
                     f'CREATE EXTENSION IF NOT EXISTS "{name}" SCHEMA verify_stat_statements')
    try:
        _psql(psql, ["-c", statement], env)
    except ValueError as exc:
        raise ValueError(f"CREATE EXTENSION {name} failed: {exc}") from exc


def _round_trip(root, env, plan):
    """Round-trip the core client tools, then exercise every planned cli module.

    The cli half is driven by `plan`, so a module the plan names is either run
    or reported by name when the bundle has no such command. A tool named for
    `database` takes part in the dump/restore round-trip; any other tool is run
    with its own proven invocation on the postgres database.
    """
    exercised = []
    database = "verify_roundtrip"
    dump = Path(env["TMPDIR"]) / "verify.dump"
    _run([root / "bin/createdb", database], env=env)
    _psql(root / "bin/psql", ["-d", database, "-f", "-"], env,
          stdin="CREATE TABLE round_trip (i int);\nINSERT INTO round_trip VALUES (1);\n")
    exercised.append("tool:createdb")
    _run([root / "bin/pg_dump", "-Fc", "-d", database, "-f", dump], env=env)
    exercised.append("tool:pg_dump")
    _run([root / "bin/pg_isready", "-d", database], env=env)
    exercised.append("tool:pg_isready")
    _run([root / "bin/dropdb", database], env=env)
    _run([root / "bin/createdb", database], env=env)
    _run([root / "bin/pg_restore", "-d", database, dump], env=env)
    exercised.append("tool:pg_restore")
    _run([root / "bin/vacuumdb", "--analyze", database], env=env)
    exercised.append("tool:vacuumdb")
    _run([root / "bin/reindexdb", database], env=env)
    exercised.append("tool:reindexdb")
    _run([root / "bin/clusterdb", database], env=env)
    exercised.append("tool:clusterdb")
    _run([root / "bin/dropdb", database], env=env)
    dump.unlink(missing_ok=True)

    # The contrib client tools have no cluster of their own; proving they run
    # against this one proves their launchers resolve libpq. Every planned cli
    # row is run here, so the plan and the execution cannot drift apart.
    for row in plan:
        if row["kind"] != "cli":
            continue
        name = row["name"]
        command = root / "bin" / name
        if not command.is_file():
            raise ValueError(f"Planned cli module {name!r} has no bin/{name} in the bundle")
        arguments = CLI_PROOFS.get(name)
        if arguments is None:
            raise ValueError(f"Planned cli module {name!r} has no proof invocation; "
                             "add one to CLI_PROOFS rather than running --version")
        if name == "vacuumlo":
            # vacuumlo only has work to do once a large object exists.
            _psql(root / "bin/psql", ["-c", "SELECT lo_from_bytea(0, 'x'::bytea)"], env)
        _run([command, *arguments], env=env)
        exercised.append(f"tool:{name}")
    return exercised


def _icu(root, manifest, env):
    """Prove ICU is linked and used, or that it is absent, as a checked fact."""
    psql = root / "bin/psql"
    if any(row["name"] == "icu" for row in manifest["libraries"]):
        # The comparison collation is "C", never a locale-derived name. A fresh
        # cluster only knows a name such as "C.utf8" because initdb ran
        # pg_import_system_collations, which enumerates the host's `locale -a`
        # (src/backend/commands/collationcmds.c). Naming one of those would make
        # this proof fail on a host whose catalogue lacks it, turning a
        # host-independent verification into a host-dependent one. "C" is byte
        # order, exists in every major without consulting the host, and keeps
        # the discriminator intact — see the assertion below.
        script = ("CREATE COLLATION verify_icu (provider = icu, locale = 'en-US');\n"
                  "SELECT ('B' < 'a' COLLATE verify_icu) AS icu_order,\n"
                  "       ('B' < 'a' COLLATE \"C\") AS builtin_order;\n")
        try:
            output = _psql(psql, ["-f", "-"], env, stdin=script)
        except ValueError as exc:
            raise ValueError(f"ICU is recorded as linked but its collation failed: {exc}") from exc
        # ICU compares letters before case, so 'B' < 'a' is false there, while
        # the C collation is byte order, where 'B' (0x42) precedes 'a' (0x61)
        # and the comparison is true. A module that fell back to byte order
        # would print t for both, which is exactly what this rejects.
        # Unaligned mode separates the two columns with a pipe, hence the
        # split on both.
        orders = output.strip().replace("|", " ").split()
        if orders != ["f", "t"]:
            raise ValueError("ICU collation did not order differently from the builtin one "
                             f"({output.strip()!r}); ICU is linked but not being used")
        return "icu:linked"
    try:
        _psql(psql, ["-c", "CREATE COLLATION verify_icu (provider = icu, locale = 'en-US')"], env)
    except ValueError as exc:
        if "icu" not in str(exc).lower():
            raise ValueError(f"ICU is absent but the collation failed for another reason: {exc}")
        return "icu:absent"
    raise ValueError("ICU is recorded as absent but CREATE COLLATION provider = icu succeeded")


def _alternate_locale(root, manifest, env):
    """Prove the alternate builtin collation the manifest promises, if any.

    18 records `locale.alternate: PG_UNICODE_FAST`, so a bundle that quietly
    fell back to the default C.UTF-8 would still ship a manifest promising a
    collation it never created. Ordering cannot catch that: `'B' < 'a'` is true
    under PG_UNICODE_FAST exactly as it is under "C", so an ordering assertion
    would pass for a collation that was never used. Case mapping can: Unicode
    uppercases sharp s to SS, while both byte order and C.UTF-8 leave it at one
    character.

    The collation is created with the builtin provider because the name is
    otherwise unaddressable — the cluster's own default is catalogued as
    `pg_c_utf8`, and `COLLATE "C.UTF-8"` is an error on a fresh database
    (measured on this bundle). Nothing here names a locale-derived collation,
    for the same reason _icu does not.

    A manifest with no alternate (`17.11` records null) sends nothing and
    returns None, so this adds no check to a major that has no such promise.
    """
    alternate = manifest["locale"].get("alternate")
    if not alternate:
        return None
    script = (f"CREATE COLLATION verify_alternate (provider = builtin, "
              f"locale = '{alternate}');\n"
              # U&'\00DF' rather than a literal ß: the same character, but it
              # survives any client encoding on the way to the server.
              "SELECT upper(U&'\\00DF' COLLATE verify_alternate) AS alternate_upper,\n"
              "       length(upper(U&'\\00DF' COLLATE verify_alternate)) AS alternate_length;\n")
    try:
        output = _psql(root / "bin/psql", ["-f", "-"], env, stdin=script)
    except ValueError as exc:
        raise ValueError(f"The alternate locale {alternate!r} the manifest promises "
                         f"could not be used: {exc}") from exc
    # Unaligned mode separates the two columns with a pipe: SS|2 is the
    # Unicode answer, ß|1 is the default collation answering instead.
    fields = output.strip().replace("|", " ").split()
    if fields != ["SS", "2"]:
        raise ValueError(f"The alternate locale {alternate!r} did not change case "
                         f"mapping ({output.strip()!r}, expected 'SS|2'); it is "
                         "recorded in the manifest but not being used")
    return f"locale:{alternate}"


def relocate(bundle):
    """Copy the bundle beside itself under a name an unquoted shell would break.

    The `#` is not decoration: an unquoted expansion reaching a shell truncates
    the path there, and the launcher's quoting is what this measures.
    """
    bundle = Path(bundle)
    version = json.loads((bundle / "manifest.json").read_text())["postgresql"]["version"]
    destination = bundle.parent / f"PostgreSQL {version} #relocated"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(bundle, destination, symlinks=True)
    return destination


def pg_config_paths(root, strategy="bwrap"):
    """The four paths pg_config reports, which must all live in the bundle.

    Split by line, never by whitespace: the relocated bundle's name contains
    spaces, and a whitespace split would silently compare the truncated
    `/…/PostgreSQL` as if it were a path.

    pg_config is a staged command like any other, so under bwrap it runs
    masked, in a short-lived workspace of its own that is always removed.
    """
    workspace = _workspace()
    try:
        env = _environment(workspace, workspace / "data", workspace / "socket",
                           _isolation(workspace, strategy))
        output = _run([Path(root) / "bin/pg_config", "--bindir", "--sharedir", "--libdir",
                       "--pkglibdir"], env=env)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    values = output.splitlines()
    if len(values) != 4:
        raise ValueError(f"pg_config reported {len(values)} paths, not four: {output!r}")
    return dict(zip(("bindir", "sharedir", "libdir", "pkglibdir"), values))


def _check_launchers(bundle):
    """bin/ is exactly one shell launcher per raw binary, and never a raw ELF.

    Being a regular executable file is not enough: a stray 0755 ELF dropped in
    bin/ passes that test and then runs against the host interpreter, which is
    the one failure this layout exists to prevent. So no entry may be an ELF,
    every entry must be a script, and the two directories must name the same set
    — a launcher with no binary behind it, or a binary nothing launches, both
    mean the bundle is not the shape the launcher contract describes.
    """
    bundle = Path(bundle)
    bin_dir, libexec_bin = bundle / "bin", bundle / "libexec/bin"
    if not bin_dir.is_dir():
        raise ValueError("The bundle has no bin/ directory")
    if not libexec_bin.is_dir():
        raise ValueError("The bundle has no libexec/bin/ directory")
    raw = {path.name for path in libexec_bin.iterdir() if not path.name.endswith(".sh")}
    launched = set()
    for path in sorted(bin_dir.iterdir()):
        if path.is_symlink():
            raise ValueError(f"bin/{path.name} is a symlink; launchers resolve their own root")
        if not path.is_file():
            raise ValueError(f"bin/{path.name} is not a regular file")
        if not os.access(path, os.X_OK):
            raise ValueError(f"bin/{path.name} is not executable")
        head = path.read_bytes()[:4]
        if head == b"\x7fELF":
            raise ValueError(f"bin/{path.name} is a raw ELF; bin/ holds launchers only")
        if not path.read_bytes().startswith(b"#!"):
            raise ValueError(f"bin/{path.name} is not a shell launcher")
        if path.name not in raw:
            raise ValueError(f"bin/{path.name} has no raw binary in libexec/bin")
        launched.add(path.name)
    missing = sorted(raw - launched)
    if missing:
        raise ValueError(f"Raw binaries with no launcher: {', '.join(missing)}")


def _check_module_inventory(bundle, manifest):
    """Compared against the pinned list; any difference is fatal, in either direction.

    An extra module used to be dropped here on purpose, which made the check
    one-directional: a bundle could gain a module nothing reviewed and still
    verify. The config is generated from the staged trees, so a difference in
    either direction means the bundle is not the one that was reviewed.
    """
    major = manifest["postgresql"]["major"]
    differences = modules.compare(modules.inventory(bundle), modules.load_expected(major))
    if differences:
        raise ValueError(f"The bundle's module inventory does not match the pinned set: "
                         f"{'; '.join(differences)}")
    return differences


def _verify_binaries(bundle, manifest):
    """Every recorded binary is unchanged and still meets the ELF contract."""
    for row in manifest["binaries"]:
        relative = Path(row["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Unsafe binary path in the manifest: {row['path']}")
        path = Path(bundle) / relative
        if path.is_symlink():
            raise ValueError(f"Staged binary is a symlink: {row['path']}")
        # A missing file is a changed bundle, and must read as one rather than
        # as a FileNotFoundError from the digester.
        if not path.is_file():
            raise ValueError(f"Staged binary is missing: {row['path']}")
        if digest(path) != row["sha256"]:
            raise ValueError(f"Staged binary changed after staging: {row['path']}")
        actual = elf(path)
        for key in ("interpreter", "needed", "rpath", "runpath", "glibc_required", "machine"):
            if actual[key] != row[key]:
                raise ValueError(f"ELF contract changed for {row['path']}: {key}")


def _verify_manifest(bundle, manifest):
    """The manifest must describe a bundle this module is allowed to verify."""
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Unsupported manifest schema: {manifest.get('schema_version')!r}")
    if manifest.get("runtime") != "postgresql":
        raise ValueError(f"Not a PostgreSQL bundle: {manifest.get('runtime')!r}")
    if manifest.get("target") != "musl-dynamic" or manifest.get("linkage") != "dynamic":
        raise ValueError("verify only accepts musl-dynamic PostgreSQL bundles")
    version, major = manifest["postgresql"]["version"], manifest["postgresql"]["major"]
    if major != version.split(".")[0]:
        raise ValueError("Manifest major disagrees with its version")
    loader = manifest["loader"]
    if loader["path"] != launchers.LOADER:
        raise ValueError(f"Manifest loader is not the one the launchers exec: {loader['path']!r}")
    staged = Path(bundle) / loader["path"]
    if not staged.is_file() or digest(staged) != loader["sha256"]:
        raise ValueError("Staged musl loader does not match the manifest")
    if not os.access(staged, os.X_OK):
        raise ValueError("Staged musl loader is not executable")
    # musl accepts vi_VN.UTF-8 and collates as C, so a bundle claiming a libc
    # locale provider would be promising a collation it cannot deliver.
    if manifest["locale"]["default_provider"] != "builtin":
        raise ValueError("A PostgreSQL bundle must use the builtin locale provider; "
                         f"the manifest claims {manifest['locale']['default_provider']!r}")


def verify(bundle):
    """Prove the staged bundle is a working server, then record that it passed.

    The manifest is rewritten to NOT_RUN first, exactly as Redis does, so a
    crash halfway through cannot leave PASSED on disk. Nothing else is written
    until every check has returned.
    """
    bundle = Path(bundle)
    path = bundle / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["verification"] = "NOT_RUN"
    manifest.pop("verification_strategy", None)
    manifest.pop("checks", None)
    path.write_text(json.dumps(manifest, indent=2) + "\n")

    _verify_manifest(bundle, manifest)
    _verify_binaries(bundle, manifest)
    _check_launchers(bundle)
    # The bundle goes in: on a host where bwrap cannot create namespaces the
    # poison probe is the only proof available, and it needs a bundle to probe.
    # Without this argument that branch is unreachable and every such host
    # aborts a perfectly verifiable build.
    strategy = assert_no_host_loaders(bundle)

    preload = preload_list(manifest)
    # pg_stat_statements only loads through shared_preload_libraries, and it is
    # an sql module in the manifest, so it joins the preload list for this run.
    if any(row["kind"] == "sql" and row["name"] == "pg_stat_statements"
           for row in manifest["modules"]["included"]):
        preload = preload + ["pg_stat_statements"]
    names = lifecycle(bundle, manifest, preload, strategy)

    # Inside the try from the start: relocate() creates the copy before it
    # returns, so a failure between the copytree and the return would leak it.
    relocated = None
    try:
        relocated = relocate(bundle)
        names = names + lifecycle(relocated, manifest, preload, strategy)
        for key, value in pg_config_paths(relocated, strategy).items():
            if not Path(value).resolve().is_relative_to(relocated.resolve()):
                raise ValueError(f"pg_config --{key} reports {value!r}, outside the "
                                 "relocated bundle")
    finally:
        if relocated is not None:
            shutil.rmtree(relocated, ignore_errors=True)

    _check_module_inventory(bundle, manifest)

    manifest["checks"] = names
    manifest["verification"] = "PASSED"
    manifest["verification_strategy"] = strategy
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return {"checks": names, "strategy": strategy}


def package(bundle, output):
    """Write the three named assets for a bundle whose verification passed.

    The name is the identity the caller downloads by, so every part is checked
    against a conservative character class before anything is created: a
    build_id carrying a slash or a newline would otherwise choose the path.

    Nothing gathered here is inferred from the filesystem: `verification` is
    read from the manifest verify() wrote, so an unverified or half-verified
    tree cannot be packaged by mistake. Redis's reservation order is kept
    deliberately — all three paths are claimed with exclusive opens first, and
    only paths this call created are ever unlinked, so a collision leaves the
    directory exactly as it was found.
    """
    bundle = Path(bundle)
    output = Path(output)
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest.get("runtime") != "postgresql" or manifest.get("verification") != "PASSED":
        raise ValueError("Real PostgreSQL verification must pass before packaging")
    version_record = manifest.get("postgresql")
    version = version_record.get("version") if isinstance(version_record, dict) else None
    parts = [version, manifest.get("target"), manifest.get("architecture"),
             manifest.get("build_id")]
    # A part that is missing or not a string is a broken manifest, not an
    # archive name: str() would happily turn None into "None" here.
    if any(not isinstance(part, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", part)
           for part in parts):
        raise ValueError("Invalid PostgreSQL archive identity")
    name = "postgresql-" + "-".join(parts)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / (name + ".tar.gz")
    checksum = output / (name + ".tar.gz.sha256")
    sidecar = output / (name + ".manifest.json")
    paths = (archive, checksum, sidecar)
    if any(path.exists() or path.is_symlink() for path in paths):
        raise FileExistsError("Refusing to replace PostgreSQL assets: " + name)
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
            # Never let cleanup replace the failure that caused it: a path that
            # vanished between the open and here must not become the exception
            # a caller reports.
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
        raise
    return archive


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    stage_command = sub.add_parser("stage", help="Stage the install tree into a bundle")
    stage_command.add_argument("install_root", type=Path)
    stage_command.add_argument("output", type=Path)
    stage_command.add_argument("request", type=Path)
    stage_command.add_argument("provenance", type=Path)
    stage_command.add_argument("repo_root", type=Path)
    verify_command = sub.add_parser("verify", help="Prove a staged bundle is a working server")
    verify_command.add_argument("bundle", type=Path)
    package_command = sub.add_parser("package", help="Write the publishable assets")
    package_command.add_argument("bundle", type=Path)
    package_command.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "stage":
        stage(args.install_root, args.output,
              json.loads(args.request.read_text()),
              json.loads(args.provenance.read_text()),
              args.repo_root)
    elif args.command == "verify":
        result = verify(args.bundle)
        for name in result["checks"]:
            print(name)
        print(f"verification strategy: {result['strategy']}")
    else:
        print(package(args.bundle, args.output))


if __name__ == "__main__":
    _main()
