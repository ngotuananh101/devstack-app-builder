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


if __name__ == "__main__":
    unittest.main()
