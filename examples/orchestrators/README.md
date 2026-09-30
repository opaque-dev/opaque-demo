# Run Opaque from Airflow and Temporal

Open a real Airflow DAG or Temporal workflow, review its requested authority with
Opaque's native reviewer, and inspect what the broker recorded for each action.
Both demos consume the same repository policy and synthetic support API.

| Demo | What to inspect in its native UI |
| --- | --- |
| Airflow 3.3.2 | DAG graph, review sensors, task results, and a failed reconciliation task when a result remains uncertain |
| Temporal CLI 1.9.1 / Python SDK 1.33.0 | Workflow history, review signals, Activities, and a durable `held_for_reconciliation` state |

These are **private local examples using unreleased Opaque source**, pinned to
`180e66fe6c854d962074e8ff4e694a29689af623`. Airflow standalone and Temporal's
persistent development server are local demonstration deployments. They are not
production HA installations, an Apache/Temporal endorsement, or government
compliance evidence. The local identity and support services are fixtures; Opaque
policy enforcement, native approval verification, custody and scope ledger use
the actual broker. There is no model provider or production support account.

## Configure Opaque in the repository

| File | Controls |
| --- | --- |
| [policies/support-status.yaml](policies/support-status.yaml) | `resolved` only, two cases, two attempts, ten minutes, required scope review, execution within the reviewed scope |
| [broker/config.toml.in](broker/config.toml.in) | Native approval backend, enrolled reviewer, sealed policy digest, tenant and broker-owned provider credential |
| [workload/scope.json](workload/scope.json) | The exact request the human reviews; it grants no authority on its own |
| [workload/actions.json](workload/actions.json) | Two proposed case updates, including a fixture that applies a write and drops its acknowledgement |
| [airflow/dags/opaque_support.py](airflow/dags/opaque_support.py) | Airflow graph and task lifecycle |
| [temporal/workflow.py](temporal/workflow.py) | Durable workflow and review signals |
| [shared/opaque_bridge.py](shared/opaque_bridge.py) | Calls the Opaque CLI and journals stable action identity before dispatch |
| [k8s/](k8s/) | Separate broker custody, orchestrator storage, non-root execution and local UI ports |

Python defines the orchestration. Opaque's manifest defines the authorization
ceiling, and the broker evaluates it. Change the YAML to change that ceiling;
this controller deploys a fresh namespace with immutable configuration. Updating
an existing broker's policy requires its normal seal/restart procedure. Never
delete the ledger or tenant binding to apply a policy change.

## Build and prepare both demos

Prerequisites: Docker, minikube, Git, Python 3.12+ with PyYAML 6.0.3, and a macOS
host with Opaque's native reviewer. Native user presence is required for approval.
The checked images support the arm64 development host used here. Reserve at least
4 CPUs and 8 GiB for minikube. Run these commands from the demo repository.

```sh
python3 -m venv /tmp/opaque-orchestrator-venv
/tmp/opaque-orchestrator-venv/bin/pip install -r examples/ax-minikube/requirements.txt
git clone https://github.com/opaque-dev/opaque.git /tmp/opaque-orchestrator-core
git -C /tmp/opaque-orchestrator-core checkout --detach 180e66fe6c854d962074e8ff4e694a29689af623
(cd /tmp/opaque-orchestrator-core && OPAQUE_BUILD_REVISION=180e66fe6c854d962074e8ff4e694a29689af623 cargo build --locked -p opaque -p opaque-approver -p opaque-approve-helper)

/tmp/opaque-orchestrator-venv/bin/python -B scripts/build_orchestrator_images.py \
  --core-repo /tmp/opaque-orchestrator-core --tag local
minikube start -p opaque-ax-demo --driver=docker --cpus=4 --memory=8192 \
  --disk-size=30g --kubernetes-version=v1.35.1 --keep-context

/tmp/opaque-orchestrator-venv/bin/python -B scripts/orchestrator_demo.py \
  --state /tmp/opaque-airflow-session deploy airflow \
  --broker-image opaque-orchestrators-broker:local --worker-image opaque-airflow:local \
  --native-bin /tmp/opaque-orchestrator-core/target/debug
/tmp/opaque-orchestrator-venv/bin/python -B scripts/orchestrator_demo.py \
  --state /tmp/opaque-temporal-session deploy temporal \
  --broker-image opaque-orchestrators-broker:local --worker-image opaque-temporal:local \
  --native-bin /tmp/opaque-orchestrator-core/target/debug
```

The builder archives the pinned public revision, excluding uncommitted changes,
and never pushes images. Pinned Opaque is BUSL-1.1; the pending license proposal
does not change that source's license. Upstream images use digest pins. Preparation
checks worker source hashes, enrolls a fresh host reviewer, seals the policy, tests
that undelegated access is refused, and starts one workflow waiting for review.
It opens no approval window and sends no provider writes.

## Open the native UIs

Run these in separate terminals; stopping a tunnel retains the deployment:

```sh
/tmp/opaque-orchestrator-venv/bin/python -B scripts/orchestrator_demo.py \
  --state /tmp/opaque-airflow-session serve
/tmp/opaque-orchestrator-venv/bin/python -B scripts/orchestrator_demo.py \
  --state /tmp/opaque-temporal-session serve
```

- Airflow: <http://127.0.0.1:19750/>, DAG `opaque_support`. The local username is
  `reviewer`; its generated password is in the runner's private
  `/opt/airflow/simple_auth_manager_passwords.json.generated` file. Use the
  namespace recorded in your private `cluster.json` to read it with
  `minikube -p opaque-ax-demo kubectl -- -n NAMESPACE exec deployment/runner -c runner -- cat /opt/airflow/simple_auth_manager_passwords.json.generated`.
- Temporal: <http://127.0.0.1:19751/>, namespace `default`, workflow ID recorded in
  `cluster.json`. This development UI has no user authentication. The controller
  forwards it only to host loopback; do not expose it through ingress or a public
  tunnel. Select the workflow and query `inspect` for its Opaque state.

## Review and execute

When the human reviewer is present, run the command once for each deployment:

```sh
/tmp/opaque-orchestrator-venv/bin/python -B scripts/orchestrator_demo.py \
  --state /tmp/opaque-airflow-session review
/tmp/opaque-orchestrator-venv/bin/python -B scripts/orchestrator_demo.py \
  --state /tmp/opaque-temporal-session review
```

Each command opens three native reviews: administrative inspection delegation,
requester delegation, and exact scope approval. **Approving the third permits the
workflow to execute the two proposals automatically.** The controller verifies
the authenticated broker policy digest before requesting the scope. An Airflow
sensor result or Temporal signal only wakes orchestration; the broker verifies the
native receipt before granting authority. Tokens stay out of XCom and workflow
history. Administrative delegation and provider credentials stay outside the
orchestrator container.

Expected scenario after approval: the first write is acknowledged; the second
fixture applies its write but drops its acknowledgement. Opaque retains the
uncertain outcome. The workflow attempts to revoke remaining scope authority and
inspects the broker. Airflow ends at a failed reconciliation task; Temporal stays
running in a durable reconciliation hold. Neither authorizes a new write.

```sh
/tmp/opaque-orchestrator-venv/bin/python -B scripts/orchestrator_demo.py \
  --state /tmp/opaque-airflow-session inspect
/tmp/opaque-orchestrator-venv/bin/python -B scripts/orchestrator_demo.py \
  --state /tmp/opaque-temporal-session inspect
```

## Retry and restart behavior

Before any delegation, `restart-waiting` replaces the orchestrator pod and verifies
that the retained workflow still waits without a plan or grant. It preserves the
orchestrator database and workload journal and never starts a second workflow.

After dispatch intent is durably recorded, redelivery queries the broker using
the same request ID. A missing outcome, failed lookup, or generic broker error
remains held. A new scheduler attempt cannot mint a new logical action. Different
workflow IDs and changed proposals are rejected. Airflow tasks have zero automatic
retries; Temporal retries the execution Activity at most three times, with the
journal preventing another dispatch. Manual task clearing or workflow reset must
not be used as a reconciliation decision.

The requester token is ephemeral pod state. A pod replacement after delegation
requires human recovery; this example intentionally has no automatic credential
refresh. A crash during planning, activation or revocation preserves its intent
and may require manual inspection. Retained broker responses are inspection
records, not an independently verified signed evidence export.

Local controller state contains keys and receipts: keep it outside Git. Preserve
the namespace/PVCs when investigating unknown outcomes. To stop a demo, scale its
broker and runner to zero after reviewing outstanding work; do not reset custody.

## Validation

```sh
/tmp/opaque-orchestrator-venv/bin/python -B -m unittest discover -s scripts -p 'test_*.py'
docker run --rm --network=none --entrypoint python opaque-airflow:local -c \
  'from airflow.dag_processing.dagbag import DagBag; b=DagBag(dag_folder="/opt/opaque-demo/dags"); assert not b.import_errors; print(b.dags["opaque_support"].task_ids)'
```

The automated bridge tests simulate broker responses to exercise failures and
redelivery. Native approval and the complete write scenario require a human run.
