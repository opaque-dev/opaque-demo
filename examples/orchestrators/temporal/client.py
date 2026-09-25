"""Local orchestration commands; no session token enters Temporal history."""
import asyncio
import json
from pathlib import Path
import sys
from temporalio.client import Client
from workflow import OpaqueSupportWorkflow


async def main():
    config = json.loads(Path('/etc/demo/workload/run.json').read_text())
    client = await Client.connect('127.0.0.1:7233')
    if sys.argv[1] == 'start':
        policy = json.loads(Path('/etc/demo/workload/compiled-policy.json').read_text())
        actions = json.loads(Path('/etc/demo/workload/actions.json').read_text())
        await client.start_workflow(OpaqueSupportWorkflow.run, {'policy': policy, 'actions': actions, **config},
            id=config['logical_run_id'], task_queue='opaque-support', memo={'opaque_policy_digest': policy['digest']})
        print(json.dumps({'started': config['logical_run_id'], 'authority_granted': False}))
    else:
        handle = client.get_workflow_handle(config['logical_run_id'])
        if sys.argv[1] == 'delegated':
            await handle.signal(OpaqueSupportWorkflow.delegation_available)
        elif sys.argv[1] == 'reviewed':
            await handle.signal(OpaqueSupportWorkflow.scope_reviewed, sys.argv[2])
        elif sys.argv[1] == 'inspect':
            print(json.dumps(await handle.query(OpaqueSupportWorkflow.inspect), indent=2))
        else:
            raise ValueError('unknown local command')


if __name__ == '__main__':
    asyncio.run(main())
