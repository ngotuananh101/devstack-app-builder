"""Bootstrap safety and build wiring tests (no network or upstream code execution)."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from test_build import load, REPO


class BootstrapContracts(unittest.TestCase):
    def test_digest_mismatch_prevents_extraction(self):
        module = load("bootstrap")
        self.assertIsNotNone(module, "verified bootstrap is not implemented")
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "untrusted.tar.gz"
            archive.write_bytes(b"not the locked source")
            with self.assertRaisesRegex(ValueError, "digest"):
                module.extract_verified(archive, Path(tmp) / "source", "0" * 64)
            self.assertFalse((Path(tmp) / "source").exists())

    def test_tar_traversal_is_rejected(self):
        module = load("bootstrap")
        self.assertIsNotNone(module, "verified bootstrap is not implemented")
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                member = tarfile.TarInfo("../escape")
                member.size = 1
                tar.addfile(member, io.BytesIO(b"x"))
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            with self.assertRaises(ValueError):
                module.extract_verified(archive, Path(tmp) / "source", digest)
            self.assertFalse((Path(tmp) / "escape").exists())

    def test_extract_requires_fresh_directory(self):
        module = load("bootstrap")
        self.assertIsNotNone(module, "verified bootstrap is not implemented")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                member = tarfile.TarInfo("spc/composer.lock")
                member.size = 2
                tar.addfile(member, io.BytesIO(b"{}"))
            destination = root / "source"
            destination.mkdir()
            (destination / "planted.php").touch()
            with self.assertRaises(FileExistsError):
                module.extract_verified(archive, destination, hashlib.sha256(archive.read_bytes()).hexdigest())

    def test_spc_lock_is_immutable_v3(self):
        lock = json.loads((REPO / "runtimes/php/config/spc-lock.json").read_text())
        self.assertEqual(len(lock["commit"]), 40)
        self.assertIn(lock["commit"], lock["source_url"])
        self.assertNotIn("nightly", lock["source_url"])
        self.assertEqual(len(lock["source_sha256"]), 64)

    def test_dependencies_verified_before_local_composer_urls(self):
        module = load("bootstrap")
        self.assertIsNotNone(module, "verified bootstrap is not implemented")
        self.assertTrue(hasattr(module, "prepare_dependencies"), "dependency byte verification is not implemented")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            archive = root / "fixture.zip"
            import zipfile
            with zipfile.ZipFile(archive, "w") as zip:
                zip.writestr("package/file.php", "<?php // fixture, never executed")
            lock = {"packages": [{"name": "fixture/package", "version": "1.0", "dist": {"url": archive.as_uri(), "reference": "immutable"}}]}
            (source / "composer.lock").write_text(json.dumps(lock))
            registry = [{"name": "fixture/package", "sha256": "0" * 64}]
            with self.assertRaisesRegex(ValueError, "digest"):
                module.prepare_dependencies(source, registry, root / "download-bad")
            self.assertEqual(json.loads((source / "composer.lock").read_text()), lock)
            registry[0]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            provenance = module.prepare_dependencies(source, registry, root / "download-good")
            result = json.loads((source / "composer.lock").read_text())
            self.assertTrue(result["packages"][0]["dist"]["url"].startswith("file://"))
            self.assertEqual(provenance[0]["sha256"], registry[0]["sha256"])
            self.assertNotEqual(result["packages"][0]["dist"]["url"], archive.as_uri())

    def test_source_patches_are_digest_pinned_and_idempotent(self):
        module = load("bootstrap")
        self.assertIsNotNone(getattr(module, "apply_source_patches", None), "reviewed source patch gate is not implemented")
        before, new = "append crt\n", "append crt once\n"
        original = f"#!/usr/bin/env bash\n{before}"
        patch = {
            "file": "src/globals/scripts/zig-cc.sh",
            "reason": "fixture",
            "before_sha256": hashlib.sha256(original.encode()).hexdigest(),
            "after_sha256": hashlib.sha256(f"#!/usr/bin/env bash\n{new}".encode()).hexdigest(),
            "replace": {"old": before, "new": new},
        }
        registry = {"schema_version": 1, "patches": [patch]}
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            target = source / patch["file"]
            target.parent.mkdir(parents=True)
            target.write_text(original)
            self.assertEqual(module.apply_source_patches(source, registry)[0]["status"], "applied")
            self.assertEqual(target.read_text(), f"#!/usr/bin/env bash\n{new}")
            # A second run over the patched tree must be a no-op, not a re-application.
            self.assertEqual(module.apply_source_patches(source, registry)[0]["status"], "already-applied")
            self.assertEqual(target.read_text(), f"#!/usr/bin/env bash\n{new}")
            # The registry pins the digest of the whole reviewed file, so a fixture with the
            # anchor text but different bytes must fail rather than be patched.
            target.write_text(f"#!/usr/bin/env bash\n{before}\n")
            with self.assertRaisesRegex(ValueError, "Pinned source file changed"):
                module.apply_source_patches(source, registry)
            # A patch whose result does not match the pinned post-image is refused.
            registry["patches"][0]["before_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, "Patch result digest mismatch"):
                module.apply_source_patches(source, registry)

    def test_source_patch_registry_matches_the_pinned_upstream_file(self):
        registry = json.loads((REPO / "runtimes/php/config/spc-source-patches.json").read_text())
        self.assertEqual(registry["schema_version"], 1)
        for patch in registry["patches"]:
            self.assertRegex(patch["before_sha256"], r"^[a-f0-9]{64}$")
            self.assertRegex(patch["after_sha256"], r"^[a-f0-9]{64}$")
            self.assertNotEqual(patch["before_sha256"], patch["after_sha256"])
            self.assertTrue(patch["replace"]["old"])
            self.assertNotEqual(patch["replace"]["old"], patch["replace"]["new"])
            self.assertTrue(patch["reason"])
        crt = next(row for row in registry["patches"] if row["file"] == "src/globals/scripts/zig-cc.sh")
        self.assertIn("crtbegin", crt["replace"]["old"])
        self.assertIn("HAVE_CRT", crt["replace"]["new"])

    def test_locked_dependency_digests_are_valid(self):
        registry = json.loads((REPO / "runtimes/php/config/spc-dependencies.json").read_text())
        self.assertEqual(len(registry), 15)
        self.assertEqual(len({row["name"] for row in registry}), 15)
        for row in registry:
            self.assertRegex(row["sha256"], r"^[a-f0-9]{64}$")

    def test_invalid_build_version_fails_before_spc(self):
        result = subprocess.run([str(REPO / "build.sh"), "php", "8.1"], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported PHP version", result.stderr)


if __name__ == "__main__":
    unittest.main()
