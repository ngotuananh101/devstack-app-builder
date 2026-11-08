"""All-runtime planning, independent scheduled no-ops and strict Redis history."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_build import REPO


def load_planner():
    path = REPO / "scripts/updates.py"
    spec = importlib.util.spec_from_file_location("runtime_updates", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class RuntimeUpdateContracts(unittest.TestCase):
    def setUp(self):
        self.assertTrue((REPO / "scripts/updates.py").exists(), "Shared runtime planner is missing")
        self.module = load_planner()
        self.latest = {"8.5": "8.5.1", "8.4": "8.4.10", "8.3": "8.3.2", "8.2": "8.2.3"}

    def make(self, event="schedule", php_current=False, redis_current=False, bypass=False, apache_current=True):
        history = {(series, "musl-dynamic", "x86_64"): version for series, version in self.latest.items()} if php_current else {}
        redis_history = {(version, "musl", "x86_64"): version for version in ("8.10.3", "7.4.12", "7.2.17")} if redis_current else {}
        apache_history = self.apache_history() if apache_current else {}
        with patch.object(self.module.php, "latest_php", side_effect=self.latest.__getitem__), patch.object(self.module.php, "published_versions", return_value=history), patch.object(self.module, "published_redis", return_value=redis_history), patch.object(self.module, "published_apache", return_value=apache_history):
            return self.module.make_plan(event=event, repository="owner/repo", commit="a" * 40, run_id="123", run_attempt="1", publish=False, bypass=bypass)

    def apache_history(self):
        return {(version, "musl-dynamic", "x86_64"): version
                for version in self.module.apache.read("sources.json")["versions"]}

    def test_manual_bypass_builds_all_php_series_and_pinned_redis_on_musl_only(self):
        policy = self.make("workflow_dispatch", True, True, bypass=True)
        php = policy["matrix"]["include"]
        redis = policy["redis_matrix"]["include"]
        self.assertEqual(len(php), 4)
        self.assertEqual({row["php_series"] for row in php}, {"8.2", "8.3", "8.4", "8.5"})
        self.assertEqual({row["target"] for row in php}, {"musl-dynamic"})
        self.assertTrue(all("profile" not in row for row in php))
        self.assertEqual(len(redis), 3)
        self.assertEqual({row["redis_version"] for row in redis}, {"8.10.3", "7.4.12", "7.2.17"})
        self.assertEqual({row["target"] for row in redis}, {"musl"})
        self.assertTrue(all("profile" not in row for row in redis))
        self.assertFalse(policy["publish"])

    def test_apache_rows_are_planned_on_the_musl_dynamic_target(self):
        policy = self.make("workflow_dispatch", True, True, apache_current=False)
        rows = policy["apache_matrix"]["include"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target"], "musl-dynamic")
        self.assertEqual(rows[0]["architecture"], "x86_64")
        self.assertNotIn("profile", rows[0])
        self.assertTrue(policy["has_updates"])
        self.assertEqual(self.make("schedule", True, True)["apache_matrix"], {"include": []})

    def test_scheduled_and_default_manual_runs_check_versions(self):
        for event in ("schedule", "workflow_dispatch"):
            with self.subTest(event=event):
                policy = self.make(event, php_current=True, redis_current=True)
                self.assertEqual(policy["matrix"], {"include": []})
                self.assertEqual(policy["redis_matrix"], {"include": []})
                self.assertEqual(policy["apache_matrix"], {"include": []})
                self.assertFalse(policy["has_updates"])
        policy = self.make("workflow_dispatch", php_current=False, redis_current=True)
        self.assertEqual(len(policy["matrix"]["include"]), 4)
        self.assertEqual(policy["redis_matrix"], {"include": []})
        self.assertEqual(policy["publish"], False, "Manual runs never publish without an explicit tag")

    def test_scheduled_noops_are_independent_per_runtime(self):
        for php_current, redis_current, apache_current, php_count, redis_count, apache_count in [
                (False, False, False, 4, 3, 1), (True, False, True, 0, 3, 0),
                (False, True, True, 4, 0, 0), (True, True, False, 0, 0, 1),
                (True, True, True, 0, 0, 0)]:
            with self.subTest(php=php_current, redis=redis_current, apache=apache_current):
                policy = self.make(php_current=php_current, redis_current=redis_current,
                                   apache_current=apache_current)
                self.assertEqual(len(policy["matrix"]["include"]), php_count)
                self.assertEqual(len(policy["redis_matrix"]["include"]), redis_count)
                self.assertEqual(len(policy["apache_matrix"]["include"]), apache_count)
                self.assertEqual(policy["has_updates"], bool(php_count + redis_count + apache_count))
                self.assertEqual(policy["publish"], bool(php_count + redis_count + apache_count))

    def test_outputs_always_expose_both_matrices_and_runtime_gates(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "github-output"
            policy = self.make(php_current=True)
            self.module.write_outputs(output, policy)
            fields = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(json.loads(fields["matrix"]), {"include": []})
            self.assertEqual(len(json.loads(fields["redis_matrix"])["include"]), 3)
            self.assertEqual(json.loads(fields["apache_matrix"]), {"include": []})
            self.assertEqual(fields["has_php_updates"], "false")
            self.assertEqual(fields["has_redis_updates"], "true")
            self.assertEqual(fields["has_apache_updates"], "false")
            self.assertEqual(fields["publish"], "true")

    def test_removed_environment_inputs_cannot_narrow_builds(self):
        env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REPOSITORY": "owner/repo", "GITHUB_SHA": "a" * 40,
               "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "PUBLISH": "false", "BYPASS": "true", "PROFILE_INPUT": "full",
               "PHP_VERSION_INPUT": "8.2", "TARGET_INPUT": "musl", "RUNTIME_INPUT": "php"}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, env, clear=True), patch.object(self.module.php, "latest_php", side_effect=self.latest.__getitem__), \
             patch.object(self.module.php, "published_versions", return_value={}), patch.object(self.module, "published_redis", return_value={}), \
             patch.object(self.module, "published_apache", return_value={}), \
             patch("sys.argv", ["updates.py", "--output", str(Path(temp) / "plan.json")]):
            self.module.main()
            policy = json.loads((Path(temp) / "plan.json").read_text())
            self.assertEqual(len(policy["matrix"]["include"]), 4)
            self.assertEqual(len(policy["redis_matrix"]["include"]), 3)

    def test_missing_or_invalid_bypass_input_fails_closed(self):
        base = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REPOSITORY": "owner/repo", "GITHUB_SHA": "a" * 40,
                "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "PUBLISH": "false"}
        for value, accepted in (("false", True), ("true", True), ("", True), ("yes", False), ("1", False)):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp:
                env = {**base, **({"BYPASS": value} if value != "" else {})}
                argv = ["updates.py", "--output", str(Path(temp) / "plan.json")]
                with patch.dict(os.environ, env, clear=True), patch.object(self.module.php, "latest_php", side_effect=self.latest.__getitem__), \
                        patch.object(self.module.php, "published_versions", return_value={}), \
                        patch.object(self.module, "published_redis", return_value={}), \
                        patch.object(self.module, "published_apache", return_value={}), patch("sys.argv", argv):
                    if accepted:
                        self.module.main()
                        policy = json.loads((Path(temp) / "plan.json").read_text())
                        self.assertEqual(policy["bypass"], value == "true", "An absent bypass input must fail closed")
                    else:
                        with self.assertRaises(SystemExit):
                            self.module.main()
                        self.assertFalse((Path(temp) / "plan.json").exists())

    def test_redis_history_rejects_incomplete_or_unverified_asset_groups(self):
        root = "redis-8.10.3-musl-x86_64-build123"
        assets = [{"name": root + suffix, "id": index + 10, "state": "uploaded", "size": 100} for index, suffix in enumerate((".tar.gz", ".tar.gz.sha256", ".manifest.json"))]
        manifest = {"schema_version": 1, "runtime": "redis", "redis": {"version": "8.10.3"}, "target": "musl", "architecture": "x86_64", "build_id": "build123", "verification": "PASSED"}
        releases = [{"tag_name": "runtimes-example", "id": 1, "draft": False, "prerelease": False}]
        for defect in (None, "missing", "unverified", "identity", "schema"):
            data = copy.deepcopy(manifest)
            selected = assets[:-1] if defect == "missing" else assets
            if defect == "unverified": data["verification"] = "NOT_RUN"
            if defect == "identity": data["target"] = "glibc"
            if defect == "schema": data["schema_version"] = True
            def pages(endpoint):
                return iter(releases if endpoint.endswith("/releases") else selected)
            with self.subTest(defect=defect), patch.object(self.module.php, "pages", side_effect=pages), patch.object(self.module.php, "gh_json", return_value=data):
                if defect:
                    with self.assertRaises(ValueError):
                        self.module.published_redis("owner/repo")
                else:
                    self.assertEqual(self.module.published_redis("owner/repo"), {("8.10.3", "musl", "x86_64"): "8.10.3"})
