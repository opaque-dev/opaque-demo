import asyncio
from concurrent.futures import ThreadPoolExecutor
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker
import opaque_bridge as opaque
from workflow import OpaqueSupportWorkflow


@activity.defn(name='opaque_plan')
def plan(_):
    return opaque.plan_scope()


@activity.defn(name='opaque_activate')
def activate(round_id):
    return opaque.activate_scope(round_id)


@activity.defn(name='opaque_execute')
def execute(request):
    return opaque.execute_action(request['logical_run_id'], request['action'])


@activity.defn(name='opaque_revoke')
def revoke(_):
    return opaque.revoke_scope()


@activity.defn(name='opaque_inspect')
def inspect(_):
    return opaque.inspect_all()


async def main():
    deadline = asyncio.get_running_loop().time() + 60
    while True:
        try:
            client = await Client.connect('127.0.0.1:7233')
            break
        except RuntimeError:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(.5)
    with ThreadPoolExecutor(max_workers=4) as executor:
        worker = Worker(client, task_queue='opaque-support', workflows=[OpaqueSupportWorkflow],
                        activities=[plan, activate, execute, revoke, inspect], activity_executor=executor)
        await worker.run()


if __name__ == '__main__':
    asyncio.run(main())
