"""Factual notes, exact-plan publication and draft-first release lifecycle."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from test_build import load

REPOSITORY = "ngotuananh101/devstack-app-builder"
COMMIT = "a" * 40
TAG = "runtimes-20261106-123-1"


LOADER = "lib/musl/ld-musl-x86_64.so.1"


def metadata():
    return {"schema_version": 1, "runtime": "php", "php": {"version": "8.4.10", "static_modules": ["Core", "PDO", "Zend OPcache"]},
            "target": "musl-dynamic", "linkage": "dynamic", "loader": {"path": LOADER, "sha256": "0" * 64, "version": "1.2.5"},
            "architecture": "x86_64", "build_id": "fixture", "verification": "PASSED",
            "builder": {"commit": COMMIT}, "shared_modules": [{"name": "redis", "enabled_by_default": {"cli": False, "fpm": False}}]}


def plan():
    return {"schema_version": 1, "event": "schedule", "repository": REPOSITORY, "commit": COMMIT,
            "run_id": "123", "run_attempt": "1", "release_tag": TAG, "publish": True, "has_updates": True,
            "matrix": {"include": [{"php_series": "8.4", "php_version": "8.4.10", "target": "musl-dynamic",
                                    "architecture": "x86_64", "previous_version": "8.4.9"}]}}


def packaged(directory):
    bundle = directory / "bundle"
    for executable in ("bin/php", "bin/php-fpm", "bin/php-ext", "libexec/php", "libexec/php-fpm"):
        path = bundle / executable
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    loader = bundle / LOADER
    loader.parent.mkdir(parents=True, exist_ok=True)
    loader.write_bytes(b"loader fixture")
    loader.chmod(0o755)
    (bundle / "manifest.json").write_text(json.dumps(metadata()))
    load("package").package(bundle, directory / "assets")
    return directory / "assets"


class PublishContracts(unittest.TestCase):
    def setUp(self):
        self.module = load("publish")

    def test_mixed_plan_requires_every_redis_and_php_bundle(self):
        redis = {"schema_version": 1, "runtime": "redis", "redis": {"version": "8.10.3", "tls": False},
                 "target": "musl", "architecture": "x86_64", "build_id": "redisfixture", "verification": "PASSED",
                 "builder": {"commit": COMMIT}, "libc": {"family": "musl", "minimum_version": None, "static": True}}
        policy = plan()
        policy["redis_matrix"] = {"include": [{"redis_version": "8.10.3", "target": "musl", "architecture": "x86_64", "previous_version": None}]}
        self.module.validate_plan(policy, [metadata(), redis], TAG, REPOSITORY, COMMIT)
        for defect in ("missing_redis", "missing_php", "redis_patch", "redis_commit", "redis_profile"):
            data = [metadata(), copy.deepcopy(redis)]
            if defect == "missing_redis": data.pop()
            if defect == "missing_php": data.pop(0)
            if defect == "redis_patch": data[1]["redis"]["version"] = "7.2.17"
            if defect == "redis_commit": data[1]["builder"]["commit"] = "b" * 40
            if defect == "redis_profile": data[1]["profile"] = "full"
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                self.module.validate_plan(policy, data, TAG, REPOSITORY, COMMIT)
        policy["matrix"] = {"include": []}
        self.module.validate_plan(policy, [redis], TAG, REPOSITORY, COMMIT)

    def test_mixed_assets_accept_only_safe_redis_aliases(self):
        from test_redis import module as redis_module
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            assets = packaged(root)
            redis = root / "redis-bundle"
            (redis / "bin").mkdir(parents=True)
            for name in ("redis-server", "redis-cli", "redis-benchmark"):
                binary = redis / "bin" / name
                binary.write_text("#!/bin/sh\nexit 0\n")
                binary.chmod(0o755)
            for name in ("redis-sentinel", "redis-check-rdb", "redis-check-aof"):
                (redis / "bin" / name).symlink_to("redis-server")
            manifest = {"schema_version": 1, "runtime": "redis", "redis": {"version": "8.10.3"}, "target": "musl", "architecture": "x86_64", "build_id": "redisfixture", "verification": "PASSED", "builder": {"commit": COMMIT}}
            (redis / "manifest.json").write_text(json.dumps(manifest))
            packager = redis_module("bundle")
            packager.package(redis, assets)
            self.assertEqual(len(self.module.validate_assets(assets)), 6)
            (redis / "bin/redis-sentinel").unlink()
            (redis / "bin/redis-sentinel").symlink_to("../../outside")
            packager.package(redis, root / "bad-assets")
            with self.assertRaises(ValueError):
                self.module.validate_assets(root / "bad-assets")

    def test_mixed_notes_describe_fully_static_redis(self):
        redis = {"runtime": "redis", "redis": {"version": "8.10.3", "tls": False}, "target": "musl", "architecture": "x86_64", "build_id": "redisfixture", "libc": {"family": "musl", "minimum_version": None, "static": True}}
        notes = self.module.release_notes([metadata(), redis], REPOSITORY, COMMIT, plan())
        for expected in ("PHP", "Redis", "8.10.3", "redis-server", "redis-cli", "No profiles", "native", "fully static musl"):
            self.assertIn(expected, notes)
        self.assertIn("PHP", self.module.release_notes([metadata()], REPOSITORY, COMMIT))
        redis_notes = self.module.release_notes([redis], REPOSITORY, COMMIT)
        self.assertNotIn("2.17", redis_notes)
        for static in (False, None):
            with self.subTest(static=static):
                broken = copy.deepcopy(redis)
                broken["libc"]["static"] = static
                with self.assertRaisesRegex(ValueError, "fully static musl"):
                    self.module.release_notes([broken], REPOSITORY, COMMIT)

    def test_notes_describe_actual_bundles_and_workflow(self):
        notes = self.module.release_notes([metadata()], REPOSITORY, COMMIT, plan())
        for expected in ("8.4.10", "8.4.9", "musl-dynamic", "x86_64", "fixture", "PDO", "Zend OPcache", "redis", "SHA-256",
                         "lib/musl/", f"https://github.com/{REPOSITORY}/actions/runs/123", f"https://github.com/{REPOSITORY}/commit/{COMMIT}"):
            self.assertIn(expected, notes)
        self.assertIn("caller-owned", notes)
        self.assertIn("initial CLI disabled, FPM disabled", notes)
        self.assertNotIn("profile", notes.lower())
        self.assertNotIn("tested on glibc 2.17", notes)
        self.assertNotIn("fully reproducible", notes)

    def test_notes_describe_builtin_modules_separately_from_shared(self):
        data = metadata()
        data["builtin_modules"] = [{"name": "opcache", "runtime_name": "Zend OPcache", "directive": None, "always_loaded": True}]
        data["shared_modules"] = [{"name": "redis", "enabled_by_default": {"cli": False, "fpm": False}}]
        notes = self.module.release_notes([data], REPOSITORY, COMMIT, plan())
        self.assertIn("Always-loaded built-in `opcache`", notes)
        self.assertIn("Shared `redis`: initial CLI disabled, FPM disabled", notes)
        broken = copy.deepcopy(data)
        broken["builtin_modules"] = [{"name": "opcache", "always_loaded": False}]
        with self.assertRaisesRegex(ValueError, "built-in"):
            self.module.release_notes([broken], REPOSITORY, COMMIT)

    def test_plan_rejects_missing_extra_wrong_patch_or_commit(self):
        self.module.validate_plan(plan(), [metadata()], TAG, REPOSITORY, COMMIT)
        for defect in ("missing", "extra", "patch", "builder", "plan_commit", "plan_tag", "duplicate", "manual_false", "row_series", "row_profile", "row_target", "static_target"):
            with self.subTest(defect=defect):
                data, policy = [metadata()], plan()
                if defect == "missing": data = []
                if defect == "extra":
                    other = metadata(); other["build_id"] = "other"; data.append(other)
                if defect == "patch": data[0]["php"]["version"] = "8.4.11"
                if defect == "builder": data[0]["builder"]["commit"] = "b" * 40
                if defect == "plan_commit": policy["commit"] = "b" * 40
                if defect == "plan_tag": policy["release_tag"] = "runtimes-other"
                if defect == "duplicate": policy["matrix"]["include"].append(copy.deepcopy(policy["matrix"]["include"][0]))
                if defect == "manual_false": policy["publish"] = False
                if defect == "row_series": policy["matrix"]["include"][0]["php_series"] = "8.3"
                if defect == "row_profile": policy["matrix"]["include"][0]["profile"] = "minimal"
                if defect == "row_target": policy["matrix"]["include"][0]["target"] = "musl"
                if defect == "static_target": data[0]["linkage"] = "static"
                with self.assertRaises(ValueError):
                    self.module.validate_plan(policy, data, TAG, REPOSITORY, COMMIT)

    def test_assets_require_the_bundled_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = packaged(root)
            self.assertEqual(len(self.module.validate_assets(assets)), 3)
            bundle = root / "bundle"
            (bundle / LOADER).unlink()
            load("package").package(bundle, root / "no-loader")
            with self.assertRaisesRegex(ValueError, "loader"):
                self.module.validate_assets(root / "no-loader")

    def gh_fixture(self, fail_upload=False, existing=False, lookup_error=False):
        calls = []
        def invoke(command, **kwargs):
            calls.append(command)
            if command[:2] == ["gh", "api"]:
                if existing: return subprocess.CompletedProcess(command, 0, "{}", "")
                return subprocess.CompletedProcess(command, 1, "", "HTTP 403 rate limit" if lookup_error else "HTTP 404 Not Found")
            if command[1:3] == ["release", "create"]:
                notes_path = Path(command[command.index("--notes-file") + 1])
                self.assertIn("8.4.10", notes_path.read_text())
                self.assertIn("--draft", command)
                self.assertFalse(any(str(part).endswith(".tar.gz") for part in command))
            if command[1:3] == ["release", "upload"] and fail_upload:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0, "", "")
        return calls, invoke

    def api_fixture(self, *, fail_upload=False, draft_page=False):
        calls = []
        uploaded = []
        def query(command, **kwargs):
            calls.append(command)
            endpoint = command[2]
            if endpoint == f"repos/{REPOSITORY}":
                return json.dumps({"private": False})
            if endpoint == f"repos/{REPOSITORY}/releases?per_page=100&page=1":
                return json.dumps([{"id": i, "tag_name": f"other-{i}", "draft": False} for i in range(1, 101)] if draft_page else [])
            if endpoint == f"repos/{REPOSITORY}/releases?per_page=100&page=2":
                return json.dumps([{"id": 900, "tag_name": TAG, "draft": True}])
            if endpoint == f"repos/{REPOSITORY}/releases":
                payload = json.loads(kwargs["input"])
                self.assertTrue(payload["draft"])
                self.assertEqual(payload["tag_name"], TAG)
                self.assertIn("8.4.10", payload["body"])
                return json.dumps({"id": 777, "tag_name": TAG, "draft": True})
            if endpoint.startswith(f"https://uploads.github.com/repos/{REPOSITORY}/releases/777/assets?name="):
                self.assertIn("--input", command)
                source = Path(command[command.index("--input") + 1])
                self.assertGreater(source.stat().st_size, 0)
                if fail_upload:
                    raise subprocess.CalledProcessError(1, command)
                uploaded.append(source.name)
                return json.dumps({"id": 1000 + len(uploaded), "name": source.name, "state": "uploaded", "size": source.stat().st_size})
            if endpoint == f"repos/{REPOSITORY}/releases/777":
                self.assertEqual(command[command.index("--method") + 1], "PATCH")
                self.assertEqual(json.loads(kwargs["input"]), {"draft": False})
                self.assertEqual(len(uploaded), 3)
                return json.dumps({"id": 777, "tag_name": TAG, "draft": False})
            raise AssertionError(f"Unexpected API operation: {command}")
        def absence(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 1, "", "HTTP 404 Not Found")
        return calls, query, absence

    def test_existing_draft_on_later_page_prevents_creation(self):
        calls, query, absence = self.api_fixture(draft_page=True)
        with tempfile.TemporaryDirectory() as tmp, patch.object(self.module.subprocess, "check_output", side_effect=query), patch.object(self.module.subprocess, "run", side_effect=absence), self.assertRaisesRegex(ValueError, "exists"):
            self.module.publish(packaged(Path(tmp)), TAG, REPOSITORY, COMMIT, plan=plan())
        self.assertFalse(any("--method" in c and c[c.index("--method") + 1] == "POST" for c in calls))

    def test_publish_creates_draft_uploads_then_promotes(self):
        calls, query, absence = self.api_fixture()
        with tempfile.TemporaryDirectory() as tmp, patch.object(self.module.subprocess, "check_output", side_effect=query), patch.object(self.module.subprocess, "run", side_effect=absence):
            self.module.publish(packaged(Path(tmp)), TAG, REPOSITORY, COMMIT, plan=plan())
        mutations = [c for c in calls if "--method" in c and c[c.index("--method") + 1] in {"POST", "PATCH"}]
        self.assertEqual([c[c.index("--method") + 1] for c in mutations], ["POST", "POST", "POST", "POST", "PATCH"])
        self.assertEqual(mutations[0][2], f"repos/{REPOSITORY}/releases")
        self.assertTrue(all("/releases/777/assets?name=" in c[2] for c in mutations[1:4]))
        self.assertEqual(mutations[-1][2], f"repos/{REPOSITORY}/releases/777")
        self.assertFalse(any(c[:2] == ["gh", "release"] for c in calls))

    def test_upload_failure_leaves_draft(self):
        calls, query, absence = self.api_fixture(fail_upload=True)
        with tempfile.TemporaryDirectory() as tmp, patch.object(self.module.subprocess, "check_output", side_effect=query), patch.object(self.module.subprocess, "run", side_effect=absence), self.assertRaises(subprocess.CalledProcessError):
            self.module.publish(packaged(Path(tmp)), TAG, REPOSITORY, COMMIT, plan=plan())
        self.assertFalse(any("PATCH" in c for c in calls))

    def test_schema_requires_an_integer_for_plan_and_archive(self):
        for schema in (True, 1.0):
            with self.subTest(schema=schema, boundary="plan"):
                policy = plan(); policy["schema_version"] = schema
                with self.assertRaises(ValueError):
                    self.module.validate_plan(policy, [metadata()], TAG, REPOSITORY, COMMIT)
            with self.subTest(schema=schema, boundary="archive"), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                packaged(root)
                data = metadata(); data["schema_version"] = schema
                (root / "bundle/manifest.json").write_text(json.dumps(data))
                load("package").package(root / "bundle", root / "invalid")
                with self.assertRaises(ValueError):
                    self.module.validate_assets(root / "invalid")

    def test_existing_tag_or_lookup_failure_prevents_publication(self):
        for existing, lookup_error in ((True, False), (False, True)):
            with self.subTest(existing=existing):
                calls, invoke = self.gh_fixture(existing=existing, lookup_error=lookup_error)
                with tempfile.TemporaryDirectory() as tmp, patch.object(self.module.subprocess, "check_output", return_value='{"private": false}'), patch.object(self.module.subprocess, "run", side_effect=invoke), self.assertRaises(ValueError):
                    self.module.publish(packaged(Path(tmp)), TAG, REPOSITORY, COMMIT, plan=plan())
                self.assertFalse(any(c[1] == "release" for c in calls))


if __name__ == "__main__":
    unittest.main()
