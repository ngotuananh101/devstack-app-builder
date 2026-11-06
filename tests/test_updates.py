"""Exact stable-version planning with external I/O controlled at its boundary."""
import datetime
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_build import load

REPOSITORY = "ngotuananh101/devstack-app-builder"
COMMIT = "a" * 40


def manifest(version="8.4.9", target="glibc", profile="full"):
    return {"schema_version": 1, "runtime": "php", "php": {"version": version},
            "target": target, "profile": profile, "architecture": "x86_64",
            "build_id": "fixture", "verification": "PASSED"}


def assets(data, first_id=10):
    name = f'php-{data["php"]["version"]}-{data["target"]}-{data["profile"]}-x86_64-fixture'
    return [{"id": first_id + i, "name": name + suffix, "size": 123, "state": "uploaded"}
            for i, suffix in enumerate((".tar.gz", ".tar.gz.sha256", ".manifest.json"))]


def release(identifier, **extra):
    return {"id": identifier, "tag_name": f"runtimes-test-{identifier}",
            "draft": False, "prerelease": False, **extra}


class UpdateContracts(unittest.TestCase):
    def setUp(self):
        self.module = load("updates")
        self.assertIsNotNone(self.module, "update planner is not implemented")

    def test_numeric_comparison_and_independent_combinations(self):
        rows = self.module.plan_updates({"8.4": "8.4.10"},
            {("8.4", "glibc", "full", "x86_64"): "8.4.9"}, "both", "full")
        self.assertEqual(rows, [
            {"php_series": "8.4", "php_version": "8.4.10", "target": "glibc", "profile": "full", "architecture": "x86_64", "previous_version": "8.4.9"},
            {"php_series": "8.4", "php_version": "8.4.10", "target": "musl", "profile": "full", "architecture": "x86_64", "previous_version": None}])
        for previous in ("8.4.10", "8.4.11"):
            with self.subTest(previous=previous):
                self.assertEqual(self.module.plan_updates({"8.4": "8.4.10"},
                    {("8.4", "glibc", "full", "x86_64"): previous}, "glibc", "full"), [])
        # Other profiles/architectures cannot make a missing full/x86_64 bundle look present.
        self.assertEqual(len(self.module.plan_updates({"8.4": "8.4.10"},
            {("8.4", "glibc", "minimal", "x86_64"): "8.4.99"}, "glibc", "full")), 1)

    def test_manual_forces_exact_patch_matrix(self):
        rows = self.module.plan_updates({"8.4": "8.4.10"},
            {("8.4", "glibc", "minimal", "x86_64"): "8.4.10"}, "glibc", "minimal", force=True)
        self.assertEqual([(r["php_version"], r["profile"]) for r in rows], [("8.4.10", "minimal")])
        for version in ("8.4.10RC1", "8.1.10", "8.4", "8.4.010"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.module.version_tuple(version)

    def test_public_history_uses_all_pages_and_companions(self):
        older, newer = manifest("8.4.9"), manifest("8.4.10", "musl")
        fixtures = {
            f"repos/{REPOSITORY}/releases?per_page=100&page=1": [release(1)] + [release(i, tag_name="other") for i in range(2, 101)],
            f"repos/{REPOSITORY}/releases?per_page=100&page=2": [release(101)],
            f"repos/{REPOSITORY}/releases/1/assets?per_page=100&page=1": assets(older) + [{"id": i, "name": f"unrelated-{i}", "size": 1, "state": "uploaded"} for i in range(20, 117)],
            f"repos/{REPOSITORY}/releases/1/assets?per_page=100&page=2": [],
            f"repos/{REPOSITORY}/releases/101/assets?per_page=100&page=1": assets(newer, 200),
            f"repos/{REPOSITORY}/releases/assets/12": older,
            f"repos/{REPOSITORY}/releases/assets/202": newer,
        }
        visited = []
        def response(command, **kwargs):
            endpoint = command[2]
            visited.append(endpoint)
            return json.dumps(fixtures[endpoint])
        with patch.object(self.module.subprocess, "check_output", side_effect=response):
            result = self.module.published_versions(REPOSITORY)
        self.assertEqual(result, {("8.4", "glibc", "full", "x86_64"): "8.4.9", ("8.4", "musl", "full", "x86_64"): "8.4.10"})
        self.assertIn(f"repos/{REPOSITORY}/releases?per_page=100&page=2", visited)
        self.assertIn(f"repos/{REPOSITORY}/releases/1/assets?per_page=100&page=2", visited)

    def test_latest_compatible_bundle_wins_without_blocking_other_architectures(self):
        old, new = manifest("8.4.9"), manifest("8.4.10")
        alternate = manifest("8.4.99")
        entries = assets(old) + assets(new, 20)
        entries += [{"id": 30, "name": "php-8.4.99-glibc-full-aarch64-fixture.tar.gz", "size": 123, "state": "uploaded"}]
        fixtures = {
            f"repos/{REPOSITORY}/releases?per_page=100&page=1": [release(1)],
            f"repos/{REPOSITORY}/releases/1/assets?per_page=100&page=1": entries,
            f"repos/{REPOSITORY}/releases/assets/12": old,
            f"repos/{REPOSITORY}/releases/assets/22": new,
        }
        with patch.object(self.module.subprocess, "check_output", side_effect=lambda command, **kwargs: json.dumps(fixtures[command[2]])):
            self.assertEqual(self.module.published_versions(REPOSITORY), {("8.4", "glibc", "full", "x86_64"): "8.4.10"})

    def test_drafts_prereleases_and_unrelated_assets_do_not_count(self):
        fixtures = [[release(1, draft=True), release(2, prerelease=True), release(3, tag_name="application-v1")]]
        with patch.object(self.module.subprocess, "check_output", side_effect=[json.dumps(x) for x in fixtures]):
            self.assertEqual(self.module.published_versions(REPOSITORY), {})

    def test_invalid_manifest_or_api_error_fails_closed(self):
        for defect in ("missing_companion", "unverified", "wrong_identity", "oversized", "malformed_json", "duplicate_name"):
            with self.subTest(defect=defect):
                data = manifest()
                entries = assets(data)
                if defect == "missing_companion": entries.pop(1)
                if defect == "unverified": data["verification"] = "NOT_RUN"
                if defect == "wrong_identity": data["target"] = "musl"
                if defect == "oversized": entries[2]["size"] = 10_000_000
                if defect == "duplicate_name": entries.append(entries[0])
                responses = [json.dumps([release(1)]), json.dumps(entries), "not json" if defect == "malformed_json" else json.dumps(data)]
                with patch.object(self.module.subprocess, "check_output", side_effect=responses), self.assertRaises((ValueError, json.JSONDecodeError)):
                    self.module.published_versions(REPOSITORY)
        with patch.object(self.module.subprocess, "check_output", side_effect=subprocess.CalledProcessError(1, ["gh"], stderr="rate limited")), self.assertRaises((ValueError, subprocess.CalledProcessError)):
            self.module.published_versions(REPOSITORY)
        with patch.object(self.module.subprocess, "check_output", return_value='{"message":"error"}'), self.assertRaises(ValueError):
            self.module.published_versions(REPOSITORY)

    def test_upstream_version_and_source_metadata_are_validated(self):
        good = {"version": "8.4.10", "source": [{"filename": "php-8.4.10.tar.xz", "sha256": "a" * 64}]}
        with patch.object(self.module.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(good).encode())):
            self.assertEqual(self.module.latest_php("8.4"), "8.4.10")
        for data in ({"version": "8.5.1", "source": good["source"]}, {"version": "8.4.10RC1"}, {"version": "8.4.10", "source": []}, {"error": "not available"}):
            with self.subTest(data=data), patch.object(self.module.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(data).encode())), self.assertRaises(ValueError):
                self.module.latest_php("8.4")

    def test_nonpublishing_manual_tag_cannot_inject_outputs(self):
        with patch.object(self.module, "latest_php", return_value="8.4.10"):
            policy = self.module.make_plan(event="workflow_dispatch", repository=REPOSITORY, commit=COMMIT,
                run_id="123", run_attempt="1", php_version="8.4", target="glibc", profile="full",
                publish=False, release_tag="runtimes-X\nhas_updates=false")
        self.assertEqual(policy["release_tag"], "")
        self.assertTrue(policy["has_updates"])

    def test_manifest_schema_requires_an_integer(self):
        for schema in (True, 1.0):
            with self.subTest(schema=schema):
                data = manifest(); data["schema_version"] = schema
                responses = [json.dumps([release(1)]), json.dumps(assets(data)), json.dumps(data)]
                with patch.object(self.module.subprocess, "check_output", side_effect=responses), self.assertRaises(ValueError):
                    self.module.published_versions(REPOSITORY)

    def test_empty_plan_and_automatic_tag_outputs(self):
        kwargs = dict(event="schedule", repository=REPOSITORY, commit=COMMIT,
                      run_id="123", run_attempt="2", php_version="8.4", target="both", profile="full",
                      publish=False, release_tag="", now=datetime.datetime(2026, 11, 6, tzinfo=datetime.timezone.utc))
        upstream = {"8.5": "8.5.1", "8.4": "8.4.10", "8.3": "8.3.2", "8.2": "8.2.3"}
        with patch.object(self.module, "latest_php", side_effect=upstream.__getitem__), patch.object(self.module, "published_versions", return_value={}):
            plan = self.module.make_plan(**kwargs)
        self.assertEqual(plan["release_tag"], "runtimes-20261106-123-2")
        self.assertTrue(plan["publish"])
        self.assertTrue(plan["has_updates"])
        self.assertEqual([r["php_version"] for r in plan["matrix"]["include"]], ["8.5.1", "8.5.1", "8.4.10", "8.4.10", "8.3.2", "8.3.2", "8.2.3", "8.2.3"])
        history = {(series, target, "full", "x86_64"): version for series, version in upstream.items() for target in ("glibc", "musl")}
        with patch.object(self.module, "latest_php", side_effect=upstream.__getitem__), patch.object(self.module, "published_versions", return_value=history):
            plan = self.module.make_plan(**kwargs)
        self.assertFalse(plan["has_updates"])
        self.assertFalse(plan["publish"])
        self.assertEqual(plan["matrix"], {"include": []})
        kwargs.update(event="workflow_dispatch", publish=False, profile="minimal")
        with patch.object(self.module, "latest_php", return_value="8.4.10"), patch.object(self.module, "published_versions", side_effect=AssertionError("No release lookup for a non-publishing manual build")):
            plan = self.module.make_plan(**kwargs)
        self.assertFalse(plan["publish"])
        self.assertEqual(len(plan["matrix"]["include"]), 2)


if __name__ == "__main__":
    unittest.main()
