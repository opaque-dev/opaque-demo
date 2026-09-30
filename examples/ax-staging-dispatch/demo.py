#!/usr/bin/env python3
"""Run the AX staging-dispatch demo: every machine step scripted, every human step listed.

`build` and `deploy` perform the machine work and stop where a human must act.
`delegate-reviewer`, `delegate-requester` and `scope-review` each open exactly one
native review window on this host. `run` resumes the AX workload afterwards.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import partial
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import ssl
from string import Template
import subprocess
import sys
import threading
import time
import uuid

import yaml

EXAMPLE = Path(__file__).resolve().parent
ROOT = EXAMPLE.parents[1]
PROFILE = 'opaque-ax-demo'
CORE_REVISION = '7067aa039f1ef75ae160df102147c0d182522271'
AX_REVISION = 'f009cc81c9a571073bc1dd58cd2ed934bf2d5b1c'
CORE_SOURCE = 'https://github.com/opaque-dev/opaque.git'
AX_SOURCE = 'https://github.com/google/ax.git'
OWNER = 'opaque.info/demo-run'
IMAGE_NAME = 'opaque-ax-staging-dispatch'
IMAGE_KIND = 'staging-dispatch-broker'
TENANT = 'opaque-ax-dispatch'
POLICY_FILE = 'staging-dispatch.yaml'
KILL_VARIANT = 'sigkill-second-dispatch'
RUNTIME_FILES = ('worker.py', 'broker/install.sh', 'fixtures/client.py', 'fixtures/services.py')


def command(args, *, data=None, timeout=60, env=None, cwd=None):
    return subprocess.run([str(a) for a in args], input=data, capture_output=True, text=True,
                          check=True, timeout=timeout, env=env, cwd=cwd).stdout


def kubectl(*args, data=None, timeout=60):
    return command(['minikube', '-p', PROFILE, 'kubectl', '--', *args], data=data, timeout=timeout)


def save(path, value):
    temporary = path.with_suffix('.pending')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def human_steps(state, python=None):
    python = python or sys.executable
    script = Path(__file__).resolve()
    run = f'{python} -B {script} --state {state}'
    return [
        (f'{run} delegate-reviewer',
         'One native window. Approve the operator delegation for the trusted broker-side protocol client. '
         'The script then reads `opaque scope snapshot` and checks the running policy digest against the compiled repository policy.'),
        (f'{run} delegate-requester',
         'One native window. Approve the requester delegation for the AX pod. '
         'The AX worker then runs `opaque scope plan --manifest /etc/demo/workload/scope.json`.'),
        (f'{run} scope-review',
         'One native window showing the complete signed scope: one workflow target, two attempts, ten minutes. Approve it. '
         'The AX worker then runs `opaque scope activate`.'),
        (f'{run} run            (or: {run} run --variant {KILL_VARIANT})',
         'No native window. Resumes the AX workload: three proposals go through `opaque scope run`; the variant SIGKILLs the worker '
         'during the second dispatch and restarts it so `opaque scope outcome` resolves the lost answer.'),
        (f'{run} evidence',
         'No native window. Stops the broker, exports and signs the retained ledger, verifies it with `opaque-evidence`, '
         'and shows that one flipped byte is rejected.'),
    ]


SHOT_LIST = [
    ('0:00', 'Terminal: `opaque authority-policy compile policies/staging-dispatch.yaml --json` prints the digest; '
             '`demo.py delegate-reviewer` output shows the running broker reports the same digest via `opaque scope snapshot`.'),
    ('0:15', 'One Touch ID window: `opaque-approver scope-review` shows the signed scope (opaque-dev/opaque-staging-scratch, '
             'staging.yml, main, 2 attempts, 10 minutes). Approve.'),
    ('0:30', 'Browser view or `demo.py run`: three proposals. production.yml is denied by the approved scope before any GitHub request. '
             'staging.yml dispatch 1 records api_accepted. staging.yml dispatch 2 records api_accepted and exhausts the budget.'),
    ('0:50', 'Variant take: the worker is SIGKILLed during dispatch 2 (AX runner log shows exit code -1). Restart shows the worker '
             'reading `opaque scope outcome` for the lost answer and holding; no second POST, the attempt stays charged.'),
    ('1:05', '`demo.py evidence`: `opaque-evidence verify` accepts the signed export with the pinned checkpoint; the same command '
             'rejects a copy with one flipped byte.'),
    ('1:20', 'GitHub Actions tab of opaque-dev/opaque-staging-scratch: exactly two staging runs from this take (one or two if the '
             'variant killed dispatch 2 before the broker claimed it), zero production runs, plus the labelled direct validity run.'),
]


def print_stop(state):
    print('\nMachine steps are complete. STOP: a human must now run these commands, in order, on this host:\n')
    for number, (cmd, why) in enumerate(human_steps(state), 1):
        print(f'{number}. {cmd}\n   {why}\n')
    print('90-second shot list:')
    for at, shot in SHOT_LIST:
        print(f'  {at}  {shot}')
    print()


class Demo:
    def __init__(self, directory):
        self.directory = directory
        self.cluster = json.loads((directory / 'cluster.json').read_text())
        if self.cluster.get('kind') != IMAGE_KIND or self.cluster.get('profile') != PROFILE:
            raise ValueError('this command requires a staging-dispatch deployment')
        if not re.fullmatch(r'opaque-dispatch-[0-9a-f]{10}', self.cluster['namespace']):
            raise ValueError('invalid namespace')
        self.lock, self.busy, self.cache, self.error = threading.Lock(), False, None, None

    def persist(self):
        save(self.directory / 'cluster.json', self.cluster)

    def owned(self):
        obj = json.loads(kubectl('get', 'namespace', self.cluster['namespace'], '-o', 'json'))
        if obj['metadata']['uid'] != self.cluster['namespace_uid'] or obj['metadata'].get('labels', {}).get(OWNER) != self.cluster['run_id']:
            raise ValueError('namespace ownership changed')

    def k(self, *args, **kwargs):
        self.owned()
        return kubectl('-n', self.cluster['namespace'], *args, **kwargs)

    def exec(self, *args, peer='identity-client', data=None, timeout=60):
        deployment = 'runner' if peer == 'runner' else 'broker'
        if peer == 'broker':
            args = ('setpriv', '--reuid=7581', '--regid=7581', '--clear-groups', *args)
        return self.k('exec', '-i', 'deployment/' + deployment, '-c', peer, '--', *args, data=data, timeout=timeout)

    def rpc(self, method, params=None, *, peer='identity-client', session=False, allow_error=False):
        result = json.loads(self.exec('python3', '-B', '/opt/demo/fixtures/client.py', 'rpc', peer=peer,
                    data=json.dumps({'method': method, 'params': params or {}, 'use_session': session}), timeout=190))
        if not allow_error and (result.get('error') or result.get('transport_error')):
            save(self.directory / 'last-rpc-error.json', result)
            raise ValueError('broker refused ' + method)
        return result

    def resource(self, filename, **values):
        raw = Template((EXAMPLE / 'k8s' / filename).read_text()).substitute(IMAGE=self.cluster['image'], **values)
        docs = list(yaml.safe_load_all(raw))
        for doc in docs:
            doc['metadata'].update(namespace=self.cluster['namespace'], labels={OWNER: self.cluster['run_id']})
        self.k('apply', '-f', '-', data=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': docs}))

    def configmap(self, name, data):
        existing = self.k('get', 'configmap', name, '--ignore-not-found', '-o', 'json').strip()
        if existing:
            item = json.loads(existing)
            if item.get('data') != data or not item.get('immutable') or item['metadata'].get('labels', {}).get(OWNER) != self.cluster['run_id']:
                raise ValueError('existing ConfigMap differs from this deployment')
            return
        self.k('create', '-f', '-', data=json.dumps({'apiVersion': 'v1', 'kind': 'ConfigMap',
            'metadata': {'name': name, 'labels': {OWNER: self.cluster['run_id']}}, 'immutable': True, 'data': data}))

    def token_secret(self, token_file):
        """The token goes host file -> kubectl -> Secret -> broker init container. This
        process reads it only for a truncated fingerprint, and the AX pod never mounts the Secret."""
        info = token_file.stat()
        if not token_file.is_file() or token_file.is_symlink() or not 0 < info.st_size <= 4096:
            raise ValueError('GitHub token file must be a regular non-empty file of at most 4096 bytes')
        if info.st_mode & 0o077:
            raise ValueError('GitHub token file must not be group or world readable')
        if self.k('get', 'secret', 'github-token', '--ignore-not-found').strip():
            raise ValueError('github-token Secret already exists in this namespace')
        self.k('create', 'secret', 'generic', 'github-token', f'--from-file=token={token_file}')
        self.k('label', 'secret', 'github-token', f'{OWNER}={self.cluster["run_id"]}')
        self.cluster['github_token'] = {'source_sha256': hashlib.sha256(token_file.read_bytes()).hexdigest()[:16] + '...',
                                        'custody': 'Kubernetes Secret github-token, mounted only by the broker init container'}
        self.persist()

    def start_broker(self, stage):
        mapped = stage != 'bootstrap'
        device = {'name': 'Native staging-dispatch reviewer', 'public_key_hex': self.cluster['workstation_public_key']}
        reviewer = self.cluster.get('reviewer_identity', {}).get('principal_id', '')
        if mapped:
            device['principal_id'] = reviewer
        source = (EXAMPLE / 'broker/config.toml.in').read_text()
        if stage != 'policy':
            source = source.split('[authority_policy]')[0]
        config = Template(source).substitute(WORKSTATION='{'+', '.join(k+' = '+json.dumps(v) for k,v in device.items())+'}',
            POLICY_DIGEST=self.cluster['compiled']['digest'], REVIEWER_ID=reviewer,
            REVIEWER_PUBLIC_KEY=self.cluster['workstation_public_key'])
        self.configmap('broker-config-' + stage, {'config.toml': config})
        self.resource('broker.yaml', CONFIG_MAP='broker-config-' + stage)
        try:
            self.k('rollout', 'status', 'deployment/broker', '--timeout=120s', timeout=150)
        except subprocess.CalledProcessError:
            self.broker_logs('broker-startup-failure-' + stage)
            raise
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                version = self.rpc('version')['result']
                if (version['trust_domain_enforced'] and version['approval_backend'] == 'native'
                    and not version['workstation_test_mode'] and version['version'].endswith('+' + CORE_REVISION[:7])):
                    self.cluster.update(broker_stage=stage, broker_version=version,
                        config_sha256=hashlib.sha256(config.encode()).hexdigest())
                    self.persist()
                    return
            except (subprocess.SubprocessError, ValueError, KeyError):
                time.sleep(.5)
        self.broker_logs('broker-startup-failure-' + stage)
        raise RuntimeError('broker startup did not confirm pinned native custody settings')

    def broker_logs(self, name):
        try:
            logs = self.k('logs', 'deployment/broker', '-c', 'broker', '--tail=40')
        except subprocess.CalledProcessError:
            logs = self.k('logs', 'deployment/broker', '-c', 'install', '--tail=40')
        (self.directory / (name + '.log')).write_text(logs)

    @contextmanager
    def forward(self):
        self.owned()
        with (self.directory / 'port-forward.log').open('a') as log:
            process = subprocess.Popen(['minikube', '-p', PROFILE, 'kubectl', '--', '-n', self.cluster['namespace'],
                'port-forward', '--address=127.0.0.1', 'deployment/broker', f"{self.cluster['approval_port']}:18913"], stdout=log, stderr=log, start_new_session=True)
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError('owned approval port-forward exited')
                    try:
                        with socket.create_connection(('127.0.0.1', self.cluster['approval_port']), timeout=.2):
                            break
                    except OSError:
                        time.sleep(.2)
                else:
                    raise RuntimeError('approval port-forward unavailable')
                yield
            finally:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                process.wait(timeout=10)

    def native(self, *args, timeout=30):
        for name, expected in self.cluster['native_hashes'].items():
            if hashlib.sha256((Path(self.cluster['native_bin']) / name).read_bytes()).hexdigest() != expected:
                raise ValueError('native reviewer executable changed')
        try:
            return command([Path(self.cluster['native_bin']) / 'opaque-approver', *args,
                            '--state-dir', self.cluster['workstation']], timeout=timeout)
        except subprocess.CalledProcessError as error:
            # The reviewer's own reason (expired, rejected round, UI unavailable) is the diagnosis.
            raise RuntimeError(f'opaque-approver {args[0]} failed: {(error.stderr or "").strip()}') from error

    def enroll(self):
        fingerprint = self.exec('sha256sum', '/var/lib/opaque/approval_server.cert', peer='broker').split()[0]
        if self.cluster.get('tls_fingerprint', fingerprint) != fingerprint:
            raise ValueError('broker TLS pin changed')
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        with self.forward():
            connection = http.client.HTTPSConnection('127.0.0.1', self.cluster['approval_port'], context=context, timeout=10)
            try:
                connection.connect()
                if hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest() != fingerprint:
                    raise ValueError('broker TLS pin mismatch')
                connection.request('POST', '/workstation/enrollment/challenge',
                    json.dumps({'public_key_hex': self.cluster['workstation_public_key']}), {'Content-Type': 'application/json'})
                response = connection.getresponse()
                if response.status != 200:
                    raise ValueError('enrollment challenge refused')
                broker_id = json.loads(response.read(262144))['broker_id']
            finally:
                connection.close()
            self.native('enroll', '--broker', f"https://127.0.0.1:{self.cluster['approval_port']}",
                        '--broker-id', broker_id, '--tls-fingerprint', fingerprint)
        self.cluster.update(tls_fingerprint=fingerprint, broker_id=broker_id)
        self.persist()

    def login(self, subject):
        start = self.rpc('identity.login_start')['result']
        proof = json.loads(self.exec('python3', '-B', '/opt/demo/fixtures/client.py', 'login',
            data=json.dumps({'auth_url': start['auth_url'], 'subject': subject})))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = self.rpc('identity.login_status', {'attempt_id': start['attempt_id']})['result']
            if result['status'] != 'pending':
                break
            time.sleep(.1)
        if result['status'] != 'complete':
            raise RuntimeError('synthetic OIDC protocol did not complete')
        self.cluster[subject + '_identity'] = result['identity']
        save(self.directory / (subject + '-identity.json'), {'synthetic': True, 'human_identity_proven': False, 'protocol': proof, 'result': result})
        self.persist()

    def new_key(self, name):
        path = self.directory / name
        result = json.loads(command([Path(self.cluster['native_bin']) / 'opaque-approver', 'init',
                              '--state-dir', path, '--name', 'Staging dispatch demo ' + name]))
        self.cluster.update(workstation=str(path), workstation_public_key=result['public_key_hex'])
        self.persist()

    def custody(self):
        """The AX pod must hold no credential and obtain no authority before native delegation."""
        self.exec('sh', '-c', 'set -eu; [ "$(id -u)" = 7582 ]; [ ! -e /var/lib/opaque ]; [ ! -e /input ]; [ ! -e /fixture ]; '
                  '! grep -qs github-token /proc/mounts; [ "$(stat -c "%a %u %g" /run/opaque/daemon.token)" = "640 7581 7987" ]', peer='runner')
        denied = self.rpc('scope_plan', json.loads((EXAMPLE / 'workload/scope.json').read_text()), peer='runner', allow_error=True)
        if not (denied.get('error') or denied.get('transport_error')):
            raise ValueError('AX runner obtained authority before native delegation')
        save(self.directory / 'pre-delegation-denial.json', denied)
        # The token exists only as the broker-owned private custody file.
        listing = self.exec('stat', '-c', '%a %u %g %h %F', '/var/lib/opaque/github.token', peer='broker').split()
        if listing[:4] != ['600', '7581', '7581', '1']:
            raise ValueError('GitHub token custody is not a broker-owned private regular file')
        self.cluster['token_custody_observed'] = ' '.join(listing)
        self.persist()

    def delegate(self, subject, peer):
        marker = self.directory / (subject + '-delegation-attempt.json')
        if marker.exists():
            raise ValueError('native delegation already attempted; inspect its retained result')
        self.login(subject)
        if json.loads(self.native('list')):
            raise ValueError('another native challenge is pending')
        save(marker, {'subject': subject, 'native_only': True})
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(self.rpc, 'agent_session_start', {'mode': 'delegated', 'ttl_secs': 3600,
                'label': 'staging-dispatch-' + subject, 'reason': 'Inspect repository policy' if subject == 'reviewer' else 'Dispatch the approved staging workflow'}, allow_error=True)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                pending = json.loads(self.native('list'))
                if len(pending) == 1 and pending[0]['operation'] == 'agent_session_start':
                    break
                if pending or waiting.done():
                    raise ValueError('ambiguous or failed native delegation request')
                time.sleep(.25)
            else:
                raise RuntimeError('native challenge did not appear')
            print('Opening native ' + subject + ' delegation review.', flush=True)
            self.native('review', '--approval-id', pending[0]['approval_id'], timeout=200)
            response = waiting.result(timeout=190)
        if response.get('error') or response.get('transport_error'):
            raise RuntimeError('delegation was not granted')
        session = response['result']
        self.exec('python3', '-B', '/opt/demo/fixtures/client.py', 'session', peer=peer, data=json.dumps(session))
        # A delegation challenge carries no task binding, so the broker stores no workstation
        # receipt for it (GET /workstation/receipts answers 404). The broker log records it as
        # approval.granted for operation agent_session_start.
        save(self.directory / (subject + '-delegation.json'), {'approval_id': pending[0]['approval_id'], 'granted': True,
             'workstation_receipt': None, 'record': 'broker audit log: approval.granted, operation agent_session_start'})

    def delegate_reviewer(self):
        if self.cluster.get('phase') != 'awaiting_native_review':
            raise ValueError('deployment is not awaiting native review')
        with self.forward():
            # Administrative inspection credentials never enter the AX pod.
            self.delegate('reviewer', 'identity-client')
            snapshot = self.rpc('scope_snapshot', session=True)['result']
            observed = snapshot.get('authority_policy')
            expected = {k: self.cluster['compiled'][k] for k in ('digest', 'identity')}
            if observed != expected:
                raise ValueError('running broker policy differs from the compiled repository policy')
            save(self.directory / 'broker-policy-snapshot.json', snapshot)
            self.cluster.update(policy_observation=observed, phase='policy_verified')
            self.persist()
        print(json.dumps({'step': 'delegate-reviewer', 'policy_digest_compiled': expected['digest'],
                          'policy_digest_observed_by_snapshot': observed['digest'], 'policy_verified': True}, indent=2))

    def delegate_requester(self):
        if self.cluster.get('phase') != 'policy_verified':
            raise ValueError('run delegate-reviewer first')
        with self.forward():
            self.delegate('requester', 'runner')
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            view = self.read()
            if view['phase'] == 'awaiting_scope_review':
                break
            if view['phase'] == 'held':
                raise RuntimeError('scope proposal is held')
            time.sleep(.5)
        else:
            raise RuntimeError('AX child did not request scope review')
        self.cluster.update(phase='awaiting_scope_review', issuance_round_id=view['issuance_round_id'])
        self.persist()
        print(json.dumps({'step': 'delegate-requester', 'issuance_round_id': view['issuance_round_id'],
                          'scope_request': self.cluster['scope_request'], 'approved': False}, indent=2))

    def scope_review(self):
        if self.cluster.get('phase') != 'awaiting_scope_review':
            raise ValueError('run delegate-requester first')
        round_id = self.cluster['issuance_round_id']
        with self.forward():
            print('Opening native review for workload/scope.json.', flush=True)
            self.native('scope-review', '--round-id', round_id, timeout=310)
            receipt = json.loads(self.native('scope-receipt', '--round-id', round_id))
        if receipt['response']['decision'] != 'approve':
            raise ValueError('scope was not approved')
        save(self.directory / 'scope-receipt.json', receipt)
        self.signal('activate.json', {'deployment_id': self.cluster['namespace'], 'run_id': self.cluster['logical_run_id']})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            view = self.read()
            if view['phase'] == 'ready':
                break
            if view['phase'] == 'held':
                raise RuntimeError('activation is held')
            time.sleep(.5)
        else:
            raise RuntimeError('AX child did not activate the scope')
        self.cluster.update(phase='scope_reviewed', scope_id=view['scope_id'])
        self.persist()
        print(json.dumps({'step': 'scope-review', 'scope_id': view['scope_id'], 'issuance_round_id': round_id,
                          'next': 'run (optionally --variant ' + KILL_VARIANT + ')'}, indent=2))

    def signal(self, name, value):
        if name not in ('activate.json', 'start.json'):
            raise ValueError('unknown control file')
        self.exec('python3', '-B', '-c', 'import json,sys;sys.path.insert(0,"/opt/demo");import worker;worker.once(worker.ROOT/sys.argv[1],json.load(sys.stdin))',
                  name, peer='runner', data=json.dumps(value))

    def worker_alive(self):
        return self.exec('python3', '-c', 'import os;print(any(b"/opt/demo/worker.py" in open(f"/proc/{p}/cmdline","rb").read() '
                         'for p in os.listdir("/proc") if p.isdigit() and p != str(os.getpid())))', peer='runner').strip() == 'True'

    def run(self, variant=None, after_ms=900):
        view = self.read()
        if view.get('phase') != 'ready' or not view.get('policy_verified'):
            raise ValueError('run requires a broker-verified policy and native-approved scope; no replay')
        start = {'requested': True}
        if variant:
            if variant != KILL_VARIANT:
                raise ValueError('unknown variant')
            start.update(variant=variant, sigkill_after_ms=after_ms)
        self.signal('start.json', start)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            view = self.read()
            if view['phase'] in ('inspected', 'held'):
                break
            if variant and view['phase'] == 'running' and not self.worker_alive():
                (self.directory / 'runner-log-after-sigkill.txt').write_text(self.k('logs', 'deployment/runner', '-c', 'runner', '--tail=20'))
                print('Worker process group is gone (SIGKILL). Restarting the worker for reconciliation only.', flush=True)
                self.exec('env', 'AX_METADATA_URL=http://127.0.0.1:8080', 'python3', '-B', '/opt/demo/worker.py', peer='runner', timeout=120)
                view = self.read()
                break
            time.sleep(1)
        else:
            raise RuntimeError('workload did not finish; inspect the retained view')
        self.cluster['phase'] = 'workload_' + view['phase']
        self.persist()
        # The CLI sees one generic refusal code; the broker log carries the reason.
        logs = re.sub(r'\x1b\[[0-9;]*m', '', self.k('logs', 'deployment/broker', '-c', 'broker', '--tail=300'))
        (self.directory / 'broker-log-after-run.txt').write_text(logs)
        denials = [line.split('opaqued', 1)[-1].strip() for line in logs.splitlines() if 'scope request denied' in line]
        print(json.dumps({'step': 'run', 'variant': variant, 'phase': view['phase'], 'message': view.get('message'), 'broker_denials': denials,
                          'actions': [{'proposal': a['action']['action_key'], 'resource': a['action']['resource'],
                                       'broker_response': (a.get('response') or {}).get('error', {}).get('code') if (a.get('response') or {}).get('error') else ('result' if a.get('response') else None),
                                       'retained_state': a.get('state'), 'reconciled_via_scope_outcome': a.get('reconciled'),
                                       'retry_authorized': False} for a in view.get('actions', [])]}, indent=2))

    def evidence(self):
        """Stop the broker, export and sign the retained ledger, verify it on the host, reject a flipped byte."""
        if (self.directory / 'evidence').exists():
            raise ValueError('evidence already exported; inspect the retained directory')
        native = Path(self.cluster['native_bin']) / 'opaque-evidence'
        self.k('scale', 'deployment/broker', '--replicas=0')
        self.k('wait', '--for=delete', 'pod', '-l', 'app=opaque-broker', '--timeout=120s', timeout=150)
        self.resource('evidence.yaml')
        self.k('wait', '--for=condition=Ready', 'pod/evidence', '--timeout=120s', timeout=150)
        ex = lambda *a, **kw: self.k('exec', '-i', 'pod/evidence', '--', *a, **kw)  # noqa: E731
        try:
            keygen = json.loads(ex('/opt/opaque/opaque-evidence', 'keygen', '--private-key', '/out/producer.key', '--public-key', '/out/producer.pub'))
            ex('/opt/opaque/opaque-evidence', 'enroll', '--public-key', keygen['public_key'], '--key-id', keygen['key_id'],
               '--tenant', TENANT, '--broker', self.cluster['broker_id'], '--stream', 'scope-ledger', '--generation', '1', '--output', '/out/producer.json')
            created = json.loads(ex('/opt/opaque/opaque-evidence', 'create-scope', '--database', '/var/lib/opaque/scopes.db',
                '--private-key', '/out/producer.key', '--enrollment', '/out/producer.json', '--build-identity', CORE_REVISION, '--output', '/out/snapshot'))
            out = self.directory / 'evidence'
            out.mkdir(mode=0o700)
            for name, path in (('producer.json', '/out/producer.json'), ('checkpoint.json', '/out/snapshot/checkpoint.json'), ('scope.json', '/out/snapshot/scope.json')):
                (out / name).write_bytes(base64.b64decode(ex('base64', '-w0', path)))
        finally:
            # The disposable signing key dies with the pod's memory volume.
            self.k('delete', 'pod/evidence', '--wait=true', '--timeout=60s', timeout=90)
            self.k('scale', 'deployment/broker', '--replicas=1')
        pin = hashlib.sha256((out / 'checkpoint.json').read_bytes()).hexdigest()
        if pin != created['checkpoint_sha256']:
            raise ValueError('fetched checkpoint differs from the producer report')
        verify = json.loads(command([native, 'verify', '--enrollment', out / 'producer.json', '--checkpoint', out / 'checkpoint.json',
                                     '--export', out / 'scope.json', '--expected-checkpoint-sha256', pin]))
        flipped = bytearray((out / 'scope.json').read_bytes())
        index = len(flipped) // 2
        flipped[index] ^= 0x01
        (out / 'scope-flipped.json').write_bytes(flipped)
        rejected = subprocess.run([str(native), 'verify', '--enrollment', out / 'producer.json', '--checkpoint', out / 'checkpoint.json',
                                   '--export', out / 'scope-flipped.json', '--expected-checkpoint-sha256', pin], capture_output=True, text=True)
        if rejected.returncode == 0:
            raise RuntimeError('verifier accepted altered export bytes')
        summary = {'step': 'evidence', 'checkpoint_sha256': pin, 'export_sha256': created['export_sha256'],
                   'scope_count': created['scope_count'], 'action_count': created['action_count'], 'event_count': created['event_count'],
                   'verify': verify, 'flipped_byte_index': index, 'flipped_byte_rejected': True,
                   'flipped_verify_stderr': rejected.stderr.strip()[:300], 'pin_source': 'same run; a relying party must retain the pin independently',
                   'broker': 'restarted after export; delegated sessions and pending rounds do not survive the restart'}
        save(out / 'summary.json', summary)
        self.k('rollout', 'status', 'deployment/broker', '--timeout=120s', timeout=150)
        print(json.dumps(summary, indent=2))

    def read(self):
        self.cluster = json.loads((self.directory / 'cluster.json').read_text())
        raw = self.exec('python3', '-c', 'from pathlib import Path; p=Path("/workspace/demo/view.json"); print(p.read_text() if p.exists() else "{}")', peer='runner')
        if len(raw) > 200000:
            raise ValueError('oversized view')
        view = json.loads(raw)
        observed = self.cluster.get('policy_observation')
        view.update(namespace=self.cluster['namespace'], policy_source=self.cluster['policy_source'],
            compiled_policy=self.cluster['compiled'], policy_observation=observed,
            policy_verified=observed == {k: self.cluster['compiled'][k] for k in ('digest', 'identity')},
            broker_stage=self.cluster.get('broker_stage'), scope_request=self.cluster['scope_request'],
            token_custody=self.cluster.get('token_custody_observed'))
        self.cache = view
        save(self.directory / 'last-view.json', view)
        return view

    def action(self, kind):
        with self.lock:
            view = self.read()
            if kind != 'run' or view.get('phase') != 'ready' or not view.get('policy_verified'):
                raise ValueError('run requires a broker-verified policy and native-approved scope; no replay')
            self.signal('start.json', {'requested': True})


def build(work, image, arch):
    """Clone and build the pinned core and AX sources, then build the demo image."""
    if not work.is_absolute() or any((p / '.git').exists() for p in (work, *work.parents)):
        raise ValueError('use an absolute work directory outside Git')
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, url, revision in (('core', CORE_SOURCE, CORE_REVISION), ('ax', AX_SOURCE, AX_REVISION)):
        checkout = work / name
        if not (checkout / '.git').exists():
            command(['git', 'clone', '--quiet', url, checkout], timeout=600)
        command(['git', '-C', checkout, 'fetch', '--quiet', 'origin'], timeout=600)
        command(['git', '-C', checkout, 'checkout', '--quiet', '--detach', revision])
        if command(['git', '-C', checkout, 'rev-parse', 'HEAD']).strip() != revision:
            raise RuntimeError(name + ' checkout is not at the pinned revision')
    print('Building native opaque, opaque-approver and opaque-approve-helper (host reviewer binaries).', flush=True)
    command(['cargo', 'build', '--locked', '-p', 'opaque', '-p', 'opaque-approver', '-p', 'opaque-approve-helper'],
            cwd=work / 'core', env={**os.environ, 'OPAQUE_BUILD_REVISION': CORE_REVISION}, timeout=3600)
    context = work / 'context'
    if context.exists():
        if not (context / '.opaque-demo-context').exists():
            raise ValueError('refusing to replace a context directory this script did not create')
        shutil.rmtree(context)
    (context / 'source').mkdir(parents=True)
    (context / '.opaque-demo-context').write_text(CORE_REVISION + '\n')
    archive = subprocess.run(['git', '-C', work / 'core', 'archive', CORE_REVISION], capture_output=True, check=True, timeout=120).stdout
    subprocess.run(['tar', '-xf', '-', '-C', context / 'source'], input=archive, check=True, timeout=120)
    print('Building the pinned AX task runner for linux/' + arch + '.', flush=True)
    command(['go', 'build', '-trimpath', '-o', context / 'ax-task-runner', './cmd/ax-task-runner'], cwd=work / 'ax',
            env={**os.environ, 'GOOS': 'linux', 'GOARCH': arch, 'CGO_ENABLED': '0', 'GOTOOLCHAIN': 'auto'}, timeout=1800)
    shutil.copy(work / 'ax' / 'LICENSE', context / 'ax-LICENSE')
    shutil.copy(EXAMPLE / 'Dockerfile', context / 'Dockerfile')
    shutil.copytree(EXAMPLE, context / 'demo', ignore=shutil.ignore_patterns('__pycache__', 'scratch-repo'))
    print('Building the broker/AX image ' + image + '.', flush=True)
    subprocess.run(['docker', 'build', '--build-arg', 'OPAQUE_BUILD_REVISION=' + CORE_REVISION, '--build-arg', 'AX_REVISION=' + AX_REVISION,
                    '-t', image, context], check=True, timeout=3600)
    print(json.dumps({'image': image, 'native_bin': str(work / 'core' / 'target' / 'debug'), 'core_revision': CORE_REVISION,
                      'ax_revision': AX_REVISION, 'next': f'{sys.executable} -B {Path(__file__).resolve()} --state STATE deploy --image {image} '
                      f'--native-bin {work / "core" / "target" / "debug"} --github-token-file TOKEN_FILE'}, indent=2))


def deploy(directory, image, native_bin, token_file):
    if not directory.is_absolute() or any((p / '.git').exists() for p in (directory, *directory.parents)):
        raise ValueError('use a new absolute runtime directory outside Git')
    if not re.fullmatch(IMAGE_NAME + r':[a-zA-Z0-9_.-]+', image):
        raise ValueError('use a locally built ' + IMAGE_NAME + ' image')
    info = json.loads(command(['docker', 'image', 'inspect', image]))[0]
    labels = info['Config']['Labels']
    if (labels.get('org.opencontainers.image.revision') != CORE_REVISION or labels.get('io.opaque.ax.revision') != AX_REVISION
            or labels.get('io.opaque.demo.kind') != IMAGE_KIND):
        raise ValueError('image is not the reviewed staging-dispatch broker demo')
    runtime_hashes = {name: hashlib.sha256((EXAMPLE / name).read_bytes()).hexdigest() for name in RUNTIME_FILES}
    image_hashes = json.loads(command(['docker', 'run', '--rm', '--network=none', info['Id'], 'python3', '-c',
        'import hashlib,json,sys;from pathlib import Path;print(json.dumps({n:hashlib.sha256((Path("/opt/demo")/n).read_bytes()).hexdigest() for n in sys.argv[1:]}))',
        *RUNTIME_FILES]))
    if image_hashes != runtime_hashes:
        raise ValueError('image runtime differs from this demo checkout; rebuild before deployment')
    native_bin = native_bin.resolve()
    if not command([native_bin / 'opaque', '--version']).strip().endswith('+' + CORE_REVISION[:7]):
        raise ValueError('native opaque CLI is not built from the pinned core revision')
    check = json.loads(command([native_bin / 'opaque-approver', 'check-native']))
    if not check.get('ready') or not check.get('authentication_available'):
        raise ValueError('native reviewer unavailable')
    source = (EXAMPLE / 'policies' / POLICY_FILE).read_text()
    # Compile with the actual binary shipped in the broker image and with the host CLI.
    compiled = json.loads(command(['docker', 'run', '--rm', '--network=none', '-i', info['Id'], 'sh', '-c',
        'cat > /tmp/policy.yaml; /opt/opaque/opaque authority-policy compile /tmp/policy.yaml --json'], data=source))
    host_compiled = json.loads(command([native_bin / 'opaque', 'authority-policy', 'compile', EXAMPLE / 'policies' / POLICY_FILE, '--json']))
    if host_compiled['digest'] != compiled['digest']:
        raise ValueError('host and image compilers disagree on the policy digest')
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    with socket.socket() as endpoint:
        endpoint.bind(('127.0.0.1', 0))
        port = endpoint.getsockname()[1]
    run_id = uuid.uuid4().hex
    state = {'kind': IMAGE_KIND, 'profile': PROFILE, 'namespace': 'opaque-dispatch-' + run_id[:10], 'run_id': run_id,
        'logical_run_id': str(uuid.uuid4()), 'image': image, 'image_id': info['Id'], 'compiled': compiled, 'runtime_hashes': runtime_hashes,
        'policy_source': source, 'scope_request': json.loads((EXAMPLE / 'workload/scope.json').read_text()),
        'native_bin': str(native_bin), 'native_hashes': {n: hashlib.sha256((native_bin / n).read_bytes()).hexdigest()
            for n in ('opaque-approver', 'opaque-approve-helper', 'opaque-evidence', 'opaque')}, 'approval_port': port, 'phase': 'preparing'}
    save(directory / 'cluster.json', state)
    command(['minikube', '-p', PROFILE, 'image', 'load', image], timeout=300)
    created = json.loads(kubectl('create', '-f', '-', '-o', 'json', data=json.dumps({'apiVersion': 'v1', 'kind': 'Namespace',
                'metadata': {'name': state['namespace'], 'labels': {OWNER: run_id}}})))
    state['namespace_uid'] = created['metadata']['uid']
    save(directory / 'cluster.json', state)
    demo = Demo(directory)
    demo.resource('storage.yaml')
    demo.configmap('authority-policy', {POLICY_FILE: source})
    demo.configmap('workload', {n: (EXAMPLE / 'workload' / n).read_text() for n in ('scope.json', 'actions.json', 'task.yaml')})
    demo.token_secret(token_file.resolve())
    demo.new_key('bootstrap-workstation')
    demo.start_broker('bootstrap')
    demo.enroll()
    demo.login('reviewer')
    demo.new_key('native-reviewer')
    demo.start_broker('mapped')
    demo.enroll()
    demo.start_broker('policy')
    demo.resource('runner.yaml')
    demo.k('rollout', 'status', 'deployment/runner', '--timeout=120s', timeout=150)
    demo.custody()
    demo.cluster['phase'] = 'awaiting_native_review'
    demo.persist()
    print(json.dumps({'namespace': state['namespace'], 'phase': 'awaiting_native_review', 'policy_digest': compiled['digest'],
          'broker_pinned_digest': 'sealed in broker-config-policy; verified by scope snapshot after step 1',
          'token_custody': demo.cluster['token_custody_observed'], 'ax_pod_pre_delegation_request': 'refused',
          'human_approval': False, 'github_dispatches': 0}, indent=2))
    print_stop(directory)


class Handler(http.server.BaseHTTPRequestHandler):
    def __init__(self, *args, demo, **kwargs):
        self.demo = demo
        super().__init__(*args, **kwargs)

    def log_message(self, *_):
        pass

    def send(self, code, data, content_type="application/json"):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def host(self):
        return f"127.0.0.1:{self.server.server_port}"

    def do_GET(self):
        if self.headers.get("Host") != self.host():
            return self.send(403, {"error": "invalid host"})
        if self.path == "/api/state":
            try:
                state = self.demo.cache if self.demo.busy and self.demo.cache else self.demo.read()
                return self.send(200, {**state, "busy": self.demo.busy, "action_error": self.demo.error})
            except Exception:
                return self.send(503, {"error": "Cluster state unavailable. No action was retried."})
        files = {"/": ("examples/ax-staging-dispatch/index.html", "text/html; charset=utf-8"),
                 "/demo.js": ("examples/ax-staging-dispatch/demo.js", "text/javascript; charset=utf-8"),
                 "/demo.css": ("examples/ax-staging-dispatch/demo.css", "text/css; charset=utf-8"),
                 "/archivo.ttf": ("assets/brand/fonts/archivo-variable.ttf", "font/ttf"),
                 "/mono.ttf": ("assets/brand/fonts/ibm-plex-mono-regular.ttf", "font/ttf"),
                 "/archivo-license.txt": ("assets/brand/licenses/archivo-OFL.txt", "text/plain"),
                 "/mono-license.txt": ("assets/brand/licenses/ibm-plex-mono-OFL.txt", "text/plain")}
        item = files.get(self.path)
        if not item:
            return self.send(404, {"error": "not found"})
        self.send(200, (ROOT / item[0]).read_bytes(), item[1])

    def do_POST(self):
        if self.headers.get("Host") != self.host() or self.headers.get("Origin") != "http://" + self.host():
            return self.send(403, {"error": "same-origin local request required"})
        if self.headers.get("Content-Length", "0") != "0" or self.headers.get("Transfer-Encoding"):
            return self.send(400, {"error": "request body not accepted"})
        if self.path not in ("/api/run",):
            return self.send(404, {"error": "not found"})
        try:
            self.demo.action(self.path.rsplit("/", 1)[1])
            self.send(202, {"accepted": True})
        except ValueError as error:
            self.send(409, {"error": str(error)})
        except Exception:
            self.send(503, {"error": "Cluster action unavailable; inspect current state."})


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--state', type=Path, help='runtime directory outside Git (required except for build)')
    sub = parser.add_subparsers(dest='command', required=True)
    make = sub.add_parser('build', help='Clone pinned sources, build native reviewer binaries and the broker/AX image')
    make.add_argument('--work', required=True, type=Path)
    make.add_argument('--image', default=IMAGE_NAME + ':' + CORE_REVISION[:7])
    make.add_argument('--arch', default='arm64', choices=['arm64', 'amd64'])
    create = sub.add_parser('deploy', help='All machine steps up to the first native ceremony; then STOP')
    create.add_argument('--image', required=True)
    create.add_argument('--native-bin', required=True, type=Path)
    create.add_argument('--github-token-file', required=True, type=Path)
    sub.add_parser('delegate-reviewer', help='Human step 1: one native window, then scope snapshot digest check')
    sub.add_parser('delegate-requester', help='Human step 2: one native window, then the AX child plans the scope')
    sub.add_parser('scope-review', help='Human step 3: one native scope review, then the AX child activates')
    resume = sub.add_parser('run', help='Resume: the AX workload submits its three proposals')
    resume.add_argument('--variant', choices=[KILL_VARIANT])
    resume.add_argument('--sigkill-after-ms', type=int, default=900)
    sub.add_parser('evidence', help='Stop the broker, export/sign the ledger, verify, reject a flipped byte')
    serve = sub.add_parser('serve')
    serve.add_argument('--port', type=int, default=19741)
    sub.add_parser('inspect')
    sub.add_parser('human-steps', help='Print the numbered human steps again')
    sub.add_parser('shot-list', help='Print the 90-second shot list')
    args = parser.parse_args()
    if args.command == 'build':
        return build(args.work, args.image, args.arch)
    if args.state is None:
        parser.error('--state is required')
    if args.command == 'deploy':
        deploy(args.state, args.image, args.native_bin, args.github_token_file)
    elif args.command == 'inspect':
        print(json.dumps(Demo(args.state).read(), indent=2))
    elif args.command == 'delegate-reviewer':
        Demo(args.state).delegate_reviewer()
    elif args.command == 'delegate-requester':
        Demo(args.state).delegate_requester()
    elif args.command == 'scope-review':
        Demo(args.state).scope_review()
    elif args.command == 'run':
        Demo(args.state).run(args.variant, args.sigkill_after_ms)
    elif args.command == 'evidence':
        Demo(args.state).evidence()
    elif args.command in ('human-steps', 'shot-list'):
        print_stop(args.state)
    else:
        server = http.server.ThreadingHTTPServer(('127.0.0.1', args.port), partial(Handler, demo=Demo(args.state)))
        print(f'AX + Opaque staging dispatch demo: http://127.0.0.1:{server.server_port}/', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
