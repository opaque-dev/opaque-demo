"""Failure-boundary tests for scheduler redelivery, using a simulated broker."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from string import Template
import yaml

EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/orchestrators'
spec = importlib.util.spec_from_file_location('opaque_bridge_test', EXAMPLE / 'shared/opaque_bridge.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class RedeliveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, value in [('ROOT', root / 'state'), ('CONFIG', root / 'config'), ('SESSION', root / 'session.json')]:
            p = patch.object(b, name, value)
            p.start()
            self.addCleanup(p.stop)
        b.save(b.CONFIG / 'run.json', {'logical_run_id': 'run-1'})
        b.save(b.SESSION, {'mode': 'delegated', 'session_token': 'opqd1.fake-test-token'})
        b.save(b.ROOT / 'grant.json', {'result': {'grant': {'scope_id': 'scope-1', 'subject': 'agent-1', 'owner': 'tenant-1'}}})
        b.save(b.ROOT / 'plan.json', {'result': {'document': {'round_id': 'review-1'}}})
        b.save(b.ROOT / 'policy-observation.json', {'digest': 'simulated'})
        self.action = {'action_key': 'resolve-accepted', 'resource': 'accepted', 'status': 'resolved'}
        self.calls = []

    def broker(self, *args):
        self.calls.append(args)
        if args[0] == 'run':
            return {'result': {}}
        if args[0] == 'show':
            return {'result': {'revoked_at': 123}}
        return {'result': {'state': 'api_accepted'}}

    def test_scheduler_redelivery_never_repeats_write(self):
        with patch.object(b, 'cli', self.broker):
            first = b.execute_action('run-1', self.action)
            again = b.execute_action('run-1', self.action)
        self.assertEqual(first['request_id'], again['request_id'])
        self.assertTrue(again['redelivery'])
        self.assertEqual(sum(c[0] == 'run' for c in self.calls), 1)
        self.assertNotIn('opqd1.', str(again))

    def test_lost_response_and_worker_crash_query_before_any_redelivery(self):
        for failure in (subprocess.TimeoutExpired('opaque', 30), SystemExit('worker killed')):
            with self.subTest(failure=type(failure).__name__):
                action = {**self.action, 'action_key': type(failure).__name__.lower()}
                def fail(*args):
                    if args[0] == 'run':
                        raise failure
                    return {'result': None}
                with patch.object(b, 'cli', fail):
                    try:
                        first = b.execute_action('run-1', action)
                        self.assertTrue(first['hold'])
                    except SystemExit:
                        pass
                with patch.object(b, 'cli', return_value={'result': None}) as cli:
                    again = b.execute_action('run-1', action)
                    self.assertEqual(cli.call_args.args[0], 'outcome')
                    cli.assert_called_once()
                    self.assertTrue(again['hold'])
                    self.assertFalse(again['retry_authorized'])

    def test_generic_broker_error_is_not_reported_as_certain_denial(self):
        with patch.object(b, 'cli', return_value={'error': {'code': 'scope_unavailable'}}):
            observed = b.execute_action('run-1', self.action)
        self.assertEqual(observed['state'], 'unavailable')
        with patch.object(b, 'cli', return_value={'result': None}):
            report = b.inspect_all()
        self.assertTrue(report['observations'][0]['hold'])
        self.assertEqual(report['observations'][0]['state'], 'not_observed')

    def test_failed_outcome_lookup_remains_a_business_hold(self):
        def broker(*args):
            self.calls.append(args)
            if args[0] == 'run':
                return {'result': {}}
            raise subprocess.TimeoutExpired('opaque outcome', 30)
        with patch.object(b, 'cli', broker):
            first = b.execute_action('run-1', self.action)
            again = b.execute_action('run-1', self.action)
        self.assertEqual(sum(c[0] == 'run' for c in self.calls), 1)
        self.assertTrue(first['hold'] and again['hold'])
        self.assertEqual(again['state'], 'unavailable')

    def test_new_run_and_changed_proposal_cannot_reuse_scope(self):
        with patch.object(b, 'cli', self.broker):
            b.execute_action('run-1', self.action)
        with patch.object(b, 'cli') as cli:
            with self.assertRaises(ValueError):
                b.execute_action('run-2', self.action)
            with self.assertRaises(ValueError):
                b.execute_action('run-1', {**self.action, 'status': 'closed'})
            cli.assert_not_called()

    def test_scope_must_have_confirmed_revocation(self):
        with patch.object(b, 'cli', return_value={'error': {'code': 'scope_unavailable'}}):
            with self.assertRaises(RuntimeError):
                b.revoke_scope()
        with patch.object(b, 'cli', return_value={'result': {'revoked_at': None}}) as cli:
            with self.assertRaises(RuntimeError):
                b.revoke_scope()
            self.assertEqual(cli.call_args.args[0], 'show')

    def test_local_review_marker_cannot_grant_broker_authority(self):
        b.save(b.ROOT / 'reviewed.json', {'round_id': 'review-1'})
        (b.ROOT / 'grant.json').unlink()
        with patch.object(b, 'cli', return_value={'error': {'code': 'scope_unavailable'}}):
            with self.assertRaises(RuntimeError):
                b.activate_scope('review-1')


class DeploymentBoundaryTests(unittest.TestCase):
    def test_orchestrators_have_no_provider_or_approver_custody(self):
        for platform in ('airflow', 'temporal'):
            with self.subTest(platform=platform):
                pod = yaml.safe_load(Template((EXAMPLE / 'k8s' / (platform + '.yaml')).read_text()).substitute(
                    IMAGE='broker:test', WORKER_IMAGE='worker:test'))['spec']['template']['spec']
                self.assertFalse(pod['automountServiceAccountToken'])
                self.assertNotIn('fsGroup', pod['securityContext'])
                self.assertFalse(pod.get('shareProcessNamespace', False))
                self.assertTrue(all('hostPath' not in v and 'secret' not in v for v in pod['volumes']))
                claims = {v['persistentVolumeClaim']['claimName'] for v in pod['volumes'] if 'persistentVolumeClaim' in v}
                self.assertEqual(claims, {'broker-socket', 'evidence', 'platform'})
                runner = next(c for c in pod['containers'] if c['name'] == 'runner')
                mounts = {m['mountPath']: m for m in runner['volumeMounts']}
                self.assertTrue(mounts['/run/opaque']['readOnly'])
                self.assertNotIn('/var/lib/opaque', mounts)
                if platform == 'temporal':
                    server = next(c for c in pod['containers'] if c['name'] == 'server')
                    self.assertEqual([m['name'] for m in server['volumeMounts']], ['platform'])


if __name__ == '__main__':
    unittest.main()
