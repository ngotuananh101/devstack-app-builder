"""Build-host tests: profile resolution, staging and archive contracts."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
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
    def test_resolution_is_a_disjoint_static_and_shared_set(self):
        module = load("configure")
        self.assertIsNotNone(module, "extension resolver is not implemented")
        result = module.resolve(REPO / "runtimes/php", "8.4")
        self.assertEqual(result["target"], "musl-dynamic")
        self.assertEqual(result["spc_target"], "x86_64-linux-musl -dynamic")
        self.assertEqual(len(result["shared"]), 20)
        self.assertEqual(len(result["static"]), 21)
        self.assertFalse(set(result["static"]) & set(result["shared"]))
        self.assertNotIn("profile", result)
        yaml = module.craft(result)
        self.assertIn('shared-extensions: "apcu,bcmath', yaml)
        self.assertIn('extensions: "ctype,curl', yaml)

    def test_configure_rejects_bad_versions_and_overlap(self):
        module = load("configure")
        for version in ("8.1", "9.0", "8.4RC1", '8.4"\nx: x', "8"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                module.resolve(REPO / "runtimes/php", version)
        with tempfile.TemporaryDirectory() as tmp:
            php_dir = Path(tmp)
            shutil.copytree(REPO / "runtimes/php/config", php_dir / "config")
            (php_dir / "config/static-extensions.txt").write_text("pdo,redis\n")
            (php_dir / "config/shared-extensions.txt").write_text("redis\n")
            with self.assertRaisesRegex(ValueError, "both static and shared"):
                module.resolve(php_dir, "8.4")
            (php_dir / "config/shared-extensions.txt").write_text("redis,redis\n")
            with self.assertRaisesRegex(ValueError, "Invalid extension list"):
                module.resolve(php_dir, "8.4")

    def test_download_knobs_only_change_fetching(self):
        module = load("configure")
        resolution = module.resolve(REPO / "runtimes/php", "8.4")
        self.assertIn("parallel: 8\n  retry: 3\n", module.craft(resolution))
        self.assertIn("parallel: 2\n  retry: 9\n", module.craft(resolution, 2, 9))
        import os
        import unittest.mock
        with unittest.mock.patch.dict(os.environ, {"SPC_DOWNLOAD_PARALLEL": "1", "SPC_DOWNLOAD_RETRY": "0"}):
            self.assertIn("parallel: 1\n  retry: 0\n", module.craft(resolution))
        for name, value in (("SPC_DOWNLOAD_PARALLEL", "0"), ("SPC_DOWNLOAD_PARALLEL", "many"), ("SPC_DOWNLOAD_RETRY", "-1")):
            with self.subTest(name=name, value=value), unittest.mock.patch.dict(os.environ, {name: value}), self.assertRaises(ValueError):
                module.craft(resolution)

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

    def test_omitted_version_builds_default_and_explicit_version_is_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            php_dir = root / "runtimes/php"
            (php_dir / "scripts").mkdir(parents=True)
            for name in ("build.sh", "configure.py"):
                shutil.copy2(SCRIPTS / name, php_dir / "scripts" / name)
            shutil.copytree(REPO / "runtimes/php/config", php_dir / "config")
            spc = root / "spc"
            spc.write_text(f'#!{sys.executable} -I\nimport pathlib, sys\nsys.stdout.write((pathlib.Path.cwd() / "resolution.json").read_text())\nsys.exit(42)\n')
            spc.chmod(0o755)
            provenance = root / "provenance.json"
            provenance.write_text("{}")
            env = {**os.environ, "SPC_BIN": str(spc), "SPC_PROVENANCE": str(provenance), "PHP_VERSION": "8.4"}
            for arguments, expected in [([], "8.4"), (["8.4"], "8.4"), (["8.3.10"], "8.3.10"), (["8.5"], "8.5")]:
                with self.subTest(arguments=arguments):
                    result = subprocess.run([str(php_dir / "scripts/build.sh"), *arguments], text=True, capture_output=True, env=env)
                    self.assertEqual(result.returncode, 42, result.stderr)
                    resolution = json.loads(result.stdout)
                    self.assertEqual(resolution["version"], expected)
                    self.assertEqual(resolution["target"], "musl-dynamic")
                    self.assertNotIn("profile", resolution)

    def test_extra_or_unsupported_arguments_fail_before_spc(self):
        for arguments in (["8.4", "8.3"], ["8.1"], ["-glbic"], ["musl"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([str(REPO / "build.sh"), "php", *arguments], text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("PKGROOT=", result.stdout)

    def test_fresh_build_reuses_prepared_toolchain_path(self):
        # A contract test must not depend on existing real bundles under repo/dist.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            php_dir = root / "runtimes/php"
            (php_dir / "scripts").mkdir(parents=True)
            for name in ("build.sh", "configure.py"):
                shutil.copy2(SCRIPTS / name, php_dir / "scripts" / name)
            shutil.copytree(REPO / "runtimes/php/config", php_dir / "config")
            spc = root / "spc"
            spc.write_text('#!/bin/sh\nprintf "PKGROOT=%s\\nWORK=%s\\n" "$PKG_ROOT_PATH" "$PWD"\nexit 42\n')
            spc.chmod(0o755)
            provenance = root / "provenance.json"
            provenance.write_text("{}")
            env = {**os.environ, "SPC_BIN": str(spc), "SPC_PROVENANCE": str(provenance)}
            env.pop("PKG_ROOT_PATH", None)
            result = subprocess.run([str(php_dir / "scripts/build.sh"), "8.4"], text=True, capture_output=True, env=env)
            self.assertEqual(result.returncode, 42, result.stderr)
            self.assertIn(f"PKGROOT={php_dir}/.cache/pkgroot/x86_64-linux\n", result.stdout)
            self.assertIn(f"WORK={php_dir}/.build.", result.stdout)
            bootstrap = (SCRIPTS / "download-spc.sh").read_text()
            self.assertIn('export PKG_ROOT_PATH=', bootstrap)

    def test_shared_extension_configure_inherits_no_rpath(self):
        """phpize configure needs PHP_RPATH=no, not the main --disable-rpath."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            php_dir = root / "runtimes/php"
            (php_dir / "scripts").mkdir(parents=True)
            for name in ("build.sh", "configure.py"):
                shutil.copy2(SCRIPTS / name, php_dir / "scripts" / name)
            shutil.copytree(REPO / "runtimes/php/config", php_dir / "config")
            spc = root / "spc"
            spc.write_text('#!/bin/sh\nprintf "PHP_RPATH=%s\\nSPC_TARGET=%s\\n" "$PHP_RPATH" "$SPC_TARGET"\nexit 42\n')
            spc.chmod(0o755)
            provenance = root / "provenance.json"
            provenance.write_text("{}")
            for inherited in (None, "yes"):
                with self.subTest(inherited=inherited):
                    env = {**os.environ, "SPC_BIN": str(spc), "SPC_PROVENANCE": str(provenance)}
                    env.pop("PHP_RPATH", None)
                    if inherited is not None:
                        env["PHP_RPATH"] = inherited
                    result = subprocess.run([str(php_dir / "scripts/build.sh"), "8.4"], text=True, capture_output=True, env=env)
                    self.assertEqual(result.returncode, 42, result.stderr)
                    self.assertIn("PHP_RPATH=no\n", result.stdout)
                    self.assertIn("SPC_TARGET=x86_64-linux-musl -dynamic\n", result.stdout)

    def test_build_doctor_never_modifies_host(self):
        script = (SCRIPTS / "build.sh").read_text()
        self.assertIn('doctor --auto-fix=never', script)
        self.assertNotIn('"$SPC_BIN" doctor\n', script)

    def test_loader_is_built_out_of_tree_and_tolerates_no_soname(self):
        """musl's Makefile never sets -soname; the loader matches libc.so by name."""
        source = (SCRIPTS / "musl_loader.py").read_text()
        self.assertIn("build.mkdir(parents=True, exist_ok=False)", source)
        self.assertIn('if re.search(r"\\((?:NEEDED|RPATH|RUNPATH)\\)", dynamic):', source)
        self.assertNotIn('must carry SONAME libc.so', source)
        self.assertIn('"memcpy", "memset", "__stack_chk_fail", "__libc_start_main", "_dlstart"', source)
        self.assertIn("toolchain_musl_version(zig_dir)", source)

    def test_loader_metadata_records_the_name_it_satisfies(self):
        module = load("manifest")
        self.assertIsNotNone(module, "module staging is not implemented")
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "bundle"
            (bundle / "lib/musl").mkdir(parents=True)
            (bundle / "share/licenses").mkdir(parents=True)
            loader = Path(tmp) / "libc.so"
            loader.write_bytes(b"loader fixture")
            notice = Path(tmp) / "COPYRIGHT"
            notice.write_text("musl fixture notice")
            metadata = {"source": str(loader), "pin": {"version": "1.2.5"},
                        "loader": {"sha256": hashlib.sha256(loader.read_bytes()).hexdigest(),
                                   "version": "1.2.5", "soname": None},
                        "license": {"path": str(notice), "sha256": hashlib.sha256(notice.read_bytes()).hexdigest()},
                        "toolchain_musl_version": "1.2.5", "compiler": "cc"}
            record = module.stage_loader(bundle, metadata)
            self.assertIsNone(record["soname"])
            self.assertEqual(record["satisfies"], ["libc.so"])

    def test_stage_records_loader_and_relative_paths(self):
        module = load("manifest")
        self.assertIsNotNone(module, "module staging is not implemented")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            buildroot = root / "buildroot"
            (buildroot / "bin").mkdir(parents=True)
            for name in ("php", "php-fpm"):
                shutil.copy2("/usr/bin/php", buildroot / "bin" / name)
            (buildroot / "build-manifest.json").write_text(json.dumps({"target": {"libc": "musl"}, "targets": {"php": {"extensions": {"static": ["pdo"], "shared": [], "all": ["pdo"]}}}}))
            loader = root / "libc.so"
            loader.write_bytes(b"loader fixture")
            license_notice = root / "COPYRIGHT"
            license_notice.write_text("musl fixture notice")
            metadata = {"source": str(loader), "pin": {"version": "1.2.5"},
                        "loader": {"sha256": hashlib.sha256(loader.read_bytes()).hexdigest(), "version": "1.2.5", "soname": "libc.so"},
                        "license": {"path": str(license_notice), "sha256": hashlib.sha256(license_notice.read_bytes()).hexdigest()},
                        "toolchain_musl_version": "1.2.5", "compiler": "cc"}
            bundle = root / "staged bundle"
            resolution = {"version": "8.4", "target": "musl-dynamic", "static": ["pdo"], "shared": []}
            manifest = module.stage(REPO / "runtimes/php", bundle, resolution, {"commit": "fixture"}, REPO, buildroot, None, metadata)
            self.assertEqual(manifest["resolved_modes"]["static"], ["pdo"])
            self.assertEqual(manifest["verification"], "NOT_RUN")
            self.assertEqual(manifest["target"], "musl-dynamic")
            self.assertEqual(manifest["linkage"], "dynamic")
            self.assertEqual(manifest["libc"], {"family": "musl", "minimum_version": None})
            self.assertEqual(manifest["loader"]["path"], "lib/musl/ld-musl-x86_64.so.1")
            self.assertTrue((bundle / "lib/musl/ld-musl-x86_64.so.1").is_file())
            self.assertTrue((bundle / "share/licenses/musl.txt").is_file())
            self.assertTrue((bundle / "bin/php-ext").is_file())
            self.assertTrue((bundle / "spc-build-manifest.json").is_file())
            for path in manifest["paths"].values():
                self.assertFalse(Path(path).is_absolute())
            launcher = (bundle / "bin/php").read_text()
            self.assertIn('exec "$ROOT/lib/musl/ld-musl-x86_64.so.1" --library-path "$ROOT/lib/musl" "$ROOT/libexec/php"', launcher)
            self.assertNotIn(str(bundle), (bundle / "etc/php.ini").read_text())
            self.assertFalse((bundle / "etc/php-fpm.conf").exists())
            with self.assertRaises(FileExistsError):
                module.stage(REPO / "runtimes/php", bundle, resolution, {}, REPO, buildroot, None, metadata)

    def test_stage_refuses_a_static_or_unreported_target(self):
        module = load("manifest")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "musl-dynamic"):
                module.stage(REPO / "runtimes/php", root / "bundle", {"version": "8.4", "target": "musl", "static": [], "shared": []}, {}, REPO)
            with self.assertRaisesRegex(ValueError, "loader"):
                module.stage(REPO / "runtimes/php", root / "bundle", {"version": "8.4", "target": "musl-dynamic", "static": [], "shared": []}, {}, REPO)

    def test_package_preserves_permissions_and_checksum(self):
        module = load("package")
        self.assertIsNotNone(module, "archive packaging is not implemented")
        with tempfile.TemporaryDirectory(prefix="archive spaces ") as tmp:
            root = Path(tmp)
            bundle = root / "bundle"
            (bundle / "bin").mkdir(parents=True)
            (bundle / "bin/php").write_text("#!/bin/sh\nexit 0\n")
            (bundle / "bin/php").chmod(0o755)
            (bundle / "manifest.json").write_text(json.dumps({"php": {"version": "8.4.27"}, "target": "musl-dynamic", "architecture": "x86_64", "build_id": "fixture123"}))
            archive = module.package(bundle, root / "assets")
            self.assertIn("8.4.27-musl-dynamic-x86_64-fixture123", archive.name)
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
