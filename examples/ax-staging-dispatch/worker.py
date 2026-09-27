#!/usr/bin/env python3
"""AX glue for the staging-dispatch demo.

Submits the checked-in proposals to the real Opaque CLI over the broker socket.
It never approves, never retries a dispatch, and never holds the GitHub token.
Every intent is journaled before the call that could have an external effect, so
a process that dies mid-dispatch leaves a record that `opaque scope outcome`
resolves on restart without a second POST.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.environ.get('OPAQUE_AX_ADAPTER', '/opt/opaque/ax-scope'))
import adapter  # noqa: E402  public core helper: metadata fetch, task identity, request IDs

ROOT = Path('/workspace/demo')
WORKLOAD = Path('/etc/demo/workload')
SESSION = Path('/tmp/scope-session.json')
OPAQUE = ['/opt/opaque/opaque', '--json', '--socket', '/run/opaque/opaqued.sock', 'scope']
# {owner}/{repo}:.github/workflows/{file}.yml:{branch}; the broker's compiler is authoritative.
TARGET = re.compile(r'[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}:\.github/workflows/[A-Za-z0-9_.-]{1,100}\.ya?ml:[A-Za-z0-9_./-]{1,200}\Z')
KILL_VARIANT = 'sigkill-second-dispatch'


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


def load(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def bind(metadata, values):
    """Correlate one logical AX action with one Opaque request ID for the dispatch kind.

    The public adapter binds support cases only (its resource grammar has no
    repository or path separators and it requires a status), so this mirrors its
    scheme for workflow targets: the request ID derives from identity fields only,
    never from the resource, so a changed target under the same action key cannot
    quietly become another attempt.
    """
    atespace, task_name = adapter.task_identity(metadata)
    context = {'schema_version': 1, 'kind': 'github.workflow.dispatch', 'atespace': atespace,
               'task_name': task_name, **values}
    for field in ('deployment_id', 'atespace', 'task_name', 'action_key', 'requester_id', 'scope_id',
                  'tenant_id', 'broker_id', 'generation'):
        adapter.identifier(context[field])
    adapter.canonical_uuid(context['run_id'])
    adapter.canonical_uuid(context['issuance_round_id'])
    if not TARGET.fullmatch(context['resource']):
        raise ValueError('invalid workflow dispatch target')
    context['request_id'] = adapter.request_id(context)
    manifest = {field: context[field] for field in ('scope_id', 'issuance_round_id', 'resource', 'request_id')}
    return context, manifest


def environment():
    session = load(SESSION)
    return {**os.environ, 'OPAQUE_SESSION_TOKEN': session['session_token']}


def cli(*args):
    result = subprocess.run([*OPAQUE, *args], env=environment(), capture_output=True, text=True, timeout=60)
    # A transport failure is not a denial and never authorizes replay.
    response = json.loads(result.stdout)
    if result.returncode and not response.get('error'):
        raise RuntimeError('opaque CLI failed without a broker response')
    if result.stderr.strip():
        # The CLI states an unknown outcome plainly; retain its words verbatim.
        response['cli_stderr'] = result.stderr.strip()[:2000]
    return response


def interrupt(directory, after_ms):
    """Start the real dispatch, then SIGKILL this worker and its CLI child while the
    request is outstanding. The broker keeps the claim it already recorded and
    finishes or records the attempt on its own; nothing here can resend it."""
    once(directory / 'sigkill-attempt.json', {'signal': 'SIGKILL', 'after_ms': after_ms,
         'target': 'worker process group including the opaque CLI child'})
    process = subprocess.Popen([*OPAQUE, 'run', '--manifest', str(directory / 'action.json')],
                               env=environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(after_ms / 1000)
    os.killpg(os.getpgrp(), signal.SIGKILL)
    process.wait()  # unreachable; the group is gone


def actions():
    return json.loads((WORKLOAD / 'actions.json').read_text())


def reconcile(base):
    """Restart path: read retained outcomes for journaled attempts; never dispatch."""
    prior = load(ROOT / 'view.json', {})
    scope_id = prior.get('scope_id')
    results = []
    for action in actions():
        directory = ROOT / action['action_key']
        if not directory.exists() or not (directory / 'dispatch-attempt.json').exists():
            results.append({'action': action, 'request_id': None, 'response': None, 'outcome': None,
                            'state': 'not_attempted', 'reconciled': False, 'retry_authorized': False})
            continue
        context = load(directory / 'correlation.json')
        response = load(directory / 'response.json')
        outcome = load(directory / 'outcome.json')
        reconciled = False
        if outcome is None:
            outcome = cli('outcome', '--scope-id', scope_id, '--request-id', context['request_id'])
            once(directory / 'outcome.json', outcome)
            reconciled = True
        results.append({'action': action, 'request_id': context['request_id'], 'response': response,
                        'outcome': outcome, 'reconciled': reconciled, 'retry_authorized': False,
                        'state': state_of(response, outcome)})
    save(ROOT / 'view.json', {**prior, **base, 'phase': 'held', 'actions': results, 'reconciled': True,
         'message': 'Worker restarted after an interrupted dispatch. Retained outcomes were read with '
                    '`opaque scope outcome`; nothing was resent and no attempt was refunded.'})


def state_of(response, outcome):
    if outcome and 'result' in outcome and isinstance(outcome['result'], dict) and 'state' in outcome['result']:
        return outcome['result']['state']
    if outcome and outcome.get('error'):
        return 'no_charged_attempt' if response is None else 'denied'
    if response and response.get('error'):
        return 'denied'
    return 'unknown_to_worker'


def execute(base, grant, round_id, binding):
    start = load(ROOT / 'start.json')
    variant = start.get('variant')
    if variant not in (None, KILL_VARIANT):
        raise ValueError('unknown run variant')
    once(ROOT / 'execution-attempt.json', {'scope_id': grant['scope_id'], 'variant': variant})
    metadata = adapter.fetch_metadata(os.environ['AX_METADATA_URL'])
    results = []
    for action in actions():
        context, proposal = bind(metadata, {
            'deployment_id': binding['deployment_id'], 'run_id': binding['run_id'],
            'action_key': action['action_key'], 'requester_id': grant['subject'],
            'scope_id': grant['scope_id'], 'issuance_round_id': round_id,
            'resource': action['resource'], **grant['owner']})
        directory = ROOT / action['action_key']
        directory.mkdir(mode=0o700)
        once(directory / 'correlation.json', context)
        once(directory / 'action.json', proposal)
        # The durable intent precedes dispatch. A missing response stays held.
        once(directory / 'dispatch-attempt.json', {'request_id': context['request_id'], 'started_at': time.time()})
        save(ROOT / 'view.json', {**base, 'phase': 'running', 'native_human_review': True, 'scope_id': grant['scope_id'],
             'issuance_round_id': round_id, 'actions': results, 'in_flight': action['action_key'], 'variant': variant})
        if variant == KILL_VARIANT and action['action_key'] == start.get('sigkill_action', 'dispatch-staging-2'):
            interrupt(directory, int(start.get('sigkill_after_ms', 900)))
        response = cli('run', '--manifest', str(directory / 'action.json'))
        save(directory / 'response.json', response)
        outcome = cli('outcome', '--scope-id', grant['scope_id'], '--request-id', context['request_id'])
        save(directory / 'outcome.json', outcome)
        results.append({'action': action, 'request_id': context['request_id'], 'response': response,
                        'outcome': outcome, 'reconciled': False, 'retry_authorized': False,
                        'state': state_of(response, outcome)})
    save(ROOT / 'view.json', {**base, 'phase': 'inspected', 'native_human_review': True, 'scope_id': grant['scope_id'],
         'issuance_round_id': round_id, 'actions': results, 'variant': variant})


def main():
    os.umask(0o077)
    if os.getpgrp() != os.getpid():
        os.setpgrp()  # own the process group so the SIGKILL variant cannot reach the AX runner
    with (ROOT / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        base = {'pod_uid': os.environ['POD_UID'], 'live_broker_rpc': True, 'native_human_review': False,
                'provider': 'GitHub REST API (api.github.com)', 'target': json.loads((WORKLOAD / 'scope.json').read_text())['resources'][0],
                'core_revision': os.environ['OPAQUE_CORE_REVISION'], 'ax_revision': os.environ['AX_REVISION']}
        if (ROOT / 'execution-attempt.json').exists():
            return reconcile(base)
        if (ROOT / 'plan-attempt.json').exists():
            previous = load(ROOT / 'view.json', {})
            save(ROOT / 'view.json', {**previous, 'pod_uid': base['pod_uid'], 'phase': 'held',
                 'message': 'Runner restarted before execution. Retained records require inspection; nothing was replayed.'})
            return
        save(ROOT / 'view.json', {**base, 'phase': 'awaiting_delegation'})
        while not SESSION.exists():
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
        binding = load(ROOT / 'activate.json')
        save(ROOT / 'view.json', {**base, 'phase': 'ready', 'native_human_review': True,
             'scope_id': grant['scope_id'], 'issuance_round_id': round_id})
        while not (ROOT / 'start.json').exists():
            time.sleep(.5)
        execute(base, grant, round_id, binding)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        prior = load(ROOT / 'view.json', {}) if ROOT.exists() else {}
        save(ROOT / 'view.json', {**prior, 'phase': 'held', 'error_type': type(error).__name__,
             'message': 'Execution stopped. Inspect retained records; no automatic retry.'})
        raise
