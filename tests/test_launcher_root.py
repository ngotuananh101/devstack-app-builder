"""Every launcher derives ROOT from its real location, even through symlinks.

Devstack's "add to PATH" writes shims outside the bundle. When that shim is a
symlink, `$0` names the symlink, not the launcher, so a launcher that resolves
only `dirname "$0"` looks for libexec/ one level above the wrong directory. The
launchers must follow the link chain before taking their root.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]

# One entry per installed entry point, across all three runtimes.
LAUNCHERS = {
    "php": REPO / "runtimes/php/packaging/php",
    "php-fpm": REPO / "runtimes/php/packaging/php-fpm",
    "php-ext": REPO / "runtimes/php/packaging/php-ext",
    "postgresql": REPO / "runtimes/postgresql/packaging/launcher.in",
    "apache-launcher": REPO / "runtimes/apache/packaging/launcher",
    "apachectl": REPO / "runtimes/apache/packaging/apachectl",
}

ROOT_LINE = "ROOT=$("


def root_probe(source: Path) -> str:
    """The launcher's own preamble, ending in a print of ROOT.

    Everything after the root resolution is packaging-specific (exec lines,
    placeholders, FPM argument handling); resolving the root is the part that
    must work the same way everywhere.
    """
    lines = source.read_text().splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith(ROOT_LINE):
            return "".join(lines[: index + 1]) + "printf '%s\\n' \"$ROOT\"\n"
    raise AssertionError(f"{source} no longer assigns ROOT from its own path")


class LauncherRootResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="launcher root ")
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self.bundle = self.workspace / "runtime bundle with spaces"
        (self.bundle / "bin").mkdir(parents=True)
        (self.bundle / "libexec").mkdir()

    def install(self, name: str) -> Path:
        source = LAUNCHERS[name]
        self.assertTrue(source.is_file(), f"missing launcher template: {source}")
        installed = self.bundle / "bin" / source.name.replace(".in", "")
        installed.write_text(root_probe(source))
        installed.chmod(0o755)
        return installed

    def probe(self, launcher: Path) -> str:
        result = subprocess.run([str(launcher)], cwd="/", text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_launcher_run_directly_resolves_its_bundle(self):
        for name in LAUNCHERS:
            with self.subTest(launcher=name):
                self.assertEqual(self.probe(self.install(name)), f"{self.bundle}\n")

    def test_launcher_reached_through_a_path_shim_symlink_resolves_its_bundle(self):
        # The devstack shim: ~/.ponta/bin/php -> <bundle>/bin/php.
        shims = self.workspace / "ponta bin"
        shims.mkdir()
        for name in LAUNCHERS:
            with self.subTest(launcher=name):
                launcher = self.install(name)
                shim = shims / launcher.name
                if shim.exists() or shim.is_symlink():
                    shim.unlink()
                shim.symlink_to(launcher)
                self.assertEqual(self.probe(shim), f"{self.bundle}\n")

    def test_launcher_reached_through_a_chain_of_symlinks_resolves_its_bundle(self):
        launcher = self.install("php")
        first = self.workspace / "first link"
        second = self.workspace / "second link"
        first.symlink_to(launcher)
        second.symlink_to(first)
        self.assertEqual(self.probe(second), f"{self.bundle}\n")

    def test_launcher_reached_through_a_relative_symlink_resolves_its_bundle(self):
        self.install("php")
        relative = self.workspace / "relative link"
        relative.symlink_to(Path("runtime bundle with spaces") / "bin" / "php")
        self.assertEqual(self.probe(relative), f"{self.bundle}\n")

    def test_resolution_loop_is_bounded(self):
        # The kernel already refuses to exec a cycle (ELOOP), so the loop cannot
        # be driven from a test. What has to hold is that the loop is bounded and
        # that exceeding the bound is a refusal, not a hang.
        for name, source in LAUNCHERS.items():
            with self.subTest(launcher=name):
                text = source.read_text()
                self.assertIn('-lt 40', text)
                self.assertIn('too deep', text.lower())


if __name__ == "__main__":
    unittest.main()
