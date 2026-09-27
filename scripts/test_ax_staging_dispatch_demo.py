"""Staging-dispatch demo fixtures: policy shape, credential boundaries, no web authority bypass."""
import http.client
import http.server
import importlib.util
import json
from functools import partial
from pathlib import Path
from string import Template
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "ax-staging-dispatch"
TARGET = "opaque-dev/opaque-staging-scratch:.github/workflows/staging.yml:main"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


demo = load_module("ax_staging_dispatch_demo", EXAMPLE / "demo.py")


def load_worker():
    """worker.py imports the public core adapter from the image path; supply a
    minimal stand-in with the same request-ID derivation so the correlation
    logic can be tested on a host without the core checkout."""
    import hashlib
    import uuid as uuid_module
    import re

    adapter = types.ModuleType("adapter")
    adapter.identifier = lambda v: v if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,128}", v) else (_ for _ in ()).throw(ValueError("id"))
    adapter.canonical_uuid = lambda v: v if str(uuid_module.UUID(v)) == v else (_ for _ in ()).throw(ValueError("uuid"))
    adapter.task_identity = lambda data: ("local-demo", "opaque-staging-dispatch")

    def request_id(context):
        identity = ["opaque.ax.logical-action.v1", context["deployment_id"], context["atespace"], context["task_name"], context["run_id"],
                    context["action_key"], context["requester_id"], context["scope_id"], context["tenant_id"], context["broker_id"], context["generation"]]
        return "ax-" + hashlib.sha256(json.dumps(identity, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    adapter.request_id = request_id
    adapter.write_new = Mock()
    adapter.fetch_metadata = Mock()
    with patch.dict(sys.modules, {"adapter": adapter}):
        return load_module("ax_staging_dispatch_worker", EXAMPLE / "worker.py")


def values(**overrides):
    base = dict(deployment_id="opaque-dispatch-0123456789", run_id="00000000-0000-4000-8000-000000000001", action_key="dispatch-staging-1",
                requester_id="human:requester", scope_id="scope-1", issuance_round_id="00000000-0000-4000-8000-000000000002",
                resource=TARGET, tenant_id="opaque-ax-dispatch", broker_id="broker-1", generation="1")
    base.update(overrides)
    return base


class PolicyAndWorkloadTests(unittest.TestCase):
    def setUp(self):
        self.policy = yaml.safe_load((EXAMPLE / "policies/staging-dispatch.yaml").read_text())
        self.scope = json.loads((EXAMPLE / "workload/scope.json").read_text())
        self.actions = json.loads((EXAMPLE / "workload/actions.json").read_text())

    def test_policy_allows_exactly_one_workflow_two_attempts_ten_minutes_with_scope_review(self):
        spec = self.policy["spec"]
        self.assertEqual(self.policy["kind"], "AuthorityPolicy")
        self.assertEqual(spec["authority"]["operation"], "github.workflow.dispatch")
        self.assertEqual(spec["authority"]["workflows"], [{"repository": "opaque-dev/opaque-staging-scratch",
                                                           "path": ".github/workflows/staging.yml", "ref": "main"}])
        self.assertEqual((spec["authority"]["maxResources"], spec["authority"]["maxAttempts"], spec["authority"]["maxDuration"]), (1, 2, "10m"))
        self.assertEqual(spec["approval"], {"scope": "Required", "action": "WithinApprovedScope", "reviewerRef": "native-reviewer"})
        self.assertNotIn("allowedStatuses", spec["authority"])
        self.assertEqual(spec["tenantRef"], demo.TENANT)

    def test_scope_request_names_only_the_policy_target_within_its_ceilings(self):
        self.assertEqual(self.scope, {"resources": [TARGET], "expires_in_secs": 600, "max_attempts": 2})
        self.assertNotIn("statuses", self.scope)

    def test_workload_proposes_one_foreign_workflow_and_two_in_scope_dispatches(self):
        resources = [a["resource"] for a in self.actions]
        self.assertEqual(len(self.actions), 3)
        self.assertEqual(resources.count(TARGET), 2)
        foreign = [r for r in resources if r != TARGET]
        self.assertEqual(foreign, ["opaque-dev/opaque-staging-scratch:.github/workflows/production.yml:main"])
        self.assertEqual(self.actions[0]["action_key"], "dispatch-production", "the denial comes first so the SIGKILL variant still shows all three")
        self.assertEqual({a["action_key"] for a in self.actions}, {"dispatch-production", "dispatch-staging-1", "dispatch-staging-2"})

    def test_broker_template_binds_the_policy_and_keeps_the_token_in_broker_custody(self):
        template = (EXAMPLE / "broker/config.toml.in").read_text()
        for line in ('endpoint = "https://api.github.com/"', 'token_file = "/var/lib/opaque/github.token"',
                     'path = "/etc/opaque/policy/staging-dispatch.yaml"', 'name = "ax-staging-dispatch"', 'tenant_ref = "opaque-ax-dispatch"',
                     'connector_ref = "github"', 'reviewer_ref = "native-reviewer"', 'digest = "${POLICY_DIGEST}"', "workstation_test_mode = false",
                     'approval_backend = "native"', "require_seal = true"):
            self.assertIn(line, template)
        self.assertNotIn("ca_certificate_file", template)
        self.assertNotIn("insecure_auto_approve", template)
        self.assertNotIn("[scope_workflow]", template)


class CredentialBoundaryTests(unittest.TestCase):
    def pod(self, name, **values):
        raw = (EXAMPLE / "k8s" / name).read_text()
        return yaml.safe_load(Template(raw).substitute(IMAGE="opaque-ax-staging-dispatch:test", **values))

    def test_ax_pod_mounts_no_secret_token_or_broker_custody(self):
        pod = self.pod("runner.yaml")["spec"]["template"]["spec"]
        self.assertFalse(pod.get("shareProcessNamespace", False))
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertNotIn("fsGroup", pod["securityContext"])
        claims = {v["persistentVolumeClaim"]["claimName"] for v in pod["volumes"] if "persistentVolumeClaim" in v}
        self.assertEqual(claims, {"broker-socket", "evidence"})
        mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
        self.assertTrue(mounts["/run/opaque"]["readOnly"])
        self.assertNotIn("/var/lib/opaque", mounts)
        self.assertTrue(all("hostPath" not in v and "secret" not in v for v in pod["volumes"]))
        self.assertNotIn("github", json.dumps(pod).lower())

    def test_token_secret_reaches_only_the_broker_init_container(self):
        pod = self.pod("broker.yaml", CONFIG_MAP="test")["spec"]["template"]["spec"]
        secrets = [v for v in pod["volumes"] if "secret" in v]
        self.assertEqual(secrets, [{"name": "github-token", "secret": {"secretName": "github-token", "defaultMode": 0o400}}])
        init = {c["name"]: c for c in pod["initContainers"]}["install"]
        token_mounts = [m for m in init["volumeMounts"] if m["name"] == "github-token"]
        self.assertEqual(token_mounts, [{"name": "github-token", "mountPath": "/input/github", "readOnly": True}])
        for container in pod["containers"]:
            self.assertFalse(any(m["name"] == "github-token" for m in container["volumeMounts"]), container["name"])
        broker = next(c for c in pod["containers"] if c["name"] == "broker")
        self.assertIn("--bounding-set=-all,+sys_ptrace", broker["command"])
        self.assertNotIn("+sys_ptrace", broker["command"])

    def test_install_script_installs_the_token_only_at_policy_activation_as_private_broker_file(self):
        script = (EXAMPLE / "broker/install.sh").read_text()
        self.assertIn("install -o 7581 -g 7581 -m 0600 /input/github/token /state/custody/github.token", script)
        self.assertIn("grep -q '^\\[authority_policy\\]' /input/config.toml", script)
        self.assertNotIn("provider-ca", script)

    def test_evidence_pod_runs_as_custody_account_with_memory_only_key_storage(self):
        pod = self.pod("evidence.yaml")["spec"]
        self.assertEqual(pod["securityContext"], {"runAsUser": 7581, "runAsGroup": 7581, "runAsNonRoot": True})
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertFalse(pod["automountServiceAccountToken"])
        out = next(v for v in pod["volumes"] if v["name"] == "out")
        self.assertEqual(out["emptyDir"], {"medium": "Memory"})

    def test_deploy_refuses_group_readable_or_oversized_token_files(self):
        instance = object.__new__(demo.Demo)
        instance.cluster = {"run_id": "abc"}
        instance.k = Mock(return_value="")
        instance.persist = Mock()
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("github_pat_placeholder\n")
            os.chmod(token, 0o644)
            with self.assertRaisesRegex(ValueError, "group or world readable"):
                instance.token_secret(token)
            os.chmod(token, 0o600)
            instance.token_secret(token)
            self.assertNotIn("github_pat_placeholder", json.dumps(instance.cluster))
            created = [c for c in instance.k.call_args_list if c.args[:3] == ("create", "secret", "generic")]
            self.assertEqual(len(created), 1)
            self.assertIn(f"--from-file=token={token}", created[0].args)
            big = Path(directory) / "big"
            big.write_text("x" * 4097)
            os.chmod(big, 0o600)
            instance.k.return_value = ""
            with self.assertRaisesRegex(ValueError, "4096"):
                instance.token_secret(big)


class WorkerCorrelationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.worker = load_worker()

    def test_request_id_binds_the_logical_action_not_the_target(self):
        context, manifest = self.worker.bind(b"", values())
        moved, _ = self.worker.bind(b"", values(resource="opaque-dev/opaque-staging-scratch:.github/workflows/production.yml:main"))
        self.assertEqual(context["request_id"], moved["request_id"], "a changed target under one action key is not a new attempt")
        other, _ = self.worker.bind(b"", values(action_key="dispatch-staging-2"))
        self.assertNotEqual(context["request_id"], other["request_id"])
        self.assertEqual(set(manifest), {"scope_id", "issuance_round_id", "resource", "request_id"})
        self.assertNotIn("status", manifest)
        self.assertEqual(context["kind"], "github.workflow.dispatch")

    def test_malformed_targets_are_refused_before_any_broker_call(self):
        for resource in ("case-101", "opaque-dev/opaque-staging-scratch", "opaque-dev/opaque-staging-scratch:staging.yml:main",
                         TARGET + ":extra", "opaque-dev/opaque-staging-scratch:.github/workflows/staging.yml:"):
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                self.worker.bind(b"", values(resource=resource))

    def test_reconcile_reads_outcomes_and_never_runs(self):
        worker = self.worker
        calls = []
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(worker, "ROOT", root), patch.object(worker, "WORKLOAD", EXAMPLE / "workload"), \
                    patch.object(worker, "cli", side_effect=lambda *a: calls.append(a) or {"result": {"state": "api_accepted"}}), \
                    patch.object(worker, "once", side_effect=lambda p, v: p.write_text(json.dumps(v))):
                (root / "view.json").write_text(json.dumps({"phase": "running", "scope_id": "scope-1"}))
                for key, answered in (("dispatch-staging-1", True), ("dispatch-staging-2", False)):
                    (root / key).mkdir()
                    (root / key / "correlation.json").write_text(json.dumps({"request_id": "ax-" + key}))
                    (root / key / "dispatch-attempt.json").write_text("{}")
                    if answered:
                        (root / key / "response.json").write_text(json.dumps({"result": {"state": "api_accepted"}}))
                        (root / key / "outcome.json").write_text(json.dumps({"result": {"state": "api_accepted"}}))
                worker.reconcile({"pod_uid": "pod"})
                view = json.loads((root / "view.json").read_text())
        self.assertEqual(calls, [("outcome", "--scope-id", "scope-1", "--request-id", "ax-dispatch-staging-2")])
        self.assertEqual(view["phase"], "held")
        states = {a["action"]["action_key"]: (a["state"], a["reconciled"], a["retry_authorized"]) for a in view["actions"]}
        self.assertEqual(states["dispatch-production"], ("not_attempted", False, False))
        self.assertEqual(states["dispatch-staging-1"], ("api_accepted", False, False))
        self.assertEqual(states["dispatch-staging-2"], ("api_accepted", True, False))

    def test_state_of_distinguishes_denials_from_lost_answers(self):
        worker = self.worker
        self.assertEqual(worker.state_of({"error": {"code": "scope_unavailable"}}, {"error": {"code": "scope_unavailable"}}), "denied")
        self.assertEqual(worker.state_of(None, {"error": {"code": "scope_unavailable"}}), "no_charged_attempt")
        self.assertEqual(worker.state_of(None, {"result": {"state": "unknown"}}), "unknown")


class HumanStopTests(unittest.TestCase):
    def test_human_steps_are_numbered_exact_commands_for_the_three_ceremonies_and_resume(self):
        steps = demo.human_steps(Path("/private/tmp/state"), python="/usr/bin/python3")
        commands = [s[0] for s in steps]
        self.assertEqual(len(commands), 5)
        for expected, command in zip(("delegate-reviewer", "delegate-requester", "scope-review", "run", "evidence"), commands):
            self.assertIn(" --state /private/tmp/state " + expected, command)
            self.assertTrue(command.startswith("/usr/bin/python3 -B "))
        self.assertIn(demo.KILL_VARIANT, commands[3])
        self.assertEqual(sum("native window" in s[1].lower() and "no native window" not in s[1].lower() for s in steps), 3)

    def test_shot_list_covers_every_required_beat(self):
        text = " ".join(shot for _, shot in demo.SHOT_LIST).lower()
        for beat in ("digest", "touch id", "denied", "api_accepted", "sigkill", "scope outcome", "opaque-evidence verify", "flipped byte", "actions tab"):
            self.assertIn(beat, text)


class WebBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fake = Mock()
        self.fake.cache = None
        self.fake.busy = False
        self.fake.error = None
        self.fake.read.return_value = {"phase": "awaiting_delegation"}
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(demo.Handler, demo=self.fake))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.host = f"127.0.0.1:{self.server.server_port}"

    def request(self, method, path, **headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def test_foreign_origin_cannot_start_work_and_private_files_are_unreachable(self):
        for headers in ({}, {"Origin": "https://example.invalid"}, {"Origin": "http://" + self.host, "Host": "example.invalid"}):
            self.assertEqual(self.request("POST", "/api/run", **headers)[0], 403)
        for path in ("/cluster.json", "/evidence/producer.json", "/../examples/ax-staging-dispatch/demo.py"):
            self.assertEqual(self.request("GET", path)[0], 404)
        for action in ("review", "delegate-reviewer", "scope-review", "evidence", "run-sigkill"):
            self.assertEqual(self.request("POST", "/api/" + action, Origin="http://" + self.host)[0], 404)
        self.fake.action.assert_not_called()
        self.assertEqual(self.request("POST", "/api/run", Origin="http://" + self.host)[0], 202)
        self.fake.action.assert_called_once_with("run")

    def test_run_requires_ready_phase_and_verified_policy(self):
        instance = object.__new__(demo.Demo)
        instance.lock, instance.busy = threading.Lock(), False
        instance.signal = Mock()
        for phase in ("awaiting_delegation", "awaiting_scope_review", "inspected", "held", "running"):
            instance.read = Mock(return_value={"phase": phase, "policy_verified": True})
            with self.subTest(phase=phase), self.assertRaisesRegex(ValueError, "run requires"):
                instance.action("run")
        instance.read = Mock(return_value={"phase": "ready", "policy_verified": False})
        with self.assertRaises(ValueError):
            instance.action("run")
        instance.signal.assert_not_called()
        instance.read = Mock(return_value={"phase": "ready", "policy_verified": True})
        instance.action("run")
        instance.signal.assert_called_once_with("start.json", {"requested": True})


if __name__ == "__main__":
    unittest.main()
