#!/usr/bin/env python3
"""Record bounded local broker fixtures using verified canonical core binaries."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import selectors
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

import demo_artifacts as artifacts

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ("quickstart", "sandbox-exec", "security-audit-detail-leak", "security-sandbox-secret-leak", "security-onepassword-read-field")


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


def bounded(command, *, env=None, cwd=None, timeout=30, limit=1024 * 1024):
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, bufsize=0)
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ValueError("recording command exceeded its time bound")
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                if len(output) + len(chunk) > limit:
                    raise ValueError("recording command exceeded its output bound")
                output.extend(chunk)
        process.wait(timeout=max(.001, deadline-time.monotonic()))
        return process.returncode, output.decode("utf-8", errors="replace")
    finally:
        stop(process)
        process.stdout.close()


def execute_scenario(name, binaries):
    # Every child gets fresh state and an explicit environment. No ambient
    # provider credentials, existing socket, keychain lookup or browser session.
    with tempfile.TemporaryDirectory(prefix="odr-", dir=Path(tempfile.gettempdir()).resolve()) as temporary:
        root = Path(temporary)
        home, runtime, project = [root / p for p in ("home", "run", "project")]
        for path in (home, runtime, project):
            path.mkdir(mode=0o700)
        state = home / ".opaque"
        (state / "profiles").mkdir(parents=True, mode=0o700)
        config = state / "config.toml"
        scope = "test.noop" if name == "quickstart" else "onepassword.*" if name == "security-onepassword-read-field" else "sandbox.exec"
        policy = f'''approval_backend = "insecure_auto_approve"
[[rules]]
name = "disposable-recording-only"
operation_pattern = "{scope}"
allow = true
client_types = ["agent"]
[rules.approval]
require = "always"
factors = ["local_bio"]
'''
        if name == "security-sandbox-secret-leak":
            policy += '[rules.secret_names]\npatterns = ["env:ALLOWED_*"]\n'
        config.write_text(policy)
        config.chmod(0o600)
        profile = f'''[profile]
name = "recording"
project_dir = {json.dumps(str(project))}
[network]
allow = []
[secrets]
'''
        if name == "security-sandbox-secret-leak":
            profile += 'DEMO_VALUE = "env:OPAQUE_RECORDING_SYNTHETIC_VALUE"\n'
        profile += '[limits]\ntimeout_secs = 5\nmax_output_bytes = 4096\n'
        (state / "profiles/recording.toml").write_text(profile)
        env = {"HOME": str(home), "XDG_RUNTIME_DIR": str(runtime), "OPAQUE_CONFIG": str(config), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8", "TERM": "xterm-256color", "NO_COLOR": "1", "OPAQUE_INSECURE_AUTO_APPROVE": "1", "OPAQUE_RECORDING_SYNTHETIC_VALUE": "synthetic_recording_value", "OPAQUE_1PASSWORD_TOKEN_REF": "env:OPAQUE_RECORDING_SYNTHETIC_VALUE", "OPAQUE_1PASSWORD_CONNECT_URL": "http://127.0.0.1:1"}
        daemon = None
        print("SYNTHETIC LOCAL FIXTURE · test approval only · no real provider credentials", flush=True)
        def cli(args, expected_error=None):
            print("$ opaque " + shlex.join(args), flush=True)
            code, output = bounded([str(binaries["opaque"]), "--json", *args], env=env, cwd=project)
            try:
                if args[:2] == ["audit", "tail"]:
                    events = [json.loads(line) for line in output.splitlines() if line.strip()]
                    if len(events) > 8 or any(not isinstance(event, dict) or "event_id" not in event for event in events):
                        raise ValueError("invalid audit JSONL")
                    response = {"audit_events": events}
                else:
                    response = json.loads(output)
            except ValueError:
                raise ValueError("core CLI returned non-JSON output; recording rejected") from None
            if expected_error:
                if not code or response.get("error", {}).get("code") not in expected_error:
                    raise ValueError("expected broker rejection was not observed (code=" + str(response.get("error", {}).get("code")) + ", exit=" + str(code) + ")")
            elif code or response.get("error"):
                raise ValueError("broker command failed; host capability or source contract needs qualification")
            print(json.dumps(response, indent=2), flush=True)
            return response
        with (root / "daemon.log").open("wb") as log:
            try:
                # Signing a disposable config seal is a local file operation.
                code, _ = bounded([str(binaries["opaque"]), "setup", "--seal"], env=env, cwd=project)
                if code:
                    raise ValueError("could not seal disposable recording policy")
                daemon = subprocess.Popen([str(binaries["opaqued"])], env=env, cwd=project, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                deadline = time.monotonic()+15
                while not (runtime / "opaque/opaqued.sock").is_socket() or not (runtime / "opaque/daemon.token").is_file():
                    if daemon.poll() is not None or time.monotonic() >= deadline:
                        raise ValueError("isolated daemon did not become ready within 15 seconds")
                    time.sleep(.05)
                cli(["ping"])
                if name == "quickstart":
                    cli(["version"])
                    cli(["execute", "test.noop"])
                elif name == "security-onepassword-read-field":
                    cli(["1p", "read-field", "--vault", "SyntheticVault", "--item", "SyntheticItem", "--field", "password"], {"safety_violation", "policy_denied"})
                elif name == "security-sandbox-secret-leak":
                    cli(["exec", "--profile", "recording", "--", "/bin/sh", "-c", 'printf "%s" "$DEMO_VALUE"'], {"policy_denied"})
                else:
                    marker = "synthetic_audit_marker" if name == "security-audit-detail-leak" else "synthetic_sandbox_output"
                    response = cli(["exec", "--profile", "recording", "--", "/bin/sh", "-c", f"printf {marker}"])
                    result = response.get("result", {})
                    if marker in json.dumps(response) or "stdout" in result or "stderr" in result or result.get("exit_code") != 0 or result.get("stdout_length") != len(marker):
                        raise ValueError("sandbox metadata/output contract differs from this recording")
                    if name == "security-audit-detail-leak":
                        with sqlite3.connect(f"file:{state / 'audit.db'}?mode=ro", uri=True) as db:
                            rows = db.execute("select detail from audit_events").fetchall()
                        if marker in json.dumps(rows):
                            raise ValueError("argv marker persisted in audit detail")
                cli(["audit", "tail", "--limit", "8"])
                print("PASS: " + name + " (local fixture only)", flush=True)
            finally:
                if daemon:
                    stop(daemon)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-dir", type=Path, default=os.environ.get("OPAQUE_CORE_DIR"))
    parser.add_argument("--core-revision", default=os.environ.get("OPAQUE_CORE_REVISION"))
    parser.add_argument("--manifest", type=Path, default=os.environ.get("OPAQUE_CORE_BINARY_MANIFEST"))
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(tempfile.gettempdir()) / ("opaque-recordings-" + str(os.getpid())))
    parser.add_argument("--scenario", choices=SCENARIOS, action="append")
    parser.add_argument("--smoke", action="store_true", help="Run without asciinema/agg; defaults to quickstart")
    parser.add_argument("--internal-scenario", choices=SCENARIOS, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not all((args.core_dir, args.core_revision, args.manifest)):
        parser.error("explicit --core-dir, --core-revision and --manifest are required; build with scripts/demo_artifacts.py")
    manifest, binaries = artifacts.verify_core(args.manifest, args.core_dir, args.core_revision, args.allow_dirty)
    if args.internal_scenario:
        def interrupted(_signal, _frame):
            raise KeyboardInterrupt
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, interrupted)
        execute_scenario(args.internal_scenario, binaries)
        return
    demo_source = artifacts.source_snapshot(ROOT, allow_dirty=True)
    scenarios = args.scenario or (["quickstart"] if args.smoke else list(SCENARIOS))
    args.output = args.output.resolve()
    if args.output == ROOT or ROOT in args.output.parents:
        raise ValueError("recordings must be written outside the source checkout")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not args.smoke and not all(shutil.which(name) for name in ("asciinema", "agg")):
        raise ValueError("recording requires asciinema and agg; --smoke runs the bounded flow without them")
    for scenario in scenarios:
        if args.smoke:
            execute_scenario(scenario, binaries)
        else:
            command = [sys.executable, "-B", str(Path(__file__).resolve()), "--core-dir", str(args.core_dir.resolve()), "--core-revision", args.core_revision, "--manifest", str(args.manifest.resolve()), "--internal-scenario", scenario]
            if args.allow_dirty:
                command.append("--allow-dirty")
            cast, gif = args.output / (scenario+".cast"), args.output / (scenario+".gif")
            if cast.exists() or gif.exists():
                raise ValueError("recording outputs already exist; choose a fresh output directory")
            code, _ = bounded(["asciinema", "rec", "-q", "--cols", "120", "--rows", "28", "-c", shlex.join(command), str(cast)], timeout=120)
            if code:
                raise ValueError("recording failed")
            if cast.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("recording exceeded the artifact size bound")
            frames = [json.loads(line) for line in cast.read_text().splitlines()[1:]]
            transcript = "".join(frame[2] for frame in frames if len(frame) == 3 and frame[1] == "o")
            if "PASS: " + scenario + " (local fixture only)" not in transcript:
                raise ValueError("recorded child did not finish its checked scenario")
            code, _ = bounded(["agg", "--quiet", "--theme", "github-light", str(cast), str(gif)], timeout=120)
            if code:
                raise ValueError("render failed")
    if artifacts.source_snapshot(ROOT, allow_dirty=True) != demo_source:
        raise ValueError("demo source changed during recording")
    record = {"schema": "opaque.demo.recordings.v1", "mode": "synthetic-local-fixture", "approval": "insecure-test-only", "core": manifest, "demo": demo_source, "scenarios": scenarios, "smoke": args.smoke}
    if not args.smoke:
        record["artifacts"] = {p.name: artifacts.digest(p) for scenario in scenarios for p in (args.output/(scenario+".cast"), args.output/(scenario+".gif"))}
    artifacts.write_json(args.output / "recording-provenance.json", record)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Recording failed: {error}") from None
