"""Installed shell contracts; recorder binaries do not validate PHP/SPC builds."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
PACKAGING = REPO / "runtimes/php/packaging"


class LauncherContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="php contracts ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "installed version one"
        for directory in ("bin", "libexec", "etc/cli/conf.d", "etc/fpm/conf.d", "lib/php/extensions"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.available = all((PACKAGING / name).exists() for name in ("php", "php-fpm", "php-ext", "launch.sh"))
        if self.available:
            for name in ("php", "php-fpm", "php-ext"):
                shutil.copy2(PACKAGING / name, self.root / "bin" / name)
                (self.root / "bin" / name).chmod(0o755)
            shutil.copy2(PACKAGING / "launch.sh", self.root / "libexec/launch.sh")
        (self.root / "etc/php.ini").write_text("[PHP]\ndate.timezone=UTC\n")
        (self.root / "etc/shared-modules.tsv").write_text("redis\textension\tredis.so\nopcache\tzend_extension\topcache.so\n")
        for name in ("redis", "opcache"):
            (self.root / f"lib/php/extensions/{name}.so").touch()
        recorder = '#!/bin/sh\nprintf "SCAN=%s\\nPHPRC=%s\\n" "$PHP_INI_SCAN_DIR" "$PHPRC"\nfor arg do printf "ARG=%s\\n" "$arg"; done\n'
        for name in ("php", "php-fpm"):
            (self.root / "libexec" / name).write_text(recorder)
            (self.root / "libexec" / name).chmod(0o755)

    def run_bin(self, name, *args, env=None):
        self.assertTrue(self.available, "installed launchers have not been implemented")
        return subprocess.run([str(self.root / "bin" / name), *args], cwd="/", env={**os.environ, **(env or {})}, text=True, capture_output=True)

    def test_cli_resolves_spaces_and_preserves_arguments(self):
        result = self.run_bin("php", "-r", 'echo "a b";', "--", "argument with spaces", env={"PHP_INI_SCAN_DIR": "/unrelated", "PHPRC": "/other"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"SCAN={self.root}/etc/cli/conf.d\n", result.stdout)
        self.assertIn(f"PHPRC={self.root}/etc/php.ini\n", result.stdout)
        self.assertIn(f"ARG=extension_dir={self.root}/lib/php/extensions\n", result.stdout)
        self.assertIn('ARG=echo "a b";\nARG=--\nARG=argument with spaces\n', result.stdout)

    def test_relocated_second_bundle_is_isolated(self):
        self.assertTrue(self.available, "installed launchers have not been implemented")
        second = Path(self.tmp.name) / "second php version"
        shutil.copytree(self.root, second)
        result = subprocess.run([str(second / "bin/php"), "-n", "-m"], cwd="/", text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"SCAN={second}/etc/cli/conf.d", result.stdout)
        self.assertNotIn(str(self.root), result.stdout)
        self.assertIn("ARG=-n\nARG=-m", result.stdout)

    def test_module_toggle_is_sapi_specific(self):
        result = self.run_bin("php-ext", "enable", "redis", "cli")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "etc/cli/conf.d/20-redis.ini").read_text(), "extension=redis.so\n")
        self.assertFalse((self.root / "etc/fpm/conf.d/20-redis.ini").exists())
        result = self.run_bin("php-ext", "disable", "redis", "all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "etc/cli/conf.d/20-redis.ini").exists())

    def test_zend_module_uses_zend_directive(self):
        result = self.run_bin("php-ext", "enable", "opcache", "fpm")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "etc/fpm/conf.d/20-opcache.ini").read_text(), "zend_extension=opcache.so\n")

    def test_rejects_static_missing_or_invalid_modules(self):
        for name in ("pdo", "xdebug", "../redis", "redis.so"):
            with self.subTest(name=name):
                self.assertNotEqual(self.run_bin("php-ext", "enable", name).returncode, 0)
        (self.root / "lib/php/extensions/redis.so").unlink()
        self.assertNotEqual(self.run_bin("php-ext", "enable", "redis").returncode, 0)

    def test_list_reports_actual_enabled_state(self):
        self.run_bin("php-ext", "enable", "redis", "cli")
        result = self.run_bin("php-ext", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("redis\textension\tcli=enabled\tfpm=disabled", result.stdout)

    def test_fpm_requires_explicit_config(self):
        result = self.run_bin("php-fpm", "-t")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("config", result.stderr.lower())

    @unittest.skipIf(os.getuid() == 0, "non-root FPM launch contract")
    def test_fpm_uses_own_scan_dir_and_caller_config(self):
        config = self.root / "caller fpm.conf"
        config.write_text("[global]\ndaemonize=no\n")
        result = self.run_bin("php-fpm", "-t", env={"DEVSTACK_FPM_CONFIG": str(config)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"SCAN={self.root}/etc/fpm/conf.d", result.stdout)
        self.assertIn(f"ARG=-y\nARG={config}", result.stdout)
        result = self.run_bin("php-fpm", "--fpm-config=" + str(config), "-t")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ARG=--fpm-config=" + str(config), result.stdout)

    def test_repo_helper_delegates_to_installed_helper(self):
        self.assertTrue(self.available, "installed launchers have not been implemented")
        result = subprocess.run([str(REPO / "runtimes/php/scripts/ext.sh"), "enable", str(self.root), "redis", "cli"], cwd="/", text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "etc/cli/conf.d/20-redis.ini").exists())


if __name__ == "__main__":
    unittest.main()
