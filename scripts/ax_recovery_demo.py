#!/usr/bin/env python3
"""Run the pinned AX task runner in an owned minikube namespace and show evidence."""
import argparse
from functools import partial
import http.server
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
PROFILE = "opaque-ax-demo"
CORE_REVISION = "180e66fe6c854d962074e8ff4e694a29689af623"
AX_REVISION = "f009cc81c9a571073bc1dd58cd2ed934bf2d5b1c"
OWNER = "opaque.info/demo-run"


def command(args, *, data=None, timeout=60):
    return subprocess.run(args, input=data, capture_output=True, text=True,
                          check=True, timeout=timeout).stdout


def kubectl(*args, data=None, timeout=60):
    # Never use the ambient context or a production cluster.
    return command(["minikube", "-p", PROFILE, "kubectl", "--", *args], data=data, timeout=timeout)


def manifest(name, run_id, image):
    labels = {OWNER: run_id, "app": "opaque-ax-demo"}
    task = {"apiVersion": "ax.io/v1alpha1", "kind": "Task",
            "metadata": {"name": "opaque-recovery", "atespace": "synthetic"},
            "spec": {"debug": False, "workspaces": [{"name": "empty", "path": "/workspace/empty"}],
                     "command": ["python3", "-B", "/opt/opaque/worker.py"]}}
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "evidence", "namespace": name, "labels": labels},
         "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "runner", "namespace": name, "labels": labels},
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": labels},
                  "template": {"metadata": {"labels": labels}, "spec": {
                      "automountServiceAccountToken": False, "terminationGracePeriodSeconds": 15,
                      "securityContext": {"runAsUser": 7582, "runAsGroup": 7582, "runAsNonRoot": True, "fsGroup": 7582},
                      "containers": [{"name": "runner", "image": image, "imagePullPolicy": "Never",
                          "args": ["--port", "8080"],
                          "env": [{"name": "AX_TASK_YAML", "value": json.dumps(task)},
                                  {"name": "POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}}],
                          "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                              "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}},
                          "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "2", "memory": "512Mi"}},
                          "readinessProbe": {"httpGet": {"path": "/readyz", "port": 8080}, "periodSeconds": 2},
                          "volumeMounts": [{"name": "evidence", "mountPath": "/workspace"},
                                           {"name": "ax", "mountPath": "/ax"}, {"name": "tmp", "mountPath": "/tmp"}]}],
                      "volumes": [{"name": "evidence", "persistentVolumeClaim": {"claimName": "evidence"}},
                                  {"name": "ax", "emptyDir": {}}, {"name": "tmp", "emptyDir": {}}]}}}}]}


def save(path, value):
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def deploy(directory, image):
    if not directory.is_absolute() or any((p / ".git").exists() for p in (directory, *directory.parents)):
        raise ValueError("use a new absolute runtime directory outside Git")
    if not re.fullmatch(r"opaque-ax-demo:[a-zA-Z0-9_.-]+", image):
        raise ValueError("use a locally built opaque-ax-demo image")
    image_info = json.loads(command(["docker", "image", "inspect", image]))[0]
    labels = image_info["Config"]["Labels"]
    if labels.get("org.opencontainers.image.revision") != CORE_REVISION or labels.get("io.opaque.ax.revision") != AX_REVISION:
        raise ValueError("image does not carry the reviewed source pins")
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    run_id = uuid.uuid4().hex
    name = "opaque-ax-" + run_id[:10]
    state = {"profile": PROFILE, "namespace": name, "run_id": run_id, "image": image, "image_id": image_info["Id"],
             "core_revision": CORE_REVISION, "ax_revision": AX_REVISION, "status": "creating"}
    save(directory / "cluster.json", state)
    command(["minikube", "-p", PROFILE, "image", "load", image], timeout=180)
    namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": {OWNER: run_id}}}
    created = json.loads(kubectl("create", "-f", "-", "-o", "json", data=json.dumps(namespace)))
    state["namespace_uid"] = created["metadata"]["uid"]
    save(directory / "cluster.json", state)
    kubectl("create", "-f", "-", data=json.dumps(manifest(name, run_id, image)))
    kubectl("-n", name, "rollout", "status", "deployment/runner", "--timeout=120s", timeout=150)
    state["status"] = "ready"
    save(directory / "cluster.json", state)
    print(json.dumps(state, indent=2))


class Demo:
    def __init__(self, directory):
        self.directory = directory
        self.cluster = json.loads((directory / "cluster.json").read_text())
        if self.cluster.get("profile") != PROFILE or not re.fullmatch(r"opaque-ax-[0-9a-f]{10}", self.cluster["namespace"]):
            raise ValueError("unrecognized demo state")
        self.lock = threading.Lock()
        self.busy = False
        self.cache = None
        self.error = None

    def owned(self):
        obj = json.loads(kubectl("get", "namespace", self.cluster["namespace"], "-o", "json"))
        if obj["metadata"]["uid"] != self.cluster["namespace_uid"] or obj["metadata"].get("labels", {}).get(OWNER) != self.cluster["run_id"]:
            raise ValueError("namespace ownership changed")

    def read(self):
        self.owned()
        raw = kubectl("-n", self.cluster["namespace"], "exec", "deployment/runner", "--", "python3", "-c",
                      "from pathlib import Path; p=Path('/workspace/demo/view.json'); print(p.read_text() if p.exists() else '{\"phase\":\"starting\"}')")
        if len(raw) > 200000:
            raise ValueError("oversized view")
        value = json.loads(raw)
        value["namespace"] = self.cluster["namespace"]
        self.cache = value
        save(self.directory / "last-view.json", value)
        return value

    def action(self, kind):
        with self.lock:
            if self.busy:
                raise ValueError("another demo action is in progress")
            value = self.read()
            if kind == "run" and value["phase"] != "awaiting_start":
                raise ValueError("this experiment already started; inspect its retained result")
            if kind == "restart" and value["phase"] not in ("inspected", "recovered"):
                raise ValueError("inspect the completed first experiment before restarting")
            self.busy = True
            self.error = None
        def execute():
            try:
                self.owned()
                if kind == "run":
                    kubectl("-n", self.cluster["namespace"], "exec", "deployment/runner", "--", "python3", "-c",
                            "import os; p='/workspace/demo/start.json'; f=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(f,b'{\"synthetic_experiment_requested\":true}'); os.fsync(f); os.close(f)")
                elif kind == "restart":
                    before = value["pod_uid"]
                    save(self.directory / "before-restart.json", value)
                    kubectl("-n", self.cluster["namespace"], "rollout", "restart", "deployment/runner")
                    kubectl("-n", self.cluster["namespace"], "rollout", "status", "deployment/runner", "--timeout=120s", timeout=150)
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline:
                        after = self.read()
                        if after.get("pod_uid") != before and after["phase"] == "recovered":
                            for key in ("run_id", "checkpoint_sha256", "actions", "producer_sha256"):
                                if value[key] != after[key]:
                                    raise ValueError("restart changed retained evidence")
                            save(self.directory / "restart-verification.json", {"result": "passed", "before_pod_uid": before,
                                 "after_pod_uid": after["pod_uid"], "identity_and_evidence_unchanged": True,
                                 "producer_not_repeated": after["recovered_existing_run"], "core_revision": CORE_REVISION,
                                 "ax_revision": AX_REVISION, "scope": "AX runner pod replacement with a minikube PVC"})
                            break
                        if after["phase"] == "held":
                            raise ValueError("recovery is on hold")
                        time.sleep(1)
                    else:
                        raise ValueError("replacement verification timed out")
            except Exception as error:
                self.error = "Action failed or its outcome is uncertain. Inspect retained state before continuing."
                save(self.directory / "action-error.json", {"kind": kind, "error_type": type(error).__name__})
            finally:
                self.busy = False
        threading.Thread(target=execute, daemon=True).start()


class Handler(http.server.BaseHTTPRequestHandler):
    def __init__(self, *args, demo, **kwargs):
        self.demo = demo
        super().__init__(*args, **kwargs)

    def log_message(self, *_):
        pass

    def send(self, code, data, content_type="application/json"):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def host(self):
        return f"127.0.0.1:{self.server.server_port}"

    def do_GET(self):
        if self.headers.get("Host") != self.host():
            return self.send(403, {"error": "invalid host"})
        if self.path == "/api/state":
            try:
                state = self.demo.cache if self.demo.busy and self.demo.cache else self.demo.read()
                return self.send(200, {**state, "busy": self.demo.busy, "action_error": self.demo.error})
            except Exception:
                return self.send(503, {"error": "Cluster state unavailable. No action was retried."})
        files = {"/": ("examples/ax-recovery-fixture/index.html", "text/html; charset=utf-8"),
                 "/demo.js": ("examples/ax-recovery-fixture/demo.js", "text/javascript; charset=utf-8"),
                 "/demo.css": ("examples/ax-recovery-fixture/demo.css", "text/css; charset=utf-8"),
                 "/archivo.ttf": ("assets/brand/fonts/archivo-variable.ttf", "font/ttf"),
                 "/mono.ttf": ("assets/brand/fonts/ibm-plex-mono-regular.ttf", "font/ttf"),
                 "/archivo-license.txt": ("assets/brand/licenses/archivo-OFL.txt", "text/plain"),
                 "/mono-license.txt": ("assets/brand/licenses/ibm-plex-mono-OFL.txt", "text/plain")}
        item = files.get(self.path)
        if not item:
            return self.send(404, {"error": "not found"})
        self.send(200, (ROOT / item[0]).read_bytes(), item[1])

    def do_POST(self):
        if self.headers.get("Host") != self.host() or self.headers.get("Origin") != "http://" + self.host():
            return self.send(403, {"error": "same-origin local request required"})
        if self.headers.get("Content-Length", "0") != "0" or self.headers.get("Transfer-Encoding"):
            return self.send(400, {"error": "request body not accepted"})
        if self.path not in ("/api/run", "/api/restart"):
            return self.send(404, {"error": "not found"})
        try:
            self.demo.action(self.path.rsplit("/", 1)[1])
            self.send(202, {"accepted": True})
        except ValueError as error:
            self.send(409, {"error": str(error)})
        except Exception:
            self.send(503, {"error": "Cluster action unavailable; inspect current state."})


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("deploy")
    create.add_argument("--image", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=19740)
    sub.add_parser("inspect")
    args = parser.parse_args()
    if args.command == "deploy":
        deploy(args.state, args.image)
    elif args.command == "inspect":
        print(json.dumps(Demo(args.state).read(), indent=2))
    else:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), partial(Handler, demo=Demo(args.state)))
        print(f"AX + Opaque demo: http://127.0.0.1:{server.server_port}/", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
