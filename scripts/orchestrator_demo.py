#!/usr/bin/env python3
"""Run Airflow or Temporal with the same repository policy and native Opaque broker."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import uuid

from ax_minikube_demo import CORE_REVISION, ROOT, PROFILE, OWNER, Demo, command, kubectl, save

EXAMPLE = ROOT / 'examples/orchestrators'
TEMPORAL_IMAGE = 'temporalio/temporal@sha256:ad4c82c97bd12b417d1ea942610dbcd511afb250c4d5ed26c694009533df447e'


def verify_worker(image_id, platform):
    paths = [EXAMPLE / 'shared/opaque_bridge.py', *sorted((EXAMPLE / platform).rglob('*.py'))]
    sources = {str(p.relative_to(EXAMPLE)).removeprefix('airflow/'): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    output = command(['docker', 'run', '--rm', '--network=none', '--entrypoint', 'python3', image_id,
        '-c', 'import hashlib,json,sys;from pathlib import Path;print(json.dumps({p:hashlib.sha256((Path("/opt/opaque-demo")/p).read_bytes()).hexdigest() for p in sys.argv[1:]}))', *sources])
    if json.loads(output) != sources:
        raise ValueError('worker image source differs from repository; rebuild before deployment')
    return sources


class Orchestrator(Demo):
    example = EXAMPLE

    def __init__(self, directory):
        state = json.loads((directory / 'cluster.json').read_text())
        if state.get('orchestrator') not in ('airflow', 'temporal'):
            raise ValueError('expected an Airflow or Temporal deployment')
        self.platform = state['orchestrator']
        self.namespace_prefix = 'opaque-' + self.platform
        super().__init__(directory)

    def retained(self, name):
        if name not in ('plan.json', 'grant.json', 'inspection.json', 'reviewed.json', 'policy-observation.json'):
            raise ValueError('unknown observation file')
        raw = self.exec('python3', '-c', 'from pathlib import Path;import sys;p=Path("/workspace/demo")/sys.argv[1];print(p.read_text() if p.exists() else "null")', name, peer='runner')
        return json.loads(raw)

    def record(self, name, value):
        if name not in ('reviewed.json', 'policy-observation.json'):
            raise ValueError('unknown operator record')
        self.exec('python3', '-c', 'import json,sys;import opaque_bridge as b;b.save(b.ROOT/sys.argv[1],json.load(sys.stdin))',
                  name, data=json.dumps(value), peer='runner')

    def temporal(self, *args):
        return self.exec('python3', '-B', '/opt/opaque-demo/temporal/client.py', *args, peer='runner')

    def start_workflow(self):
        marker = self.directory / 'workflow-start-intent.json'
        if marker.exists():
            raise ValueError('workflow start already attempted; inspect its actual history')
        if self.platform == 'airflow':
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                listing = self.exec('airflow', 'dags', 'list', '-o', 'json', peer='runner')
                if 'opaque_support' in listing:
                    break
                time.sleep(2)
            else:
                raise RuntimeError('Airflow has not parsed the checked-in DAG')
            self.exec('airflow', 'dags', 'unpause', 'opaque_support', peer='runner')
        save(marker, {'id': self.cluster['logical_run_id'], 'authority_granted': False})
        if self.platform == 'temporal':
            result = self.temporal('start')
        else:
            result = self.exec('airflow', 'dags', 'trigger', 'opaque_support', '--run-id', self.cluster['logical_run_id'], peer='runner')
        (self.directory / 'workflow-start-result.txt').write_text(result)
        self.cluster['phase'] = 'awaiting_native_review'
        self.persist()

    def read(self):
        self.cluster = json.loads((self.directory / 'cluster.json').read_text())
        if self.platform == 'temporal':
            workflow = json.loads(self.temporal('inspect'))
        else:
            workflow = json.loads(self.exec('airflow', 'dags', 'list-runs', 'opaque_support', '-o', 'json', peer='runner'))
        return {'platform': self.platform, 'namespace': self.cluster['namespace'],
            'phase': self.cluster['phase'], 'expected_policy_digest': self.cluster['compiled']['digest'],
            'policy_observation': self.cluster.get('policy_observation'), 'workflow': workflow,
            'plan': self.retained('plan.json'), 'grant': self.retained('grant.json'),
            'inspection': self.retained('inspection.json')}

    def review(self):
        if self.cluster['phase'] != 'awaiting_native_review':
            raise ValueError('workflow is not awaiting native review')
        with self.forward():
            self.delegate('reviewer', 'identity-client')
            snapshot = self.rpc('scope_snapshot', session=True)['result']
            expected = {k: self.cluster['compiled'][k] for k in ('digest', 'identity')}
            if snapshot.get('authority_policy') != expected:
                raise ValueError('running broker policy differs from the repository manifest')
            save(self.directory / 'broker-policy-snapshot.json', snapshot)
            self.cluster['policy_observation'] = snapshot['authority_policy']
            self.persist()
            self.record('policy-observation.json', snapshot['authority_policy'])
            self.delegate('requester', 'runner')
            if self.platform == 'temporal':
                self.temporal('delegated')
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                plan = self.retained('plan.json')
                if plan:
                    if plan.get('error'):
                        raise RuntimeError('broker refused the proposed scope')
                    break
                time.sleep(1)
            else:
                raise RuntimeError('workflow did not request scope review')
            round_id = plan['result']['document']['round_id']
            print('Opening native scope review. Approval allows this workflow to execute its repository proposals.', flush=True)
            self.native('scope-review', '--round-id', round_id, timeout=310)
            receipt = json.loads(self.native('scope-receipt', '--round-id', round_id))
            if receipt['response']['decision'] != 'approve' or receipt['review'] != plan['result']:
                raise ValueError('native receipt does not approve the exact requested scope')
            save(self.directory / 'scope-receipt.json', receipt)
            self.record('reviewed.json', {'round_id': round_id})
            if self.platform == 'temporal':
                self.temporal('reviewed', round_id)
            self.cluster['phase'] = 'native_scope_reviewed'
            self.persist()

    def restart_waiting_workflow(self):
        if self.cluster['phase'] != 'awaiting_native_review' or (self.directory / 'reviewer-delegation-attempt.json').exists():
            raise ValueError('this smoke check is only for an unreviewed workflow')
        before = self.read()
        self.k('rollout', 'restart', 'deployment/runner')
        self.k('rollout', 'status', 'deployment/runner', '--timeout=180s', timeout=210)
        after = self.read()
        if self.platform == 'temporal' and before['workflow'] != after['workflow']:
            raise ValueError('workflow state changed while waiting for review')
        if self.platform == 'airflow':
            expected = [self.cluster['logical_run_id']]
            if any([r['run_id'] for r in report['workflow']] != expected for report in (before, after)):
                raise ValueError('Airflow run identity changed while waiting for review')
        if after['plan'] is not None or after['grant'] is not None:
            raise ValueError('restart unexpectedly produced scope authority')
        save(self.directory / 'restart-check.json', {'before': before, 'after': after, 'workflow_start_not_repeated': True})
        print(json.dumps({'restarted': True, 'authority_granted': False, 'platform': self.platform}))

    def serve(self, port):
        self.owned()
        remote_port = 8080 if self.platform == 'airflow' else 8233
        print(f'{self.platform.title()} UI: http://127.0.0.1:{port}/', flush=True)
        # Foreground command; Ctrl-C stops the tunnel, retaining the deployment.
        subprocess.run(['minikube', '-p', PROFILE, 'kubectl', '--', '-n', self.cluster['namespace'],
                        'port-forward', '--address=127.0.0.1', 'deployment/runner', f'{port}:{remote_port}'], check=True)


def deploy(args):
    directory = args.state
    if not directory.is_absolute() or any((p / '.git').exists() for p in (directory, *directory.parents)):
        raise ValueError('use a new absolute runtime directory outside Git')
    images = {'image': args.broker_image, 'worker_image': args.worker_image}
    inspected = {key: json.loads(command(['docker', 'image', 'inspect', image]))[0] for key, image in images.items()}
    worker_sources = verify_worker(inspected['worker_image']['Id'], args.platform)
    labels = inspected['image']['Config']['Labels']
    if labels.get('org.opencontainers.image.revision') != CORE_REVISION or labels.get('io.opaque.demo.kind') != 'policy-broker':
        raise ValueError('broker image is not the pinned Opaque implementation')
    native_bin = args.native_bin.resolve()
    ready = json.loads(command([native_bin / 'opaque-approver', 'check-native']))
    if not ready.get('ready') or not ready.get('authentication_available'):
        raise ValueError('native reviewer unavailable')
    policy = (EXAMPLE / 'policies/support-status.yaml').read_text()
    compiled = json.loads(command(['docker', 'run', '--rm', '--network=none', '-i', inspected['image']['Id'], 'sh', '-c',
        'cat > /tmp/policy.yaml; /opt/opaque/opaque authority-policy compile /tmp/policy.yaml --json'], data=policy))
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        approval_port = sock.getsockname()[1]
    run_id = uuid.uuid4().hex
    state = {'kind': 'policy-broker', 'orchestrator': args.platform, 'profile': PROFILE, **images,
        'image_id': inspected['image']['Id'], 'worker_image_id': inspected['worker_image']['Id'],
        'worker_sources': worker_sources,
        'namespace': 'opaque-' + args.platform + '-' + run_id[:10], 'run_id': run_id,
        'logical_run_id': 'opaque-' + args.platform + '-' + str(uuid.uuid4()), 'approval_port': approval_port,
        'compiled': compiled, 'policy_source': policy, 'native_bin': str(native_bin),
        'native_hashes': {n: hashlib.sha256((native_bin / n).read_bytes()).hexdigest() for n in ('opaque-approver', 'opaque-approve-helper')},
        'phase': 'preparing'}
    save(directory / 'cluster.json', state)
    for image in images.values():
        command(['minikube', '-p', PROFILE, 'image', 'load', image], timeout=240)
    created = json.loads(kubectl('create', '-f', '-', '-o', 'json', data=json.dumps({'apiVersion': 'v1', 'kind': 'Namespace',
        'metadata': {'name': state['namespace'], 'labels': {OWNER: run_id}}})))
    state['namespace_uid'] = created['metadata']['uid']
    save(directory / 'cluster.json', state)
    demo = Orchestrator(directory)
    demo.resource('storage.yaml')
    demo.configmap('authority-policy', {'support-status.yaml': policy})
    workload = {name: (EXAMPLE / 'workload' / name).read_text() for name in ('scope.json', 'actions.json')}
    workload.update({'compiled-policy.json': json.dumps(compiled), 'run.json': json.dumps({'logical_run_id': state['logical_run_id']})})
    demo.configmap('workload', workload)
    demo.new_key('bootstrap-workstation')
    demo.start_broker('bootstrap')
    demo.enroll()
    demo.login('reviewer')
    demo.new_key('native-reviewer')
    demo.start_broker('mapped')
    demo.enroll()
    demo.start_broker('policy')
    demo.resource(args.platform + '.yaml', WORKER_IMAGE=args.worker_image)
    demo.k('rollout', 'status', 'deployment/runner', '--timeout=240s', timeout=270)
    demo.custody()
    demo.start_workflow()
    print(json.dumps({'platform': args.platform, 'namespace': state['namespace'], 'phase': demo.cluster['phase'],
                      'policy_digest': compiled['digest'], 'native_review_opened': False}, indent=2))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    prepare = sub.add_parser('deploy')
    prepare.add_argument('platform', choices=('airflow', 'temporal'))
    prepare.add_argument('--broker-image', required=True)
    prepare.add_argument('--worker-image', required=True)
    prepare.add_argument('--native-bin', required=True, type=Path)
    view = sub.add_parser('serve')
    view.add_argument('--port', type=int)
    sub.add_parser('review', help='Open three native reviews; approved scope permits workflow dispatch')
    sub.add_parser('inspect')
    sub.add_parser('restart-waiting', help='Replace the pod before delegation; retain workflow history')
    args = parser.parse_args()
    if args.command == 'deploy':
        deploy(args)
    else:
        demo = Orchestrator(args.state)
        if args.command == 'review':
            demo.review()
        elif args.command == 'inspect':
            print(json.dumps(demo.read(), indent=2))
        elif args.command == 'restart-waiting':
            demo.restart_waiting_workflow()
        else:
            demo.serve(args.port or (19750 if demo.platform == 'airflow' else 19751))


if __name__ == '__main__':
    main()
