"""Visible Airflow pipeline; Opaque's checked-in policy governs every write."""
from datetime import datetime, timezone

from airflow.sdk import DAG, task
from airflow.providers.standard.sensors.python import PythonSensor
from airflow.sdk.exceptions import AirflowFailException
import opaque_bridge as opaque

with DAG(
    dag_id='opaque_support', schedule=None, start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    catchup=False, max_active_runs=1, tags=['opaque', 'repository-policy', 'native-review'],
    doc_md='''## Opaque support workflow
Read `examples/orchestrators/policies/support-status.yaml` in the repository.
The DAG proposes two support-case updates. The local identity/provider are fixtures.

`await_native_delegation` and `await_native_scope_review` pause until the operator
runs the documented Opaque native review command. Scheduler state cannot grant
Opaque authority. The broker verifies the native receipt at activation.

A lost acknowledgement leaves this workflow held for reconciliation. Retrying a
write task reads the retained Opaque outcome under the same request ID.
''',
) as dag:
    @task(retries=0)
    def inspect_repository_policy():
        from airflow.sdk import get_current_context
        return opaque.describe_policy(get_current_context()['run_id'])

    delegated = PythonSensor(task_id='await_native_delegation', python_callable=opaque.delegated,
                             mode='reschedule', poke_interval=5, timeout=86400)

    @task(retries=0)
    def propose_scope():
        return opaque.plan_scope()

    reviewed = PythonSensor(task_id='await_native_scope_review', python_callable=opaque.reviewed,
                            mode='reschedule', poke_interval=5, timeout=86400)

    @task(retries=0)
    def activate_scope(round_id):
        return opaque.activate_scope(round_id)['scope_id']

    @task(retries=0)
    def run_proposals():
        from airflow.sdk import get_current_context
        logical_run = get_current_context()['run_id']
        results = []
        for action in opaque.read(opaque.CONFIG / 'actions.json'):
            observed = opaque.execute_action(logical_run, action)
            results.append(observed)
            if observed['hold']:
                break
        return results

    @task(retries=0)
    def revoke_remaining_authority():
        return opaque.revoke_scope()

    @task(retries=0)
    def inspect_broker_outcomes():
        return opaque.inspect_all()

    @task(retries=0)
    def require_reconciliation(observations):
        if any(item['hold'] for item in observations['observations']):
            raise AirflowFailException('Opaque retained an unresolved outcome. Inspect broker evidence; no write retry is authorized.')
        return {'state': 'inspected', 'retry_authorized': False}

    policy = inspect_repository_policy()
    plan = propose_scope()
    activation = activate_scope(plan)
    writes = run_proposals()
    revocation = revoke_remaining_authority()
    inspection = inspect_broker_outcomes()
    policy >> delegated >> plan >> reviewed >> activation >> writes >> revocation >> inspection
    inspection >> require_reconciliation(inspection)
