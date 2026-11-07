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
