# GitHub CI demo through the real broker

This local demo presents real public GitHub CI observations and the configured
broker's actual inference receipts. It invokes `opaque task` for planning,
approval, execution, inspection and revocation. It has no separate task store,
approval shortcut or simulated model response. The existing hosted portfolio
demonstration remains a separate fixture experience.

Use the core CLI and daemon built from the same reviewed revision containing the
`github-ci-v1` source adapter. Configure the daemon's tenant, identity, model
profile, approval policies and public repository/workflow/branch as described in
core's `docs/github-ci-inference.md`. The CLI is an explicit runtime dependency;
this runner does not change the showcase crate's pinned core dependency.

```sh
python3 scripts/github_ci_demo.py --opaque /path/to/opaque plan \
  --repository your-organization/your-public-repository \
  --workflow-id 123456789 --branch main

# Use the task ID printed by planning. This requests normal broker approval.
python3 scripts/github_ci_demo.py --opaque /path/to/opaque run <task-id>

# Inspect the same task, including rejected/unknown outcomes, without retrying.
python3 scripts/github_ci_demo.py --opaque /path/to/opaque show <task-id>
python3 scripts/github_ci_demo.py --opaque /path/to/opaque revoke <task-id>
```

The workflow ID is an example; supply your actual public workflow ID. Source
arguments verify what the broker captured; they do not override its trusted
configuration. Optional `--socket /path/to/broker.sock` selects the normal CLI
transport. CLI identity, delegation and native/paired-workstation approval
requirements remain in effect. The runner never adds `--yes` or test approval.

The command prints a local `index.html` path. Open it in a browser to inspect
the source sample, three outcomes, token counts and task bindings. Reports are
written with private permissions under a temporary directory by default. Use
`--output /private/path` to select a private directory. Do not commit or publish
runtime reports; the report contains tenant and task references. No HTTP server,
cluster change or public deployment is needed.

A timeout causes one read-only inspection of the existing task. The runner
never re-plans or retries an execution automatically. If no model output was
observed, the page says so. Insecure test approvals remain prominently labeled.
The page is a display of broker evidence, not an independent cryptographic
verifier. CI success does not prove deployment or service health.

```sh
python3 -B -m unittest discover -s scripts -p 'test_github_ci_demo.py'
```

These tests use explicitly labeled task fixtures to exercise rendering and CLI
failure handling. Live GitHub capture, model execution and the real approval
ceremony are separate acceptance checks.

## Tie the local report to a CLI build

To check the exact CLI bytes and source snapshot before any broker request, use a
manifest produced by `scripts/demo_artifacts.py` (see the recording guide):

```sh
python3 scripts/github_ci_demo.py \
  --core-dir /path/to/opaque --core-revision FULL_40_CHARACTER_REVISION \
  --core-manifest /private/build/core-binaries.json \
  plan --repository your-organization/your-public-repository \
  --workflow-id 123456789 --branch main
```

The verified manifest selects the CLI; `--opaque`, if supplied, must agree. Dirty
source builds require `--allow-dirty-core` and an exact matching source digest.
The private report directory receives `client-provenance.json` with the CLI
build, demo source digest, task/manifest identifiers and final report hash. This
checks the local client artifact. The connected broker's installed artifact,
provider observation and native approval still require separate acceptance.
