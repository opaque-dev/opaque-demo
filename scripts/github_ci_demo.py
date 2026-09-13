#!/usr/bin/env python3
"""Run the real Opaque task CLI and render a read-only GitHub CI receipt view.

No approval, inference, source fetch or task ledger is implemented here. The
configured broker owns all of those. Reports are local, private runtime output.
"""
import argparse
import datetime
import html
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import tempfile
import time
import uuid

import demo_artifacts as artifacts

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 1024 * 1024


def cli(binary, socket, args):
    command = [binary, "--json"]
    if socket:
        command += ["--socket", socket]
    command += ["task", *args]
    # stderr stays in the terminal. Bound stdout while reading it, including
    # when the CLI stalls or never terminates. Never persist a raw response.
    process = subprocess.Popen(command, stdout=subprocess.PIPE, bufsize=0)
    output = bytearray()
    deadline = time.monotonic() + 180
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise subprocess.TimeoutExpired(command, 180)
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                if len(output) + len(chunk) > LIMIT:
                    raise ValueError("broker response exceeds the report limit")
                output.extend(chunk)
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
        try:
            response = json.loads(output)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("CLI did not return a JSON broker response") from error
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
    if process.returncode or not isinstance(response, dict) or response.get("error"):
        raise ValueError("broker refused or could not complete the request; inspect the CLI status")
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError("missing broker result")
    return result.get("task", result)


def validate(task, expected_repository=None, expected_workflow=None, expected_branch=None, *, expected_id=None, expected_digest=None):
    """Validate the displayed subset; the broker remains the contract verifier."""
    if not isinstance(task, dict):
        raise ValueError("missing task record")
    uuid.UUID(task["id"])
    if expected_id is not None and task["id"] != expected_id:
        raise ValueError("broker returned a different task")
    if expected_digest is not None and task["manifest_digest"] != expected_digest:
        raise ValueError("broker changed the selected manifest")
    if not re.fullmatch(r"[0-9a-f]{64}", task["manifest_digest"]):
        raise ValueError("invalid manifest digest")
    if task["state"] not in {"planned", "running", "completed", "partial", "revoked", "expired"}:
        raise ValueError("unknown task state")
    manifest = task["manifest"]
    if manifest["schema_version"] != 3 or len(manifest["actions"]) != 3 or len(task["slots"]) != 3:
        raise ValueError("expected three-slot inference task")
    first = manifest["actions"][0]
    snapshot = first.get("github_ci_snapshot")
    if first["source_id"] != "github-ci-v1" or not isinstance(snapshot, dict):
        raise ValueError("broker did not capture a GitHub source; synthetic tasks are not accepted")
    source = snapshot["source"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source["repository"]):
        raise ValueError("invalid repository")
    if not isinstance(source["workflow_id"], int) or source["workflow_id"] <= 0:
        raise ValueError("invalid workflow ID")
    for actual, expected in [(source["repository"], expected_repository), (source["workflow_id"], expected_workflow), (source["branch"], expected_branch)]:
        if expected is not None and actual != expected:
            raise ValueError("broker source differs from the requested demo source")
    if len(snapshot["runs"]) > 3:
        raise ValueError("unbounded run sample")
    ordinals = set()
    for action, slot in zip(manifest["actions"], task["slots"]):
        ordinals.add(action["ordinal"])
        if action.get("github_ci_snapshot") != snapshot or slot["action"] != action:
            raise ValueError("snapshot or slot binding differs")
        if action["source_id"] != "github-ci-v1" or action["source_snapshot_sha256"] != first["source_snapshot_sha256"]:
            raise ValueError("source binding differs")
        if slot["state"] not in {"pending", "reserved", "api_accepted", "rejected", "unknown"}:
            raise ValueError("unknown slot state")
        receipt = (slot.get("outcome") or {}).get("inference_receipt")
        if slot["state"] == "api_accepted" and (not receipt or receipt.get("code") != "completion_observed" or not isinstance(receipt.get("output_text"), str)):
            raise ValueError("completion is missing its observed output receipt")
        if receipt and (receipt["prompt_sha256"] != action["prompt_sha256"] or receipt["profile_sha256"] != action["profile_sha256"] or receipt["tenant"] != action["tenant"]):
            raise ValueError("inference receipt binding differs")
    if ordinals != {1, 2, 3}:
        raise ValueError("invalid inference ordinals")
    return snapshot


def esc(value):
    return html.escape(str(value), quote=True)


def timestamp(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%d %b %Y · %H:%M:%S UTC")


def render(task):
    snapshot = validate(task)
    source = snapshot["source"]
    states = {"pending": "Not attempted", "reserved": "In flight · allowance charged", "api_accepted": "Completion observed", "rejected": "Rejected · allowance charged", "unknown": "Unknown · allowance charged"}
    rows = []
    for run in snapshot["runs"]:
        run_id = int(run["id"])
        rows.append(f'<tr><td><a href="https://github.com/{esc(source["repository"])}/actions/runs/{run_id}" rel="noreferrer">{run_id} ↗</a></td><td>{esc(run["head_sha"][:12])}</td><td>{esc(run["status"])}</td><td>{esc(run["conclusion"] or "Pending")}</td></tr>')
    cards = []
    for slot in task["slots"]:
        action = slot["action"]
        receipt = (slot.get("outcome") or {}).get("inference_receipt")
        output = receipt.get("output_text") if receipt else None
        content = f'<p class="answer">{esc(output)}</p>' if output else '<p class="muted">No observed model output.</p>'
        usage = f'{receipt.get("input_tokens", "—")} input / {receipt.get("observed_output_tokens", "—")} output tokens' if receipt else '96 output tokens reserved only when attempted'
        cards.append(f'<article><div class="label">REQUEST {int(action["ordinal"]):02d}</div><h3>{esc(states[slot["state"]])}</h3>{content}<div class="usage">{esc(usage)}</div><details><summary>Prompt and receipt binding</summary><code>{esc(action["prompt_sha256"])}</code><pre>{esc(json.dumps(receipt, indent=2))}</pre></details></article>')
    approval = task.get("approval_mode") or "Not granted"
    if approval == "insecure_test":
        approval = "INSECURE TEST APPROVAL — fixture validation only"
    context = {
        "task_id": task["id"], "manifest_digest": task["manifest_digest"],
        "source_snapshot_sha256": task["manifest"]["actions"][0]["source_snapshot_sha256"],
        "tenant": task.get("tenant"), "approval_mode": task.get("approval_mode"),
        "approved_at": task.get("approved_at"), "expires_at": task["expires_at"],
        "workstation_receipt": task.get("workstation_receipt"),
    }
    template = (ROOT / "examples/github-ci/report.html").read_text()
    replacements = {
        "REPOSITORY": esc(source["repository"]), "BRANCH": esc(source["branch"]),
        "WORKFLOW": str(int(source["workflow_id"])), "OBSERVED": esc(timestamp(snapshot["observed_at"])),
        "STATE": esc(task["state"]), "APPROVAL": esc(approval),
        "RUN_ROWS": "".join(rows) or '<tr><td colspan="4">No runs in the captured sample.</td></tr>',
        "RECEIPTS": "".join(cards), "CONTEXT": esc(json.dumps(context, indent=2)),
        "TASK_ID": esc(task["id"]),
    }
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda match: replacements[match[1]], template)


def save(task, directory, client_provenance=None):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError("report directory must be private (chmod 700) and not a symlink")
    # Publish only a display projection. Do not copy raw responses or owner keys.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as file:
        temporary = Path(file.name)
        file.write(render(task))
    destination = directory / "index.html"
    temporary.replace(destination)
    assets = directory / "brand"
    if assets.is_symlink():
        raise ValueError("report assets directory must not be a symlink")
    if not assets.exists():
        shutil.copytree(ROOT / "assets/brand", assets)
    if client_provenance is not None:
        artifacts.write_json(directory / "client-provenance.json", {"schema": "opaque.demo.github-client.v1", "client_build": client_provenance, "demo_source": artifacts.source_snapshot(ROOT, allow_dirty=True), "task_id": task["id"], "manifest_digest": task["manifest_digest"], "report_sha256": artifacts.digest(destination), "scope": "CLI artifact and local report only; connected broker deployment is a separate acceptance check"})
    return destination.resolve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opaque", default="opaque", help="trusted core CLI executable")
    parser.add_argument("--socket", help="configured broker socket (defaults to opaque's socket)")
    parser.add_argument("--core-manifest", type=Path, help="Verify a local core build manifest before any broker request")
    parser.add_argument("--core-dir", type=Path)
    parser.add_argument("--core-revision")
    parser.add_argument("--allow-dirty-core", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(tempfile.gettempdir()) / f"opaque-github-ci-{os.getuid()}")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="capture configured public GitHub source and plan three requests")
    plan.add_argument("--repository", required=True)
    plan.add_argument("--workflow-id", type=int, required=True)
    plan.add_argument("--branch", default="main")
    plan.add_argument("--title", default="Review public GitHub CI evidence")
    for name in ["run", "show", "revoke"]:
        command = sub.add_parser(name)
        command.add_argument("task_id", type=lambda value: str(uuid.UUID(value)))
    args = parser.parse_args()
    client_provenance = None
    if any((args.core_manifest, args.core_dir, args.core_revision)):
        if not all((args.core_manifest, args.core_dir, args.core_revision)):
            parser.error("--core-manifest, --core-dir and --core-revision must be supplied together")
        client_provenance, binaries = artifacts.verify_core(args.core_manifest, args.core_dir, args.core_revision, args.allow_dirty_core)
        if args.opaque != "opaque" and Path(args.opaque).resolve() != binaries["opaque"]:
            parser.error("--opaque differs from the verified manifest's CLI")
        args.opaque = str(binaries["opaque"])
    if args.command == "plan":
        task = cli(args.opaque, args.socket, ["plan-inference", "--title", args.title, "--expires-in-secs", "600"])
        validate(task, args.repository, args.workflow_id, args.branch)
    else:
        # Check source before requesting an execution. Reuse an existing task;
        # never re-plan or retry a charged allowance after a failure.
        task = cli(args.opaque, args.socket, ["show", args.task_id])
        validate(task, expected_id=args.task_id)
        digest = task["manifest_digest"]
        if args.command != "show":
            try:
                task = cli(args.opaque, args.socket, [args.command, args.task_id])
                validate(task, expected_id=args.task_id, expected_digest=digest)
            except (ValueError, subprocess.TimeoutExpired):
                task = cli(args.opaque, args.socket, ["show", args.task_id])
                validate(task, expected_id=args.task_id, expected_digest=digest)
                path = save(task, args.output, client_provenance)
                raise ValueError(f"request failed or timed out; inspected existing task without retry. Receipt: {path}")
    path = save(task, args.output, client_provenance)
    print(f'Task: {task["id"]}\nState: {task["state"]}\nReport: {path}')
    if args.command == "plan":
        print("Run the same task ID to request normal broker approval. Planning does not generate model output.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as error:
        raise SystemExit(str(error)) from error
