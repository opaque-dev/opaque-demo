# Dispatch a real GitHub Actions workflow from AX under one approved scope

Approve one staging workflow, one branch, two attempts and ten minutes with
Opaque's native reviewer, then let a Google AX task dispatch it through the real
broker. The broker holds the GitHub token, refuses a production workflow before
GitHub is contacted, charges each dispatch durably, and keeps the truth when the
agent process is killed mid-request.

```sh
opaque authority-policy validate examples/ax-staging-dispatch/policies/staging-dispatch.yaml --json
opaque authority-policy compile examples/ax-staging-dispatch/policies/staging-dispatch.yaml --json
```

Both commands print digest `a5b142b3e30cd4b519927b9d171b71b567a51f16c45098adecc0c11d36e42805`
with a CLI built from core `2109af7ffdb2c7bd414d7f6b107109e3134dd045`. This example
uses **unreleased pinned core source**, not a released CLI contract. The dispatch
target is the private scratch repository
[opaque-dev/opaque-staging-scratch](https://github.com/opaque-dev/opaque-staging-scratch),
whose content is checked in under [scratch-repo/](scratch-repo/).

## What the repository defines

| File | Responsibility |
| --- | --- |
| [policies/staging-dispatch.yaml](policies/staging-dispatch.yaml) | Kind `github.workflow.dispatch`; exactly one target (`opaque-dev/opaque-staging-scratch`, `.github/workflows/staging.yml`, `main`); one resource, two attempts, ten minutes; scope review required; `WithinApprovedScope` so approved work runs without a second ceremony |
| [broker/config.toml.in](broker/config.toml.in) | Sealed digest and identity binding, enrolled native reviewer, tenant `opaque-ax-dispatch`, GitHub REST endpoint and the broker-owned token file path |
| [broker/install.sh](broker/install.sh) | Trusted init container: installs the token from the Kubernetes Secret as a broker-owned mode 0600 regular file at policy activation only, then seals |
| [workload/scope.json](workload/scope.json) | The proposed scope: the one policy target, 600 seconds, 2 attempts. A request, not approval |
| [workload/actions.json](workload/actions.json) | Three proposals: `production.yml` (out of scope), `staging.yml` twice (in scope; the second exhausts the budget) |
| [workload/task.yaml](workload/task.yaml) | AX Task running [worker.py](worker.py) |
| [k8s/](k8s/) | Separate broker and AX pods, immutable ConfigMaps, the token Secret mounted only by the broker init container, an evidence export pod |
| [worker.py](worker.py) | Journals each intent, calls the real `opaque scope plan / activate / run / outcome`, supports the SIGKILL variant, reconciles on restart, never retries |
| [demo.py](demo.py) | Every machine step, the numbered human steps, the resume, the evidence export and the browser view |
| [scratch-repo/](scratch-repo/) | Content of the scratch GitHub repository: `staging.yml` and `production.yml`, both `workflow_dispatch`, echo their inputs and run id |
| [fixtures/](fixtures/) | Synthetic OIDC identity protocol and bootstrap RPC client; no policy engine, approval signer or GitHub credential |

The denial comes first in `actions.json` on purpose: with the budget still full, the
refusal of `production.yml` can only be the approved scope, and the SIGKILL variant
still shows all three proposals.

## Prerequisites

- macOS arm64 host with Touch ID (the native reviewer), Docker Desktop, minikube
  with the Docker driver and a running profile named `opaque-ax-demo`
  (`minikube start -p opaque-ax-demo --driver=docker --cpus=4 --memory=8192 --kubernetes-version=v1.35.1 --keep-context`).
- Rust matching the core pin, Go compatible with the AX pin, Python 3.12+ with
  PyYAML 6.0.3, Git, `gh`.
- A fine-grained GitHub personal access token restricted to
  `opaque-dev/opaque-staging-scratch` with Actions: read and write, Contents: read,
  Metadata: read. Save it alone in a mode 0600 file under `/private/tmp`. Never
  commit it. Only `kubectl create secret` reads that file; `demo.py` never does.
- Use short paths under `/private/tmp` for the work and state directories.

## Machine steps

```sh
python3 -m venv /private/tmp/opaque-dispatch-venv
/private/tmp/opaque-dispatch-venv/bin/pip install -r examples/ax-staging-dispatch/requirements.txt
/private/tmp/opaque-dispatch-venv/bin/python -B examples/ax-staging-dispatch/demo.py build --work /private/tmp/opaque-dispatch-work
/private/tmp/opaque-dispatch-venv/bin/python -B examples/ax-staging-dispatch/demo.py \
  --state /private/tmp/opaque-dispatch-session deploy \
  --image opaque-ax-staging-dispatch:2109af7 \
  --native-bin /private/tmp/opaque-dispatch-work/core/target/debug \
  --github-token-file /private/tmp/opaque-dispatch-github.token
```

`build` clones core at `2109af7` and AX at `f009cc81`, builds the host reviewer
binaries, cross-builds the AX task runner, and builds the broker/AX image (use
`--arch amd64` for an amd64 node). `deploy` compiles the policy with the image's
own binary and the host CLI, creates a new owned namespace, stores the token in a
Secret, enrolls a fresh host reviewer key, starts the broker through its
bootstrap, mapped and sealed policy stages, starts the AX runner, and checks that:

- the AX pod has no `/var/lib/opaque`, no Secret mount and no token;
- the token exists only as `/var/lib/opaque/github.token` in the broker pod, mode
  600, owner 7581, one link;
- an undelegated `scope_plan` from the AX pod is refused at the broker transport.

It then **stops** and prints the numbered human steps below and the shot list.
It opens no native window and sends no GitHub request.

## The exact human steps

Run these in order from the repository root with the same `--state` directory.
Steps 1 to 3 each open exactly one native authentication window on this host.

1. `python -B examples/ax-staging-dispatch/demo.py --state /private/tmp/opaque-dispatch-session delegate-reviewer`
   Approve the operator delegation for the broker-side protocol client. The script
   then calls `opaque scope snapshot` and requires `authority_policy.digest` and
   identity to equal the compiled repository policy. Expect the printed
   `policy_verified: true`.
2. `python -B examples/ax-staging-dispatch/demo.py --state /private/tmp/opaque-dispatch-session delegate-requester`
   Approve the requester delegation for the AX pod. The AX worker then runs
   `opaque scope plan --manifest /etc/demo/workload/scope.json`. Expect an
   `issuance_round_id`.
3. `python -B examples/ax-staging-dispatch/demo.py --state /private/tmp/opaque-dispatch-session scope-review`
   The signed scope document shows one target, `main`, two attempts, ten minutes.
   Approve it. The AX worker then runs `opaque scope activate`. Expect a `scope_id`.
4. `python -B examples/ax-staging-dispatch/demo.py --state /private/tmp/opaque-dispatch-session run`
   or, for the crash take,
   `python -B examples/ax-staging-dispatch/demo.py --state /private/tmp/opaque-dispatch-session run --variant sigkill-second-dispatch`
5. `python -B examples/ax-staging-dispatch/demo.py --state /private/tmp/opaque-dispatch-session evidence`

`demo.py --state ... human-steps` reprints the list; `serve` opens the browser view
at <http://127.0.0.1:19741/>, which can start step 4 but cannot approve anything.

## Expected results

These are expectations. No brokered dispatch has been performed yet; see Limits.

- **Step 4, plain run.** `dispatch-production` is refused by the approved scope
  before any GitHub request; nothing is charged. The CLI sees the generic code
  `scope_unavailable`; the broker log line `scope request denied or unavailable`
  carries `error=proposed change or resource is outside scope`, and `demo.py run`
  prints those lines. `dispatch-staging-1` records
  `api_accepted` after the broker reads the workflow, the branch head and the
  absence of a same-named tag, then POSTs `{"ref":"main"}` with no inputs.
  `dispatch-staging-2` records `api_accepted` and exhausts the two-attempt budget.
  The GitHub Actions tab of the scratch repository shows two new `staging` runs
  and no `production` run.
- **Step 4, SIGKILL variant.** During `dispatch-staging-2` the worker journals the
  attempt, starts the real `opaque scope run`, and 900 ms later SIGKILLs its own
  process group (worker plus CLI child). The AX runner logs exit code -1 and keeps
  the pod up. `demo.py` restarts the worker once, for reconciliation only: it
  reads `opaque scope outcome` for the journaled request and holds. It never calls
  `run` again. The retained state depends on where the kill landed: `api_accepted`
  if the broker had already claimed the attempt (the daemon finishes an in-flight
  dispatch even when its client dies), or no charged attempt if the kill arrived
  before the reservation. `unknown` appears only if the broker itself is
  interrupted between claim and acknowledgment, or GitHub answers ambiguously.
  In every case the attempt count on the Actions tab equals the charged attempts.
- **Step 5.** The broker is scaled to zero, an export pod running as the custody
  account signs the ledger with a disposable key, `opaque-evidence verify` on the
  host accepts the export against the checkpoint pin, and the same command rejects
  a copy with one flipped byte. The broker restarts afterwards; delegated sessions
  and pending rounds do not survive that restart.

## 90-second shot list

`demo.py --state ... shot-list` prints it. Policy digest from the compiler and the
authenticated snapshot; one Touch ID scope review; three proposals with one denial
and two `api_accepted`; the SIGKILL take with the retained outcome and no second
POST; `opaque-evidence verify` accepting the export and rejecting a flipped byte;
the GitHub Actions tab with exactly the brokered runs, zero production runs, and
the labelled direct validity run.

## Limits

- **Synthetic:** the OIDC identities (`reviewer`, `requester`) come from a local
  fixture and prove no organizational identity. The evidence checkpoint pin is
  computed in the same run; a relying party must retain it independently.
- **Real:** the Opaque broker and CLI, the AX task runner, the GitHub REST API,
  the native Touch ID reviewer, the scratch repository and its workflow runs.
- **Unverified as of this writing:** no Opaque-brokered dispatch against GitHub
  has been performed, so `api_accepted` on a live 204, the exact Actions-tab
  count, the SIGKILL timing and the evidence export on a ledger with real
  dispatches are expectations. Core's automated tests cover the dispatch path
  against a synthetic HTTPS GitHub, and a throwaway test of `scope run` under
  `WithinApprovedScope` for this kind passed in a scratch core clone; core itself
  ships no such test.
- A `workflow_dispatch` has no idempotency key and GitHub returns no run id, so an
  `unknown` dispatch may or may not have started a run. Inspect the Actions tab
  before requesting a new dispatch. Correlation is by branch and time only.
- The cluster operator is trusted. minikube networking is not claimed to enforce
  egress restrictions. The Secret lives in the namespace; the AX pod has no service
  account token and no mount for it.
- The public `examples/ax-scope/adapter.py` binds support cases only; the worker
  derives dispatch correlation with the same request-ID scheme locally.
- Replaying `opaque scope run` for a request id the ledger already holds is
  refused (`request identity already binds different content`) rather than
  answered with the retained record, because each `run` prepares a fresh action
  identity. Use `opaque scope outcome` to read a consumed request; the worker does.
  A third in-scope proposal fails with `an ancestor budget is exhausted`; the
  outcome of a request that was never reserved is `scope or action not found`.
  These strings were observed in a scratch core clone at `2109af7`, not live.

## Tests

```sh
python3 -B -m unittest discover -s scripts -p test_ax_staging_dispatch_demo.py -v
node --check examples/ax-staging-dispatch/demo.js
```

The runtime directory must be outside Git. Retain it and the namespace for
inspection if any step fails; there is no automatic restart, re-enrollment,
repeated review, reset or retry.
