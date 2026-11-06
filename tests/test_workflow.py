"""Release/build workflow policy: no implicit publication or raw directory upload."""
from pathlib import Path
import unittest
from test_build import REPO


import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import textwrap
from test_build import load


class WorkflowContracts(unittest.TestCase):
    def test_release_validator_rejects_tampered_or_unverified_archives(self):
        packager = load("package")
        publisher = load("publish")
        self.assertIsNotNone(publisher)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "bundle"
            for directory in ("bin", "libexec"):
                (bundle / directory).mkdir(parents=True)
            for executable in ("bin/php", "bin/php-fpm", "bin/php-ext", "libexec/php", "libexec/php-fpm"):
                path = bundle / executable
                path.write_text("#!/bin/sh\nexit 0\n")
                path.chmod(0o755)
            manifest = {"schema_version": 1, "runtime": "php", "php": {"version": "8.4.27"}, "target": "glibc", "profile": "full", "architecture": "x86_64", "build_id": "test", "verification": "PASSED"}
            (bundle / "manifest.json").write_text(json.dumps(manifest))
            archive = packager.package(bundle, root / "assets")
            self.assertEqual(len(publisher.validate_assets(root / "assets")), 3)
            checksum = archive.with_name(archive.name + ".sha256")
            checksum.write_text("0" * 64 + "  " + archive.name + "\n")
            with self.assertRaisesRegex(ValueError, "checksum"):
                publisher.validate_assets(root / "assets")
            checksum.write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n")
            sidecar = archive.with_name(archive.name[:-7] + ".manifest.json")
            manifest["verification"] = "NOT_RUN"
            sidecar.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "unverified"):
                publisher.validate_assets(root / "assets")
            manifest["verification"] = "PASSED"
            manifest["profile"] = "minimal"
            sidecar.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "manifest differ"):
                publisher.validate_assets(root / "assets")

    def test_release_validator_rejects_invalid_archive_structure(self):
        publisher = load("publish")
        # Hand-authored tar fixtures exercise validation, not the packager's naming logic.
        identity = "php-8.4.27-glibc-full-x86_64-test"
        manifest = {"schema_version": 1, "runtime": "php", "php": {"version": "8.4.27"}, "target": "glibc", "profile": "full", "architecture": "x86_64", "build_id": "test", "verification": "PASSED"}
        executables = ("bin/php", "bin/php-fpm", "bin/php-ext", "libexec/php", "libexec/php-fpm")
        for defect in ("directories", "split_roots", "wrong_identity", "missing_identity", "duplicate", "special"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                name = "other" if defect == "wrong_identity" else identity
                archive = root / (name + ".tar.gz")
                data = {"verification": "PASSED"} if defect == "missing_identity" else manifest
                with tarfile.open(archive, "w:gz") as tar:
                    member = tarfile.TarInfo(identity + "/manifest.json")
                    payload = json.dumps(data).encode()
                    member.size = len(payload)
                    tar.addfile(member, io.BytesIO(payload))
                    for executable in executables:
                        prefix = "different-root" if defect == "split_roots" else identity
                        member = tarfile.TarInfo(prefix + "/" + executable)
                        member.mode = 0o755
                        if defect == "directories":
                            member.type = tarfile.DIRTYPE
                            tar.addfile(member)
                        else:
                            payload = b"#!/bin/sh\nexit 0\n"
                            member.size = len(payload)
                            tar.addfile(member, io.BytesIO(payload))
                    if defect in {"duplicate", "special"}:
                        member = tarfile.TarInfo(identity + ("/bin/php" if defect == "duplicate" else "/device"))
                        member.mode = 0o755
                        if defect == "special":
                            member.type = tarfile.FIFOTYPE
                        tar.addfile(member)
                (root / (name + ".manifest.json")).write_text(json.dumps(data))
                archive.with_name(archive.name + ".sha256").write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n")
                with self.assertRaises(ValueError):
                    publisher.validate_assets(root)

    def test_scheduled_and_manual_matrices_default_to_full(self):
        workflow = (REPO / ".github/workflows/build.yml").read_text()
        manual = re.search(r"^      profile:\n(?P<config>(?:^        .*\n)+)", workflow, re.MULTILINE)
        self.assertIsNotNone(manual)
        manual_default = re.search(r"^        default: (\w+)$", manual["config"], re.MULTILINE)[1]
        schedule_default = re.search(r"PROFILE_INPUT: \$\{\{ inputs\.profile \|\| '(\w+)' \}\}", workflow)[1]
        plan = workflow.split("  plan:\n", 1)[1].split("  build-php:\n", 1)[0]
        command = textwrap.dedent(plan.split("        run: |\n", 1)[1])
        for event, profile, expected in (("schedule", schedule_default, "full"), ("manual-default", manual_default, "full"), ("manual-minimal", "minimal", "minimal")):
            with self.subTest(event=event), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "output"
                env = {**os.environ, "PHP_VERSION_INPUT": "all", "TARGET_INPUT": "both", "PROFILE_INPUT": profile, "PUBLISH": "false", "RELEASE_TAG": "", "GITHUB_OUTPUT": str(output)}
                result = subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                matrix = json.loads(output.read_text().removeprefix("matrix="))
                self.assertEqual(matrix["profile"], [expected])
                self.assertEqual(matrix["php_version"], ["8.5", "8.4", "8.3", "8.2"])
                self.assertEqual(matrix["target"], ["glibc", "musl"])

    def test_ci_uploads_verified_archives_and_exposes_full(self):
        workflow = (REPO / ".github/workflows/build.yml").read_text()
        self.assertIn("python3 -I -m unittest discover -s tests -v", workflow)
        self.assertIn("dist/assets/", workflow)
        self.assertIn("options: [minimal, full]", workflow)
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("publish", workflow)
        self.assertIn("inputs.publish", workflow)
        self.assertIn("contents: write", workflow)
        self.assertNotIn("uses: actions/checkout@v4", workflow)
        self.assertNotIn("--clobber", workflow)

    def test_release_assets_require_verified_manifest(self):
        workflow = (REPO / ".github/workflows/build.yml").read_text()
        self.assertIn("publish.py", workflow)
        module_path = REPO / "runtimes/php/scripts/publish.py"
        self.assertTrue(module_path.exists(), "publication validator is not implemented")


if __name__ == "__main__":
    unittest.main()
