"""Redis source authenticity, CLI and portable package contracts."""
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
SCRIPTS = ROOT / "runtimes/redis/scripts"


def module(name):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"redis_{name}", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class RedisSourceTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((SCRIPTS / "source.py").is_file(), "Redis source authentication module is missing")
        self.source = module("source")

    def test_pinned_source_selects_exact_upstream_archive(self):
        row = self.source.load_source("8.10.3")
        self.assertEqual(row["url"], "https://download.redis.io/releases/redis-8.10.3.tar.gz")
        self.assertEqual(row["sha256"], "edde778f6bdee7581e42fc139ca9e07e2822212092f7501f99989656e3629d02")
        for value in ("8.10", "latest", "8.12-rc2", "../../tmp", "8.10.99"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.source.load_source(value)

    def test_source_digest_rejects_modified_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source.tar.gz"
            path.write_bytes(b"source")
            self.source.verify_digest(path, "41cf6794ba4200b839c53531555f0f3998df4cbb01a4d5cb0b94e3ca5e23947d")
            path.write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                self.source.verify_digest(path, "41cf6794ba4200b839c53531555f0f3998df4cbb01a4d5cb0b94e3ca5e23947d")

    def test_extraction_is_fresh_and_confined(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "archive.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                info = tarfile.TarInfo("redis-8.10.3/src/Makefile")
                info.size = 4
                tar.addfile(info, io.BytesIO(b"all:"))
            dest = root / "source"
            result = self.source.extract_source(archive, dest)
            self.assertEqual((result / "src/Makefile").read_bytes(), b"all:")
            with self.assertRaises(FileExistsError):
                self.source.extract_source(archive, dest)

    def test_safe_documentation_symlinks_are_not_materialized(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "archive.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                info = tarfile.TarInfo("redis-8.10.3/src/Makefile")
                info.size = 4
                tar.addfile(info, io.BytesIO(b"all:"))
                link = tarfile.TarInfo("redis-8.10.3/modules/docs/CLAUDE.md")
                link.type = tarfile.SYMTYPE
                link.linkname = "AGENTS.md"
                tar.addfile(link)
            extracted = self.source.extract_source(archive, root / "source")
            self.assertTrue((extracted / "src/Makefile").is_file())
            self.assertFalse((extracted / "modules/docs/CLAUDE.md").is_symlink())

    def test_extraction_rejects_escape_links_and_special_members(self):
        for name, kind, link in [("../outside", tarfile.REGTYPE, ""), ("/tmp/outside", tarfile.REGTYPE, ""), ("redis/src/link", tarfile.SYMTYPE, "../../outside"), ("redis/src/link", tarfile.LNKTYPE, "outside"), ("redis/device", tarfile.CHRTYPE, "")]:
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                archive = root / "archive.tar.gz"
                with tarfile.open(archive, "w:gz") as tar:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    info.linkname = link
                    tar.addfile(info)
                with self.assertRaises(ValueError):
                    self.source.extract_source(archive, root / "source")
                self.assertFalse((root / "outside").exists())


class RedisCLITests(unittest.TestCase):
    def test_dispatcher_plan_and_exact_version(self):
        for args in ([], ["8.10.3"]):
            with self.subTest(args=args):
                run = subprocess.run([str(ROOT / "build.sh"), "redis", *args, "--print-plan"], text=True, capture_output=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                plan = json.loads(run.stdout)
                self.assertEqual((plan["version"], plan["target"]), ("8.10.3", "musl"))
                self.assertNotIn("profile", plan)
                self.assertNotIn("tls", plan)
        run = subprocess.run([str(ROOT / "build.sh"), "redis", "7.2.17", "--print-plan"], text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["version"], "7.2.17")

    def test_invalid_arguments_fail_before_build(self):
        self.assertTrue((SCRIPTS / "build.sh").is_file(), "Redis CLI is missing")
        for args in [["other"], ["8.12-rc2"], ["glibc"], ["musl"], ["glibc", "musl"], ["full"], ["minimal"], ["--docker"], ["8.10.3", "7.2.17"], ["--bad"], ["../../tmp"]]:
            with self.subTest(args=args):
                run = subprocess.run([str(ROOT / "build.sh"), "redis", *args, "--print-plan"], text=True, capture_output=True)
                self.assertNotEqual(run.returncode, 0)
                self.assertNotIn("docker", run.stdout.lower())


class RedisBundleTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((SCRIPTS / "bundle.py").is_file(), "Redis bundle verification is missing")
        self.bundle = module("bundle")

    def test_elf_contract_rejects_dynamic_musl_and_wrong_architecture(self):
        header = "Class: ELF64\nMachine: Advanced Micro Devices X86-64\n"
        result = self.bundle.parse_elf(header, "", "There is no dynamic section", "", "musl")
        self.assertTrue(result["static"])
        self.assertIsNone(result["interpreter"])
        self.assertIsNone(result["glibc_required"])
        for h, p, d, v in [(header, "INTERP", "", ""), (header, "", "(NEEDED) libc.so", ""), (header, "", "", "GLIBC_2.17"), ("Class: ELF64\nMachine: AArch64", "", "", "")]:
            with self.subTest(h=h, p=p, d=d, v=v), self.assertRaises(ValueError):
                self.bundle.parse_elf(h, p, d, v, "musl")

    def test_only_the_static_musl_target_is_accepted(self):
        header = "Class: ELF64\nMachine: Advanced Micro Devices X86-64"
        with self.assertRaisesRegex(ValueError, "musl"):
            self.bundle.parse_elf(header, "[Requesting program interpreter: /lib64/ld-linux-x86-64.so.2]", "(NEEDED) Shared library: [libc.so.6]", "GLIBC_2.34", "glibc")

    def test_musl_license_notice_is_required_and_recorded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            notice = root / "COPYRIGHT"
            notice.write_text("musl fixture copyright notice")
            result = self.bundle.libc_notice("musl", notice)
            self.assertEqual(result["sha256"], hashlib.sha256(notice.read_bytes()).hexdigest())
            with self.assertRaises(FileNotFoundError):
                self.bundle.libc_notice("musl", root / "missing")
            with self.assertRaisesRegex(ValueError, "musl"):
                self.bundle.libc_notice("glibc", root / "missing")

    def test_native_probe_requires_musl_headers_before_source_build(self):
        import shutil
        if not shutil.which("gcc") or not shutil.which("readelf"):
            self.skipTest("Native GCC/binutils not installed")
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            with self.assertRaises(ValueError):
                self.bundle.probe("gcc", "musl", work)
            self.assertFalse((work / "toolchain.json").exists())
            with self.assertRaisesRegex(ValueError, "musl"):
                self.bundle.probe("gcc", "glibc", work)
        if shutil.which("musl-gcc"):
            with tempfile.TemporaryDirectory() as temp:
                metadata = self.bundle.probe("musl-gcc", "musl", Path(temp))
                self.assertTrue(metadata["libc_probe"]["static"])
                self.assertIsNone(metadata["libc_probe"]["glibc_required"])

    def test_actual_version_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "redis-server"
            executable.write_text("#!/bin/sh\nprintf 'Redis server v=7.2.17 bits=64\\n'\n")
            executable.chmod(0o755)
            self.assertEqual(self.bundle.binary_version(executable), "7.2.17")
            with self.assertRaises(ValueError):
                self.bundle.require_version(executable, "8.10.3")

    def test_packaging_requires_verified_manifest_and_preserves_aliases(self):
        with tempfile.TemporaryDirectory(prefix="redis package spaces ") as temp:
            root = Path(temp)
            bundle = root / "bundle"
            (bundle / "bin").mkdir(parents=True)
            (bundle / "bin/redis-server").write_text("#!/bin/sh\nexit 0\n")
            (bundle / "bin/redis-server").chmod(0o755)
            (bundle / "bin/redis-sentinel").symlink_to("redis-server")
            manifest = {"runtime": "redis", "redis": {"version": "8.10.3"}, "target": "musl", "architecture": "x86_64", "build_id": "abc123", "verification": "NOT_RUN"}
            (bundle / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                self.bundle.package(bundle, root / "assets")
            manifest["verification"] = "PASSED"
            (bundle / "manifest.json").write_text(json.dumps(manifest))
            archive = self.bundle.package(bundle, root / "assets")
            self.assertEqual(archive.name, "redis-8.10.3-musl-x86_64-abc123.tar.gz")
            self.assertEqual(archive.with_name(archive.name + ".sha256").read_text().split()[0], hashlib.sha256(archive.read_bytes()).hexdigest())
            with tarfile.open(archive) as tar:
                server = tar.getmember("redis-8.10.3-musl-x86_64-abc123/bin/redis-server")
                self.assertEqual(server.mode & 0o777, 0o755)
                sentinel = tar.getmember("redis-8.10.3-musl-x86_64-abc123/bin/redis-sentinel")
                self.assertTrue(sentinel.issym())
                self.assertEqual(sentinel.linkname, "redis-server")
            with self.assertRaises(FileExistsError):
                self.bundle.package(bundle, root / "assets")

    def test_verify_rejects_changed_binary_before_smoke(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bin").mkdir()
            binary = root / "bin/redis-server"
            binary.write_bytes(b"tampered")
            manifest = {"verification": "PASSED", "binaries": [{"path": "bin/redis-server", "sha256": "0" * 64}], "target": "musl", "redis": {"version": "8.10.3"}}
            (root / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                self.bundle.verify(root)
            self.assertEqual(json.loads((root / "manifest.json").read_text())["verification"], "NOT_RUN")

    def test_sidecar_collision_never_overwrites_or_creates_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "bundle"
            bundle.mkdir()
            manifest = {"runtime": "redis", "redis": {"version": "8.10.3"}, "target": "musl", "architecture": "x86_64", "build_id": "abc123", "verification": "PASSED"}
            (bundle / "manifest.json").write_text(json.dumps(manifest))
            assets = root / "assets"
            assets.mkdir()
            sidecar = assets / "redis-8.10.3-musl-x86_64-abc123.manifest.json"
            sidecar.write_text("keep")
            with self.assertRaises(FileExistsError):
                self.bundle.package(bundle, assets)
            self.assertEqual(sidecar.read_text(), "keep")
            self.assertEqual(list(assets.glob("*.tar.gz")), [])


if __name__ == "__main__":
    unittest.main()
