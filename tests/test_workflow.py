"""Release/build workflow policy: planned publication and verified archive uploads."""
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
import unittest
from test_build import REPO


import fnmatch
import hashlib
import io
import itertools
import json
import os
import re
import subprocess
import tarfile
import tempfile
import textwrap
from unittest.mock import patch
from test_build import load


WORKFLOW = REPO / ".github/workflows/build.yml"


def job_block(text, name):
    """Slice one job out of the workflow at its two-space job key.

    Splitting on "build-" instead would cut a job at the `build-essential`
    package name and assert against the wrong region.
    """
    match = re.search(rf"^  {re.escape(name)}:\n", text, re.MULTILINE)
    if match is None:
        raise AssertionError(f"Missing workflow job: {name}")
    following = re.compile(r"^  [a-z][a-z0-9-]*:$", re.MULTILINE).search(text, match.end())
    return text[match.start():following.start() if following else len(text)]


class WorkflowContracts(unittest.TestCase):
    def test_manual_inputs_do_not_allow_profile_target_or_series_narrowing(self):
        workflow = (REPO / ".github/workflows/build.yml").read_text()
        inputs = workflow.split("    inputs:\n", 1)[1].split("\npermissions:", 1)[0]
        names = re.findall(r"^      ([a-z_]+):$", inputs, re.MULTILINE)
        self.assertEqual(set(names), {"bypass", "publish", "release_tag"})
        planner = workflow.split("  plan:\n", 1)[1].split("  build-php:\n", 1)[0]
        self.assertNotIn("inputs.php_version", planner)
        self.assertNotIn("inputs.target", planner)
        self.assertNotIn("PROFILE_INPUT", planner)
        self.assertIn("scripts/updates.py", planner)
        self.assertIn("redis_matrix:", planner)
        self.assertIn("BYPASS: ${{ inputs.bypass || false }}", planner)
        self.assertIn("  build-redis:\n", workflow, "Missing native Redis build job")
        php = job_block(workflow, "build-php")
        self.assertIn('./build.sh php "$BUILD_VERSION"', php)
        self.assertNotIn("BUILD_TARGET", php)
        self.assertNotIn("BUILD_PROFILE", php)
        redis = job_block(workflow, "build-redis")
        self.assertIn('./build.sh redis "$BUILD_VERSION"', redis)
        self.assertNotIn("BUILD_TARGET", redis)
        self.assertNotIn("BUILD_PROFILE", redis)
        self.assertNotIn("docker", redis.lower())
        publish = workflow.split("  publish:\n", 1)[1]
        self.assertIn("needs: [plan, build-php, build-redis, build-postgresql]", publish)
        self.assertIn("always()", publish)
        self.assertIn("needs.build-redis.result", publish)
        self.assertIn("pattern: runtime-*", publish)

    def test_bypass_input_is_manual_only_and_defaults_to_version_checks(self):
        workflow = (REPO / ".github/workflows/build.yml").read_text()
        inputs = workflow.split("    inputs:\n", 1)[1].split("\npermissions:", 1)[0]
        bypass = re.search(r"^      bypass:\n((?:^        .*\n)+)", inputs, re.MULTILINE)[1]
        self.assertIn("type: boolean", bypass)
        self.assertIn("default: false", bypass)
        # A scheduled run has no inputs, so it can never request a bypass.
        schedule = workflow.split("  schedule:\n", 1)[1].split("  workflow_dispatch:", 1)[0]
        self.assertNotIn("bypass", schedule)
        # Bypass only changes planning; build jobs must keep building the planned version.
        for job, following in (("  build-php:\n", "  build-redis:\n"), ("  build-redis:\n", "  publish:\n")):
            section = workflow.split(job, 1)[1].split(following, 1)[0]
            self.assertNotIn("bypass", section.lower())

    def test_php_ci_installs_host_musl_before_spc_preflight(self):
        workflow = (REPO / ".github/workflows/build.yml").read_text()
        php = workflow.split("  build-php:\n", 1)[1].split("  build-redis:\n", 1)[0]
        preparation = php.split("      - name: Install CI-only build dependencies\n", 1)[1].split("      - name:", 1)[0]
        command = textwrap.dedent(preparation.split("        run:", 1)[1]).strip()
        command = command.removeprefix("|").strip()
        import shlex
        commands = [shlex.split(line) for line in command.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        installs = [index for index, argv in enumerate(commands)
                    if argv[:3] == ["sudo", "apt-get", "install"] and "musl" in argv[3:]]
        self.assertEqual(len(installs), 1, "PHP CI needs the host musl interpreter; SPC doctor does not install it")
        update = commands.index(["sudo", "apt-get", "update"])
        loader_check = commands.index(["test", "-x", "/lib/ld-musl-x86_64.so.1"])
        doctor = commands.index(["./runtimes/php/spc", "doctor", "--auto-fix"])
        self.assertLess(update, installs[0])
        self.assertLess(installs[0], loader_check)
        self.assertLess(loader_check, doctor)
        self.assertLess(php.index("Install CI-only build dependencies"), php.index("Build, verify actual CLI/FPM requests"))
        self.assertNotIn("SPC_SKIP_TOOLCHAIN_CHECK", php)

    def test_daily_schedule_leads_catalog_by_more_than_two_hours(self):
        workflow = (REPO / ".github/workflows/build.yml").read_text()
        schedule = workflow.split("  schedule:\n", 1)[1].split("  workflow_dispatch:", 1)[0]
        registrations = re.findall(r'- cron: [\"\']([^\"\']+)[\"\']', schedule)
        self.assertEqual(len(registrations), 1)
        minute, hour, day, month, weekday = registrations[0].split()
        self.assertEqual((day, month, weekday), ("*", "*", "*"))
        scheduled_utc = datetime(2026, 11, 6, int(hour), int(minute), tzinfo=timezone.utc)
        scheduled_local = scheduled_utc.astimezone(timezone(timedelta(hours=7)))
        self.assertEqual(scheduled_local.time(), time(4, 37))
        catalog_local = datetime(2026, 11, 7, 7, 0, tzinfo=scheduled_local.tzinfo)
        self.assertEqual(catalog_local - scheduled_local, timedelta(hours=2, minutes=23))

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
            loader = bundle / "lib/musl/ld-musl-x86_64.so.1"
            loader.parent.mkdir(parents=True)
            loader.write_bytes(b"loader fixture")
            loader.chmod(0o755)
            manifest = {"schema_version": 1, "runtime": "php", "php": {"version": "8.4.27"}, "target": "musl-dynamic",
                        "linkage": "dynamic", "loader": {"path": "lib/musl/ld-musl-x86_64.so.1"}, "architecture": "x86_64",
                        "build_id": "test", "verification": "PASSED"}
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
        identity = "php-8.4.27-musl-dynamic-x86_64-test"
        manifest = {"schema_version": 1, "runtime": "php", "php": {"version": "8.4.27"}, "target": "musl-dynamic",
                    "linkage": "dynamic", "loader": {"path": "lib/musl/ld-musl-x86_64.so.1"}, "architecture": "x86_64",
                    "build_id": "test", "verification": "PASSED"}
        executables = ("bin/php", "bin/php-fpm", "bin/php-ext", "libexec/php", "libexec/php-fpm", "lib/musl/ld-musl-x86_64.so.1")
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

    def test_manual_and_scheduled_matrices_cover_all_series_on_one_target(self):
        workflow = (REPO / ".github/workflows/build.yml").read_text()
        self.assertNotIn("PROFILE_INPUT", workflow)
        self.assertNotIn("inputs.profile", workflow)
        plan_job = workflow.split("  plan:\n", 1)[1].split("  build-php:\n", 1)[0]
        command = textwrap.dedent(plan_job.split("        run: |\n", 1)[1].split("      - name:", 1)[0])
        from test_runtime_updates import load_planner
        planner = load_planner()
        upstream = {"8.5": "8.5.1", "8.4": "8.4.10", "8.3": "8.3.2", "8.2": "8.2.3"}
        from test_updates import REPOSITORY
        # Scheduled and default manual runs check public versions; only bypass rebuilds
        # the whole matrix regardless of what is already published.
        scenarios = (("schedule", False, False, False, False, 4, 3, 2),
                     ("schedule", True, False, False, False, 0, 3, 2),
                     ("schedule", False, True, False, False, 4, 0, 2),
                     ("schedule", True, True, False, False, 0, 0, 2),
                     ("schedule", True, True, True, False, 0, 0, 0),
                     ("workflow_dispatch", True, True, True, False, 0, 0, 0),
                     ("workflow_dispatch", False, True, True, False, 4, 0, 0),
                     ("workflow_dispatch", True, True, True, True, 4, 3, 2),
                     ("workflow_dispatch", False, False, False, True, 4, 3, 2))
        for event, php_current, redis_current, postgres_current, bypass, php_count, redis_count, postgres_count in scenarios:
            with self.subTest(event=event, php_current=php_current, redis_current=redis_current, postgres_current=postgres_current, bypass=bypass), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                output = root / "output"
                history = {(series, "musl-dynamic", "x86_64"): version for series, version in upstream.items()} if php_current else {}
                redis_history = {(version, "musl", "x86_64"): version for version in ("8.10.3", "7.4.12", "7.2.17")} if redis_current else {}
                postgres_history = {(version, "musl-dynamic", "x86_64"): version for version in ("18.6", "17.11")} if postgres_current else {}
                env = {**os.environ, "PUBLISH": "false", "RELEASE_TAG": "", "BYPASS": str(bypass).lower(),
                       "GITHUB_OUTPUT": str(output), "GITHUB_EVENT_NAME": event, "GITHUB_REPOSITORY": REPOSITORY,
                       "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
                # Execute the workflow's planner entry point; control only upstream/public network data.
                args = __import__("shlex").split(command.strip())
                self.assertEqual(args[:3], ["python3", "-I", "scripts/updates.py"])
                argv = args[2:]
                argv[argv.index("--output") + 1] = str(root / "runtime-update-plan.json")
                argv[argv.index("--github-output") + 1] = str(output)
                with patch.dict(os.environ, env, clear=True), patch("sys.argv", argv), \
                     patch.object(planner.php, "latest_php", side_effect=upstream.__getitem__), \
                     patch.object(planner.php, "published_versions", return_value=history), \
                     patch.object(planner, "published_redis", return_value=redis_history), \
                     patch.object(planner, "published_postgresql", return_value=postgres_history):
                    planner.main()
                policy = json.loads((root / "runtime-update-plan.json").read_text())
                outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
                rows = policy["matrix"]["include"]
                self.assertEqual(outputs["has_updates"], "true" if php_count or redis_count or postgres_count else "false")
                self.assertEqual(outputs["has_php_updates"], "true" if php_count else "false")
                self.assertEqual(outputs["has_redis_updates"], "true" if redis_count else "false")
                self.assertEqual(outputs["has_postgres_updates"], "true" if postgres_count else "false")
                self.assertEqual(outputs["publish"], "true" if event == "schedule" and (php_count or redis_count or postgres_count) else "false")
                self.assertEqual(json.loads(outputs["matrix"]), policy["matrix"])
                self.assertEqual(json.loads(outputs["redis_matrix"]), policy["redis_matrix"])
                self.assertEqual(json.loads(outputs["postgres_matrix"]), policy["postgres_matrix"])
                self.assertEqual(len(rows), php_count)
                self.assertEqual(len(policy["redis_matrix"]["include"]), redis_count)
                self.assertEqual(len(policy["postgres_matrix"]["include"]), postgres_count)
                if rows:
                    self.assertEqual({row["php_version"] for row in rows}, {"8.5.1", "8.4.10", "8.3.2", "8.2.3"})
                    self.assertEqual({row["target"] for row in rows}, {"musl-dynamic"})
                    self.assertTrue(all("profile" not in row for row in rows))
                if redis_count:
                    self.assertEqual({row["target"] for row in policy["redis_matrix"]["include"]}, {"musl"})
                if postgres_count:
                    self.assertEqual({row["target"] for row in policy["postgres_matrix"]["include"]}, {"musl-dynamic"})

    def test_noop_matrix_and_publication_are_gated_by_persisted_plan(self):
        workflow = (REPO / ".github/workflows/build.yml").read_text()
        php = workflow.split("  build-php:\n", 1)[1].split("  build-redis:\n", 1)[0]
        publish = workflow.split("  publish:\n", 1)[1]
        self.assertIn("if: needs.plan.outputs.has_php_updates == 'true'", php)
        self.assertIn("needs: [plan, build-php, build-redis, build-postgresql]", publish)
        self.assertIn("name: publication-plan", publish)
        self.assertIn("--plan dist/plans/runtime-update-plan.json", publish)
        self.assertIn("group: runtime-build-publication", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("GH_TOKEN: ${{ github.token }}", workflow.split("  plan:\n", 1)[1])

    def test_publish_gate_allows_only_successful_selected_builds(self):
        workflow = (REPO / ".github/workflows/build.yml").read_text()
        publish = workflow.split("  publish:\n", 1)[1]
        match = re.search(r"^    if: >-\n((?:^      .*\n)+)", publish, re.MULTILINE)
        self.assertIsNotNone(match, "Publication needs an explicit skip-safe success gate")
        expression = " ".join(line.strip() for line in match[1].splitlines())
        expression = expression.removeprefix("${{").removesuffix("}}").strip()
        # Evaluate our emitted boolean expression over the entire small state space.
        states = ("success", "failure", "cancelled", "skipped")
        for cancelled, plan_result, publish_enabled, php_selected, redis_selected, postgres_selected, php_result, redis_result, postgres_result in itertools.product(
                (False, True), states, (False, True), (False, True), (False, True), (False, True), states, states, states):
            expected = (not cancelled and plan_result == "success" and publish_enabled
                        and php_result == ("success" if php_selected else "skipped")
                        and redis_result == ("success" if redis_selected else "skipped")
                        and postgres_result == ("success" if postgres_selected else "skipped"))
            values = {"needs.plan.result": plan_result, "needs.plan.outputs.publish": str(publish_enabled).lower(),
                      "needs.plan.outputs.has_php_updates": str(php_selected).lower(),
                      "needs.plan.outputs.has_redis_updates": str(redis_selected).lower(),
                      "needs.plan.outputs.has_postgres_updates": str(postgres_selected).lower(),
                      "needs.build-php.result": php_result, "needs.build-redis.result": redis_result,
                      "needs.build-postgresql.result": postgres_result}
            candidate = expression.replace("always()", "True").replace("!cancelled()", repr(not cancelled))
            for name, value in values.items():
                candidate = candidate.replace(name, repr(value))
            candidate = candidate.replace("&&", " and ").replace("||", " or ")
            self.assertEqual(eval(candidate, {"__builtins__": {}}, {}), expected, values)

    def test_workflow_builds_postgresql_with_explicit_ci_dependencies(self):
        text = WORKFLOW.read_text()
        job = job_block(text, "build-postgresql")
        for package in ("bison", "flex", "perl", "build-essential", "binutils", "curl"):
            self.assertIn(package, job, package)
        # Zig and every library archive come from the build script, digest-verified.
        self.assertNotIn("musl-tools", job)
        self.assertIn("${{ matrix.postgresql_version }}", job)
        self.assertIn("if-no-files-found: error", job)
        self.assertIn("if: needs.plan.outputs.has_postgres_updates == 'true'", job)
        self.assertIn("needs: [plan, contracts]", job)
        self.assertIn("matrix: ${{ fromJson(needs.plan.outputs.postgres_matrix) }}", job)
        self.assertIn('./build.sh postgresql "$BUILD_VERSION"', job)
        # Without these plan outputs the job's matrix is empty and invalid.
        plan_job = job_block(text, "plan")
        self.assertIn("postgres_matrix: ${{ steps.set.outputs.postgres_matrix }}", plan_job)
        self.assertIn("has_postgres_updates: ${{ steps.set.outputs.has_postgres_updates }}", plan_job)
        # The uploaded artifact must still be picked up by the publish download.
        artifact = re.search(r"^          name: (runtime-postgresql-.+)$", job, re.MULTILINE)[1]
        pattern = re.search(r"^          pattern: (.+)$", job_block(text, "publish"), re.MULTILINE)[1]
        self.assertTrue(fnmatch.fnmatchcase(artifact.replace("${{ matrix.postgresql_version }}", "18.6"), pattern))

    def test_publish_gate_requires_every_selected_runtime_to_succeed(self):
        gate = job_block(WORKFLOW.read_text(), "publish")
        # Names match what scripts/updates.py writes to $GITHUB_OUTPUT.
        for runtime in ("php", "redis", "postgres"):
            self.assertIn(f"has_{runtime}_updates", gate, runtime)
        self.assertIn("build-postgresql", gate, "publish must depend on the postgresql build")

    def test_asset_download_pattern_excludes_the_publication_plan(self):
        workflow = (REPO / ".github/workflows/build.yml").read_text()
        plan_job = workflow.split("  plan:\n", 1)[1].split("  build-php:\n", 1)[0]
        publish = workflow.split("  publish:\n", 1)[1]
        pattern = re.search(r"^          pattern: (.+)$", publish, re.MULTILINE)[1]
        plan_name = re.search(r"^          name: (.+)$", plan_job, re.MULTILINE)[1]
        for artifact in ("runtime-php-8.4.10-musl-dynamic-x86_64", "runtime-redis-8.10.3-musl-x86_64"):
            self.assertTrue(fnmatch.fnmatchcase(artifact, pattern))
        self.assertFalse(fnmatch.fnmatchcase(plan_name, pattern), "Plan JSON must not be merged into bundle assets")

    def test_ci_uploads_verified_archives_without_profile_inputs(self):
        workflow = (REPO / ".github/workflows/build.yml").read_text()
        self.assertIn("python3 -I -m unittest discover -s tests -v", workflow)
        self.assertIn("dist/assets/", workflow)
        self.assertNotIn("options: [minimal, full]", workflow)
        self.assertNotIn("minimal", workflow)
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
