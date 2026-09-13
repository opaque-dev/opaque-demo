#!/usr/bin/env python3
"""Wrangler's mandatory local public-asset build and complete privacy gate."""
from pathlib import Path
import shutil
import tempfile

import check_site_privacy as privacy


def build(repository=privacy.ROOT):
    repository = Path(repository).resolve()
    parent = repository / "deploy/cloudflare-demo"
    output = parent / ".public-artifact"
    if output.is_symlink():
        raise ValueError("publication output must not be a symlink")
    if output.exists() and privacy.inspect_site(output, profile="worker"):
        raise ValueError("existing publication output is not a recognized artifact; inspect it before replacing")
    with tempfile.TemporaryDirectory(prefix=".public-build-", dir=parent) as temporary:
        temporary = Path(temporary)
        generated = temporary / "site"
        if privacy.package_worker_site(generated, repository):
            raise ValueError("generated Worker artifact failed privacy inspection")
        backup = temporary / "previous"
        if output.exists():
            output.rename(backup)
        try:
            generated.rename(output)
        except OSError:
            if backup.exists():
                backup.rename(output)
            raise
    return output


if __name__ == "__main__":
    try:
        print("Verified public artifact: " + str(build()))
    except (OSError, ValueError) as error:
        raise SystemExit(f"Worker publication gate failed: {error}") from None
