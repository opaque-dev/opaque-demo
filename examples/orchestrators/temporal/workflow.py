"""Durable orchestration only. Opaque remains the authorization boundary."""
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class OpaqueSupportWorkflow:
    def __init__(self):
        self.delegation_ready = False
        self.reviewed_round = None
        self.state = {'phase': 'awaiting_native_delegation', 'results': []}

    @workflow.signal
    def delegation_available(self):
        self.delegation_ready = True

    @workflow.signal
    def scope_reviewed(self, round_id: str):
        self.reviewed_round = round_id

    @workflow.query
    def inspect(self) -> dict:
        return self.state

    async def activity(self, name, argument=None, *, retry=False):
        return await workflow.execute_activity(name, argument, start_to_close_timeout=timedelta(seconds=45),
            retry_policy=RetryPolicy(maximum_attempts=3 if retry else 1))

    @workflow.run
    async def run(self, configuration: dict) -> dict:
        if workflow.info().workflow_id != configuration['logical_run_id']:
            raise ValueError('workflow identity differs from the deployment')
        self.state['policy'] = configuration['policy']
        await workflow.wait_condition(lambda: self.delegation_ready)
        round_id = await self.activity('opaque_plan')
        self.state.update(phase='awaiting_native_scope_review', round_id=round_id)
        await workflow.wait_condition(lambda: self.reviewed_round == round_id)
        grant = await self.activity('opaque_activate', round_id)
        self.state.update(phase='running', scope_id=grant['scope_id'])
        for action in configuration['actions']:
            observed = await self.activity('opaque_execute', {'logical_run_id': workflow.info().workflow_id, 'action': action}, retry=True)
            self.state['results'].append(observed)
            if observed['hold']:
                break
        await self.activity('opaque_revoke')
        self.state['inspection'] = await self.activity('opaque_inspect', retry=True)
        if (any(r['hold'] for r in self.state['results'])
                or any(r['hold'] for r in self.state['inspection']['observations'])):
            self.state['phase'] = 'held_for_reconciliation'
            # A durable hold is visible in Query/history. No signal here dispatches more work.
            await workflow.wait_condition(lambda: False)
        self.state['phase'] = 'inspected'
        return self.state
