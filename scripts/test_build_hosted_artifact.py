"""Artifact integrity and public output tests; no image or deployment operation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build_hosted_artifact as hosted
import demo_artifacts as artifacts
import check_site_privacy as privacy


class HostedArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in (*hosted.RUNTIME_FILES, *hosted.WORKER_FILES):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic code")
        (self.root / "opaque-showcase").write_bytes(b"\x7fELFsynthetic binary")
        privacy.package_worker_site(self.root / "public")
        self.files = {p.relative_to(self.root).as_posix(): artifacts.digest(p) for p in self.root.rglob("*") if p.is_file()}
        self.manifest = {"schema": hosted.SCHEMA, "provenance": "local-build", "demo_source": {"repository":hosted.DEMO_REPOSITORY, "revision":"a"*40, "dirty":False, "source_sha256":"b"*64}, "core_contract_revision":"c"*40, "target":"x86_64-unknown-linux-gnu", "profile":"debug", "privacy_gate":"passed-generated-worker-artifact", "files":self.files}
        self.save()

    def save(self):
        artifacts.write_json(self.root / "artifact-provenance.json", self.manifest)

    def test_exact_payloads_and_generated_assets_are_verified(self):
        hosted.verify(self.root, require_linux=True)
        (self.root / "scripts/metrics_chat_dogfood.py").write_text("changed")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            hosted.verify(self.root)

    def test_added_private_files_and_missing_payloads_fail(self):
        (self.root / "private-state.json").write_text("not for publication")
        with self.assertRaisesRegex(ValueError, "unrecorded"):
            hosted.verify(self.root)
        (self.root / "private-state.json").unlink()
        self.manifest["files"].pop("opaque-showcase")
        self.save()
        with self.assertRaisesRegex(ValueError, "omits"):
            hosted.verify(self.root)

    def test_macos_binary_cannot_enter_linux_image(self):
        self.manifest["target"] = "aarch64-apple-darwin"
        self.save()
        with self.assertRaisesRegex(ValueError, "Linux ELF"):
            hosted.verify(self.root, require_linux=True)

    def test_both_pin_files_must_agree(self):
        self.assertRegex(hosted.core_pin(hosted.ROOT), r"^[0-9a-f]{40}$")
        (self.root / "Cargo.toml").write_text('[workspace.dependencies]\nopaque-core={rev="'+'a'*40+'"}\n')
        (self.root / "Cargo.lock").write_text('[[package]]\nname="opaque-core"\nsource="git+https://github.com/opaque-dev/opaque.git?rev=changed#changed"\n')
        with self.assertRaisesRegex(ValueError, "differs"):
            hosted.core_pin(self.root)

    def test_manifest_path_escape_is_rejected(self):
        self.manifest["files"]["../outside"] = "a"*64
        self.save()
        with self.assertRaisesRegex(ValueError, "escapes"):
            hosted.verify(self.root)

    def test_incomplete_source_claim_or_worker_inventory_fails(self):
        self.manifest["demo_source"].pop("repository")
        self.save()
        with self.assertRaisesRegex(ValueError, "source provenance"):
            hosted.verify(self.root)
        self.manifest["demo_source"]["repository"] = "kcirtapfromspace/opaque-demo"
        self.save()
        with self.assertRaisesRegex(ValueError, "source provenance"):
            hosted.verify(self.root)
        self.manifest["demo_source"]["repository"] = hosted.DEMO_REPOSITORY
        self.manifest["files"].pop(hosted.WORKER_FILES[0])
        self.save()
        with self.assertRaisesRegex(ValueError, "omits"):
            hosted.verify(self.root)
