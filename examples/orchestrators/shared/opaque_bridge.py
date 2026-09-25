"""Orchestrator glue for the Opaque CLI. No policy evaluator or approval signer.

Durable request identity survives scheduler retries. A recorded dispatch intent
permits only an outcome query on redelivery, including when no result was saved.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(os.environ.get('OPAQUE_DEMO_STATE', '/workspace/demo'))
CONFIG = Path('/etc/demo/workload')
SESSION = Path('/tmp/scope-session.json')
BINARY = '/opt/opaque/opaque'


def read(path):
    return json.loads(path.read_text())


def save(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix('.pending')
    with os.fdopen(os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.chmod(0o600)
    tmp.replace(path)
    sync_directory(path.parent)


def sync_directory(path):
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def locked(name):
    if not re.fullmatch(r'[a-z0-9_-]{1,80}', name):
        raise ValueError('invalid operation name')
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (ROOT / (name + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def require_session():
    if not SESSION.exists():
        raise RuntimeError('Native requester delegation is required')
    session = read(SESSION)
    if session.get('mode') != 'delegated' or not session.get('session_token', '').startswith('opqd1.'):
        raise ValueError('invalid delegated session')
    return session['session_token']


def cli(*args):
    env = {**os.environ, 'OPAQUE_SESSION_TOKEN': require_session()}
    result = subprocess.run([BINARY, '--json', '--socket', '/run/opaque/opaqued.sock', 'scope', *args],
                            env=env, capture_output=True, text=True, timeout=30)
    try:
        response = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise RuntimeError('No usable broker response; inspect retained outcome') from error
    if not isinstance(response, dict) or ('result' not in response and 'error' not in response):
        raise RuntimeError('Malformed broker response')
    if result.returncode and not response.get('error'):
        raise RuntimeError('Opaque CLI failed without an explicit broker denial')
    return response


def result(response):
    if response.get('error') or response.get('result') is None:
        raise RuntimeError('Broker did not grant the requested operation')
    return response['result']


def require_run(logical_run_id):
    if logical_run_id != read(CONFIG / 'run.json')['logical_run_id']:
        raise ValueError('workflow identity differs from this deployment; create a fresh demo deployment')


def describe_policy(logical_run_id=None):
    if logical_run_id is not None:
        require_run(logical_run_id)
    compiled = read(CONFIG / 'compiled-policy.json')
    return {'policy': compiled['policy'], 'expected_digest': compiled['digest'],
            'scope_request': read(CONFIG / 'scope.json'), 'authority_granted': False}


def delegated():
    return SESSION.exists() and (ROOT / 'policy-observation.json').exists()


def plan_scope():
    with locked('plan'):
        if (ROOT / 'plan.json').exists():
            return result(read(ROOT / 'plan.json'))['document']['round_id']
        if (ROOT / 'plan-intent.json').exists():
            raise RuntimeError('Earlier planning outcome is unknown; no new review round created')
        require_session()
        save(ROOT / 'plan-intent.json', {'operation': 'scope plan', 'manifest': read(CONFIG / 'scope.json')})
        response = cli('plan', '--manifest', str(CONFIG / 'scope.json'))
        save(ROOT / 'plan.json', response)
        return result(response)['document']['round_id']


def reviewed():
    if not (ROOT / 'reviewed.json').exists() or not (ROOT / 'plan.json').exists():
        return False
    return read(ROOT / 'reviewed.json')['round_id'] == result(read(ROOT / 'plan.json'))['document']['round_id']


def activate_scope(round_id):
    with locked('activate'):
        if round_id != result(read(ROOT / 'plan.json'))['document']['round_id']:
            raise ValueError('activation round differs from retained plan')
        if (ROOT / 'grant.json').exists():
            return result(read(ROOT / 'grant.json'))['grant']
        if (ROOT / 'activate-intent.json').exists():
            raise RuntimeError('Earlier activation outcome is unknown; inspection required')
        if not reviewed() or read(ROOT / 'reviewed.json')['round_id'] != round_id:
            raise RuntimeError('Native review has not been recorded for this round')
        save(ROOT / 'activate-intent.json', {'round_id': round_id})
        response = cli('activate', round_id)
        save(ROOT / 'grant.json', response)
        return result(response)['grant']


def identity(grant, logical_run_id, action):
    # Deliberately excludes Airflow try_number and Temporal Activity attempt/Run ID.
    parts = ['opaque.orchestrator.action.v1', grant['owner'], grant['scope_id'], grant['subject'], logical_run_id, action['action_key']]
    return 'orch-' + hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def inspect_action(grant, request_id):
    try:
        response = cli('outcome', '--scope-id', grant['scope_id'], '--request-id', request_id)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return {'state': 'unavailable', 'hold': True, 'broker_response': None, 'retry_authorized': False}
    record = response.get('result')
    if response.get('error') or not isinstance(record, dict) or not record:
        return {'state': 'not_observed', 'hold': True, 'broker_response': response, 'retry_authorized': False}
    state = record.get('state', 'unknown')
    return {'state': state, 'hold': state != 'api_accepted', 'broker_response': response, 'retry_authorized': False}


def execute_action(logical_run_id, action):
    """The broker decides admissibility; this adapter only prevents blind replay."""
    require_run(logical_run_id)
    grant = result(read(ROOT / 'grant.json'))['grant']
    round_id = result(read(ROOT / 'plan.json'))['document']['round_id']
    request_id = identity(grant, logical_run_id, action)
    directory = ROOT / action['action_key']
    proposal = {'scope_id': grant['scope_id'], 'issuance_round_id': round_id,
                'resource': action['resource'], 'status': action['status'], 'request_id': request_id}
    with locked(action['action_key']):
        directory.mkdir(mode=0o700, exist_ok=True)
        sync_directory(ROOT)
        if (directory / 'proposal.json').exists() and read(directory / 'proposal.json') != proposal:
            raise ValueError('logical action was substituted; retain the hold')
        if (directory / 'intent.json').exists():
            # Cached responses are historical only; query the actual broker on redelivery.
            observed = inspect_action(grant, request_id)
            save(directory / 'redelivery.json', observed)
            return {'action_key': action['action_key'], 'request_id': request_id, 'redelivery': True, **observed}
        require_session()
        save(directory / 'proposal.json', proposal)
        save(directory / 'intent.json', {'request_id': request_id, 'operation': 'scope run'})
        try:
            response = cli('run', '--manifest', str(directory / 'proposal.json'))
        except (OSError, subprocess.SubprocessError, RuntimeError):
            # Return a held business outcome, not a transient error that invites a new write.
            observed = {'state': 'unknown', 'hold': True, 'retry_authorized': False, 'broker_response': None}
        else:
            save(directory / 'response.json', response)
            if response.get('error'):
                # scope_unavailable can mean denial OR an unavailable result.
                observed = {'state': 'unavailable', 'hold': True, 'retry_authorized': False, 'broker_response': response}
            else:
                observed = inspect_action(grant, request_id)
        save(directory / 'observation.json', observed)
        return {'action_key': action['action_key'], 'request_id': request_id, 'redelivery': False, **observed}


def inspect_all():
    grant = result(read(ROOT / 'grant.json'))['grant']
    scope = cli('show', grant['scope_id'])
    observations = []
    for path in sorted(ROOT.glob('*/proposal.json')):
        action = read(path)
        observed = inspect_action(grant, action['request_id'])
        observations.append({'request_id': action['request_id'], 'resource': action['resource'],
                             **observed})
    report = {'scope': scope, 'observations': observations, 'retry_authorized': False,
              'policy_observation': read(ROOT / 'policy-observation.json')}
    save(ROOT / 'inspection.json', report)
    return report


def revoke_scope():
    grant = result(read(ROOT / 'grant.json'))['grant']
    with locked('revoke'):
        if (ROOT / 'revoke-intent.json').exists():
            response = cli('show', grant['scope_id'])
        else:
            save(ROOT / 'revoke-intent.json', {'scope_id': grant['scope_id']})
            response = cli('revoke', grant['scope_id'])
            save(ROOT / 'revocation.json', response)
        if result(response).get('revoked_at') is None:
            raise RuntimeError('Scope revocation is not confirmed; inspect the broker before continuing')
        return response
