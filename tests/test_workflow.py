"""Release/build workflow policy: planned publication and verified archive uploads."""
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
from unittest.mock import patch
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
        plan_job = workflow.split("  plan:\n", 1)[1].split("  build-php:\n", 1)[0]
        command = textwrap.dedent(plan_job.split("        run: |\n", 1)[1].split("      - name:", 1)[0])
        planner = load("updates")
        upstream = {"8.5": "8.5.1", "8.4": "8.4.10", "8.3": "8.3.2", "8.2": "8.2.3"}
        from test_updates import REPOSITORY
        scenarios = (("schedule", schedule_default, "full", False), ("workflow_dispatch", manual_default, "full", False),
                     ("workflow_dispatch", "minimal", "minimal", False), ("schedule", schedule_default, "full", True))
        for event, profile, expected, current in scenarios:
            with self.subTest(event=event, current=current), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                output = root / "output"
                history = {(series, target, "full", "x86_64"): version for series, version in upstream.items() for target in ("glibc", "musl")} if current else {}
                env = {**os.environ, "PHP_VERSION_INPUT": "all", "TARGET_INPUT": "both", "PROFILE_INPUT": profile,
                       "PUBLISH": "false", "RELEASE_TAG": "", "GITHUB_OUTPUT": str(output), "GITHUB_EVENT_NAME": event,
                       "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
                # Execute the workflow's real planner entry point with only network I/O controlled.
                args = __import__("shlex").split(command.strip())
                self.assertEqual(args[:2], ["python3", "-I"])
                self.assertEqual(args[2], "runtimes/php/scripts/updates.py")
                with patch.dict(os.environ, env, clear=True), patch("sys.argv", args[2:]), patch.object(planner, "latest_php", side_effect=upstream.__getitem__), patch.object(planner, "published_versions", return_value=history), patch.object(Path, "cwd", return_value=root):
                    # The CLI's output paths are deliberately rooted in the temporary workspace.
                    argv = args[2:]
                    argv[argv.index("--output") + 1] = str(root / "php-update-plan.json")
                    argv[argv.index("--github-output") + 1] = str(output)
                    with patch("sys.argv", argv):
                        planner.main()
                policy = json.loads((root / "php-update-plan.json").read_text())
                outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
                rows = policy["matrix"]["include"]
                self.assertEqual(outputs["has_updates"], "false" if current else "true")
                self.assertEqual(outputs["publish"], "true" if event == "schedule" and not current else "false")
                self.assertEqual(json.loads(outputs["matrix"]), policy["matrix"])
                self.assertEqual(len(rows), 0 if current else 8)
                if not current:
                    self.assertEqual({row["profile"] for row in rows}, {expected})
                    self.assertEqual({row["php_version"] for row in rows}, {"8.5.1", "8.4.10", "8.3.2", "8.2.3"})
                    self.assertEqual({row["target"] for row in rows}, {"glibc", "musl"})

    def test_noop_matrix_and_publication_are_gated_by_persisted_plan(self):
        workflow = (REPO / ".github/workflows/build.yml").read_text()
        build = workflow.split("  build-php:\n", 1)[1].split("  publish:\n", 1)[0]
        publish = workflow.split("  publish:\n", 1)[1]
        self.assertIn("if: needs.plan.outputs.has_updates == 'true'", build)
        self.assertIn("needs: [plan, build-php]", publish)
        self.assertIn("if: needs.plan.outputs.publish == 'true'", publish)
        self.assertIn("name: runtime-update-plan", publish)
        self.assertIn("--plan dist/plans/php-update-plan.json", publish)
        self.assertIn("group: runtime-build-publication", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("GH_TOKEN: ${{ github.token }}", workflow.split("  plan:\n", 1)[1])

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
