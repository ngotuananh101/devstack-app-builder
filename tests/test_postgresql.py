"""PostgreSQL source authenticity and CLI contracts."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "runtimes/postgresql/scripts"


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


if __name__ == "__main__":
    unittest.main()
