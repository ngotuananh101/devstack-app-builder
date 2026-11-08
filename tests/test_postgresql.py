"""PostgreSQL source authenticity and CLI contracts."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "runtimes/postgresql/scripts"
HOST_LOADER = Path("/lib64/ld-linux-x86-64.so.2")

# Prints its message with raw syscalls only: no libc, no GLIBC_* version
# requirement, so the glibc host loader can run a binary that carries a musl
# PT_INTERP. That is what lets these tests exercise the real readelf contract
# and the loader indirection on a host that has no musl.
FIXTURE_SOURCE = """\
#ifndef MSG
#define MSG "postgres (PostgreSQL) 18.6\\n"
#endif

static long call(long number, long first, long second, long third)
{
\tlong result;
\t__asm__ volatile ("syscall"
\t                  : "=a" (result)
\t                  : "a" (number), "D" (first), "S" (second), "d" (third)
\t                  : "rcx", "r11", "memory");
\treturn result;
}

void _start(void)
{
\tstatic const char message[] = MSG;
\tcall(1, 1, (long) message, (long) (sizeof message - 1));
\tcall(60, 0, 0, 0);
\t__builtin_unreachable();
}
"""


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_elf(root, destination, needed=("libpq.so.5",), search=None, message=None):
    """Compile a musl-interp executable that needs no libc to run."""
    source = Path(root) / "fixture.c"
    if not source.exists():
        source.write_text(FIXTURE_SOURCE)
    command = ["cc", "-nostdlib", "-fno-stack-protector",
               "-Wl,--dynamic-linker=/lib/ld-musl-x86_64.so.1", "-Wl,-e,_start",
               "-Wl,--no-as-needed"]
    if message is not None:
        command.append(f'-DMSG="{message}"')
    command.extend(["-o", str(destination), str(source)])
    search = Path(search) if search is not None else destination.parent
    command.append(f"-L{search}")
    command.extend(f"-l:{name}" for name in needed)
    subprocess.run(command, check=True, capture_output=True)
    return destination


def build_library(destination, soname):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = destination.parent / (soname + ".c")
    source.write_text("int fixture(void) { return 1; }\n")
    subprocess.run(["cc", "-shared", "-nostdlib", "-fPIC", "-Wl,-soname," + soname,
                    "-o", str(destination), str(source)], check=True, capture_output=True)
    return destination


def module(name):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"postgresql_{name}", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class PostgreSQLSourceTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((SCRIPTS / "source.py").is_file(), "PostgreSQL source authentication module is missing")
        self.source = module("source")

    def test_pinned_sources_select_exact_upstream_archives(self):
        row = self.source.load_source("18.6")
        self.assertEqual(row["url"], "https://ftp.postgresql.org/pub/source/v18.6/postgresql-18.6.tar.bz2")
        self.assertEqual(row["sha256"], "555610c24d53e4316da5b7d3fc25c279d96856d5e0e23ee308c328c5fa881d9f")
        self.assertEqual(row["series"], "18")
        self.assertEqual(self.source.load_source("17.11")["series"], "17")

    def test_unpinned_and_wrong_shaped_versions_are_rejected(self):
        for value in ("18", "18.6.1", "latest", "17", "16.9", "../tmp", "18.6\n", "19.0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.source.load_source(value)

    def test_digest_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source.tar.bz2"
            path.write_bytes(b"source")
            self.source.verify_digest(path, "41cf6794ba4200b839c53531555f0f3998df4cbb01a4d5cb0b94e3ca5e23947d")
            path.write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                self.source.verify_digest(path, "41cf6794ba4200b839c53531555f0f3998df4cbb01a4d5cb0b94e3ca5e23947d")

    def test_extraction_is_fresh_and_confined(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "archive.tar.bz2"
            with tarfile.open(archive, "w:bz2") as tar:
                for path in ("postgresql-18.6/src/include/postgres.h", "postgresql-18.6/contrib/Makefile"):
                    info = tarfile.TarInfo(path)
                    info.size = 4
                    tar.addfile(info, io.BytesIO(b"test"))
            dest = root / "source"
            result = self.source.extract_source(archive, dest)
            self.assertEqual((result / "src/include/postgres.h").read_bytes(), b"test")
            self.assertEqual((result / "contrib/Makefile").read_bytes(), b"test")
            with self.assertRaises(FileExistsError):
                self.source.extract_source(archive, dest)

    def test_safe_documentation_symlinks_are_not_materialized(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "archive.tar.bz2"
            with tarfile.open(archive, "w:bz2") as tar:
                for path in ("postgresql-18.6/src/include/postgres.h", "postgresql-18.6/contrib/Makefile"):
                    info = tarfile.TarInfo(path)
                    info.size = 4
                    tar.addfile(info, io.BytesIO(b"test"))
                link = tarfile.TarInfo("postgresql-18.6/doc/README")
                link.type = tarfile.SYMTYPE
                link.linkname = "src/include/postgres.h"
                tar.addfile(link)
            extracted = self.source.extract_source(archive, root / "source")
            self.assertTrue((extracted / "src/include/postgres.h").is_file())
            self.assertFalse((extracted / "doc/README").is_symlink())

    def test_extraction_rejects_escape_links_and_special_members(self):
        for name, kind, link in [
            ("../outside", tarfile.REGTYPE, ""),
            ("/tmp/outside", tarfile.REGTYPE, ""),
            ("postgresql/src/link", tarfile.SYMTYPE, "../../outside"),
            ("postgresql/src/link", tarfile.LNKTYPE, "outside"),
            ("postgresql/device", tarfile.CHRTYPE, ""),
        ]:
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                archive = root / "archive.tar.bz2"
                with tarfile.open(archive, "w:bz2") as tar:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    info.linkname = link
                    tar.addfile(info)
                with self.assertRaises(ValueError):
                    self.source.extract_source(archive, root / "source")
                self.assertFalse((root / "outside").exists())

    def test_plan_rejects_target_and_profile_arguments(self):
        for args in (["glibc"], ["musl"], ["musl-dynamic"], ["full"], ["minimal"], ["--profile", "full"], ["18.6", "17.11"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.source.plan(args)

    def test_plan_defaults_to_18_6_and_reports_no_profile(self):
        plan = self.source.plan([])
        self.assertEqual((plan["version"], plan["major"], plan["target"], plan["architecture"]),
                         ("18.6", "18", "musl-dynamic", "x86_64"))
        self.assertNotIn("profile", plan)
        self.assertEqual(self.source.plan(["17.11"])["major"], "17")

    def test_every_vendored_dependency_is_pinned_before_a_build_starts(self):
        pins = self.source.dependencies()
        self.assertEqual(set(pins), {"zig", "openssl", "zlib", "libxml2", "icu", "util-linux"})
        for name, pin in pins.items():
            with self.subTest(name=name):
                self.assertTrue(pin["url"].startswith("https://"), name)
                self.assertRegex(pin["sha256"], r"^[a-f0-9]{64}$", name)
                self.assertNotIn("<", pin["version"] + pin["sha256"], name)


class PostgreSQLLauncherTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((SCRIPTS / "launchers.py").is_file(), "PostgreSQL launchers module is missing")
        self.launchers = module("launchers")

    def test_launcher_sets_argv0_to_its_own_path(self):
        text = self.launchers.render("psql")
        self.assertIn('--library-path "$ROOT/lib:$ROOT/lib/musl"', text)
        self.assertIn('--argv0 "$ROOT/bin/psql"', text)
        self.assertIn('"$ROOT/libexec/bin/psql"', text)
        self.assertIn('"$ROOT/lib/musl/ld-musl-x86_64.so.1"', text)
        self.assertIn('exec ', text)
        self.assertTrue(text.endswith('"$@"\n'))
        self.assertNotIn("$@\"", text.replace('"$@"', ""))  # no other positional expansion

    def test_launcher_never_drops_a_library_path_entry(self):
        # Both entries must be present: dropping lib/musl passes on hosts whose
        # distro ships a musl loader and fails for everyone else.
        text = self.launchers.render("initdb")
        self.assertNotIn('--library-path "$ROOT/lib/musl"', text)
        self.assertEqual(text.count('lib/musl'), 2)

    def test_launcher_isolates_the_environment_and_resolves_spaces(self):
        text = self.launchers.render("pg_ctl")
        self.assertIn('ROOT=$(CDPATH=\'\' cd -P -- "$(dirname -- "$0")/.." && pwd)', text)
        self.assertIn('. "$ROOT/libexec/launch.sh"', text)
        self.assertIn("#!/bin/sh\nset -eu\n", text)
        self.assertIn("$(dirname -- \"$0\")/..", text, "ROOT must come from the launcher's own path")
        # The environment scrub lives in the sourced file, exactly as PHP's does.
        shared = (SCRIPTS.parent / "packaging/launch.sh").read_text()
        for variable in ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT"):
            self.assertIn(variable, shared)

    def test_install_moves_every_elf_and_leaves_no_raw_binary_in_bin(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bin_dir = root / "bin"
            bin_dir.mkdir(parents=True)
            elf_payload = b"\x7fELF" + b"\0" * 60
            (bin_dir / "postgres").write_bytes(elf_payload)
            (bin_dir / "README").write_text("documentation")
            subdir = bin_dir / "subdir"
            subdir.mkdir()
            (subdir / "subfile").write_text("subcontent")

            installed = self.launchers.install(root)
            self.assertEqual(installed, ["postgres"])

            # bin/postgres is now a 0o755 shell script whose text equals render("postgres")
            postgres_launcher = bin_dir / "postgres"
            self.assertEqual(postgres_launcher.read_text(), self.launchers.render("postgres"))
            self.assertEqual(postgres_launcher.stat().st_mode & 0o777, 0o755)

            # libexec/bin/postgres holds the original bytes
            libexec_postgres = root / "libexec/bin/postgres"
            self.assertTrue(libexec_postgres.is_file())
            self.assertEqual(libexec_postgres.read_bytes(), elf_payload)

            # bin/README and subdirectory are untouched
            self.assertEqual((bin_dir / "README").read_text(), "documentation")
            self.assertTrue(subdir.is_dir())
            self.assertEqual((subdir / "subfile").read_text(), "subcontent")

            # no raw ELFs remain in bin
            self.assertEqual(self.launchers.commands(root), [])

    def test_installation_is_executable_and_idempotent_per_command(self):
        self.assertEqual(self.launchers.LOADER, "lib/musl/ld-musl-x86_64.so.1")
        self.assertEqual(self.launchers.LIBRARY_PATH, "lib:lib/musl")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bin_dir = root / "bin"
            bin_dir.mkdir(parents=True)
            for cmd in ("postgres", "psql"):
                (bin_dir / cmd).write_bytes(b"\x7fELF" + cmd.encode() + b"\0" * 40)
            (bin_dir / "symlink_cmd").symlink_to(bin_dir / "postgres")

            commands = self.launchers.install(root)
            self.assertEqual(commands, ["postgres", "psql"])
            for cmd in ("postgres", "psql"):
                launcher = bin_dir / cmd
                self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
                self.assertEqual(launcher.read_text(), self.launchers.render(cmd))
                self.assertIn(f'--argv0 "$ROOT/bin/{cmd}"', launcher.read_text())
                self.assertIn(f'"$ROOT/libexec/bin/{cmd}"', launcher.read_text())
                self.assertEqual((root / "libexec/bin" / cmd).read_bytes(), b"\x7fELF" + cmd.encode() + b"\0" * 40)
            self.assertTrue((bin_dir / "symlink_cmd").is_symlink())

            # Refuse to run twice on the same tree
            with self.assertRaises(ValueError):
                self.launchers.install(root)


class PostgreSQLCLITests(unittest.TestCase):
    def test_dispatcher_plan_and_exact_version(self):
        for args in ([], ["18.6"]):
            with self.subTest(args=args):
                run = subprocess.run([str(ROOT / "build.sh"), "postgresql", *args, "--print-plan"], text=True, capture_output=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                plan = json.loads(run.stdout)
                self.assertEqual((plan["version"], plan["target"], plan["architecture"]), ("18.6", "musl-dynamic", "x86_64"))
                self.assertNotIn("profile", plan)
        run = subprocess.run([str(ROOT / "build.sh"), "postgresql", "17.11", "--print-plan"], text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["version"], "17.11")

    def test_invalid_arguments_fail_before_build(self):
        self.assertTrue((SCRIPTS / "build.sh").is_file(), "PostgreSQL CLI is missing")
        for args in [["other"], ["18.6.1"], ["glibc"], ["musl"], ["musl-dynamic"], ["full"], ["minimal"], ["--profile", "full"], ["18.6", "17.11"], ["--bad"], ["../../tmp"]]:
            with self.subTest(args=args):
                run = subprocess.run([str(ROOT / "build.sh"), "postgresql", *args, "--print-plan"], text=True, capture_output=True)
                self.assertNotEqual(run.returncode, 0)


class PostgreSQLModulesTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((SCRIPTS / "modules.py").is_file(), "PostgreSQL modules module is missing")
        self.modules = module("modules")

    def test_inventory_separates_kinds_by_what_is_on_disk(self):
        # fixture install tree: extension/hstore.control + lib/hstore.so  -> sql
        #                      lib/auto_explain.so only              -> preload
        #                      lib/utf8_and_euc_jp.so only            -> shared
        #                      bin/oid2name                           -> cli
        #                      bin/pg_dump                            -> not a module
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "share/extension").mkdir(parents=True)
            (root / "lib").mkdir(parents=True)
            (root / "bin").mkdir(parents=True)

            (root / "share/extension/hstore.control").write_text("default_version = '1.8'\n")
            (root / "lib/hstore.so").write_bytes(b"\x7fELF")
            (root / "lib/auto_explain.so").write_bytes(b"\x7fELF")
            (root / "lib/utf8_and_euc_jp.so").write_bytes(b"\x7fELF")
            (root / "bin/oid2name").write_bytes(b"\x7fELF")
            (root / "bin/pg_dump").write_bytes(b"\x7fELF")

            inv = self.modules.inventory(root)
            self.assertEqual(inv, [
                {"name": "oid2name", "kind": "cli"},
                {"name": "auto_explain", "kind": "preload"},
                {"name": "utf8_and_euc_jp", "kind": "shared"},
                {"name": "hstore", "kind": "sql"},
            ])

    def test_inventory_reads_the_staged_tree_the_same_way(self):
        # the same fixture, restaged: bin/<cmd> are shell launchers and the ELFs
        # live in libexec/bin/. inventory() must return the identical list, which a
        # rule keyed on "an ELF in bin/" would not.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "share/extension").mkdir(parents=True)
            (root / "lib").mkdir(parents=True)
            (root / "bin").mkdir(parents=True)
            (root / "libexec/bin").mkdir(parents=True)

            (root / "share/extension/hstore.control").write_text("default_version = '1.8'\n")
            (root / "lib/hstore.so").write_bytes(b"\x7fELF")
            (root / "lib/auto_explain.so").write_bytes(b"\x7fELF")
            (root / "lib/utf8_and_euc_jp.so").write_bytes(b"\x7fELF")
            (root / "bin/oid2name").write_text("#!/bin/sh\nexec ...\n")
            (root / "bin/pg_dump").write_text("#!/bin/sh\nexec ...\n")
            (root / "libexec/bin/oid2name").write_bytes(b"\x7fELF")
            (root / "libexec/bin/pg_dump").write_bytes(b"\x7fELF")

            inv = self.modules.inventory(root)
            self.assertEqual(inv, [
                {"name": "oid2name", "kind": "cli"},
                {"name": "auto_explain", "kind": "preload"},
                {"name": "utf8_and_euc_jp", "kind": "shared"},
                {"name": "hstore", "kind": "sql"},
            ])

    def test_preload_list_matches_the_servers_own_support_objects(self):
        # The preload set is closed and explicit; anything else with a .so and no
        # .control must fall into "shared", never "preload".
        self.assertEqual(self.modules.PRELOAD_ONLY,
                         frozenset({"auth_delay", "auto_explain", "basebackup_to_shell",
                                    "basic_archive", "passwordcheck", "pg_overexplain"}))
        self.assertEqual(self.modules.CLI_ONLY, frozenset({"oid2name", "vacuumlo"}))

    def test_eighteen_expects_the_modules_the_configure_flags_enable(self):
        expected = self.modules.load_expected("18")
        # Gated on configure flags, so their absence would be a silent capability loss.
        for name in ("pgcrypto", "sslinfo", "uuid-ossp", "xml2", "plpgsql", "hstore", "citext"):
            with self.subTest(name=name):
                self.assertIn(name, expected["sql"])
        # Preload-only modules have no .control and exist in 18 only.
        self.assertIn("pg_overexplain", expected["preload"])
        self.assertNotIn("pg_overexplain", expected["sql"])

    def test_compare_reports_both_missing_and_unexpected(self):
        # compare() must name each discrepancy; an empty list only when equal
        inventory = [
            {"name": "hstore", "kind": "sql"},
            {"name": "surprise", "kind": "shared"},
            {"name": "auto_explain", "kind": "shared"},
        ]
        expected = {
            "sql": ["hstore", "pgcrypto"],
            "preload": ["auto_explain"],
            "shared": [],
            "cli": [],
        }
        diff = self.modules.compare(inventory, expected)
        self.assertEqual(diff, [
            "missing sql: pgcrypto",
            "unexpected shared: surprise",
            "wrong kind: auto_explain is shared, expected preload",
        ])
        matching_inventory = [
            {"name": "auto_explain", "kind": "preload"},
            {"name": "hstore", "kind": "sql"},
        ]
        matching_expected = {
            "sql": ["hstore"],
            "preload": ["auto_explain"],
            "shared": [],
            "cli": [],
        }
        self.assertEqual(self.modules.compare(matching_inventory, matching_expected), [])

    def test_exclusions_carry_a_reason(self):
        for name in ("plperl", "plpython3u", "sepgsql"):
            self.assertIn(name, self.modules.EXCLUSIONS)
            self.assertTrue(self.modules.EXCLUSIONS[name].strip())

    def test_seventeen_expects_no_pg_overexplain(self):
        expected17 = self.modules.load_expected("17")
        self.assertNotIn("pg_overexplain", expected17["preload"])
        self.assertNotIn("pg_overexplain", expected17["sql"])

    def test_load_expected_rejects_unsupported_majors(self):
        for major in ("16", "19", "18.6", "", "latest"):
            with self.subTest(major=major), self.assertRaises(ValueError):
                self.modules.load_expected(major)

    def test_kinds_constant_order(self):
        self.assertEqual(self.modules.KINDS, ("sql", "preload", "cli", "shared"))

    def test_inventory_supports_extension_directory_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "extension").mkdir(parents=True)
            (root / "extension/hstore.control").write_text("default_version = '1.8'\n")
            inv = self.modules.inventory(root)
            self.assertEqual(inv, [{"name": "hstore", "kind": "sql"}])

    def test_compare_accepts_list_shaped_expected(self):
        inventory = [{"name": "hstore", "kind": "sql"}]
        expected = [{"name": "hstore", "kind": "sql"}]
        self.assertEqual(self.modules.compare(inventory, expected), [])

    def test_load_expected_rejects_unsorted_and_invalid_names(self):
        with tempfile.TemporaryDirectory() as temp:
            bad_config = Path(temp) / "modules.json"
            # Unsorted list
            bad_config.write_text(json.dumps({
                "18": {
                    "sql": ["xml2", "amcheck"],
                    "preload": [],
                    "shared": [],
                    "cli": [],
                }
            }))
            old_config = self.modules.CONFIG
            try:
                self.modules.CONFIG = bad_config
                with self.assertRaises(ValueError):
                    self.modules.load_expected("18")

                # Invalid character (uppercase)
                bad_config.write_text(json.dumps({
                    "18": {
                        "sql": ["BadName"],
                        "preload": [],
                        "shared": [],
                        "cli": [],
                    }
                }))
                with self.assertRaises(ValueError):
                    self.modules.load_expected("18")

                # Duplicate across kinds
                bad_config.write_text(json.dumps({
                    "18": {
                        "sql": ["hstore"],
                        "preload": ["hstore"],
                        "shared": [],
                        "cli": [],
                    }
                }))
                with self.assertRaises(ValueError):
                    self.modules.load_expected("18")
            finally:
                self.modules.CONFIG = old_config


class PostgreSQLToolchainTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((SCRIPTS / "toolchain.py").is_file(), "PostgreSQL toolchain module is missing")
        self.assertTrue((SCRIPTS.parent / "config/build-options.json").is_file(),
                        "PostgreSQL build options are missing")
        self.toolchain = module("toolchain")
        self.options = self.toolchain.load_options(SCRIPTS.parent / "config/build-options.json")

    def test_require_names_the_first_missing_tool(self):
        self.toolchain.require(["python3"])
        with self.assertRaisesRegex(ValueError, "definitely-not-installed"):
            self.toolchain.require(["python3", "definitely-not-installed", "another-missing-tool"])

    def test_configure_flags_always_disable_rpath_and_select_musl_safe_options(self):
        flags = self.toolchain.configure_flags(self.options)
        self.assertIn("--disable-rpath", flags)
        self.assertIn("--with-ssl=openssl", flags)
        self.assertNotIn("--enable-rpath", flags)
        # Exactly one ICU decision, and it is `--with-icu` unless a recorded
        # fallback changed the config file itself.
        self.assertEqual(len([f for f in flags if "icu" in f]), 1)
        self.assertEqual(len(flags), len(set(flags)))

    def test_build_environment_keeps_musl_headers_first(self):
        env = self.toolchain.build_env(Path("/zig"), Path("/deps"))
        self.assertTrue(env["CC"].startswith("/zig/zig cc -target x86_64-linux-musl"))
        self.assertTrue(env["CXX"].startswith("/zig/zig c++ -target x86_64-linux-musl"))
        self.assertIn("-idirafter /usr/include", env["CPPFLAGS"])
        self.assertNotIn("-I/usr/include", env["CPPFLAGS"])
        self.assertTrue(env["PKG_CONFIG_PATH"].startswith("/deps"))
        self.assertEqual(env["PKG_CONFIG_LIBDIR"], env["PKG_CONFIG_PATH"])

    def test_build_environment_links_against_the_pinned_prefix_without_rpath(self):
        env = self.toolchain.build_env(Path("/zig"), Path("/deps"))
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertEqual(env["LC_ALL"], "C")
        self.assertEqual(env["SOURCE_DATE_EPOCH"], "0")
        self.assertIn("-O2 -fPIC", env["CFLAGS"])
        self.assertIn("-L/deps/lib", env["LDFLAGS"])
        # -dynamic is a Zig CLI flag. LDFLAGS is recorded in Makefile.global,
        # reported by `pg_config --ldflags` and copied into libpq.pc, so it must
        # stay something a user's own linker understands.
        self.assertNotIn("-dynamic", env["LDFLAGS"])
        self.assertIn("-dynamic", env["CC"].split())
        self.assertIn("-dynamic", env["CXX"].split())
        self.assertNotIn("-static", env["LDFLAGS"])
        self.assertNotIn("rpath", env["LDFLAGS"])

    def test_build_environment_gives_icu_the_cxx_runtime_and_pic(self):
        # ICU is a static C++ archive linked by `zig cc`; without a named C++
        # runtime the backend link fails on libc++ symbols. It rides on CC, not
        # LDFLAGS, so `pg_config --ldflags` stays usable advice.
        env = self.toolchain.build_env(Path("/zig"), Path("/deps"))
        self.assertIn("-lc++", env["CC"].split())
        self.assertIn("-lc++abi", env["CC"].split())
        self.assertIn("-lc++", env["CXX"].split())
        self.assertNotIn("-lc++", env["LDFLAGS"].split())
        self.assertIn("-fPIC", env["CFLAGS"])

    def test_zig_pin_is_checked_before_any_configure(self):
        # A mismatched declared version must raise, not warn.
        with tempfile.TemporaryDirectory() as temp:
            zig_dir = Path(temp)
            binary = zig_dir / "zig"
            binary.write_text("#!/bin/sh\nprintf '0.14.0\\n'\n")
            binary.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "0.17.1"):
                self.toolchain.zig(zig_dir, self.options)
            binary.write_text("#!/bin/sh\nprintf '0.17.1\\n'\n")
            self.assertEqual(self.toolchain.zig(zig_dir, self.options), binary)

    def test_musl_version_reads_the_toolchains_bundled_libc(self):
        with tempfile.TemporaryDirectory() as temp:
            zig_dir = Path(temp)
            header = zig_dir / "lib/libc/musl/src/internal/version.h"
            header.parent.mkdir(parents=True)
            header.write_text('#define VERSION "1.2.5"\n')
            self.assertEqual(self.toolchain.musl_version(zig_dir), "1.2.5")
            header.write_text('#define VERSION "1.2.4"\n')
            self.assertNotEqual(self.toolchain.musl_version(zig_dir), self.options["zig"]["musl_version"])

    def test_uuid_and_libxml_select_the_vendored_libraries(self):
        flags = self.toolchain.configure_flags(self.options)
        self.assertIn("--with-uuid=e2fs", flags)
        self.assertIn("--with-libxml", flags)
        self.assertNotIn("--with-uuid=bsd", flags)

    def test_build_options_hold_only_build_decisions(self):
        raw = json.loads((SCRIPTS.parent / "config/build-options.json").read_text())
        self.assertEqual(set(raw), {"zig", "configure", "locale"})
        self.assertEqual(raw["zig"], {"version": "0.17.1", "musl_version": "1.2.5"})
        self.assertEqual(raw["configure"]["cppflags"], "-D_GNU_SOURCE -idirafter /usr/include")
        self.assertEqual(raw["configure"]["make_target"], "world-bin")
        self.assertEqual(raw["configure"]["install_target"], "install-world-bin")
        self.assertEqual(raw["locale"], {"provider": "builtin", "default": "C.UTF-8",
                                          "alternate_18": "PG_UNICODE_FAST"})
        # Every dependency pin lives in sources.json, never here.
        for value in raw.values():
            self.assertNotIn("sha256", json.dumps(value))
            self.assertNotIn("https://", json.dumps(value))

    def test_build_dependencies_orders_the_stack_and_refuses_a_populated_prefix(self):
        plan = self.toolchain.dependency_plan(self.options)
        self.assertEqual([step["name"] for step in plan],
                         ["zlib", "openssl", "libxml2", "icu", "util-linux"])
        linkage = {step["name"]: step["linkage"] for step in plan}
        self.assertEqual(linkage["openssl"], "shared")
        for name in ("zlib", "libxml2", "icu", "util-linux"):
            self.assertEqual(linkage[name], "static", name)
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            (work / "deps").mkdir(parents=True)
            (work / "deps/lib").mkdir()
            with self.assertRaises(ValueError):
                self.toolchain.dependency_plan(self.options, work)

    def test_provenance_records_the_keys_staging_reads(self):
        pins = module("source").dependencies()
        licenses = {name: {"path": f"/work/licenses/{name}.txt", "sha256": "c" * 64} for name in pins}
        record = self.toolchain.provenance(
            options=self.options,
            pins=pins,
            deps_prefix=Path("/work/deps"),
            loader={"path": "/work/loader/libc.so", "sha256": "a" * 64, "musl_version": "1.2.5",
                    "license": {"path": "/work/source/COPYRIGHT", "sha256": "b" * 64}},
            licenses={name: row for name, row in licenses.items() if name != "zig"},
            icu={"decision": "with_icu", "reason": "zig c++ compiled ICU 79.1"},
        )
        self.assertEqual(record["deps_prefix"], "/work/deps")
        self.assertEqual(record["loader"]["musl_version"], "1.2.5")
        self.assertEqual(record["loader"]["sha256"], "a" * 64)
        self.assertEqual(record["zig"]["version"], "0.17.1")
        self.assertEqual(record["zig"]["sha256"], pins["zig"]["sha256"])
        self.assertEqual(record["configure_flags"], self.toolchain.configure_flags(self.options))
        self.assertEqual(record["locale"], self.options["locale"])
        self.assertEqual(record["icu"], {"decision": "with_icu", "reason": "zig c++ compiled ICU 79.1"})
        names = [row["name"] for row in record["dependencies"]]
        self.assertEqual(names, ["zlib", "openssl", "libxml2", "icu", "util-linux"])
        for row in record["dependencies"]:
            with self.subTest(name=row["name"]):
                self.assertEqual(row["version"], pins[row["name"]]["version"])
                self.assertEqual(row["sha256"], pins[row["name"]]["sha256"])
                self.assertIn(row["linkage"], ("shared", "static"))
                self.assertEqual(row["license"], licenses[row["name"]])
                self.assertTrue(row["spdx"])

    def test_provenance_refuses_a_dependency_without_a_licence_file(self):
        pins = module("source").dependencies()
        with self.assertRaises(ValueError):
            self.toolchain.provenance(
                options=self.options, pins=pins, deps_prefix=Path("/work/deps"),
                loader={"path": "/w/libc.so", "sha256": "a" * 64, "musl_version": "1.2.5",
                        "license": {"path": "/w/COPYRIGHT", "sha256": "b" * 64}},
                licenses={})

    def test_dependency_archives_never_share_the_source_stage_directory(self):
        # source.py --download creates <work>/downloads itself and refuses to run
        # when it exists, so the dependency archives must live elsewhere.
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            self.assertNotEqual(self.toolchain.archives(work), work / "downloads")
            self.assertEqual(self.toolchain.archives(work), work / "archives")

    def test_extraction_rejects_escapes_special_members_and_multiple_roots(self):
        # prepare_zig runs this on a network archive before any build output
        # exists, so a regression here is silent without a test.
        def archive(members):
            path = Path(tempfile.mkdtemp()) / "probe.tar.gz"
            with tarfile.open(path, "w:gz") as tar:
                for name, data, kind in members:
                    info = tarfile.TarInfo(name)
                    if kind == "file":
                        info.size = len(data)
                        tar.addfile(info, io.BytesIO(data))
                    elif kind == "dir":
                        info.type = tarfile.DIRTYPE
                        tar.addfile(info)
                    else:
                        info.type = tarfile.LNKTYPE
                        info.linkname = "zlib-1.4/LICENSE"
                        tar.addfile(info)
            return path

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            good = archive([("zlib-1.4/", b"", "dir"), ("zlib-1.4/LICENSE", b"text", "file")])
            extracted = self.toolchain._confined_extract(good, root / "good")
            self.assertEqual(extracted, root / "good/zlib-1.4")
            self.assertEqual((extracted / "LICENSE").read_bytes(), b"text")

            for tag, members in {
                "escape": [("zlib-1.4/", b"", "dir"), ("zlib-1.4/../../etc/passwd", b"x", "file")],
                "absolute": [("/etc/passwd", b"x", "file")],
                "hardlink": [("zlib-1.4/", b"", "dir"), ("zlib-1.4/link", b"", "link")],
                "two roots": [("a/LICENSE", b"x", "file"), ("b/LICENSE", b"y", "file")],
            }.items():
                with self.subTest(tag), self.assertRaises(ValueError):
                    self.toolchain._confined_extract(archive(members), root / tag)

            with self.assertRaises(FileExistsError):
                self.toolchain._confined_extract(good, root / "good")

    def test_fetch_reverifies_a_cached_archive_against_its_pin(self):
        with tempfile.TemporaryDirectory() as temp:
            downloads = Path(temp)
            payload = b"archive bytes"
            existing = downloads / "zlib-1.4.tar.gz"
            existing.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            pin = {"url": "https://example.invalid/zlib-1.4.tar.gz", "sha256": digest}
            # A matching cached archive is reused without any network access.
            self.assertEqual(self.toolchain._fetch(pin, downloads), existing)
            # A cached archive that no longer matches its pin is refused, not used.
            existing.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "changed on disk"):
                self.toolchain._fetch(pin, downloads)
            existing.unlink()
            # A fresh download is written to .part first, so a failed or partial
            # transfer can never be mistaken for the verified archive.
            self.assertEqual(list(downloads.glob("*.part")), [])

    def test_licence_files_match_the_spdx_the_pin_declares(self):
        pins = module("source").dependencies()
        expected = {
            "zlib": "LICENSE",
            "openssl": "LICENSE.txt",
            "libxml2": "Copyright",
            "icu": "LICENSE",
            # libuuid is LGPL-2.1-or-later; COPYING is util-linux's own GPL-2.0.
            "util-linux": "Documentation/licenses/COPYING.LGPL-2.1-or-later",
        }
        for name, filename in expected.items():
            with self.subTest(name=name):
                self.assertEqual(self.toolchain.LICENSES[name][0], filename)
        self.assertIn("LGPL", pins["util-linux"]["license"])
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            (source / "Documentation/licenses").mkdir(parents=True)
            (source / "Documentation/licenses/COPYING.LGPL-2.1-or-later").write_text("lgpl\n")
            (source / "COPYING").write_text("gpl\n")
            record = self.toolchain._license(source, "util-linux")
            self.assertTrue(record["path"].endswith("COPYING.LGPL-2.1-or-later"))

    def test_icu_fallback_only_triggers_on_a_compiler_complaint(self):
        # A broken tree or a disk error must not be reported as "switch to
        # --without-icu": the plan forbids reaching for the fallback pre-emptively.
        self.assertTrue(self.toolchain.compiler_failure(
            "ld.lld: error: undefined symbol: _Unwind_Resume"))
        self.assertTrue(self.toolchain.compiler_failure(
            "error: unknown type name 'char16_t'"))
        self.assertFalse(self.toolchain.compiler_failure(
            "make: *** No rule to make target 'install'.  Stop."))
        self.assertFalse(self.toolchain.compiler_failure(
            "cp: cannot create regular file: No space left on device"))

    def test_icu_reports_a_broken_tree_without_blaming_the_compiler(self):
        version = module("source").dependencies()["icu"]["version"]
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "icu"
            (source / "source").mkdir(parents=True)
            # A configure that fails for a non-compiler reason: no zig, no ICU.
            configure = source / "source/configure"
            configure.write_text("#!/bin/sh\necho 'configure: error: cannot find sources' >&2\nexit 1\n")
            configure.chmod(0o755)
            env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "SOURCE_DATE_EPOCH": "0"}
            (Path(temp) / "build").mkdir()
            with self.assertRaises(self.toolchain.BuildFailure) as caught:
                self.toolchain._icu(source, Path(temp) / "build", Path(temp) / "deps", env)
            self.assertNotIn("--without-icu", str(caught.exception))

            # The same failure with a compiler diagnostic names the pinned version
            # and the recorded fallback.
            configure.write_text("#!/bin/sh\necho \"ld.lld: error: undefined symbol: _Unwind_Resume\" >&2\nexit 1\n")
            (Path(temp) / "build2").mkdir()
            with self.assertRaisesRegex(ValueError, f"ICU {version} did not compile") as caught:
                self.toolchain._icu(source, Path(temp) / "build2", Path(temp) / "deps", env)
            self.assertIn("--without-icu", str(caught.exception))

    def test_icu_link_probe_uses_the_same_line_postgres_will_use(self):
        # A stub compiler records its argv and reports success, so this pins the
        # probe's shape: PostgreSQL links the backend with CC, so the C++ runtime
        # must be on CC, and the ICU flags must be the ones Makefile.global gets.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prefix = root / "deps"
            (prefix / "lib").mkdir(parents=True)
            (prefix / "include").mkdir(parents=True)
            recorded = root / "argv.txt"
            compiler = root / "zig"
            compiler.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$@\" > {recorded}\n"
                "while [ $# -gt 0 ]; do\n"
                "  case \"$1\" in -o)\n"
                "    printf '#!/bin/sh\\necho U_ZERO_ERROR\\n' > \"$2\"\n"
                "    chmod +x \"$2\"\n"
                "    shift;;\n"
                "  esac\n"
                "  shift\n"
                "done\n"
                "exit 0\n")
            compiler.chmod(0o755)

            env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "SOURCE_DATE_EPOCH": "0",
                   "CC": f"{compiler} cc -target x86_64-linux-musl -dynamic -lc++ -lc++abi",
                   "LDFLAGS": f"-L{prefix}/lib"}
            self.toolchain.check_icu_links(root / "probe", env, prefix)
            argv = recorded.read_text().split()
            self.assertIn("-lc++", argv)
            self.assertIn("-lc++abi", argv)
            self.assertIn("-dynamic", argv)
            self.assertIn(f"-L{prefix}/lib", argv)
            for flag in ("-licui18n", "-licuuc", "-licudata", "-lpthread", "-lm"):
                self.assertIn(flag, argv, "the probe must use PostgreSQL's own ICU_LIBS")

    def test_write_provenance_refuses_to_replace_a_different_record(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "toolchain.json"
            self.toolchain.write_provenance(path, {"deps_prefix": "/work/deps"})
            self.assertEqual(json.loads(path.read_text()), {"deps_prefix": "/work/deps"})
            with self.assertRaises(ValueError):
                self.toolchain.write_provenance(path, {"deps_prefix": "/other"})
            self.assertEqual(json.loads(path.read_text()), {"deps_prefix": "/work/deps"})


class PostgreSQLBundleTests(unittest.TestCase):
    """Staging a launcher-wrapped bundle and the manifest that describes it."""

    def setUp(self):
        self.assertTrue((SCRIPTS / "bundle.py").is_file(), "PostgreSQL bundle module is missing")
        self.bundle = module("bundle")
        self.modules = module("modules")
        self.launchers = module("launchers")

    # -- ELF contract -------------------------------------------------------

    def test_elf_contract_requires_dynamic_musl_without_rpath(self):
        row = self.bundle.parse_elf(
            "Class: ELF64\nMachine: Advanced Micro Devices X86-64\n",
            "[Requesting program interpreter: /lib/ld-musl-x86_64.so.1]",
            " (NEEDED)  Shared library: [libpq.so.5]\n (NEEDED)  Shared library: [ld-musl-x86_64.so.1]",
            "")
        self.assertEqual(row["needed"], ["libpq.so.5", "ld-musl-x86_64.so.1"])
        self.assertIsNone(row["rpath"])
        self.assertIsNone(row["runpath"])
        self.assertIsNone(row["glibc_required"])
        self.assertEqual(row["interpreter"], "/lib/ld-musl-x86_64.so.1")
        self.assertEqual(row["machine"], "Advanced Micro Devices X86-64")
        self.assertNotIn("static", row)
        self.assertNotIn("profile", row)

    def test_elf_contract_rejects_rpath_glibc_and_static(self):
        elf64 = "Class: ELF64\nMachine: Advanced Micro Devices X86-64\n"
        progs = "[Requesting program interpreter: /lib/ld-musl-x86_64.so.1]"
        needed = " (NEEDED)  Shared library: [libc.so]"
        for header, program, dynamic, versions in [
            (elf64, progs, " (RPATH)  Library rpath: [/usr/lib]", ""),
            (elf64, progs, " (RUNPATH)  Library runpath: [/opt/lib]", ""),
            (elf64, progs, needed, "GLIBC_2.34"),
            (elf64, "", needed, ""),                       # static: no interpreter
            (elf64, progs, "", ""),                        # nothing NEEDED
            ("Class: ELF64\nMachine: AArch64\n", progs, needed, ""),
            (elf64, "[Requesting program interpreter: /lib64/ld-linux-x86-64.so.2]", needed, ""),
        ]:
            with self.subTest(dynamic=dynamic, versions=versions), self.assertRaises(ValueError):
                self.bundle.parse_elf(header, program, dynamic, versions)

    def test_elf_runs_readelf_and_reports_the_real_contract(self):
        if not shutil.which("cc") or not shutil.which("readelf"):
            self.skipTest("ELF fixture compiler is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library = build_library(root / "libpq.so.5", "libpq.so.5")
            binary = build_elf(root, root / "postgres", search=library.parent)
            row = self.bundle.elf(binary)
            self.assertEqual(row["interpreter"], "/lib/ld-musl-x86_64.so.1")
            self.assertEqual(row["needed"], ["libpq.so.5"])
            self.assertIsNone(row["rpath"])
            self.assertIsNone(row["runpath"])
            self.assertIsNone(row["glibc_required"])
            self.assertIn("X86-64", row["machine"])

            # A host-toolchain binary is not a musl-dynamic one.
            (root / "glibc.c").write_text("int main(void) { return 0; }\n")
            subprocess.run(["cc", "-o", str(root / "glibc"), str(root / "glibc.c")],
                           check=True, capture_output=True)
            with self.assertRaises(ValueError):
                self.bundle.elf(root / "glibc")

    def test_elf_reports_an_unreadable_file_instead_of_a_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                self.bundle.elf(Path(temp) / "missing")

    # -- version through the loader ----------------------------------------

    def test_binary_version_runs_through_the_loader(self):
        if not shutil.which("cc") or not HOST_LOADER.is_file():
            self.skipTest("ELF fixture compiler or host loader is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_library(root / "deps/libpq.so.5", "libpq.so.5")
            stub = build_elf(root, root / "postgres", search=root / "deps",
                             message="postgres (PostgreSQL) 18.6")
            # The host's glibc loader stands in for the musl loader: both accept
            # `--library-path`, so this exercises the indirection without musl.
            loader = HOST_LOADER
            self.assertEqual(
                self.bundle.binary_version(stub, loader, f"{root / 'deps'}:{root}"), "18.6")
            with self.assertRaises(ValueError):
                self.bundle.require_version(stub, "17.11", loader, f"{root / 'deps'}:{root}")
            self.assertEqual(
                self.bundle.require_version(stub, "18.6", loader, f"{root / 'deps'}:{root}"),
                "18.6")

    def test_binary_version_never_executes_the_binary_directly(self):
        # The raw ELF has a musl PT_INTERP and no host loader exists, so a
        # direct exec would raise ENOENT; only the loader indirection works.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / "postgres"
            binary.write_text("#!/bin/sh\nprintf 'postgres (PostgreSQL) 18.6\\n'\n")
            binary.chmod(0o755)
            with self.assertRaises(ValueError):
                self.bundle.binary_version(binary, Path(temp) / "no-such-loader", "/deps/lib")

    def test_binary_version_rejects_output_without_a_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = root / "loader"
            fake.write_text("#!/bin/sh\nprintf 'nothing useful\\n'\n")
            fake.chmod(0o755)
            with self.assertRaises(ValueError):
                self.bundle.binary_version(root / "postgres", fake, "/deps/lib")

    # -- stage -------------------------------------------------------------

    def fixture(self, root):
        """A synthetic install tree shaped like `install-world-bin` produced it.

        The binaries NEEDED `libcrypto.so.3`, the one shared dependency, because
        that is what the real build links and what the loader can resolve from
        the dependency prefix at staging time.
        """
        install = root / "inst"
        for directory in ("bin", "lib", "share/extension", "share/timezone",
                          "share/tsearch_data", "share/doc/html", "include/server"):
            (install / directory).mkdir(parents=True, exist_ok=True)
        build_library(install / "lib/libpq.so.5", "libpq.so.5")
        (install / "lib/libpq.so").symlink_to("libpq.so.5")
        build_library(install / "lib/utf8_and_euc_jp.so", "utf8_and_euc_jp.so")
        for command in ("postgres", "initdb", "pg_ctl", "psql", "pg_config",
                        "pg_dump", "oid2name", "vacuumlo"):
            build_elf(root, install / "bin" / command, needed=("libcrypto.so.3",),
                      search=root / "shared")
            (install / "bin" / command).chmod(0o755)
        expected = self.modules.load_expected("18")
        for name in expected["sql"]:
            (install / "share/extension" / (name + ".control")).write_text(
                f"comment = '{name}'\ndefault_version = '1.0'\n")
        for name in expected["preload"]:
            build_library(install / "lib" / (name + ".so"), name + ".so")
        (install / "share/postgres.bki").write_text("# bki\n")
        for sample in ("postgresql.conf.sample", "pg_hba.conf.sample",
                       "pg_ident.conf.sample", "pg_service.conf.sample", "psqlrc.sample"):
            (install / "share" / sample).write_text(f"# {sample}\n")
        (install / "share/timezone/UTC").write_bytes(b"TZif")
        (install / "share/doc/html/index.html").write_text("<html></html>\n")
        (install / "include/server/postgres.h").write_text("/* server */\n")
        return install

    def provenance(self, root, install):
        """A synthetic `$work/toolchain.json` plus the files it names."""
        work = root / "work"
        deps = work / "deps"
        (deps / "lib").mkdir(parents=True, exist_ok=True)
        (deps / "include").mkdir(parents=True, exist_ok=True)
        # The root the fixture binaries link against; also the prefix the
        # dependency records name, so staging finds them where the build put them.
        (root / "shared").mkdir()
        build_library(root / "shared/libcrypto.so.3", "libcrypto.so.3")
        shutil.copy2(root / "shared/libcrypto.so.3", deps / "lib/libcrypto.so.3")
        build_library(deps / "lib/libssl.so.3", "libssl.so.3")
        (deps / "lib/libssl.so").symlink_to("libssl.so.3")
        (deps / "lib/libcrypto.so").symlink_to("libcrypto.so.3")
        (deps / "lib/libz.a").write_bytes(b"!<arch>\n")

        loader_dir = work / "loader"
        loader_dir.mkdir(parents=True)
        # A real loader: it honours `--library-path` and can run the fixture
        # binaries, whose PT_INTERP is the musl path this host does not have.
        loader = loader_dir / "libc.so"
        shutil.copy2(HOST_LOADER, loader)
        loader.chmod(0o755)
        loader_license = loader_dir / "COPYRIGHT"
        loader_license.write_text("musl fixture copyright\n")

        dependencies = []
        for row in [{"name": "zlib", "version": "1.4", "linkage": "static", "spdx": "Zlib"},
                    {"name": "openssl", "version": "3.6.5", "linkage": "shared", "spdx": "Apache-2.0"},
                    {"name": "libxml2", "version": "2.15.4", "linkage": "static", "spdx": "MIT"},
                    {"name": "icu", "version": "79.1", "linkage": "static", "spdx": "Unicode-3.0"},
                    {"name": "util-linux", "version": "2.42.4", "linkage": "static",
                     "spdx": "LGPL-2.1-or-later"}]:
            licence = work / "licenses" / (row["name"] + ".txt")
            licence.parent.mkdir(parents=True, exist_ok=True)
            licence.write_text(f"licence for {row['name']}\n")
            dependencies.append({**row, "sha256": "a" * 64,
                                 "license": {"path": str(licence), "sha256": sha256_file(licence)}})

        archive = work / "downloads" / "postgresql-18.6.tar.bz2"
        archive.parent.mkdir(parents=True)
        with tarfile.open(archive, "w:bz2") as tar:
            info = tarfile.TarInfo("postgresql-18.6/COPYRIGHT")
            payload = b"PostgreSQL fixture copyright\n"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

        record = {
            "zig": {"version": "0.17.1", "musl_version": "1.2.5",
                    "sha256": "b" * 64, "license": "MIT"},
            "deps_prefix": str(deps),
            "loader": {"path": str(loader), "sha256": sha256_file(loader),
                       "musl_version": "1.2.5",
                       "license": {"path": str(loader_license),
                                   "sha256": sha256_file(loader_license)}},
            "dependencies": dependencies,
            "configure_flags": ["--disable-rpath", "--with-ssl=openssl", "--with-icu"],
            "locale": {"provider": "builtin", "default": "C.UTF-8",
                       "alternate_18": "PG_UNICODE_FAST"},
            "icu": {"decision": "with_icu", "reason": "zig c++ compiled ICU 79.1"},
            "compiler": "zig cc -target x86_64-linux-musl",
        }
        (work / "request.json").write_text(json.dumps({
            "version": "18.6", "major": "18", "target": "musl-dynamic",
            "architecture": "x86_64",
            "source": {"version": "18.6", "series": "18",
                       "sha256": sha256_file(archive), "license": "PostgreSQL",
                       "url": "https://ftp.postgresql.org/pub/source/v18.6/postgresql-18.6.tar.bz2",
                       "checksum_source": "https://ftp.postgresql.org"},
        }, indent=2))
        return record

    def request(self, root):
        return json.loads((root / "work/request.json").read_text())

    def stage(self, root, install, record, request):
        return self.bundle.stage(install, root / "bundle", request, record, ROOT)

    def test_stage_builds_the_documented_layout(self):
        if not shutil.which("cc") or not shutil.which("readelf"):
            self.skipTest("ELF fixture compiler is unavailable")
        with tempfile.TemporaryDirectory(prefix="postgresql stage ") as temp:
            root = Path(temp)
            install = self.fixture(root)
            record = self.provenance(root, install)
            request = self.request(root)
            output = root / "bundle"
            manifest = self.bundle.stage(install, output, request, record, ROOT)
            for relative in ("bin/postgres", "bin/initdb", "bin/pg_ctl", "bin/psql",
                             "bin/pg_config", "libexec/bin/postgres",
                             "libexec/bin/psql", "libexec/launch.sh",
                             "lib/musl/ld-musl-x86_64.so.1", "lib/libssl.so.3",
                             "lib/libcrypto.so.3", "lib/libpq.so.5",
                             "lib/utf8_and_euc_jp.so",
                             "share/postgres.bki", "share/postgresql.conf.sample",
                             "share/licenses/builder.txt", "share/licenses/postgresql.txt",
                             "share/licenses/musl.txt", "share/licenses/zlib.txt",
                             "share/licenses/openssl.txt", "share/licenses/libxml2.txt",
                             "share/licenses/icu.txt", "share/licenses/util-linux.txt",
                             "share/sources/postgresql-18.6.tar.bz2", "manifest.json"):
                self.assertTrue((output / relative).exists(), relative)
            # share/doc is the one subtree dropped; include/ and the launcher
            # scripts are kept.
            self.assertFalse((output / "share/doc").exists())
            self.assertTrue((output / "include/server/postgres.h").is_file())
            self.assertFalse((output / "etc").exists())
            self.assertEqual((output / "lib/musl/ld-musl-x86_64.so.1").stat().st_mode & 0o777, 0o755)
            self.assertEqual((output / "libexec/bin/postgres").stat().st_mode & 0o777, 0o755)
            self.assertEqual(self.launchers.commands(output), [])
            # The staged loader is the built one, not a host loader.
            self.assertEqual(sha256_file(output / "lib/musl/ld-musl-x86_64.so.1"),
                             record["loader"]["sha256"])
            # so.N symlinks survive so NEEDED names resolve.
            self.assertTrue((output / "lib/libssl.so").is_symlink())

            self.assertEqual(manifest["runtime"], "postgresql")
            self.assertEqual(manifest["postgresql"], {"version": "18.6", "major": "18"})
            self.assertEqual(manifest["target"], "musl-dynamic")
            self.assertNotIn("profile", manifest)
            self.assertEqual(manifest["verification"], "NOT_RUN")
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["architecture"], "x86_64")
            self.assertEqual(manifest["linkage"], "dynamic")
            self.assertRegex(manifest["build_id"], r"^[a-f0-9]{16}$")
            self.assertEqual(manifest["loader"], {
                "path": "lib/musl/ld-musl-x86_64.so.1",
                "sha256": record["loader"]["sha256"],
                "musl_version": "1.2.5"})
            self.assertEqual(manifest["source_archive"],
                             "share/sources/postgresql-18.6.tar.bz2")
            self.assertEqual(manifest["licenses"], {
                "builder": "share/licenses/builder.txt",
                "postgresql": "share/licenses/postgresql.txt",
                "musl": "share/licenses/musl.txt"})
            self.assertEqual(json.loads((output / "manifest.json").read_text()), manifest)

    def test_manifest_records_modules_and_locale_contract(self):
        if not shutil.which("cc") or not shutil.which("readelf"):
            self.skipTest("ELF fixture compiler is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.fixture(root)
            record = self.provenance(root, install)
            request = self.request(root)
            output = root / "bundle"
            manifest = self.bundle.stage(install, output, request, record, ROOT)
            kinds = {row["kind"] for row in manifest["modules"]["included"]}
            self.assertEqual(kinds, {"sql", "preload", "shared", "cli"})
            self.assertEqual(manifest["locale"],
                             {"default_provider": "builtin", "default_locale": "C.UTF-8",
                              "alternate": "PG_UNICODE_FAST"})
            # The included set covers the expected `sql`, `preload` and `cli`
            # modules exactly. `shared` is whatever `lib/*.so` turned out to be
            # (libpq and the vendored OpenSSL libraries among them) and is
            # reconciled after the real build, per the design.
            expected = self.modules.load_expected(manifest["postgresql"]["major"])
            for kind in ("sql", "preload", "cli"):
                self.assertEqual(
                    [r["name"] for r in manifest["modules"]["included"] if r["kind"] == kind],
                    expected[kind], kind)
            self.assertTrue(manifest["modules"]["included"])
            self.assertEqual(manifest["modules"]["skipped"],
                             [{"name": name, "reason": reason}
                              for name, reason in sorted(self.modules.EXCLUSIONS.items())])
            self.assertEqual(manifest["modules"]["included"],
                             self.modules.inventory(output))

    def test_stage_resolves_a_binary_against_the_install_trees_own_libraries(self):
        # psql, clusterdb and every other frontend NEED libpq.so.5, which lives
        # in the install tree's lib/, not in the vendored dependency prefix.
        # Omitting it makes the loader report
        # "Error loading shared library libpq.so.5: No such file or directory"
        # and staging aborts on a green build.
        if not shutil.which("cc") or not shutil.which("readelf"):
            self.skipTest("ELF fixture compiler is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.fixture(root)
            record = self.provenance(root, install)
            request = self.request(root)
            for command in ("psql", "clusterdb"):
                build_elf(root, install / "bin" / command, needed=("libpq.so.5",),
                          search=install / "lib")
                (install / "bin" / command).chmod(0o755)
            manifest = self.bundle.stage(install, root / "bundle", request, record, ROOT)
            self.assertIn({"path": "libexec/bin/clusterdb"},
                          [{key: row[key] for key in ("path",)}
                           for row in manifest["binaries"]])

    def test_stage_refuses_a_binary_the_install_tree_cannot_resolve(self):
        # The counterpart of the test above: a NEEDED library present in no
        # searched directory must abort staging rather than be skipped, so the
        # library path stays the real cause of a failure.
        if not shutil.which("cc") or not shutil.which("readelf"):
            self.skipTest("ELF fixture compiler is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.fixture(root)
            record = self.provenance(root, install)
            request = self.request(root)
            build_library(root / "shared/libnowhere.so.1", "libnowhere.so.1")
            build_elf(root, install / "bin/orphan", needed=("libnowhere.so.1",),
                      search=root / "shared")
            (install / "bin/orphan").chmod(0o755)
            with self.assertRaisesRegex(ValueError, "libnowhere.so.1"):
                self.bundle.stage(install, root / "bundle", request, record, ROOT)

    def test_manifest_records_libraries_binaries_and_provenance(self):
        if not shutil.which("cc") or not shutil.which("readelf"):
            self.skipTest("ELF fixture compiler is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.fixture(root)
            record = self.provenance(root, install)
            request = self.request(root)
            output = root / "bundle"
            manifest = self.bundle.stage(install, output, request, record, ROOT)

            openssl = [row for row in manifest["libraries"] if row["name"] == "openssl"][0]
            self.assertEqual(openssl, {"name": "openssl", "version": "3.6.5",
                                       "sha256": "a" * 64, "linkage": "shared",
                                       "license": "share/licenses/openssl.txt"})
            self.assertEqual([row["name"] for row in manifest["libraries"]],
                             ["zlib", "openssl", "libxml2", "icu", "util-linux"])

            binaries = {row["path"]: row for row in manifest["binaries"]}
            self.assertEqual(sorted(binaries), sorted(
                "libexec/bin/" + name for name in
                ("postgres", "initdb", "pg_ctl", "psql", "pg_config", "pg_dump",
                 "oid2name", "vacuumlo")))
            for relative, row in binaries.items():
                self.assertEqual(row["sha256"], sha256_file(output / relative), relative)
                self.assertEqual(row["interpreter"], "/lib/ld-musl-x86_64.so.1", relative)
                self.assertEqual(row["needed"], ["libcrypto.so.3"], relative)
                self.assertIsNone(row["rpath"])
                self.assertIsNone(row["runpath"])
                self.assertIsNone(row["glibc_required"])
                self.assertIn("X86-64", row["machine"])

            self.assertEqual(manifest["provenance"], {
                "upstream": {"url": request["source"]["url"],
                             "sha256": request["source"]["sha256"]},
                "configure_flags": record["configure_flags"],
                "compiler": record["compiler"],
                "zig": record["zig"]})
            self.assertEqual(manifest["icu"], record["icu"])
            self.assertEqual(manifest["builder"], {
                "commit": subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                                         text=True, capture_output=True, check=True).stdout.strip(),
                "source_sha256": manifest["builder"]["source_sha256"]})
            self.assertRegex(manifest["builder"]["source_sha256"], r"^[a-f0-9]{64}$")

    def test_build_id_is_stable_for_the_same_inputs(self):
        if not shutil.which("cc") or not shutil.which("readelf"):
            self.skipTest("ELF fixture compiler is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.fixture(root)
            record = self.provenance(root, install)
            request = self.request(root)
            first = self.bundle.stage(install, root / "one", request, record, ROOT)
            second = self.bundle.stage(install, root / "two", request, record, ROOT)
            self.assertEqual(first["build_id"], second["build_id"])
            other = dict(record, icu={"decision": "without_icu", "reason": "fallback"})
            third = self.bundle.stage(install, root / "three", request, other, ROOT)
            self.assertNotEqual(first["build_id"], third["build_id"])

    def test_stage_refuses_an_existing_output(self):
        if not shutil.which("cc") or not shutil.which("readelf"):
            self.skipTest("ELF fixture compiler is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.fixture(root)
            record = self.provenance(root, install)
            request = self.request(root)
            (root / "bundle").mkdir()
            with self.assertRaises(ValueError):
                self.bundle.stage(install, root / "bundle", request, record, ROOT)

    def test_stage_refuses_a_wrong_patch_before_creating_output(self):
        if not shutil.which("cc") or not shutil.which("readelf"):
            self.skipTest("ELF fixture compiler is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.fixture(root)
            record = self.provenance(root, install)
            request = dict(self.request(root))
            request["version"] = "17.11"
            request["source"] = dict(request["source"], version="17.11", series="17")
            with self.assertRaises(ValueError):
                self.bundle.stage(install, root / "bundle", request, record, ROOT)
            self.assertFalse((root / "bundle").exists())

    def test_stage_refuses_a_missing_expected_module(self):
        if not shutil.which("cc") or not shutil.which("readelf"):
            self.skipTest("ELF fixture compiler is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.fixture(root)
            record = self.provenance(root, install)
            request = self.request(root)
            (install / "share/extension/hstore.control").unlink()
            with self.assertRaisesRegex(ValueError, "hstore"):
                self.bundle.stage(install, root / "bundle", request, record, ROOT)

    def test_stage_refuses_a_changed_loader_or_archive(self):
        if not shutil.which("cc") or not shutil.which("readelf"):
            self.skipTest("ELF fixture compiler is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.fixture(root)
            record = self.provenance(root, install)
            request = self.request(root)

            loader = Path(record["loader"]["path"])
            original = loader.read_bytes()
            loader.write_bytes(original + b"tampered")
            with self.assertRaisesRegex(ValueError, "loader"):
                self.bundle.stage(install, root / "one", request, record, ROOT)
            self.assertFalse((root / "one").exists())
            loader.write_bytes(original)

            archive = root / "work/downloads/postgresql-18.6.tar.bz2"
            changed = archive.read_bytes() + b"tampered"
            archive.write_bytes(changed)
            with self.assertRaisesRegex(ValueError, "archive"):
                self.bundle.stage(install, root / "two", request, record, ROOT)
            self.assertFalse((root / "two").exists())

    def test_stage_refuses_a_provenance_record_missing_a_key_it_reads(self):
        # A truncated toolchain.json must name the missing key, not surface a
        # KeyError from the middle of staging after the output exists.
        if not shutil.which("cc") or not shutil.which("readelf"):
            self.skipTest("ELF fixture compiler is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.fixture(root)
            record = self.provenance(root, install)
            request = self.request(root)
            for key in ("locale", "icu", "configure_flags", "compiler", "zig"):
                with self.subTest(key=key):
                    incomplete = {k: v for k, v in record.items() if k != key}
                    with self.assertRaisesRegex(ValueError, key):
                        self.bundle.stage(install, root / ("bundle " + key), request,
                                          incomplete, ROOT)
                    self.assertFalse((root / ("bundle " + key)).exists())

    def test_stage_refuses_a_needed_library_it_did_not_bundle(self):
        if not shutil.which("cc") or not shutil.which("readelf"):
            self.skipTest("ELF fixture compiler is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.fixture(root)
            record = self.provenance(root, install)
            request = self.request(root)
            build_library(root / "shared/libunbundled.so.9", "libunbundled.so.9")
            for command in ("postgres", "psql"):
                build_elf(root, install / "bin" / command,
                          needed=("libcrypto.so.3", "libunbundled.so.9"), search=root / "shared")
            with self.assertRaisesRegex(ValueError, "libunbundled.so.9"):
                self.bundle.stage(install, root / "bundle", request, record, ROOT)
            self.assertFalse((root / "bundle").exists())

    def test_stage_writes_a_manifest_that_verify_and_publish_can_read(self):
        if not shutil.which("cc") or not shutil.which("readelf"):
            self.skipTest("ELF fixture compiler is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.fixture(root)
            record = self.provenance(root, install)
            request = self.request(root)
            output = root / "bundle"
            manifest = self.bundle.stage(install, output, request, record, ROOT)
            self.assertEqual(set(manifest), {
                "schema_version", "runtime", "postgresql", "target", "architecture",
                "linkage", "build_id", "verification", "loader", "locale", "libraries",
                "modules", "binaries", "source_archive", "licenses", "provenance",
                "icu", "builder"})
            self.assertEqual(list(manifest["postgresql"]), ["version", "major"])

    def test_stage_cli_runs_end_to_end(self):
        if not shutil.which("cc") or not shutil.which("readelf"):
            self.skipTest("ELF fixture compiler is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = self.fixture(root)
            record = self.provenance(root, install)
            self.request(root)
            (root / "work/provenance.json").write_text(json.dumps(record, indent=2))
            # Run the CLI the way build.sh does, with an isolated interpreter.
            command = ["python3", "-I", str(SCRIPTS / "bundle.py"), "stage", str(install),
                       str(root / "bundle"), str(root / "work/request.json"),
                       str(root / "work/provenance.json"), str(ROOT)]
            environment = {**os.environ, "PYTHONPATH": ""}
            run = subprocess.run(command, text=True, capture_output=True, env=environment)
            self.assertEqual(run.returncode, 0, run.stderr)
            manifest = json.loads((root / "bundle/manifest.json").read_text())
            self.assertEqual(manifest["runtime"], "postgresql")
            self.assertEqual(manifest["postgresql"], {"version": "18.6", "major": "18"})
            # A second stage into the same output is refused, not merged.
            again = subprocess.run(command, text=True, capture_output=True, env=environment)
            self.assertNotEqual(again.returncode, 0)
            self.assertIn("Refusing", again.stderr)

    def test_build_script_stages_after_install_and_checks_provenance(self):
        text = (SCRIPTS / "build.sh").read_text()
        install = text.index("install-world-bin")
        stage = text.index('bundle.py" stage')
        self.assertLess(install, stage)
        self.assertIn('"$work/toolchain.json"', text[install:stage])
        self.assertIn("Staged bundle:", text[stage:])
        self.assertIn('"$work/inst/usr/local/pgsql"', text[stage:])
        self.assertIn('"$work/request.json"', text[stage:])
        self.assertIn('"$REPO_ROOT"', text[stage:])


class PostgreSQLVerifyTests(unittest.TestCase):
    """Verifying a staged bundle: plan, lifecycle, masking and relocation.

    The real bundle cannot be built inside a unit test (it needs a full
    toolchain build), so the tests here cover every decision verify() makes
    from the manifest: what it plans to run, what it refuses, and how it
    proves no host loader took part. The end-to-end run happens against the
    staged bundle at dist/postgresql/18.6/musl-dynamic.
    """

    def setUp(self):
        self.assertTrue((SCRIPTS / "bundle.py").is_file(), "PostgreSQL bundle module is missing")
        self.bundle = module("bundle")
        self.modules = module("modules")

    def manifest(self):
        """A manifest shaped exactly like the one stage() writes."""
        included = []
        for kind in ("sql", "preload", "cli", "shared"):
            for name in self.modules.load_expected("18")[kind]:
                included.append({"name": name, "kind": kind})
        return {
            "schema_version": 1,
            "runtime": "postgresql",
            "postgresql": {"version": "18.6", "major": "18"},
            "target": "musl-dynamic",
            "architecture": "x86_64",
            "linkage": "dynamic",
            "build_id": "0" * 16,
            "verification": "NOT_RUN",
            "loader": {"path": "lib/musl/ld-musl-x86_64.so.1", "sha256": "c" * 64,
                       "musl_version": "1.2.5"},
            "locale": {"default_provider": "builtin", "default_locale": "C.UTF-8",
                       "alternate": "PG_UNICODE_FAST"},
            "libraries": [
                {"name": "zlib", "version": "1.4", "sha256": "d" * 64, "linkage": "static",
                 "license": "share/licenses/zlib.txt"},
                {"name": "openssl", "version": "3.6.5", "sha256": "e" * 64,
                 "linkage": "shared", "license": "share/licenses/openssl.txt"},
                {"name": "libxml2", "version": "2.15.4", "sha256": "f" * 64,
                 "linkage": "static", "license": "share/licenses/libxml2.txt"},
                {"name": "icu", "version": "79.1", "sha256": "1" * 64, "linkage": "static",
                 "license": "share/licenses/icu.txt"},
                {"name": "util-linux", "version": "2.42.4", "sha256": "2" * 64,
                 "linkage": "static", "license": "share/licenses/util-linux.txt"}],
            "modules": {"included": included, "skipped": []},
            "binaries": [],
            "source_archive": "share/sources/postgresql-18.6.tar.bz2",
            "licenses": {"builder": "share/licenses/builder.txt",
                         "postgresql": "share/licenses/postgresql.txt",
                         "musl": "share/licenses/musl.txt"},
            "provenance": {},
            "icu": {"decision": "with_icu", "reason": "zig c++ compiled ICU 79.1"},
            "builder": {"commit": "0" * 40, "source_sha256": "3" * 64},
        }

    def staged(self, root):
        """A bundle directory shaped like stage() left it, with a real loader.

        The loader is the host's glibc loader: it accepts `--library-path`
        and runs the fixture binaries, so lifecycle/smoke plumbing can be
        exercised without a musl toolchain. Nothing here claims to be a real
        PostgreSQL server; the real one is exercised by `verify` on dist/.
        """
        bundle = root / "bundle"
        for directory in ("bin", "lib/musl", "libexec/bin", "share/extension"):
            (bundle / directory).mkdir(parents=True)
        shutil.copy2(HOST_LOADER, bundle / "lib/musl/ld-musl-x86_64.so.1")
        (bundle / "lib/musl/ld-musl-x86_64.so.1").chmod(0o755)
        for command in ("postgres", "initdb", "pg_ctl", "psql", "pg_config",
                        "createdb", "dropdb", "pg_dump", "pg_restore", "pg_isready",
                        "vacuumdb", "reindexdb", "clusterdb", "oid2name", "vacuumlo"):
            launcher = bundle / "bin" / command
            launcher.write_text("#!/bin/sh\nexit 0\n")
            if command == "pg_config":
                # Behaves like the real one: every path is derived from the
                # launcher's own location, so a copy reports the copy's paths.
                launcher.write_text(
                    "#!/bin/sh\n"
                    "ROOT=$(CDPATH='' cd -P -- \"$(dirname -- \"$0\")/..\" && pwd)\n"
                    "printf '%s\\n' \"$ROOT/bin\" \"$ROOT/share\" \"$ROOT/lib\" \"$ROOT/lib\"\n")
            launcher.chmod(0o755)
            (bundle / "libexec/bin" / command).write_bytes(b"\x7fELF" + command.encode())
        return bundle

    def write(self, bundle, manifest=None):
        """Write a manifest whose loader record names the loader actually staged."""
        manifest = manifest if manifest is not None else self.manifest()
        manifest["loader"]["sha256"] = sha256_file(bundle / manifest["loader"]["path"])
        (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return manifest

    def environment(self, root):
        """The minimal environment smoke() needs, without starting a cluster."""
        socket = Path(root) / "socket"
        socket.mkdir(parents=True, exist_ok=True)
        return self.bundle._environment(Path(root), Path(root) / "data", socket)

    def psql(self, version="18.6"):
        """A psql stand-in: the version banner, the ICU ordering, a true row otherwise."""
        def respond(psql, arguments, env, stdin=None, timeout=300):
            if any("version()" in str(argument) for argument in arguments):
                return f"PostgreSQL {version} on x86_64-pc-linux-gnu\n"
            if stdin and "COLLATE" in stdin:
                # What the real bundle prints: ICU orders B before a, builtin C.utf8 does not.
                return "f|t\n"
            return "t\n"
        return respond

    # -- the pure plan -----------------------------------------------------

    def test_check_plan_covers_every_included_kind(self):
        manifest = self.manifest()
        checks = self.bundle.checks(manifest)
        for kind in ("sql", "preload", "cli"):
            self.assertTrue(any(row["kind"] == kind for row in checks), kind)
        self.assertEqual({row["name"] for row in checks if row["kind"] == "sql"},
                         {row["name"] for row in manifest["modules"]["included"]
                          if row["kind"] == "sql"})
        for row in checks:
            self.assertEqual(set(row), {"kind", "name", "statement"}, row)

    def test_check_plan_has_one_row_per_included_module(self):
        manifest = self.manifest()
        checks = self.bundle.checks(manifest)
        planned = {(row["kind"], row["name"]) for row in checks}
        for row in manifest["modules"]["included"]:
            if row["kind"] == "shared":
                continue
            self.assertIn((row["kind"], row["name"]), planned, row)
        # `shared` libraries are loaded by something else, never on their own.
        self.assertFalse([row for row in checks if row["kind"] == "shared"])

    def test_check_plan_statements_are_real_proofs_not_creates(self):
        checks = {row["name"]: row["statement"] for row in self.bundle.checks(self.manifest())}
        # A named proof-of-linkage case per module the brief calls out.
        self.assertIn("'a=>1'::hstore->'a'", checks["hstore"])
        self.assertIn("cube_distance", checks["cube"])
        self.assertIn("'ABC'::citext = 'abc'", checks["citext"])
        self.assertIn("similarity", checks["pg_trgm"])
        self.assertIn("'a.b'::ltree <@ 'a'::ltree", checks["ltree"])
        self.assertIn("digest", checks["pgcrypto"])
        self.assertIn("uuid_generate_v4()", checks["uuid-ossp"])
        self.assertIn("xpath", checks["xml2"])
        for name, statement in checks.items():
            if not any(row["name"] == name and row["kind"] == "sql"
                       for row in self.bundle.checks(self.manifest())):
                continue
            self.assertNotRegex(statement, r"^\s*CREATE\s+EXTENSION\s+\S+\s*;?\s*$", name)
            self.assertRegex(statement, r"SELECT", name)
        # Preload rows carry the library name; CLI rows carry the command name.
        for row in self.bundle.checks(self.manifest()):
            if row["kind"] == "preload":
                self.assertEqual(row["statement"], row["name"])
            if row["kind"] == "cli":
                self.assertTrue(row["statement"], row["name"])

    def test_a_sql_module_without_a_statement_is_an_error_not_a_silent_skip(self):
        manifest = self.manifest()
        manifest["modules"]["included"].append({"name": "brand_new", "kind": "sql"})
        with self.assertRaisesRegex(ValueError, "brand_new"):
            self.bundle.checks(manifest)

    def test_every_sql_module_in_the_real_manifest_has_a_statement(self):
        # The table in the module must cover a real bundle's modules, not just
        # the brief's examples: a statement written from memory would drift.
        manifest = self.manifest()
        for row in self.bundle.checks(manifest):
            self.assertNotIn("TODO", row["statement"], row["name"])

    # -- refusals ----------------------------------------------------------

    def test_verify_rejects_a_tampered_binary_before_smoking(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.staged(root)
            manifest = self.manifest()
            manifest["loader"]["sha256"] = sha256_file(bundle / "lib/musl/ld-musl-x86_64.so.1")
            postgres = bundle / "libexec/bin/postgres"
            manifest["binaries"] = [{"path": "libexec/bin/postgres", "sha256": sha256_file(postgres),
                                     "interpreter": "/lib/ld-musl-x86_64.so.1",
                                     "needed": ["libc.so"], "rpath": None, "runpath": None,
                                     "glibc_required": None, "machine": "Advanced Micro Devices X86-64"}]
            self.write(bundle, manifest)
            data = bytearray(postgres.read_bytes())
            data[0] ^= 0xFF
            postgres.write_bytes(bytes(data))
            with mock.patch.object(self.bundle, "lifecycle") as lifecycle, \
                    mock.patch.object(self.bundle, "assert_no_host_loaders"):
                with self.assertRaises(ValueError):
                    self.bundle.verify(bundle)
            lifecycle.assert_not_called()
            # A crash mid-verification must not leave PASSED on disk.
            self.assertEqual(json.loads((bundle / "manifest.json").read_text())["verification"],
                             "NOT_RUN")

    def test_verify_refuses_to_report_passed_for_an_unsupported_locale_request(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.staged(root)
            manifest = self.manifest()
            manifest["locale"]["default_provider"] = "libc"
            self.write(bundle, manifest)
            with self.assertRaisesRegex(ValueError, "builtin"):
                self.bundle.verify(bundle)
            self.assertEqual(json.loads((bundle / "manifest.json").read_text())["verification"],
                             "NOT_RUN")

    def test_verify_refuses_a_bin_that_is_a_symlink_or_not_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.staged(root)
            manifest = self.manifest()
            self.write(bundle, manifest)
            (bundle / "bin/psql").unlink()
            (bundle / "bin/psql").symlink_to("postgres")
            with self.assertRaisesRegex(ValueError, "psql"):
                self.bundle.verify(bundle)
            (bundle / "bin/psql").unlink()
            (bundle / "bin/psql").write_text("#!/bin/sh\nexit 0\n")
            (bundle / "bin/psql").chmod(0o644)
            with self.assertRaisesRegex(ValueError, "psql"):
                self.bundle.verify(bundle)

    def test_verify_refuses_a_missing_expected_module(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.staged(root)
            manifest = self.manifest()
            self.write(bundle, manifest)
            with mock.patch.object(self.bundle, "lifecycle"), \
                    mock.patch.object(self.bundle, "assert_no_host_loaders", return_value="bwrap"), \
                    mock.patch.object(self.bundle.modules, "inventory", return_value=[]):
                with self.assertRaisesRegex(ValueError, "missing sql: hstore"):
                    self.bundle.verify(bundle)
            self.assertEqual(json.loads((bundle / "manifest.json").read_text())["verification"],
                             "NOT_RUN")

    # -- host loader masking ----------------------------------------------

    def test_host_loader_masking_is_detected_or_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = self.staged(Path(temp))
            strategy = self.bundle.assert_no_host_loaders(bundle)
        self.assertIn(strategy, ("bwrap", "poison-file"))
        # Never a silent "no isolation available".
        self.assertNotIn("none", strategy.lower())
        self.assertNotIn("unavailable", strategy.lower())

    def test_host_loader_masking_fails_when_no_strategy_can_run(self):
        with mock.patch.object(self.bundle.shutil, "which", return_value=None), \
                mock.patch.object(self.bundle, "_poison_probe", return_value=False):
            with self.assertRaisesRegex(ValueError, "host loader"):
                self.bundle.assert_no_host_loaders(Path("/nonexistent"))

    def test_verify_falls_back_to_the_poison_probe_without_bwrap(self):
        # Without unprivileged user namespaces — common in CI and containers —
        # bwrap cannot run, and a bundle that still proves its own linkage must
        # verify rather than abort the build. This is the call verify() makes,
        # with the bundle it is verifying, not a bare one.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.staged(root)
            self.write(bundle)
            copy = root / "PostgreSQL 18.6 #relocated"
            shutil.copytree(bundle, copy, symlinks=True)
            with mock.patch.object(self.bundle.shutil, "which", return_value=None), \
                    mock.patch.object(self.bundle, "_poison_probe", return_value=True), \
                    mock.patch.object(self.bundle, "lifecycle",
                                      return_value=["server-version"]) as lifecycle, \
                    mock.patch.object(self.bundle, "relocate", return_value=copy), \
                    mock.patch.object(self.bundle, "pg_config_paths", return_value={}), \
                    mock.patch.object(self.bundle, "_verify_binaries"), \
                    mock.patch.object(self.bundle, "_check_launchers"), \
                    mock.patch.object(self.bundle, "_check_module_inventory"):
                result = self.bundle.verify(bundle)
            self.assertEqual(result["strategy"], "poison-file")
            # The lifecycle must be told which strategy proved isolation, so it
            # can run unmasked under the poison fallback and masked under bwrap.
            self.assertEqual({call.args[3] for call in lifecycle.call_args_list},
                             {"poison-file"})
            stored = json.loads((bundle / "manifest.json").read_text())
            self.assertEqual(stored["verification"], "PASSED")
            self.assertEqual(stored["verification_strategy"], "poison-file")

    def test_verify_never_passes_without_bwrap_or_the_poison_probe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.staged(root)
            self.write(bundle)
            with mock.patch.object(self.bundle.shutil, "which", return_value=None), \
                    mock.patch.object(self.bundle, "_poison_probe", return_value=False), \
                    mock.patch.object(self.bundle, "lifecycle") as lifecycle:
                with self.assertRaisesRegex(ValueError, "host loader"):
                    self.bundle.verify(bundle)
            lifecycle.assert_not_called()
            stored = json.loads((bundle / "manifest.json").read_text())
            self.assertEqual(stored["verification"], "NOT_RUN")
            self.assertNotIn("verification_strategy", stored)

    def test_the_masking_strategy_makes_the_real_host_loader_unusable(self):
        # On this host /lib/ld-musl-x86_64.so.1 exists, so the strategy must
        # actually hide it; the check inside the sandbox is what verify relies on.
        if not self.bundle._bwrap_usable():
            self.skipTest("bwrap cannot create namespaces on this host")
        self.assertEqual(self.bundle._masked_host_loaders_probe(), "bwrap")

    # -- lifecycle and relocation -----------------------------------------

    def test_lifecycle_always_stops_the_server(self):
        calls = []

        def record(arguments, **kwargs):
            calls.append([str(argument) for argument in arguments])
            return ""

        manifest = self.manifest()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.staged(root)
            with mock.patch.object(self.bundle, "_run", side_effect=record), \
                    mock.patch.object(self.bundle, "smoke", return_value=["smoke"]):
                self.bundle.lifecycle(bundle, manifest, ["auto_explain"])
            verbs = [call[-1] for call in calls
                     if call[0].endswith("pg_ctl") and call[-1] in ("start", "stop")]
            self.assertEqual(verbs, ["start", "stop"])

    def test_lifecycle_stops_the_server_even_when_smoke_fails(self):
        calls = []

        def record(arguments, **kwargs):
            calls.append([str(argument) for argument in arguments])
            return ""

        manifest = self.manifest()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.staged(root)
            with mock.patch.object(self.bundle, "_run", side_effect=record), \
                    mock.patch.object(self.bundle, "smoke", side_effect=ValueError("boom")):
                with self.assertRaisesRegex(ValueError, "boom"):
                    self.bundle.lifecycle(bundle, manifest, ["auto_explain"])
            self.assertTrue(any(call[0].endswith("pg_ctl") and call[-1] == "stop" for call in calls))

    def test_lifecycle_keeps_all_cluster_state_out_of_the_bundle(self):
        # The staged tree is what gets packaged, and its socket path is already
        # 118 characters — past sun_path's 108 — so a cluster created inside it
        # could neither start nor be shipped. Every path lifecycle writes must
        # live in its own workspace, and the workspace must be gone afterwards.
        manifest = self.manifest()
        seen = []

        def record(arguments, **kwargs):
            seen.append(kwargs["env"])
            return ""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.staged(root)
            with mock.patch.object(self.bundle, "_run", side_effect=record), \
                    mock.patch.object(self.bundle, "smoke", return_value=["smoke"]):
                self.bundle.lifecycle(bundle, manifest, ["auto_explain"])
        data = Path(seen[0]["PGDATA"])
        self.assertFalse(data.resolve().is_relative_to(bundle.resolve()),
                         "the data directory must not live in the bundle")
        self.assertFalse(Path(seen[0]["PGHOST"]).resolve().is_relative_to(bundle.resolve()))
        self.assertLess(len(str(data.parent / "socket")), 108, "pg_ctl would refuse the socket")
        self.assertFalse(data.parent.exists(), "the workspace is removed after the run")
        self.assertEqual([Path(environment["PGDATA"]) for environment in seen[1:]],
                         [data] * (len(seen) - 1), "every step sees the same cluster")

    def test_lifecycle_names_the_server_log_when_preload_fails(self):
        manifest = self.manifest()

        def failing(arguments, **kwargs):
            if str(arguments[-1]) == "start":
                # lifecycle writes its log into its own workspace; the log holds
                # the real reason a preload did not load.
                Path(kwargs["env"]["PGDATA"]).parent.joinpath("server.log").write_text(
                    'LOG: could not load library "/x/auto_explain.so": no such file\n')
                raise ValueError("pg_ctl start failed")
            return ""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.staged(root)
            with mock.patch.object(self.bundle, "_run", side_effect=failing):
                with self.assertRaisesRegex(ValueError, "could not load library"):
                    self.bundle.lifecycle(bundle, manifest, ["auto_explain"])

    def test_relocated_bundle_reports_paths_inside_itself(self):
        if not shutil.which("cc") or not shutil.which("readelf"):
            self.skipTest("ELF fixture compiler is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.staged(root)
            self.write(bundle)
            relocated = self.bundle.relocate(bundle)
            try:
                self.assertNotEqual(relocated, bundle)
                self.assertIn("#", str(relocated))
                self.assertTrue(relocated.is_dir())
                self.assertTrue((relocated / "bin/psql").is_file())
                # pg_config must report the copy's own paths, not the original's.
                original = self.bundle.pg_config_paths(bundle)
                derived = self.bundle.pg_config_paths(relocated)
                self.assertEqual(set(derived), {"bindir", "sharedir", "libdir", "pkglibdir"})
                for key, value in derived.items():
                    self.assertEqual(value, original[key].replace(str(bundle), str(relocated)), key)
            finally:
                shutil.rmtree(relocated, ignore_errors=True)

    def test_every_planned_cli_row_is_executed_by_smoke(self):
        # A cli row in the plan must be run by smoke, not by a hardcoded list
        # beside it: otherwise a new cli module would be planned, ignored, and
        # still reported PASSED — the silent skip the brief forbids.
        manifest = self.manifest()
        manifest["modules"]["included"].append({"name": "pg_waldump", "kind": "cli"})
        with tempfile.TemporaryDirectory() as temp:
            bundle = self.staged(Path(temp))
            # A launcher the fixture does not ship by default, so the only way
            # it gets run is because the plan named it.
            (bundle / "bin/pg_waldump").write_text("#!/bin/sh\nexit 0\n")
            (bundle / "bin/pg_waldump").chmod(0o755)
            with mock.patch.object(self.bundle, "_run", return_value=""), \
                    mock.patch.dict(self.bundle.CLI_PROOFS, {"pg_waldump": ("--help",)}), \
                    mock.patch.object(self.bundle, "_psql", side_effect=self.psql()):
                names = self.bundle.smoke(bundle, manifest, self.environment(Path(temp)))
        self.assertIn("tool:pg_waldump", names)

    def test_a_planned_cli_without_a_proof_invocation_is_an_error_not_a_version_banner(self):
        # Without this, `--version` would be the quiet default and a tool that
        # cannot reach the cluster at all would still be recorded as exercised.
        manifest = self.manifest()
        manifest["modules"]["included"].append({"name": "pg_waldump", "kind": "cli"})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.staged(root)
            (bundle / "bin/pg_waldump").write_text("#!/bin/sh\nexit 0\n")
            (bundle / "bin/pg_waldump").chmod(0o755)
            with mock.patch.object(self.bundle, "_run", return_value=""), \
                    mock.patch.object(self.bundle, "_psql", side_effect=self.psql()):
                with self.assertRaisesRegex(ValueError, "pg_waldump"):
                    self.bundle.smoke(bundle, manifest, self.environment(root))

    def test_a_planned_cli_the_bundle_does_not_provide_fails_rather_than_passes(self):
        # The bundle has no bin/pg_waldump in this fixture, so running it must
        # fail — a plan row that cannot be executed is a failure, never a skip.
        manifest = self.manifest()
        manifest["modules"]["included"].append({"name": "pg_waldump", "kind": "cli"})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.staged(root)
            with mock.patch.object(self.bundle, "_psql", side_effect=self.psql()):
                with self.assertRaisesRegex(ValueError, "pg_waldump"):
                    self.bundle.smoke(bundle, manifest, self.environment(root))

    def test_smoke_proves_every_planned_preload_row_loaded(self):
        # Start-up is the proof: a bogus entry in shared_preload_libraries makes
        # the server refuse to start, so a cluster that came up with the planned
        # list has loaded every one of those libraries.
        manifest = self.manifest()
        with tempfile.TemporaryDirectory() as temp:
            bundle = self.staged(Path(temp))
            with mock.patch.object(self.bundle, "_run", return_value=""), \
                    mock.patch.object(self.bundle, "_psql", side_effect=self.psql()):
                names = self.bundle.smoke(bundle, manifest, self.environment(Path(temp)))
        for row in self.bundle.checks(manifest):
            if row["kind"] == "preload":
                self.assertIn(f"preload:{row['name']}", names, row["name"])

    def test_the_round_trip_no_longer_carries_its_own_tool_list(self):
        self.assertFalse(hasattr(self.bundle, "ROUND_TRIP_TOOLS"),
                         "the tool list must come from checks(), not a constant")

    # -- placeholder substitution and the sql details it must not break ----

    def test_substitution_leaves_sql_braces_and_percent_signs_alone(self):
        # str.format would read {1,2,3} as a field and raise; a regex or a LIKE
        # pattern with % must survive untouched. Both shapes are in real proofs.
        statement = ("SELECT '{1,2,3}'::text[] @> ARRAY['1'] "
                     "AND 'x' LIKE '%x%' AND host = '{host}';")
        result = self.bundle._substitute(statement, Path("/tmp/sock"))
        self.assertIn("'{1,2,3}'", result)
        self.assertIn("LIKE '%x%'", result)
        self.assertIn("host = '/tmp/sock'", result)
        # No placeholder survives that the table knows how to fill.
        for placeholder in ("{dsn}", "{host}", "{port}", "{dsn_db}", "{dbname}"):
            self.assertNotIn(placeholder, result)

    def test_every_proof_statement_survives_substitution(self):
        # The whole table, not one example: a statement added later with braces
        # must not be able to reintroduce the format() failure.
        socket = Path("/tmp/verify socket")
        for row in self.bundle.checks(self.manifest()):
            if row["kind"] != "sql":
                continue
            result = self.bundle._substitute(row["statement"], socket)
            for placeholder in ("{dsn}", "{host}", "{port}", "{dsn_db}", "{dbname}"):
                self.assertNotIn(placeholder, result, row["name"])
            if "{dsn}" in row["statement"] or "{host}" in row["statement"]:
                self.assertIn(str(socket), result, row["name"])

    def test_the_pg_stat_statements_proof_names_its_own_schema(self):
        # verify creates the extension into verify_stat_statements, so an
        # unqualified relation name raises "does not exist" on a real cluster.
        statement = self.bundle.SQL_PROOFS["pg_stat_statements"]
        self.assertIn("verify_stat_statements.pg_stat_statements", statement)

    def test_the_pg_stat_statements_extension_is_created_in_that_schema(self):
        calls = []

        def record(psql, arguments, env, stdin=None, timeout=300):
            calls.append(" ".join(str(argument) for argument in arguments))
            return ""

        with mock.patch.object(self.bundle, "_psql", side_effect=record):
            self.bundle._create_extension("psql", {"kind": "sql", "name": "pg_stat_statements",
                                                   "statement": ""}, {"PATH": "/usr/bin:/bin"})
        self.assertTrue(any("CREATE SCHEMA IF NOT EXISTS verify_stat_statements" in call
                            for call in calls), calls)
        self.assertTrue(any("SCHEMA verify_stat_statements" in call for call in calls), calls)

    def test_the_icu_ordering_parse_reads_unaligned_output(self):
        # psql separates columns with a pipe even under -tA; the parse must
        # read f|t as ICU-then-builtin, and must reject t|t, the fallback shape.
        manifest = self.manifest()

        def answer(output):
            with mock.patch.object(self.bundle, "_psql", return_value=output):
                return self.bundle._icu(Path("/nonexistent"), manifest,
                                        {"PATH": "/usr/bin:/bin"})

        self.assertEqual(answer("f|t\n"), "icu:linked")
        with self.assertRaisesRegex(ValueError, "not being used"):
            answer("t|t\n")

    # -- workspace hygiene on the failure paths ----------------------------

    def test_lifecycle_removes_its_workspace_when_isolation_raises(self):
        # _isolation runs inside the try: bwrap vanishing between the probe and
        # here must not leak a /tmp/pg-verify-* directory behind it.
        manifest = self.manifest()
        created = []
        real = self.bundle._workspace

        def record():
            path = real()
            created.append(path)
            return path

        def explode(workspace, strategy):
            raise ValueError("bwrap disappeared between the probe and the lifecycle")

        with tempfile.TemporaryDirectory() as temp:
            bundle = self.staged(Path(temp))
            with mock.patch.object(self.bundle, "_workspace", side_effect=record), \
                    mock.patch.object(self.bundle, "_isolation", side_effect=explode):
                with self.assertRaisesRegex(ValueError, "bwrap disappeared"):
                    self.bundle.lifecycle(bundle, manifest, ["auto_explain"], "bwrap")
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists(), "the workspace leaked")

    def test_verify_removes_a_relocated_copy_it_already_made(self):
        # relocate() creates the copy before returning, so a failure after that
        # point must still remove it; otherwise every failed run leaves ~300 MB.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.staged(root)
            self.write(bundle)
            copy = root / "PostgreSQL 18.6 #relocated"
            shutil.copytree(bundle, copy, symlinks=True)
            with mock.patch.object(self.bundle, "assert_no_host_loaders", return_value="bwrap"), \
                    mock.patch.object(self.bundle, "lifecycle", return_value=["server-version"]), \
                    mock.patch.object(self.bundle, "relocate", return_value=copy), \
                    mock.patch.object(self.bundle, "pg_config_paths",
                                      side_effect=ValueError("pg_config broke")), \
                    mock.patch.object(self.bundle, "_verify_binaries"), \
                    mock.patch.object(self.bundle, "_check_launchers"), \
                    mock.patch.object(self.bundle, "_check_module_inventory"):
                with self.assertRaisesRegex(ValueError, "pg_config broke"):
                    self.bundle.verify(bundle)
            self.assertFalse(copy.exists(), "the relocated copy leaked")

    def test_preload_list_is_every_preload_module_in_manifest_order(self):
        manifest = self.manifest()
        self.assertEqual(self.bundle.preload_list(manifest),
                         [row["name"] for row in manifest["modules"]["included"]
                          if row["kind"] == "preload"])

    # -- manifest bookkeeping ---------------------------------------------

    def test_verify_records_the_strategy_only_on_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.staged(root)
            manifest = self.manifest()
            manifest["loader"]["sha256"] = sha256_file(bundle / "lib/musl/ld-musl-x86_64.so.1")
            self.write(bundle, manifest)
            copy = root / "PostgreSQL 18.6 #relocated"
            shutil.copytree(bundle, copy, symlinks=True)
            with mock.patch.object(self.bundle, "assert_no_host_loaders", return_value="bwrap"), \
                    mock.patch.object(self.bundle, "lifecycle", return_value=["server-version"]), \
                    mock.patch.object(self.bundle, "relocate", return_value=copy), \
                    mock.patch.object(self.bundle, "pg_config_paths", return_value={}), \
                    mock.patch.object(self.bundle, "_verify_binaries"), \
                    mock.patch.object(self.bundle, "_check_launchers"), \
                    mock.patch.object(self.bundle, "_check_module_inventory"):
                result = self.bundle.verify(bundle)
            self.assertFalse(copy.exists(), "the relocated copy is cleaned up")
            self.assertEqual(result["strategy"], "bwrap")
            self.assertIn("checks", result)
            stored = json.loads((bundle / "manifest.json").read_text())
            self.assertEqual(stored["verification"], "PASSED")
            self.assertEqual(stored["verification_strategy"], "bwrap")
            self.assertEqual(stored["checks"], ["server-version", "server-version"])

    def test_build_script_verifies_before_packaging_and_keeps_a_failed_tree(self):
        text = (SCRIPTS / "build.sh").read_text()
        stage = text.index('bundle.py" stage')
        verify = text.index('bundle.py" verify')
        self.assertLess(stage, verify)
        self.assertIn('"$bundle"', text[verify:])
        # `set -e` is what makes a non-zero verify abort: without it the script
        # would print "Verified bundle" and carry on regardless.
        self.assertIn("set -euo pipefail", text)
        self.assertIn("printf 'Verified bundle", text)
        # A non-zero verify must abort before anything is packaged. Packaging
        # does not exist yet — a later task owns it — so assert the ordering
        # the moment it appears rather than guarding on a keyword that is
        # currently absent and leaving the check inert.
        if 'bundle.py" package' in text:
            self.assertLess(verify, text.index('bundle.py" package'))


if __name__ == "__main__":
    unittest.main()
