"""Adversarial generated-artifact fixtures; never deploy or read private services."""
import gzip
import json
from pathlib import Path
import tempfile
import unittest

import check_site_privacy as privacy


class SitePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.site = Path(self.temporary.name)
        (self.site / "search").mkdir()
        (self.site / "assets").mkdir()
        (self.site / "index.html").write_text("<h1>Visitor documentation</h1>")
        (self.site / "search/search_index.json").write_text(json.dumps({"docs": [{"location": "", "text": "Visitor"}]}))
        (self.site / "sitemap.xml").write_text('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://opaque.info/</loc></url></urlset>')

    def test_valid_generated_site_is_accepted(self):
        self.assertEqual(privacy.inspect_site(self.site, "PRIVATEFIXTURE"), [])

    def test_every_output_kind_is_scanned_for_private_source_content(self):
        for name in ("index.html", "assets/copy.bin", "assets/bundle.js", "assets/copy.txt.gz"):
            with self.subTest(name=name):
                path = self.site / name
                prior = path.read_bytes() if path.exists() else None
                raw = b"PRIVATEFIXTURE"
                path.write_bytes(gzip.compress(raw) if name.endswith(".gz") else raw)
                self.assertTrue(any("sentinel leaked" in failure for failure in privacy.inspect_site(self.site, raw.decode())))
                if prior is None:
                    path.unlink()
                else:
                    path.write_bytes(prior)

    def test_private_and_unreviewed_html_routes_fail_outside_navigation(self):
        for name in ("product/report/index.html", "dogfood/index.html", "unreviewed/index.html"):
            with self.subTest(name=name):
                path = self.site / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("<h1>Not in navigation</h1>")
                self.assertTrue(any("allowlist" in failure for failure in privacy.inspect_site(self.site)))
                path.unlink()

    def test_search_and_sitemap_are_validated_as_data(self):
        (self.site / "search/search_index.json").write_text(json.dumps({"docs": [{"location": "unreviewed/#section"}]}))
        (self.site / "sitemap.xml").write_text('<urlset><url><loc>https://opaque.info/%70roduct/internal/</loc></url></urlset>')
        failures = privacy.inspect_site(self.site)
        self.assertTrue(any("non-visitor route indexed" in failure and "search" in failure for failure in failures))
        self.assertTrue(any("non-visitor route indexed" in failure and "sitemap" in failure for failure in failures))

    def test_compressed_sitemap_cannot_hide_different_routes(self):
        path = self.site / "sitemap.xml.gz"
        path.write_bytes(gzip.compress((self.site / "sitemap.xml").read_bytes()))
        self.assertEqual(privacy.inspect_site(self.site), [])
        path.write_bytes(gzip.compress(b"<urlset><url><loc>https://opaque.info/unreviewed/</loc></url></urlset>"))
        self.assertTrue(any("compressed sitemap differs" in failure for failure in privacy.inspect_site(self.site)))

    def test_missing_and_malformed_evidence_fails_closed(self):
        (self.site / "search/search_index.json").write_text("null")
        (self.site / "sitemap.xml").unlink()
        failures = privacy.inspect_site(self.site)
        self.assertTrue(any("malformed search" in failure for failure in failures))
        self.assertTrue(any("missing" in failure for failure in failures))

    def test_private_sources_and_raw_asset_receive_the_build_sentinel(self):
        docs = self.site / "docs"
        (docs / "product").mkdir(parents=True)
        (docs / "product/review.md").write_text("# Private review")
        self.assertGreater(privacy.mark_private_sources(docs, "PRIVATEFIXTURE"), 4)
        for name in ("product/review.md", "product/privacy-check.md", "product/privacy-check.txt", "dogfood.md", "release-dogfood.md", "tenant-boundaries.md", "README.md"):
            self.assertIn("PRIVATEFIXTURE", (docs / name).read_text())

    def test_encoded_private_links_and_opaque_archives_are_rejected(self):
        (self.site / "index.html").write_text('<a href="/%2570roduct/report/">private</a>')
        (self.site / "assets/uninspected.zip").write_bytes(b"not inspectable")
        failures = privacy.inspect_site(self.site)
        self.assertTrue(any("private document route" in failure for failure in failures))
        self.assertTrue(any("inspection failed" in failure for failure in failures))



class StandaloneWorkerPrivacyTests(unittest.TestCase):
    def test_real_standalone_build_excludes_private_canaries(self):
        self.assertEqual(privacy.build_and_inspect(), [])

    def test_worker_artifact_rejects_private_extra_and_symlink_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "site"
            self.assertEqual(privacy.package_worker_site(output), [])
            (output / "unexpected.json").write_text('{"private":"data"}')
            self.assertTrue(any("allowlist" in f for f in privacy.inspect_site(output, profile="worker")))
            (output / "unexpected.json").unlink()
            (output / "index.html").unlink()
            (output / "index.html").symlink_to(output / "approval/callback/index.html")
            self.assertTrue(any("symlink" in f for f in privacy.inspect_site(output, profile="worker")))

    def test_public_content_canary_is_detected_in_actual_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "source"
            import shutil
            shutil.copytree(privacy.ROOT / "deploy/cloudflare-demo/public", repository / "deploy/cloudflare-demo/public")
            (repository / "deploy/cloudflare-demo/public/index.html").write_text("OPAQUEPRIVATECANARY")
            findings = privacy.package_worker_site(Path(temporary) / "site", repository, "OPAQUEPRIVATECANARY")
            self.assertTrue(any("sentinel leaked" in f for f in findings))

class WorkerBuildIntegrationTests(unittest.TestCase):
    def test_build_is_repeatable_and_refuses_unknown_existing_content(self):
        import build_worker_site
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            shutil.copytree(privacy.ROOT / "deploy/cloudflare-demo/public", repository / "deploy/cloudflare-demo/public")
            output = build_worker_site.build(repository)
            self.assertEqual(privacy.inspect_site(output, profile="worker"), [])
            self.assertEqual(build_worker_site.build(repository), output)
            (output / "unknown-private.json").write_text("retained for operator inspection")
            with self.assertRaises(ValueError):
                build_worker_site.build(repository)
            self.assertTrue((output / "unknown-private.json").exists())

    def test_wrangler_publishes_only_the_gated_output(self):
        import tomllib
        config = tomllib.loads((privacy.ROOT / "deploy/cloudflare-demo/wrangler.toml").read_text())
        self.assertEqual(config["assets"]["directory"], "./.public-artifact")
        self.assertIn("build_worker_site.py", config["build"]["command"])


if __name__ == "__main__":
    unittest.main()
