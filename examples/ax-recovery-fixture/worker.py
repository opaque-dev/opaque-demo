#!/usr/bin/env python3
"""AX child command for the synthetic recovery demo; never a human approval."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, "/opt/opaque/ax-scope")
import check_runtime

ROOT = Path("/workspace/demo")


def save(path, value):
    temporary = path.with_suffix(".pending")
    with temporary.open("w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def binding(report):
    return {key: report[key] for key in ("run_id", "checkpoint_sha256", "actions")}


def main():
    os.umask(0o077)
    ROOT.mkdir(mode=0o700, exist_ok=True)
    with (ROOT / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = {"phase": "awaiting_start", "pod_uid": os.environ["POD_UID"],
                 "core_revision": os.environ["OPAQUE_CORE_REVISION"],
                 "ax_revision": check_runtime.AX_REVISION,
                 "approval": "synthetic signing in the public core experiment",
                 "native_human_review": False, "live_broker_rpc": False,
                 "agent_substrate": False, "kubernetes_pod": True,
                 "provider_effects": "synthetic local files", "independent_evaluation": False}
        save(ROOT / "view.json", state)
        while not (ROOT / "start.json").exists():
            time.sleep(0.25)
        if (ROOT / "failed.json").exists():
            save(ROOT / "view.json", {**state, "phase": "held", "error": "An earlier attempt failed. Retain this volume and inspect it; no automatic retry."})
            return
        output = ROOT / "run"
        output.mkdir(mode=0o700, exist_ok=True)
        first_file = ROOT / "first-report.json"
        first = json.loads(first_file.read_text()) if first_file.exists() else None
        # Publish no passing result until real verification completes.
        save(ROOT / "view.json", {**state, "phase": "inspecting" if first else "running"})
        try:
            report = check_runtime.inside(output, Path("/opt/opaque/scope-recovery"), Path("/opt/opaque/opaque-evidence"))
            producer_hash = hashlib.sha256((output / "public-reproduction.log").read_bytes()).hexdigest()
            if first:
                if binding(first["report"]) != binding(report) or first["producer_sha256"] != producer_hash:
                    raise ValueError("retained identity, action evidence or producer log changed")
                if not report["recovered_existing_run"]:
                    raise ValueError("restart repeated the producer")
            else:
                save(first_file, {"pod_uid": state["pod_uid"], "producer_sha256": producer_hash, "report": report})
                first = json.loads(first_file.read_text())
            recovered = first["pod_uid"] != state["pod_uid"]
            save(ROOT / "view.json", {**state, "phase": "recovered" if recovered else "inspected",
                 "run_id": report["run_id"], "checkpoint_sha256": report["checkpoint_sha256"],
                 "first_pod_uid": first["pod_uid"], "pod_replaced": recovered,
                 "recovered_existing_run": report["recovered_existing_run"],
                 "producer_sha256": producer_hash, "charged_attempts": report["charged_attempts"],
                 "unknown": report["unknown"], "api_accepted": 1, "missing_outcomes_held": report["missing_outcomes_held"],
                 "budget_denials": 12, "scope_revoked": True, "crash": report["public_core_crash"],
                 "changed_effect_rejected": report["changed_effect_rejected"],
                 "altered_export_rejected": report["altered_export_rejected"],
                 "review_signatures_verified": report["synthetic_review_signatures_verified"],
                 "actions": report["actions"]})
        except Exception as error:
            save(ROOT / "failed.json", {"error_type": type(error).__name__})
            save(ROOT / "view.json", {**state, "phase": "held", "error": "Execution or verification failed. No automatic retry; inspect retained private logs."})
            raise


if __name__ == "__main__":
    main()
