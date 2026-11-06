"""Build-host tests: profile resolution, staging and archive contracts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "runtimes/php/scripts"


def load(name):
    path = SCRIPTS / f"{name}.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuildContracts(unittest.TestCase):
    def test_full_glibc_is_disjoint(self):
        module = load("configure")
        self.assertIsNotNone(module, "profile resolver is not implemented")
        result = module.resolve(REPO / "runtimes/php", "glibc", "full", "8.4")
        self.assertEqual(result["shared"], ["apcu", "redis"])
        self.assertFalse(set(result["static"]) & set(result["shared"]))
        self.assertEqual(len(result["selected"]), 40)
        yaml = module.craft(result)
        self.assertIn('shared-extensions: "apcu,redis"', yaml)
        self.assertNotIn('extensions: "apcu,', yaml.splitlines()[1])

    def test_minimal_and_musl_are_honest(self):
        module = load("configure")
        self.assertIsNotNone(module, "profile resolver is not implemented")
        result = module.resolve(REPO / "runtimes/php", "glibc", "minimal", "8.4")
        self.assertEqual(result["shared"], [])
        self.assertIn("pdo", result["static"])
        self.assertNotIn("pdo_mysql", result["static"])
        result = module.resolve(REPO / "runtimes/php", "musl", "full", "8.5")
        self.assertEqual(result["static"], result["selected"])
        self.assertEqual(result["shared"], [])

    def test_invalid_inputs_rejected(self):
        module = load("configure")
        self.assertIsNotNone(module, "profile resolver is not implemented")
        for target, profile, version in [("x", "full", "8.4"), ("glibc", "../full", "8.4"), ("musl", "full", "8.1"), ("glibc", "full", '8.4"\nx: x')]:
            with self.subTest(target=target, profile=profile, version=version):
                with self.assertRaises(ValueError):
                    module.resolve(REPO / "runtimes/php", target, profile, version)

    def test_missing_requested_module_fails_staging(self):
        module = load("manifest")
        self.assertIsNotNone(module, "module staging is not implemented")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "buildroot/modules").mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "redis"):
                module.stage_modules(root / "buildroot", root / "bundle", ["redis"])
            (root / "buildroot/modules/redis.so").write_bytes(b"module fixture")
            registry = module.stage_modules(root / "buildroot", root / "bundle", ["redis"])
            self.assertEqual(registry[0]["path"], "lib/php/extensions/redis.so")
            self.assertTrue((root / "bundle/lib/php/extensions/redis.so").is_file())

    def test_manifest_uses_actual_php_metadata(self):
        module = load("manifest")
        self.assertIsNotNone(module, "metadata extraction is not implemented")
        result = module.php_metadata(Path("/usr/bin/php"))
        self.assertRegex(result["version"], r"^8\.[0-9]+\.[0-9]+$")
        self.assertRegex(str(result["php_api"]), r"^[0-9]+$")
        self.assertIn("NTS", result["extension_build"])
        self.assertIsInstance(result["static_modules"], list)

    def test_fresh_build_reuses_prepared_toolchain_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spc = root / "spc"
            spc.write_text('#!/bin/sh\nprintf "PKGROOT=%s\\nWORK=%s\\n" "$PKG_ROOT_PATH" "$PWD"\nexit 42\n')
            spc.chmod(0o755)
            provenance = root / "provenance.json"
            provenance.write_text("{}")
            import os
            env = {**os.environ, "SPC_BIN": str(spc), "SPC_PROVENANCE": str(provenance)}
            env.pop("PKG_ROOT_PATH", None)
            result = subprocess.run([str(REPO / "build.sh"), "php", "glibc", "full", "8.4"], text=True, capture_output=True, env=env)
            self.assertEqual(result.returncode, 42, result.stderr)
            self.assertIn(f"PKGROOT={REPO}/runtimes/php/.cache/pkgroot/x86_64-linux\n", result.stdout)
            self.assertIn(f"WORK={REPO}/runtimes/php/.build.", result.stdout)
            bootstrap = (SCRIPTS / "download-spc.sh").read_text()
            self.assertIn('export PKG_ROOT_PATH=', bootstrap)

    def test_build_doctor_never_modifies_host(self):
        script = (SCRIPTS / "build.sh").read_text()
        self.assertIn('doctor --auto-fix=never', script)
        self.assertNotIn('"$SPC_BIN" doctor\n', script)

    def test_stage_keeps_requested_modes_and_relative_paths(self):
        module = load("manifest")
        self.assertIsNotNone(module, "module staging is not implemented")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            buildroot = root / "buildroot"
            (buildroot / "bin").mkdir(parents=True)
            import shutil
            for name in ("php", "php-fpm"):
                shutil.copy2("/usr/bin/php", buildroot / "bin" / name)
            (buildroot / "build-manifest.json").write_text(json.dumps({"target": {"libc": "glibc", "libc-version": "2.17"}, "targets": {"php": {"extensions": {"static": ["pdo"], "shared": [], "all": ["pdo"]}}}}))
            bundle = root / "staged bundle"
            resolution = {"version": "8.4", "target": "glibc", "profile": "minimal", "static": ["pdo"], "shared": []}
            manifest = module.stage(REPO / "runtimes/php", bundle, resolution, {"commit": "fixture"}, REPO, buildroot)
            self.assertEqual(manifest["resolved_modes"]["static"], ["pdo"])
            self.assertEqual(manifest["verification"], "NOT_RUN")
            self.assertTrue((bundle / "bin/php-ext").is_file())
            self.assertTrue((bundle / "spc-build-manifest.json").is_file())
            for path in manifest["paths"].values():
                self.assertFalse(Path(path).is_absolute())
            self.assertNotIn(str(bundle), (bundle / "etc/php.ini").read_text())
            self.assertFalse((bundle / "etc/php-fpm.conf").exists())
            with self.assertRaises(FileExistsError):
                module.stage(REPO / "runtimes/php", bundle, resolution, {}, REPO, buildroot)

    def test_package_preserves_permissions_and_checksum(self):
        module = load("package")
        self.assertIsNotNone(module, "archive packaging is not implemented")
        with tempfile.TemporaryDirectory(prefix="archive spaces ") as tmp:
            root = Path(tmp)
            bundle = root / "bundle"
            (bundle / "bin").mkdir(parents=True)
            (bundle / "bin/php").write_text("#!/bin/sh\nexit 0\n")
            (bundle / "bin/php").chmod(0o755)
            (bundle / "manifest.json").write_text(json.dumps({"php": {"version": "8.4.27"}, "target": "glibc", "profile": "full", "architecture": "x86_64", "build_id": "fixture123"}))
            archive = module.package(bundle, root / "assets")
            self.assertIn("8.4.27-glibc-full-x86_64-fixture123", archive.name)
            self.assertEqual(archive.with_name(archive.name + ".sha256").read_text().split()[0], hashlib.sha256(archive.read_bytes()).hexdigest())
            with tarfile.open(archive) as tar:
                member = next(m for m in tar.getmembers() if m.name.endswith("/bin/php"))
                self.assertEqual(member.mode & 0o777, 0o755)
                extracted = root / "extracted"
                extracted.mkdir()
                tar.extractall(extracted, filter="data")
                executable = extracted / member.name
                self.assertEqual(executable.stat().st_mode & 0o777, 0o755)
                subprocess.run([str(executable)], check=True)
            with self.assertRaises(FileExistsError):
                module.package(bundle, root / "assets")


if __name__ == "__main__":
    unittest.main()
