#!/usr/bin/env python3
"""Build and hash standalone showcase artifacts; never build/push an image."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib

import check_site_privacy as privacy
import demo_artifacts as artifacts

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "opaque.demo.hosted-build.v1"
WORKER_FILES = tuple("deploy/cloudflare-demo/src/" + name + ".mjs" for name in ("http", "queue", "models", "scheduler", "worker", "leads"))
RUNTIME_FILES = ("deploy/hosted-demo/runtime.py", "deploy/hosted-demo/controller.py", "deploy/hosted-demo/credit_source.py", "deploy/hosted-demo/model_profiles.py", "scripts/metrics_chat_dogfood.py")


def core_pin(repository):
    cargo = tomllib.loads((repository / "Cargo.toml").read_text())
    revision = cargo["workspace"]["dependencies"]["opaque-core"]["rev"]
    lock = tomllib.loads((repository / "Cargo.lock").read_text())
    records = [p for p in lock["package"] if p["name"] == "opaque-core"]
    if len(records) != 1 or records[0].get("source") != f"git+https://github.com/kcirtapfromspace/opaque.git?rev={revision}#{revision}":
        raise ValueError("locked core contract revision differs from Cargo.toml")
    return revision


def build(repository, revision, output, profile="release", target=None, allow_dirty=False):
    repository, output = repository.resolve(), output.resolve()
    if output == repository or repository in output.parents:
        raise ValueError("artifact output must be outside the source checkout")
    before = artifacts.source_snapshot(repository, revision, "kcirtapfromspace/opaque-demo", allow_dirty)
    pin = core_pin(repository)
    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    host = subprocess.run(["rustc", "-vV"], check=True, capture_output=True, text=True, timeout=10).stdout
    triple = target or next(line.removeprefix("host: ") for line in host.splitlines() if line.startswith("host: "))
    command = ["cargo", "build", "--locked", "--manifest-path", str(repository / "Cargo.toml"), "--target-dir", str(output / "target"), "-p", "opaque-showcase"]
    if profile == "release":
        command.append("--release")
    if target:
        command += ["--target", target]
    subprocess.run(command, cwd=repository, check=True, timeout=1800)
    if before != artifacts.source_snapshot(repository, revision, "kcirtapfromspace/opaque-demo", allow_dirty):
        raise ValueError("demo source changed during the build")
    compiled = output / "target"
    if target:
        compiled /= target
    compiled /= profile + "/opaque-showcase"
    payload_dir = output / "payload"
    payload_dir.mkdir(mode=0o700, exist_ok=True)
    binary = payload_dir / "opaque-showcase"
    shutil.copyfile(compiled, binary)
    binary.chmod(0o755)
    public = payload_dir / "public"
    if public.exists():
        raise ValueError("publication output already exists; choose a fresh artifact output")
    failures = privacy.package_worker_site(public, repository)
    if failures:
        raise ValueError("generated public artifact failed privacy inspection")
    payload = {"opaque-showcase": artifacts.digest(binary)}
    for name in RUNTIME_FILES:
        destination = payload_dir / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repository / name, destination)
        payload[name] = artifacts.digest(destination)
    for path in sorted(public.rglob("*")):
        if path.is_file():
            payload[path.relative_to(payload_dir).as_posix()] = artifacts.digest(path)
    # Bundle Worker source separately; it is not a public asset directory.
    for path in sorted((repository / "deploy/cloudflare-demo/src").glob("*.mjs")):
        relative = path.relative_to(repository)
        destination = payload_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        payload[relative.as_posix()] = artifacts.digest(destination)
    if before != artifacts.source_snapshot(repository, revision, "kcirtapfromspace/opaque-demo", allow_dirty):
        raise ValueError("demo source changed during artifact packaging")
    value = {"schema": SCHEMA, "provenance": "local-build", "demo_source": before, "core_contract_revision": pin, "target": triple, "profile": profile, "privacy_gate": "passed-generated-worker-artifact", "files": payload}
    artifacts.write_json(payload_dir / "artifact-provenance.json", value)
    return payload_dir / "artifact-provenance.json"


def verify(directory, require_linux=False):
    directory = directory.resolve(strict=True)
    manifest = directory / "artifact-provenance.json"
    if manifest.stat().st_size > 128 * 1024:
        raise ValueError("artifact manifest exceeds bound")
    value = json.loads(manifest.read_text())
    if value.get("schema") != SCHEMA or value.get("provenance") != "local-build" or value.get("privacy_gate") != "passed-generated-worker-artifact":
        raise ValueError("unsupported or unqualified artifact manifest")
    source = value.get("demo_source", {})
    if not isinstance(source, dict) or source.get("repository") != "kcirtapfromspace/opaque-demo" or not isinstance(source.get("dirty"), bool):
        raise ValueError("artifact source provenance is incomplete")
    for field, length in ((source.get("revision"), 40), (source.get("source_sha256"), 64), (value.get("core_contract_revision"), 40)):
        if not isinstance(field, str) or not re.fullmatch("[0-9a-f]{" + str(length) + "}", field):
            raise ValueError("artifact source provenance has an invalid digest or revision")
    if value.get("profile") not in {"debug", "release"} or not isinstance(value.get("target"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value["target"]):
        raise ValueError("artifact build target or profile is invalid")
    if not isinstance(value.get("files"), dict) or len(value["files"]) > 256:
        raise ValueError("artifact file inventory is invalid")
    required = {"opaque-showcase", *RUNTIME_FILES, *WORKER_FILES, *("public/"+name for name in privacy.WORKER_PUBLIC_FILES)}
    if not required <= set(value["files"]):
        raise ValueError("artifact manifest omits required payloads")
    for name, expected in value["files"].items():
        relative = Path(name)
        if not isinstance(expected, str) or not re.fullmatch("[0-9a-f]{64}", expected):
            raise ValueError("artifact payload digest is invalid")
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("artifact path escapes its directory")
        if any(directory.joinpath(*relative.parts[:n]).is_symlink() for n in range(1, len(relative.parts)+1)):
            raise ValueError("artifact path traverses a symlink")
        if artifacts.digest(directory / relative) != expected:
            raise ValueError("artifact payload digest mismatch")
    actual_files = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file() or p.is_symlink()}
    if actual_files != set(value["files"]) | {"artifact-provenance.json"}:
        raise ValueError("artifact contains unrecorded files or is missing payloads")
    if privacy.inspect_site(directory / "public", profile="worker"):
        raise ValueError("generated public artifact failed privacy inspection")
    if require_linux:
        with (directory / "opaque-showcase").open("rb") as binary:
            magic = binary.read(4)
        if "-linux-" not in value["target"] or magic != b"\x7fELF":
            raise ValueError("the hosted image requires a Linux ELF showcase build")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("build")
    create.add_argument("--repository", type=Path, default=ROOT)
    create.add_argument("--demo-revision", required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--profile", choices=["debug", "release"], default="release")
    create.add_argument("--target")
    create.add_argument("--allow-dirty", action="store_true")
    check = sub.add_parser("verify")
    check.add_argument("--directory", type=Path, required=True)
    check.add_argument("--require-linux", action="store_true")
    args = parser.parse_args()
    if args.command == "build":
        print(build(args.repository, args.demo_revision, args.output, args.profile, args.target, args.allow_dirty))
    else:
        value = verify(args.directory, args.require_linux)
        print(json.dumps({"verified": True, "demo_source": value["demo_source"], "core_contract_revision": value["core_contract_revision"], "target": value["target"]}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Hosted artifact failed: {error}") from None
