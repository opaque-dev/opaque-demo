#!/usr/bin/env python3
"""AX glue: submit repository proposals to the real Opaque CLI; never approve."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, '/opt/opaque/ax-scope')
import adapter

ROOT = Path('/workspace/demo')
WORKLOAD = Path('/etc/demo/workload')


def save(path, value):
    temporary = path.with_suffix('.pending')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def once(path, value):
    adapter.write_new(path, value)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def cli(*args):
    session = json.loads(Path('/tmp/scope-session.json').read_text())
    env = {**os.environ, 'OPAQUE_SESSION_TOKEN': session['session_token']}
    result = subprocess.run(['/opt/opaque/opaque', '--json', '--socket', '/run/opaque/opaqued.sock',
                             'scope', *args], env=env, capture_output=True, text=True, timeout=30)
    # A transport failure is not a denial and never authorizes replay.
    response = json.loads(result.stdout)
    if result.returncode and not response.get('error'):
        raise RuntimeError('opaque CLI failed without a broker response')
    return response


def main():
    os.umask(0o077)
    with (ROOT / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        base = {'pod_uid': os.environ['POD_UID'], 'live_broker_rpc': True,
                'native_human_review': False, 'provider': 'synthetic HTTPS support API',
                'core_revision': os.environ['OPAQUE_CORE_REVISION'], 'ax_revision': os.environ['AX_REVISION']}
        if (ROOT / 'plan-attempt.json').exists():
            previous = json.loads((ROOT / 'view.json').read_text())
            save(ROOT / 'view.json', {**previous, 'pod_uid': base['pod_uid'], 'phase': 'held',
                 'message': 'Runner restarted. Retained attempts require inspection; nothing was replayed.'})
            return
        save(ROOT / 'view.json', {**base, 'phase': 'awaiting_delegation'})
        while not Path('/tmp/scope-session.json').exists():
            time.sleep(.5)
        once(ROOT / 'plan-attempt.json', {'operation': 'scope plan', 'manifest': json.loads((WORKLOAD / 'scope.json').read_text())})
        plan = cli('plan', '--manifest', str(WORKLOAD / 'scope.json'))
        save(ROOT / 'plan.json', plan)
        if plan.get('error'):
            raise RuntimeError('broker denied scope proposal')
        round_id = plan['result']['document']['round_id']
        save(ROOT / 'view.json', {**base, 'phase': 'awaiting_scope_review', 'issuance_round_id': round_id})
        while not (ROOT / 'activate.json').exists():
            time.sleep(.5)
        # This file requests activation only. Opaque verifies the native receipt.
        once(ROOT / 'activation-attempt.json', {'round_id': round_id})
        activation = cli('activate', round_id)
        save(ROOT / 'activation.json', activation)
        if activation.get('error'):
            raise RuntimeError('broker refused activation')
        grant = activation['result']['grant']
        binding = json.loads((ROOT / 'activate.json').read_text())
        save(ROOT / 'view.json', {**base, 'phase': 'ready', 'native_human_review': True,
             'scope_id': grant['scope_id'], 'issuance_round_id': round_id})
        while not (ROOT / 'start.json').exists():
            time.sleep(.5)
        once(ROOT / 'execution-attempt.json', {'scope_id': grant['scope_id']})
        metadata = adapter.fetch_metadata(os.environ['AX_METADATA_URL'])
        results = []
        for action in json.loads((WORKLOAD / 'actions.json').read_text()):
            context, proposal = adapter.bind(metadata, {
                'deployment_id': binding['deployment_id'], 'run_id': binding['run_id'],
                'action_key': action['action_key'], 'requester_id': grant['subject'],
                'scope_id': grant['scope_id'], 'issuance_round_id': round_id,
                'resource': action['resource'], 'status': action['status'], **grant['owner']})
            directory = ROOT / action['action_key']
            directory.mkdir(mode=0o700)
            once(directory / 'correlation.json', context)
            once(directory / 'action.json', proposal)
            # The durable intent precedes dispatch. A missing response stays held.
            once(directory / 'dispatch-attempt.json', {'request_id': context['request_id']})
            response = cli('run', '--manifest', str(directory / 'action.json'))
            save(directory / 'response.json', response)
            outcome = cli('outcome', '--scope-id', grant['scope_id'], '--request-id', context['request_id'])
            save(directory / 'outcome.json', outcome)
            results.append({'action': action, 'request_id': context['request_id'], 'response': response,
                            'outcome': outcome, 'retry_authorized': False})
            save(ROOT / 'view.json', {**base, 'phase': 'running', 'native_human_review': True,
                 'scope_id': grant['scope_id'], 'issuance_round_id': round_id, 'actions': results})
        save(ROOT / 'view.json', {**base, 'phase': 'inspected', 'native_human_review': True,
             'scope_id': grant['scope_id'], 'issuance_round_id': round_id, 'actions': results})


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        prior = json.loads((ROOT / 'view.json').read_text()) if (ROOT / 'view.json').exists() else {}
        save(ROOT / 'view.json', {**prior, 'phase': 'held', 'error_type': type(error).__name__,
             'message': 'Execution stopped. Inspect retained records; no automatic retry.'})
        raise
