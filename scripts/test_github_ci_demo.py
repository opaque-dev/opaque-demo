"""Explicit fixture tests. No external account, broker or model is contacted."""
import copy
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch
import uuid

import github_ci_demo as demo


def fixture():
    tenant = {"tenant_id": "fixture-tenant", "broker_id": str(uuid.uuid4())}
    snapshot = {"source": {"repository": "example/public-ci", "workflow_id": 7, "branch": "main"}, "repository_id": 9, "observed_at": 1700000000,
                "runs": [{"id": 10, "attempt": 1, "head_sha": "a" * 40, "status": "completed", "conclusion": "failure"}]}
    actions = [{"ordinal": n, "source_id": "github-ci-v1", "github_ci_snapshot": copy.deepcopy(snapshot), "source_snapshot_sha256": "b" * 64, "prompt_sha256": str(n) * 64,
                "profile_sha256": "c" * 64, "tenant": tenant} for n in range(1, 4)]
    return {"id": str(uuid.uuid4()), "manifest_digest": "d" * 64, "state": "planned", "manifest": {"schema_version": 3, "actions": actions},
            "slots": [{"action": copy.deepcopy(a), "state": "pending", "outcome": None} for a in actions], "tenant": tenant, "approval_mode": None, "expires_at": 1700000600}


class GithubDemoTests(unittest.TestCase):
    def test_refuses_synthetic_or_substituted_source_before_run(self):
        task = fixture()
        demo.validate(task, "example/public-ci", 7, "main")
        with self.assertRaises(ValueError):
            demo.validate(task, "example/other", 7, "main")
        task["manifest"]["actions"][0]["source_id"] = "opaque-public-receipts-v1"
        with self.assertRaises(ValueError):
            demo.validate(task)

    def test_cross_slot_substitution_and_receipt_binding_rejected(self):
        for field in ["source_snapshot_sha256", "github_ci_snapshot", "ordinal"]:
            task = fixture()
            task["slots"][0]["action"][field] = "changed"
            with self.assertRaises(ValueError):
                demo.validate(task)
        task = fixture()
        task["slots"][0]["outcome"] = {"inference_receipt": {"prompt_sha256": "wrong", "profile_sha256": "c" * 64, "tenant": task["tenant"]}}
        with self.assertRaises(ValueError):
            demo.validate(task)

    def test_output_escaped_and_missing_evidence_remains_missing(self):
        task = fixture()
        result = demo.render(task)
        self.assertIn("No observed model output.", result)
        self.assertNotIn("INSECURE TEST APPROVAL", result)
        action = task["slots"][0]["action"]
        task["approval_mode"] = "insecure_test"
        task["slots"][0]["state"] = "api_accepted"
        task["slots"][0]["outcome"] = {"inference_receipt": {"code": "completion_observed", "prompt_sha256": action["prompt_sha256"], "profile_sha256": action["profile_sha256"], "tenant": task["tenant"], "output_text": '<script>alert("secret")</script>', "input_tokens": 10, "observed_output_tokens": 9}}
        result = demo.render(task)
        self.assertIn("INSECURE TEST APPROVAL", result)
        self.assertNotIn("<script>", result)
        self.assertIn("&lt;script&gt;", result)

    def test_report_private_and_has_no_raw_owner_key(self):
        task = fixture()
        task["owner_key"] = "private-owner-key-not-for-html"
        with tempfile.TemporaryDirectory() as temp:
            path = demo.save(task, Path(temp) / "report")
            self.assertEqual(path.stat().st_mode & 0o077, 0)
            self.assertNotIn(task["owner_key"], path.read_text())
            self.assertTrue((path.parent / "brand/opaque.css").exists())

    def test_cli_uses_normal_task_commands_without_approval_flags(self):
        commands = []
        task = fixture()
        real_popen = demo.subprocess.Popen
        def popen(command, **kwargs):
            commands.append(command)
            return real_popen([sys.executable, "-c", "print(" + repr(json.dumps({"id": 1, "result": {"task": task}})) + ")"], **kwargs)
        with patch.object(demo.subprocess, "Popen", popen):
            self.assertEqual(demo.cli("/trusted/opaque", "/private/broker.sock", ["run", task["id"]]), task)
        self.assertEqual(commands, [["/trusted/opaque", "--json", "--socket", "/private/broker.sock", "task", "run", task["id"]]])

    def test_failed_execution_inspects_same_task_and_never_retries(self):
        task = fixture()
        task["state"] = "partial"
        task["slots"][0]["state"] = "unknown"
        calls = []
        def cli(binary, socket, args):
            calls.append(args)
            if args[0] == "run":
                raise ValueError("connection lost")
            return task
        with tempfile.TemporaryDirectory() as temp, patch.object(demo, "cli", cli), patch("sys.argv", ["demo", "--output", str(Path(temp) / "report"), "run", task["id"]]):
            with self.assertRaisesRegex(ValueError, "without retry"):
                demo.main()
            self.assertIn("Unknown · allowance charged", (Path(temp) / "report/index.html").read_text())
        self.assertEqual(calls, [["show", task["id"]], ["run", task["id"]], ["show", task["id"]]])

    def test_wrong_task_or_changed_manifest_is_never_displayed(self):
        task = fixture()
        with self.assertRaisesRegex(ValueError, "different task"):
            demo.validate(task, expected_id=str(uuid.uuid4()))
        with self.assertRaisesRegex(ValueError, "changed the selected manifest"):
            demo.validate(task, expected_id=task["id"], expected_digest="a" * 64)
        task["slots"][0]["state"] = "api_accepted"
        with self.assertRaisesRegex(ValueError, "missing its observed"):
            demo.render(task)

    def test_oversized_stdout_is_terminated_before_process_finishes(self):
        real_popen = demo.subprocess.Popen
        processes = []
        def popen(command, **kwargs):
            process = real_popen([sys.executable, "-c", "import sys,time; sys.stdout.write('x'*2000000); sys.stdout.flush(); time.sleep(180)"], **kwargs)
            processes.append(process)
            return process
        with patch.object(demo.subprocess, "Popen", popen):
            with self.assertRaisesRegex(ValueError, "exceeds the report limit"):
                demo.cli("/trusted/opaque", None, ["show", str(uuid.uuid4())])
        self.assertIsNotNone(processes[0].returncode)



class GithubClientProvenanceTests(unittest.TestCase):
    def test_manifest_selects_verified_cli_before_contacting_broker(self):
        task = fixture()
        calls = []
        manifest = {"schema":"opaque.demo.core-binaries.v1", "source":{"revision":"a"*40}}
        def cli(binary, socket, args):
            calls.append(binary)
            return task
        with tempfile.TemporaryDirectory() as temporary, patch.object(demo.artifacts, "verify_core", return_value=(manifest,{"opaque":Path("/verified/opaque")})), patch.object(demo, "cli", cli), patch.object(demo.artifacts,"source_snapshot",return_value={"revision":"b"*40}), patch("sys.argv", ["demo", "--core-manifest","/private/build.json","--core-dir","/source","--core-revision","a"*40,"--output",str(Path(temporary)/"report"),"show",task["id"]]):
            demo.main()
            self.assertEqual(calls, ["/verified/opaque"])
            proof=json.loads((Path(temporary)/"report/client-provenance.json").read_text())
            self.assertEqual(proof["manifest_digest"],task["manifest_digest"])
            self.assertEqual(proof["client_build"],manifest)
            self.assertEqual(len(proof["report_sha256"]),64)

    def test_invalid_manifest_prevents_all_broker_requests(self):
        with patch.object(demo.artifacts,"verify_core",side_effect=ValueError("digest mismatch")), patch.object(demo,"cli") as cli, patch("sys.argv",["demo","--core-manifest","/private/build.json","--core-dir","/source","--core-revision","a"*40,"show",str(uuid.uuid4())]):
            with self.assertRaisesRegex(ValueError,"digest mismatch"):
                demo.main()
            cli.assert_not_called()


if __name__ == "__main__":
    unittest.main()
