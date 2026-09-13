"""Standalone setup regressions; only ephemeral keys and loopback listeners."""
import argparse
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import metrics_chat_dogfood as metrics


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL required")
class StandaloneMetricsTests(unittest.TestCase):
    def test_ephemeral_identity_matches_jwks_and_signatures(self):
        with tempfile.TemporaryDirectory() as temporary:
            key, public, jwks = metrics.generate_identity(Path(temporary))
            for path in (key, public, jwks):
                self.assertEqual(path.stat().st_mode & 0o077, 0)
            token = metrics.signed_token({"sub": "synthetic-test"}, key_path=key)
            content, signature = token.rsplit(".", 1)
            signature_file = Path(temporary) / "signature"
            signature_file.write_bytes(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
            verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(signature_file)], input=content.encode(), capture_output=True, timeout=10)
            self.assertEqual(verified.returncode, 0)
            jwk = json.loads(jwks.read_text())["keys"][0]
            self.assertEqual(jwk["e"], "AQAB")
            self.assertGreater(len(jwk["n"]), 300)
            with self.assertRaises(ValueError):
                metrics.generate_identity(Path(temporary))

    def test_prepare_from_standalone_checkout_and_cleanup_private_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "opaque-showcase"
            binary.write_text("synthetic unused binary placeholder")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            args = argparse.Namespace(no_build=True, showcase_bin=binary, port=port, issuer_port=port+2, source_port=port+3)
            fixture = metrics.ChatFixture(args, root)
            try:
                fixture.prepare()
                key = fixture.key_path
                self.assertTrue(key.is_file())
                config = json.loads((root / "issuer/config.json").read_text())
                self.assertEqual(config["key_path"], str(key))
                self.assertTrue(Path(config["jwks_path"]).is_file())
                self.assertNotIn("crates/opaqued/tests/fixtures", json.dumps(config))
                self.assertEqual(len(fixture.token(0).split(".")), 3)
                self.assertEqual(len(fixture.configs), 2)
                self.assertTrue(all(c["fixture_mode"] for c in fixture.configs))
            finally:
                fixture.close()
            self.assertFalse(key.exists())
            self.assertTrue((root / "binary-digest.json").is_file())

    def test_internal_issuer_receives_its_own_generated_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key, _, jwks = metrics.generate_identity(root)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            config = root / "issuer.json"
            metrics.dump(config, {"port": port, "clients": [], "openssl": shutil.which("openssl"), "key_path": str(key), "jwks_path": str(jwks)})
            process = subprocess.Popen([sys.executable, "-B", str(Path(metrics.__file__).resolve()), "--internal", "issuer", "--config", str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        status, _, body = metrics.http(f"http://127.0.0.1:{port}/jwks", timeout=.3)
                        break
                    except OSError:
                        time.sleep(.05)
                else:
                    self.fail("standalone issuer did not start")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), json.loads(jwks.read_text()))
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_identity_rejects_public_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            with self.assertRaises(ValueError):
                metrics.generate_identity(root)
