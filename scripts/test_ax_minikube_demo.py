"""Prevent web authority bypass and undeclared credential mounts."""
import http.client
import http.server
from functools import partial
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock
from string import Template

import yaml

import ax_minikube_demo as demo


class DemoBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fake = Mock()
        self.fake.cache = None
        self.fake.busy = False
        self.fake.error = None
        self.fake.read.return_value = {"phase": "awaiting_start"}
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(demo.Handler, demo=self.fake))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
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

    def test_loading_view_cannot_start_or_restart_work(self):
        self.assertEqual(self.request("GET", "/api/state")[0], 200)
        self.fake.action.assert_not_called()

    def test_foreign_missing_origin_and_rebound_host_cannot_mutate(self):
        for headers in ({}, {"Origin": "https://example.invalid"},
                        {"Origin": "http://" + self.host, "Host": "example.invalid"}):
            self.assertEqual(self.request("POST", "/api/run", **headers)[0], 403)
        self.fake.action.assert_not_called()
        self.assertEqual(self.request("POST", "/api/run", Origin="http://" + self.host)[0], 202)
        self.fake.action.assert_called_once_with("run")

    def test_private_files_and_arbitrary_actions_are_unreachable(self):
        for path in ("/cluster.json", "/../scripts/ax_minikube_demo.py", "/workspace/demo/run/producer.key"):
            self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("POST", "/api/delete", Origin="http://" + self.host)[0], 404)
        self.fake.action.assert_not_called()

    def test_completed_or_unknown_run_cannot_dispatch_again(self):
        instance = object.__new__(demo.Demo)
        instance.lock, instance.busy = threading.Lock(), False
        instance.read = Mock(return_value={"phase": "inspected"})
        for phase in ("awaiting_delegation", "awaiting_scope_review", "ready", "inspected", "held", "running"):
            instance.read.return_value = {"phase": phase}
            with self.subTest(phase=phase), self.assertRaisesRegex(ValueError, "run requires"):
                instance.action("run")

    def test_approved_scope_still_requires_observed_policy_match(self):
        instance = object.__new__(demo.Demo)
        instance.lock, instance.busy = threading.Lock(), False
        instance.read = Mock(return_value={"phase": "ready", "policy_verified": False})
        instance.signal = Mock()
        with self.assertRaises(ValueError):
            instance.action("run")
        instance.signal.assert_not_called()
        instance.read.return_value["policy_verified"] = True
        instance.action("run")
        instance.signal.assert_called_once_with("start.json", {"requested": True})

    def test_browser_cannot_open_native_review_or_restart_runner(self):
        for action in ("review", "delegate", "activate", "restart"):
            self.assertEqual(self.request("POST", "/api/" + action, Origin="http://" + self.host)[0], 404)
        self.fake.action.assert_not_called()

    def test_ax_pod_cannot_mount_broker_or_reviewer_custody(self):
        raw = (demo.EXAMPLE / "k8s/runner.yaml").read_text()
        pod = yaml.safe_load(Template(raw).substitute(IMAGE="opaque-ax-demo:test"))["spec"]["template"]["spec"]
        self.assertFalse(pod.get("shareProcessNamespace", False))
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertNotIn("fsGroup", pod["securityContext"], "fsGroup would rewrite the shared socket's custody")
        claims = {v["persistentVolumeClaim"]["claimName"] for v in pod["volumes"] if "persistentVolumeClaim" in v}
        self.assertEqual(claims, {"broker-socket", "evidence"})
        mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
        self.assertTrue(mounts["/run/opaque"]["readOnly"])
        self.assertNotIn("/var/lib/opaque", mounts)
        self.assertTrue(all("hostPath" not in v and "secret" not in v for v in pod["volumes"]))

    def test_broker_capability_argument_remains_one_yaml_scalar(self):
        raw = (demo.EXAMPLE / "k8s/broker.yaml").read_text()
        pod = yaml.safe_load(Template(raw).substitute(IMAGE="opaque-ax-demo:test", CONFIG_MAP="test"))["spec"]["template"]["spec"]
        broker = next(c for c in pod["containers"] if c["name"] == "broker")
        self.assertIn("--bounding-set=-all,+sys_ptrace", broker["command"])
        self.assertNotIn("+sys_ptrace", broker["command"])


if __name__ == "__main__":
    unittest.main()
