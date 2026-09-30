#!/usr/bin/env python3
"""Build or verify local demo artifacts against an exact source snapshot.

Manifests are local build records, not signed release attestations. No publishing.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "opaque.demo.core-binaries.v1"
CORE_REPOSITORY = "opaque-dev/opaque"
MAX_FILE = 128 * 1024 * 1024


def digest(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024 * 1024:
        raise ValueError("artifact must be a bounded regular file, not a symlink")
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True, timeout=30).stdout


def source_snapshot(root, revision=None, repository=None, allow_dirty=False):
    root = Path(root).resolve(strict=True)
    if Path(os.fsdecode(git(root, "rev-parse", "--show-toplevel")).strip()).resolve() != root:
        raise ValueError("source path must be the repository root")
    head = git(root, "rev-parse", "HEAD").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head) or revision is not None and head != revision:
        raise ValueError("source HEAD does not match the exact requested revision")
    origin = git(root, "remote", "get-url", "origin").decode().strip()
    match = re.fullmatch(r"(?:https://github.com/|git@github.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", origin)
    if not match or repository is not None and match[1] != repository:
        raise ValueError("source must use the expected canonical GitHub repository")
    dirty = bool(git(root, "status", "--porcelain", "--untracked-files=all"))
    if dirty and not allow_dirty:
        raise ValueError("source has tracked or untracked changes; explicitly opt into a local dirty build")
    names = sorted(set(git(root, "ls-files", "-c", "-o", "--exclude-standard", "-z").split(b"\0")) - {b""})
    value = hashlib.sha256()
    for raw in names:
        name = os.fsdecode(raw)
        path = root / name
        if path.is_symlink():
            raise ValueError("source symlinks are not supported in local build provenance")
        value.update(raw + b"\0")
        if not path.exists():
            value.update(b"deleted\0")
        else:
            if path.stat().st_size > MAX_FILE:
                raise ValueError("source file exceeds provenance bound")
            value.update(str(path.stat().st_mode & 0o111).encode() + b"\0")
            value.update(bytes.fromhex(digest(path)))
    return {"repository": match[1], "revision": head, "dirty": dirty, "source_sha256": value.hexdigest()}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, indent=2)
        stream.write("\n")
    temporary.replace(path)


def build_core(core, revision, output, profile="debug", allow_dirty=False):
    core, output = Path(core).resolve(), Path(output).resolve()
    if output == core or core in output.parents:
        raise ValueError("build output must be outside the source checkout")
    before = source_snapshot(core, revision, CORE_REPOSITORY, allow_dirty)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = output / "target"
    command = ["cargo", "build", "--locked", "--manifest-path", str(core / "Cargo.toml"), "--target-dir", str(target), "-p", "opaque", "-p", "opaqued"]
    if profile == "release":
        command.append("--release")
    subprocess.run(command, cwd=core, check=True, timeout=1800)
    after = source_snapshot(core, revision, CORE_REPOSITORY, allow_dirty)
    if after != before:
        raise ValueError("source changed during compilation; rebuild from a stable snapshot")
    binaries = {name: {"path": f"target/{profile}/{name}", "sha256": digest(target / profile / name)} for name in ("opaque", "opaqued")}
    manifest = {"schema": SCHEMA, "provenance": "local-build", "source": before, "profile": profile, "binaries": binaries}
    path = output / "core-binaries.json"
    write_json(path, manifest)
    return path


def verify_core(manifest_path, core, revision, allow_dirty=False):
    manifest_path = Path(manifest_path).resolve(strict=True)
    if manifest_path.stat().st_size > 64 * 1024:
        raise ValueError("oversized binary manifest")
    value = json.loads(manifest_path.read_text())
    if value.get("schema") != SCHEMA or value.get("provenance") != "local-build":
        raise ValueError("a supported local-build manifest is required")
    if value.get("source") != source_snapshot(core, revision, CORE_REPOSITORY, allow_dirty):
        raise ValueError("binary manifest source snapshot differs from the selected checkout")
    binaries = {}
    for name in ("opaque", "opaqued"):
        item = value["binaries"][name]
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("binary manifest path escapes its artifact directory")
        path = manifest_path.parent / relative
        inside = path.relative_to(manifest_path.parent)
        if any((manifest_path.parent / Path(*inside.parts[:n])).is_symlink() for n in range(1, len(inside.parts)+1)):
            raise ValueError("binary artifact path traverses a symlink")
        if not os.access(path, os.X_OK) or digest(path) != item["sha256"]:
            raise ValueError("binary digest or executable permission differs from the build manifest")
        binaries[name] = path.resolve()
    return value, binaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-dir", type=Path, required=True)
    parser.add_argument("--core-revision", required=True)
    parser.add_argument("--allow-dirty", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build-core")
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--profile", choices=["debug", "release"], default="debug")
    verify = sub.add_parser("verify-core")
    verify.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "build-core":
        print(build_core(args.core_dir, args.core_revision, args.output, args.profile, args.allow_dirty))
    else:
        value, _ = verify_core(args.manifest, args.core_dir, args.core_revision, args.allow_dirty)
        print(json.dumps({"verified": True, "source": value["source"]}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Artifact check failed: {type(error).__name__}: {error}") from None
