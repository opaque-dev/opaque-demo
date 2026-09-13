# Reproduce local broker recordings

The standalone demo workspace builds `opaque-showcase`. Its recordings use the
CLI and daemon built from the **public core repository**, supplied explicitly.
Every recording is a disposable local fixture with synthetic values and test
approval. It is not evidence of a real provider account or human ceremony.

## Build and bind the core binaries

Use a reviewed checkout of `kcirtapfromspace/opaque` and its full 40-character
revision. Build outputs must stay outside the checkout:

```sh
python3 -B scripts/demo_artifacts.py \
  --core-dir /path/to/opaque --core-revision FULL_40_CHARACTER_REVISION \
  build-core --profile debug --output /private/tmp/opaque-recording-build
```

This builds the actual `opaque` and `opaqued` packages with the lockfile, then
records their hashes, source revision and digest of tracked and untracked source
contents. A source change during compilation fails the build record. Dirty
checkouts require explicit `--allow-dirty`; the record labels them accordingly.
This is local build provenance, not a signed release attestation. The manifest
is not inferred from a binary filename or version string.

## Run the bounded smoke flow

```sh
bash scripts/record_demos.sh \
  --core-dir /path/to/opaque --core-revision FULL_40_CHARACTER_REVISION \
  --manifest /private/tmp/opaque-recording-build/core-binaries.json \
  --smoke --output /private/tmp/opaque-recording-smoke
```

The default smoke initializes isolated HOME/runtime directories, seals a
throwaway policy, starts its own daemon, checks ping/version and `test.noop`,
reads bounded audit metadata, and stops the daemon. No ambient provider token,
existing socket, SSH agent or real user state is forwarded. Test approval is
prominently labeled. Every child command has an output bound and deadline.

Select additional checks with repeated `--scenario` flags:

| Scenario | Current assertion |
|---|---|
| `quickstart` | Real local daemon transport and no-op execution |
| `sandbox-exec` | Sandbox returns output lengths and exit status; no plaintext output |
| `security-audit-detail-leak` | Synthetic argv marker is absent from returned output and stored audit detail |
| `security-sandbox-secret-leak` | A source reference outside policy is denied |
| `security-onepassword-read-field` | Agent plaintext reveal is denied before provider access |

The two executing sandbox scenarios require the core-supported local sandbox
backend and permissions. They fail visibly if the host cannot execute it.
The old names remain for links; these now check the current prevention behavior
instead of claiming old leaks still succeed. The 1Password denial does not
require a missing external mock script or contact a real account.

## Capture a local cast and GIF

Install `asciinema` and `agg`. Omit `--smoke` to record all scenarios, or select
one using `--scenario quickstart`. The script creates a local `.cast`, `.gif` and
`recording-provenance.json`, checks the child's success marker, and binds the
artifacts to the verified core binaries and unchanged demo source snapshot.
It never uploads a recording. Choose a fresh output directory outside the source
checkout; existing recordings are not overwritten.

The legacy `scripts/demo_*.sh` entry points run their single smoke scenario and
accept these same arguments. Alternatively set `OPAQUE_CORE_DIR`,
`OPAQUE_CORE_REVISION` and `OPAQUE_CORE_BINARY_MANIFEST`. A missing manifest,
wrong source revision/digest, binary change or escaping artifact path fails
before any broker starts. Rebuild after source changes.
