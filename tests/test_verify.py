"""Runtime verifier contracts, including real preinstalled PHP CLI/FPM tests."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from test_build import load, REPO


def host_php_with_a_shared_module():
    """Return (php binary, module path) from the host, or (None, None).

    The spawn fixture needs any real PHP CLI that can load a module from a
    directory, not a musl bundle: it exercises the launcher/INI contract. Ask
    the binary where it lives and where its extensions live instead of
    hardcoding a distro path, so the contract runs on Fedora, Debian and the CI
    runner alike. Setup-php on CI puts a shim on PATH, so resolve through
    PHP_BINARY rather than assuming the PATH entry is the interpreter.
    """
    entry = shutil.which("php")
    if entry is None:
        return None, None
    try:
        binary = subprocess.run([entry, "-n", "-r", "echo PHP_BINARY;"],
                                text=True, capture_output=True, check=True, timeout=120).stdout.strip()
        with open(binary, "rb") as handle:
            if handle.read(4) != b"\x7fELF":
                return None, None
    except (OSError, subprocess.SubprocessError):
        return None, None
    # Distros disagree on whether the compile-time default or the packaged INI
    # names the module directory, and a PATH shim's INI is invisible to a bare
    # ELF, so try both binaries with and without INI loading.
    for command in (entry, binary):
        for extra in ([], ["-n"]):
            try:
                probed = subprocess.run([command, *extra, "-r", "echo ini_get('extension_dir');"],
                                        text=True, capture_output=True, check=True, timeout=120)
            except (OSError, subprocess.SubprocessError):
                continue
            module = Path(probed.stdout.strip()) / "opcache.so"
            if module.is_file():
                return binary, module
    return None, None


HOST_PHP, HOST_PHP_MODULE = host_php_with_a_shared_module()


class VerifyContracts(unittest.TestCase):
    def test_builtin_modules_are_always_expected_present(self):
        module = load("verify")
        self.assertEqual(module.expected_states([], [], True), {})
        self.assertEqual(module.expected_states(["redis"], [], False), {"redis": False})
        self.assertEqual(module.expected_states(["redis"], ["opcache"], False), {"redis": False, "opcache": True})
        self.assertEqual(module.expected_states(["redis"], ["opcache"], True), {"redis": True, "opcache": True})

    def test_builtin_modules_need_no_module_file(self):
        module = load("verify")
        shared = [{"name": "redis", "path": "lib/php/extensions/redis.so"}]
        builtin = [{"name": "opcache", "runtime_name": "Zend OPcache", "directive": None, "always_loaded": True}]
        required, probed, states = module.module_plan({"shared_modules": shared, "builtin_modules": builtin})
        self.assertEqual([row["name"] for row in required], ["redis"])
        self.assertEqual(sorted(probed), ["opcache", "redis"])
        self.assertEqual(states, {"redis": False, "opcache": True})
        self.assertEqual(module.module_plan({"shared_modules": shared})[1], ["redis"])
        for defect in ({"name": "redis", "runtime_name": "redis", "directive": None, "always_loaded": True},
                       {"name": "opcache", "runtime_name": "Zend OPcache", "directive": "zend_extension", "always_loaded": False},
                       {"name": "opcache"}):
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                module.module_plan({"shared_modules": shared, "builtin_modules": [defect]})
        with self.assertRaises(ValueError):
            module.module_plan({"shared_modules": [{"name": "opcache", "path": "lib/php/extensions/opcache.so"}],
                                "builtin_modules": [builtin[0]]})

    def test_only_the_loader_is_an_allowed_runtime_dependency(self):
        module = load("verify")
        self.assertIsNotNone(module, "runtime verifier is not implemented")
        self.assertEqual(module.parse_needed(' 0x000 (NEEDED) Shared library: [libc.so]\n'), ["libc.so"])
        module.check_dependencies([], "musl-dynamic")
        module.check_dependencies(["libc.so"], "musl-dynamic")
        with self.assertRaisesRegex(ValueError, "libssl"):
            module.check_dependencies(["libc.so", "libssl.so.3"], "musl-dynamic")
        for target in ("glibc", "musl"):
            with self.assertRaises(ValueError):
                module.check_dependencies([], target)

    def test_glibc_abi_tokens_are_rejected(self):
        module = load("verify")
        self.assertEqual(module.check_abi("Name: musl\n"), [])
        for token in ("GLIBC_2.17", "GLIBC_ABI_DT_RELR", "GLIBC_FUTURE_ABI"):
            with self.subTest(token=token), self.assertRaisesRegex(ValueError, token):
                module.check_abi(f"Name: {token}")

    @unittest.skipUnless(shutil.which("cc") and shutil.which("readelf"), "ELF fixture compiler is unavailable")
    def test_foreign_dynamic_binary_fails_elf_inventory(self):
        module = load("verify")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "libexec").mkdir()
            (root / "lib/musl").mkdir(parents=True)
            (root / "lib/php/extensions").mkdir(parents=True)
            (root / "lib/musl/ld-musl-x86_64.so.1").write_bytes(b"loader fixture")
            source = root / "fixture.c"
            source.write_text('extern int puts(const char *); static int value; int *pointer = &value; int probe(void) { return puts("fixture") + *pointer; }\n')
            binary = root / "libexec/php"
            subprocess.run(["cc", "-shared", "-fPIC", "-Wl,-z,pack-relative-relocs", str(source), "-o", str(binary)], check=True, capture_output=True)
            shutil.copy2(binary, root / "libexec/php-fpm")
            dynamic = subprocess.check_output(["readelf", "-d", str(binary)], text=True)
            self.assertIn("(RELR)", dynamic)
            # A host-toolchain binary requests glibc and must never be accepted as musl.
            with self.assertRaisesRegex(ValueError, "Unbundled runtime dependencies"):
                module.elf_inventory(root, "musl-dynamic")

    def test_elf_inventory_requires_the_bundled_loader(self):
        module = load("verify")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "libexec").mkdir()
            (root / "lib/php/extensions").mkdir(parents=True)
            shutil.copy2("/usr/bin/php", root / "libexec/php")
            shutil.copy2("/usr/bin/php", root / "libexec/php-fpm")
            with self.assertRaisesRegex(ValueError, "loader"):
                module.elf_inventory(root, "musl-dynamic")

    def test_fastcgi_record_framing(self):
        module = load("verify")
        self.assertIsNotNone(module, "runtime verifier is not implemented")
        record = module.record(1, b"12345678")
        self.assertEqual(record[:8], bytes([1, 1, 0, 1, 0, 8, 0, 0]))
        self.assertEqual(record[8:], b"12345678")

    @unittest.skipUnless(Path("/usr/bin/php").is_file(), "system PHP CLI is unavailable")
    def test_empty_shared_set_cli_map_is_an_object(self):
        module = load("verify")
        self.assertIsNotNone(module)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            (root / "bin/php").symlink_to("/usr/bin/php")
            self.assertEqual(module.cli_modules(root, [], no_ini=True), {})

    @unittest.skipUnless(Path("/usr/bin/php").is_file(), "system PHP CLI is unavailable")
    def test_real_cli_scan_and_relocation_without_system_ini(self):
        packaging = REPO / "runtimes/php/packaging"
        self.assertTrue((packaging / "php").exists())
        with tempfile.TemporaryDirectory(prefix="real PHP spaces ") as tmp:
            bundle = Path(tmp) / "version one"
            for directory in ("bin", "libexec", "etc/cli/conf.d", "etc/fpm/conf.d", "lib/php/extensions"):
                (bundle / directory).mkdir(parents=True, exist_ok=True)
            shutil.copy2("/usr/bin/php", bundle / "libexec/php")
            shutil.copy2(packaging / "php", bundle / "bin/php")
            (bundle / "bin/php").chmod(0o755)
            shutil.copy2(packaging / "launch.sh", bundle / "libexec/launch.sh")
            (bundle / "etc/php.ini").write_text("memory_limit=128M\n")
            (bundle / "etc/cli/conf.d/10-test.ini").write_text("memory_limit=321M\n")
            second = Path(tmp) / "version two"
            shutil.copytree(bundle, second)
            (second / "etc/cli/conf.d/10-test.ini").write_text("memory_limit=654M\n")
            for root, expected in ((bundle, "321M"), (second, "654M")):
                result = subprocess.run([str(root / "bin/php"), "-r", 'echo ini_get("memory_limit")."|".ini_get("extension_dir");'], cwd="/", env={**os.environ, "PHP_INI_SCAN_DIR": "/etc/php.d"}, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f"{expected}|{root}/lib/php/extensions")

    @unittest.skipUnless(Path("/opt/remi/php84/root/usr/lib64/php/modules/opcache.so").is_file() and Path("/opt/remi/php84/root/usr/sbin/php-fpm").is_file() and os.getuid() != 0, "preinstalled Zend/FPM runtime is unavailable")
    def test_real_zend_toggle_cli_and_fpm_requests(self):
        module = load("verify")
        self.assertIsNotNone(module, "runtime verifier is not implemented")
        packaging = REPO / "runtimes/php/packaging"
        with tempfile.TemporaryDirectory(prefix="zend-test-") as tmp:
            root = Path(tmp) / "PHP with spaces"
            for directory in ("bin", "libexec", "etc/cli/conf.d", "etc/fpm/conf.d", "lib/php/extensions"):
                (root / directory).mkdir(parents=True, exist_ok=True)
            for name, source in (("php", "/usr/bin/php"), ("php-fpm", "/opt/remi/php84/root/usr/sbin/php-fpm")):
                shutil.copy2(source, root / "libexec" / name)
            for name in ("php", "php-fpm", "php-ext"):
                shutil.copy2(packaging / name, root / "bin" / name)
                (root / "bin" / name).chmod(0o755)
            shutil.copy2(packaging / "launch.sh", root / "libexec/launch.sh")
            shutil.copy2("/opt/remi/php84/root/usr/lib64/php/modules/opcache.so", root / "lib/php/extensions/opcache.so")
            (root / "etc/php.ini").write_text("opcache.enable_cli=1\n")
            (root / "etc/shared-modules.tsv").write_text("opcache\tzend_extension\topcache.so\n")
            # The production verifier receives manifest/INI basenames, not PHP's display name.
            names = ["opcache"]
            self.assertEqual(module.cli_modules(root, names, no_ini=True), {"opcache": False})
            for enabled in (True, False):
                action = "enable" if enabled else "disable"
                subprocess.run([str(root / "bin/php-ext"), action, "opcache", "all"], check=True)
                self.assertEqual(module.cli_modules(root, names), {"opcache": enabled})
                self.assertEqual(module.fpm_probe(root, names, Path(tmp))["modules"], {"opcache": enabled})

    @unittest.skipUnless(HOST_PHP and HOST_PHP_MODULE, "host PHP CLI with a shared module is unavailable")
    def test_spawned_child_inherits_the_bundle_extension_dir(self):
        """The INI, not the launcher's argv, must locate extensions for children.

        A framework resolves PHP through `PHP_BINARY` (the real ELF) and inherits
        only the environment, so a bundle that relies on `-d` alone loses every
        shared module one process down.
        """
        module = load("verify")
        self.assertIsNotNone(module, "runtime verifier is not implemented")
        packaging = REPO / "runtimes/php/packaging"
        with tempfile.TemporaryDirectory(prefix="spawn probe ") as tmp:
            root = Path(tmp) / "runtime with spaces"
            for directory in ("bin", "libexec", "etc/cli/conf.d", "etc/fpm/conf.d", "lib/php/extensions"):
                (root / directory).mkdir(parents=True, exist_ok=True)
            shutil.copy2(HOST_PHP, root / "libexec/php")
            shutil.copy2(packaging / "php", root / "bin/php")
            (root / "bin/php").chmod(0o755)
            shutil.copy2(packaging / "launch.sh", root / "libexec/launch.sh")
            source = (REPO / "runtimes/php/config/php.ini").read_text()
            (root / "etc/php.ini").write_text(source)
            shutil.copy2(HOST_PHP_MODULE, root / "lib/php/extensions/opcache.so")
            shared = [{"name": "opcache", "directive": "zend_extension", "path": "lib/php/extensions/opcache.so"}]
            report = module.check_spawned_child_environment(root, shared)
            self.assertEqual(report["extension_dir"], str(root / "lib/php/extensions"))
            self.assertEqual(report["probed_module"], "opcache")
            # Without the exported root the same INI must fail loudly, not fall
            # back to the compile-time directory.
            (root / "libexec/launch.sh").write_text("#!/bin/sh\nexport PHPRC=\"$ROOT/etc/php.ini\"\n")
            with self.assertRaisesRegex(ValueError, "extension_dir"):
                module.check_spawned_child_environment(root, [])

    @unittest.skipUnless(Path("/opt/remi/php84/root/usr/sbin/php-fpm").is_file() and os.getuid() != 0, "preinstalled non-root FPM is unavailable")
    def test_real_fpm_serves_request_as_caller(self):
        module = load("verify")
        self.assertIsNotNone(module, "runtime verifier is not implemented")
        packaging = REPO / "runtimes/php/packaging"
        with tempfile.TemporaryDirectory(prefix="real FPM spaces ") as tmp:
            root = Path(tmp) / "runtime"
            for directory in ("bin", "libexec", "etc/cli/conf.d", "etc/fpm/conf.d", "lib/php/extensions"):
                (root / directory).mkdir(parents=True, exist_ok=True)
            for name, source in (("php", "/usr/bin/php"), ("php-fpm", "/opt/remi/php84/root/usr/sbin/php-fpm")):
                shutil.copy2(source, root / "libexec" / name)
                shutil.copy2(packaging / name, root / "bin" / name)
                (root / "bin" / name).chmod(0o755)
            shutil.copy2(packaging / "launch.sh", root / "libexec/launch.sh")
            (root / "etc/php.ini").write_text("memory_limit=128M\n")
            (root / "etc/fpm/conf.d/10-test.ini").write_text("memory_limit=777M\n")
            response = module.fpm_probe(root, [], Path(tmp))
            self.assertEqual(response["memory_limit"], "777M")
            self.assertEqual(response["uid"], os.getuid())
            self.assertEqual(response["extension_dir"], str(root / "lib/php/extensions"))


if __name__ == "__main__":
    unittest.main()
