#!/usr/bin/env python3
"""Deploy repository policy + real Opaque broker + AX runner to owned minikube."""
import argparse
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
import signal
import socket
import ssl
from string import Template
import subprocess
import threading
import time
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'examples/ax-minikube'
PROFILE = 'opaque-ax-demo'
CORE_REVISION = '180e66fe6c854d962074e8ff4e694a29689af623'
AX_REVISION = 'f009cc81c9a571073bc1dd58cd2ed934bf2d5b1c'
OWNER = 'opaque.info/demo-run'


def command(args, *, data=None, timeout=60):
    return subprocess.run([str(a) for a in args], input=data, capture_output=True,
                          text=True, check=True, timeout=timeout).stdout


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


class Demo:
    def __init__(self, directory):
        self.directory = directory
        self.cluster = json.loads((directory / 'cluster.json').read_text())
        if self.cluster.get('kind') != 'policy-broker' or self.cluster.get('profile') != PROFILE:
            raise ValueError('this command requires a policy-broker deployment, not the recovery fixture')
        if not re.fullmatch(r'opaque-ax-[0-9a-f]{10}', self.cluster['namespace']):
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

    def start_broker(self, stage):
        mapped = stage != 'bootstrap'
        device = {'name': 'Native AX demo reviewer', 'public_key_hex': self.cluster['workstation_public_key']}
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
        self.k('rollout', 'status', 'deployment/broker', '--timeout=120s', timeout=150)
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
        raise RuntimeError('broker startup did not confirm pinned native custody settings')

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
                # minikube spawns kubectl; stop this owned process group so a
                # stale child cannot retain the previous pod's review endpoint.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                process.wait(timeout=10)

    def native(self, *args, timeout=30):
        for name, expected in self.cluster['native_hashes'].items():
            if hashlib.sha256((Path(self.cluster['native_bin']) / name).read_bytes()).hexdigest() != expected:
                raise ValueError('native reviewer executable changed')
        return command([Path(self.cluster['native_bin']) / 'opaque-approver', *args,
                        '--state-dir', self.cluster['workstation']], timeout=timeout)

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
                              '--state-dir', path, '--name', 'AX demo ' + name]))
        self.cluster.update(workstation=str(path), workstation_public_key=result['public_key_hex'])
        self.persist()

    def custody(self):
        self.exec('sh', '-c', 'set -eu; [ "$(id -u)" = 7582 ]; [ ! -e /var/lib/opaque ]; [ ! -e /fixture ]; '
                  '[ "$(stat -c "%a %u %g" /run/opaque/daemon.token)" = "640 7581 7987" ]', peer='runner')
        denied = self.rpc('scope_plan', json.loads((EXAMPLE / 'workload/scope.json').read_text()), peer='runner', allow_error=True)
        if not (denied.get('error') or denied.get('transport_error')):
            raise ValueError('AX runner obtained authority before native delegation')
        save(self.directory / 'pre-delegation-denial.json', denied)

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
                'label': 'ax-demo-' + subject, 'reason': 'Inspect repository policy' if subject == 'reviewer' else 'Run repository support scope'}, allow_error=True)
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
        receipt = json.loads(self.native('receipt', '--approval-id', pending[0]['approval_id']))
        save(self.directory / (subject + '-delegation-receipt.json'), receipt)

    def review(self):
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
            self.cluster['policy_observation'] = observed
            self.persist()
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
            print('Opening native review for workload/scope.json.', flush=True)
            self.native('scope-review', '--round-id', view['issuance_round_id'], timeout=310)
            receipt = json.loads(self.native('scope-receipt', '--round-id', view['issuance_round_id']))
            if receipt['response']['decision'] != 'approve':
                raise ValueError('scope was not approved')
            save(self.directory / 'scope-receipt.json', receipt)
            self.signal('activate.json', {'deployment_id': self.cluster['namespace'], 'run_id': self.cluster['logical_run_id']})
            self.cluster['phase'] = 'scope_reviewed'
            self.persist()

    def signal(self, name, value):
        if name not in ('activate.json', 'start.json'):
            raise ValueError('unknown control file')
        self.exec('python3', '-B', '-c', 'import json,sys;sys.path.insert(0,"/opt/demo");import worker;worker.once(worker.ROOT/sys.argv[1],json.load(sys.stdin))',
                  name, peer='runner', data=json.dumps(value))

    def read(self):
        # Refresh after a separate operator review command updates state.
        self.cluster = json.loads((self.directory / 'cluster.json').read_text())
        raw = self.exec('python3', '-c', 'from pathlib import Path; p=Path("/workspace/demo/view.json"); print(p.read_text() if p.exists() else "{}")', peer='runner')
        if len(raw) > 200000:
            raise ValueError('oversized view')
        view = json.loads(raw)
        observed = self.cluster.get('policy_observation')
        view.update(namespace=self.cluster['namespace'], policy_source=self.cluster['policy_source'],
            compiled_policy=self.cluster['compiled'], policy_observation=observed,
            policy_verified=observed == {k: self.cluster['compiled'][k] for k in ('digest', 'identity')},
            broker_stage=self.cluster.get('broker_stage'), scope_request=self.cluster['scope_request'])
        self.cache = view
        save(self.directory / 'last-view.json', view)
        return view

    def action(self, kind):
        with self.lock:
            view = self.read()
            if kind != 'run' or view.get('phase') != 'ready' or not view.get('policy_verified'):
                raise ValueError('run requires a broker-verified policy and native-approved scope; no replay')
            self.signal('start.json', {'requested': True})


def deploy(directory, image, native_bin):
    if not directory.is_absolute() or any((p / '.git').exists() for p in (directory, *directory.parents)):
        raise ValueError('use a new absolute runtime directory outside Git')
    if not re.fullmatch(r'opaque-ax-demo:[a-zA-Z0-9_.-]+', image):
        raise ValueError('use a locally built opaque-ax-demo image')
    info = json.loads(command(['docker', 'image', 'inspect', image]))[0]
    labels = info['Config']['Labels']
    if (labels.get('org.opencontainers.image.revision') != CORE_REVISION or labels.get('io.opaque.ax.revision') != AX_REVISION
            or labels.get('io.opaque.demo.kind') != 'policy-broker'):
        raise ValueError('image is not the reviewed real-broker demo')
    runtime_files = ('worker.py', 'broker/install.sh', 'fixtures/client.py', 'fixtures/services.py')
    runtime_hashes = {name: hashlib.sha256((EXAMPLE / name).read_bytes()).hexdigest() for name in runtime_files}
    image_hashes = json.loads(command(['docker', 'run', '--rm', '--network=none', info['Id'], 'python3', '-c',
        'import hashlib,json,sys;from pathlib import Path;print(json.dumps({n:hashlib.sha256((Path("/opt/demo")/n).read_bytes()).hexdigest() for n in sys.argv[1:]}))',
        *runtime_files]))
    if image_hashes != runtime_hashes:
        raise ValueError('image runtime differs from this demo checkout; rebuild before deployment')
    native_bin = native_bin.resolve()
    check = json.loads(command([native_bin / 'opaque-approver', 'check-native']))
    if not check.get('ready') or not check.get('authentication_available'):
        raise ValueError('native reviewer unavailable')
    # Compile with the actual binary shipped in the broker image.
    source = (EXAMPLE / 'policies/support-status.yaml').read_text()
    compiled = json.loads(command(['docker', 'run', '--rm', '--network=none', '-i', info['Id'], 'sh', '-c',
        'cat > /tmp/policy.yaml; /opt/opaque/opaque authority-policy compile /tmp/policy.yaml --json'], data=source))
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    with socket.socket() as endpoint:
        endpoint.bind(('127.0.0.1', 0))
        port = endpoint.getsockname()[1]
    run_id = uuid.uuid4().hex
    state = {'kind': 'policy-broker', 'profile': PROFILE, 'namespace': 'opaque-ax-' + run_id[:10], 'run_id': run_id,
        'logical_run_id': str(uuid.uuid4()), 'image': image, 'image_id': info['Id'], 'compiled': compiled, 'runtime_hashes': runtime_hashes,
        'policy_source': source, 'scope_request': json.loads((EXAMPLE / 'workload/scope.json').read_text()),
        'native_bin': str(native_bin), 'native_hashes': {n: hashlib.sha256((native_bin / n).read_bytes()).hexdigest()
            for n in ('opaque-approver', 'opaque-approve-helper')}, 'approval_port': port, 'phase': 'preparing'}
    save(directory / 'cluster.json', state)
    command(['minikube', '-p', PROFILE, 'image', 'load', image], timeout=180)
    created = json.loads(kubectl('create', '-f', '-', '-o', 'json', data=json.dumps({'apiVersion': 'v1', 'kind': 'Namespace',
                'metadata': {'name': state['namespace'], 'labels': {OWNER: run_id}}})))
    state['namespace_uid'] = created['metadata']['uid']
    save(directory / 'cluster.json', state)
    demo = Demo(directory)
    demo.resource('storage.yaml')
    demo.configmap('authority-policy', {'support-status.yaml': source})
    demo.configmap('workload', {n: (EXAMPLE / 'workload' / n).read_text() for n in ('scope.json', 'actions.json', 'task.yaml')})
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
    print(json.dumps({'namespace': state['namespace'], 'phase': 'awaiting_native_review',
          'policy_digest': compiled['digest'], 'human_approval': False, 'provider_writes': 0}, indent=2))


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
        files = {"/": ("examples/ax-minikube/index.html", "text/html; charset=utf-8"),
                 "/demo.js": ("examples/ax-minikube/demo.js", "text/javascript; charset=utf-8"),
                 "/demo.css": ("examples/ax-minikube/demo.css", "text/css; charset=utf-8"),
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('deploy')
    create.add_argument('--image', required=True)
    create.add_argument('--native-bin', required=True, type=Path)
    serve = sub.add_parser('serve')
    serve.add_argument('--port', type=int, default=19740)
    sub.add_parser('inspect')
    sub.add_parser('review', help='Operator command: opens native authentication windows')
    args = parser.parse_args()
    if args.command == 'deploy':
        deploy(args.state, args.image, args.native_bin)
    elif args.command == 'inspect':
        print(json.dumps(Demo(args.state).read(), indent=2))
    elif args.command == 'review':
        Demo(args.state).review()
    else:
        server = http.server.ThreadingHTTPServer(('127.0.0.1', args.port), partial(Handler, demo=Demo(args.state)))
        print(f'AX + Opaque policy demo: http://127.0.0.1:{server.server_port}/', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
