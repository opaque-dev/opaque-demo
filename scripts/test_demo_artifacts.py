"""Synthetic repositories/binaries prove provenance checks, never live releases."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import demo_artifacts as artifacts
import recording_demo as recording


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.core = self.base / "core"
        self.core.mkdir()
        def git(*args):
            subprocess.run(["git", "-C", str(self.core), *args], check=True, capture_output=True)
        git("init", "-q")
        git("config", "user.name", "Synthetic Test")
        git("config", "user.email", "fixture@example.invalid")
        git("remote", "add", "origin", "https://github.com/opaque-dev/opaque.git")
        (self.core / "source.txt").write_text("synthetic source\n")
        git("add", "source.txt")
        git("commit", "-q", "-m", "synthetic fixture only")
        self.rev = artifacts.git(self.core, "rev-parse", "HEAD").decode().strip()
        self.out = self.base / "artifact"
        self.out.mkdir()
        self.binaries = {}
        for name in ("opaque", "opaqued"):
            path = self.out / name
            path.write_text("#!/bin/sh\nprintf synthetic\\n\n")
            path.chmod(0o700)
            self.binaries[name] = {"path": name, "sha256": artifacts.digest(path)}
        self.value = {"schema": artifacts.SCHEMA, "provenance": "local-build", "source": artifacts.source_snapshot(self.core, self.rev, artifacts.CORE_REPOSITORY), "binaries": self.binaries}
        self.manifest = self.out / "core-binaries.json"
        artifacts.write_json(self.manifest, self.value)

    def test_exact_revision_source_and_binary_digest_are_required(self):
        artifacts.verify_core(self.manifest, self.core, self.rev)
        with self.assertRaisesRegex(ValueError, "revision"):
            artifacts.verify_core(self.manifest, self.core, "0" * 40)
        (self.out / "opaque").write_text("changed")
        with self.assertRaisesRegex(ValueError, "digest"):
            artifacts.verify_core(self.manifest, self.core, self.rev)

    def test_dirty_and_untracked_changes_are_not_covered_by_head(self):
        (self.core / "new-source.rs").write_text("untracked source")
        with self.assertRaisesRegex(ValueError, "untracked"):
            artifacts.verify_core(self.manifest, self.core, self.rev)
        with self.assertRaisesRegex(ValueError, "snapshot"):
            artifacts.verify_core(self.manifest, self.core, self.rev, allow_dirty=True)
        snapshot = artifacts.source_snapshot(self.core, self.rev, allow_dirty=True)
        self.assertTrue(snapshot["dirty"])
        (self.core / "new-source.rs").write_text("changed untracked source")
        self.assertNotEqual(snapshot["source_sha256"], artifacts.source_snapshot(self.core, self.rev, allow_dirty=True)["source_sha256"])

    def test_source_repository_and_artifact_path_cannot_be_substituted(self):
        with self.assertRaisesRegex(ValueError, "canonical"):
            artifacts.source_snapshot(self.core, self.rev, "somewhere/else")
        self.value["binaries"]["opaque"]["path"] = "../outside"
        artifacts.write_json(self.manifest, self.value)
        with self.assertRaisesRegex(ValueError, "escapes"):
            artifacts.verify_core(self.manifest, self.core, self.rev)

    def test_symlink_binary_rejected_even_if_bytes_match(self):
        (self.out / "outside").write_text((self.out / "opaque").read_text())
        (self.out / "opaque").unlink()
        (self.out / "opaque").symlink_to(self.out / "outside")
        with self.assertRaisesRegex(ValueError, "symlink"):
            artifacts.verify_core(self.manifest, self.core, self.rev)

    def test_build_uses_explicit_core_workspace_and_rechecks_source(self):
        real_run = artifacts.subprocess.run
        commands = []
        def run(command, **kwargs):
            if command[0] != "cargo":
                return real_run(command, **kwargs)
            commands.append(command)
            target = Path(command[command.index("--target-dir") + 1]) / "debug"
            target.mkdir(parents=True)
            for name in ("opaque", "opaqued"):
                (target / name).write_text("synthetic compiled fixture")
                (target / name).chmod(0o700)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(artifacts.subprocess, "run", run):
            path = artifacts.build_core(self.core, self.rev, self.base / "build")
        artifacts.verify_core(path, self.core, self.rev)
        self.assertEqual(commands[0][commands[0].index("--manifest-path")+1], str(self.core.resolve() / "Cargo.toml"))
        self.assertEqual(commands[0][-4:], ["-p", "opaque", "-p", "opaqued"])

    def test_recording_commands_have_time_and_output_bounds(self):
        import sys
        with self.assertRaisesRegex(ValueError, "output bound"):
            recording.bounded([sys.executable, "-c", "print('x'*10000)"], limit=100)
        with self.assertRaisesRegex(ValueError, "time bound"):
            recording.bounded([sys.executable, "-c", "import time; time.sleep(30)"], timeout=.1)

    def test_legacy_wrappers_do_not_build_missing_demo_packages(self):
        root = Path(recording.__file__).resolve().parents[1]
        for path in (root / "scripts").glob("demo_*.sh"):
            self.assertIn("recording_demo.py", path.read_text())
            self.assertNotIn("target/release", path.read_text())
        self.assertNotIn("cargo build", (root / "scripts/record_demos.sh").read_text())
