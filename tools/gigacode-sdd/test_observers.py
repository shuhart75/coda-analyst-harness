import copy
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import observers


def event(kind, content):
    return {'type': kind, 'message': {'content': content}}


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'run'
        self.root.mkdir()
        self.logs = self.root / 'logs'
        self.logs.mkdir()
        self.activities = []
        self.runner = SimpleNamespace(
            root=self.root, cfg={'cli': ['gigacode'], 'execution_models': ['model-a', 'model-b']},
            stop=threading.Event(), env={}, progress=Mock(),
            state={'status': 'running', 'tasks': {'task': {'status': 'running'}}},
            _display=SimpleNamespace(telemetry=Mock()))

        @contextmanager
        def activity(label, **kwargs):
            self.activities.append((label, kwargs))
            yield 'activity-token'
        self.runner.activity = activity
        self.manager = observers.Manager(self.runner)

    def write_events(self, name, events):
        path = self.logs / name / 'stdout.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps(e) for e in events) + '\n', encoding='utf-8')
        return path

    def snapshot(self):
        return {'status': 'running', 'done': 0, 'total': 1, 'questions': [], 'service_stop': {},
                'telemetry_stale': False, 'telemetry_incomplete': False, 'operations': [],
                'tasks': [{'id': 'task', 'title': 'Tests', 'status': 'running', 'waiting_for': [],
                           'deferred_evidence': [], 'candidates': [{'model': 'model-a', 'stage': 'execute'}]}]}

    def packet(self, snapshot=None, previous=None):
        (self.root / 'plan.json').write_text(json.dumps({'tasks': [
            {'id': 'task', 'prompt': 'Implement tests; no production changes', 'requirements': ['REQ-1']}]}))
        with patch('run_status.snapshot', return_value=snapshot or self.snapshot()):
            return observers.packet(self.root, previous)

    def fake_transport(self, *, tools=None, response=None, reported_model=None):
        calls = []
        lock = threading.Lock()

        def run(argv, cwd, prompt, log, **kwargs):
            requested = argv[argv.index('--model') + 1]
            profile = json.loads((Path(cwd) / '.gigacode/settings.json').read_text())
            with lock:
                calls.append({'argv': argv, 'cwd': Path(cwd), 'prompt': prompt, 'log': Path(log),
                              'kwargs': kwargs, 'profile': profile})
            Path(log).mkdir(parents=True, exist_ok=True)
            path = Path(log) / 'stdout.jsonl'
            init = {'type': 'system', 'subtype': 'init', 'model': reported_model or requested,
                    'tools': [] if tools is None else tools, 'mcp_servers': []}
            path.write_text(json.dumps(init) + '\n')
            kwargs['activity_callback']({'events_path': str(path)})
            return {'ok': True, 'events_path': str(path), 'tool_calls': [],
                    'text': response if response is not None else
                        json.dumps({'status': 'waiting', 'summary': 'Проверка выполняется.'})}
        return run, calls

    def test_public_window_is_bounded_redacted_and_omits_reasoning(self):
        events = [event('assistant', [{'type': 'text', 'text': 'old claim'}])]
        events += [event('assistant', [{'type': 'thinking', 'thinking': 'PRIVATE_THOUGHT'},
                   {'type': 'tool_use', 'id': str(i), 'name': 'read_file',
                    'input': {'command': 'curl --token credential-value', 'padding': 'x' * 900}}])
                   for i in range(30)]
        events.append(event('user', [{'type': 'tool_result', 'tool_use_id': '29',
                      'content': 'Authorization: Bearer credential-result\n' + 'y' * 2000}]))
        events.append(event('assistant', [{'type': 'text', 'text': 'Current check running.'}]))
        path = self.write_events('execute', events)
        with path.open('rb') as stream:
            data = stream.read()
        path.write_bytes(b'x' * (128 * 1024 + 10) + b'\n' + data)
        result = observers.recent_events(self.root, path)
        encoded = json.dumps(result)
        self.assertEqual(len(result['events']), 20)
        self.assertTrue(result['window_limited'])
        self.assertGreater(result['bytes_observed'], 128 * 1024)
        for hidden in ('PRIVATE_THOUGHT', 'credential-value', 'credential-result', 'old claim'):
            self.assertNotIn(hidden, encoded)
        self.assertEqual(result['events'][-1]['kind'], 'model_claim')
        for item in result['events']:
            self.assertLessEqual(len(item.get('input_excerpt', '')), 600)
            self.assertLessEqual(len(item.get('result_excerpt', '')), 1000)

    def test_packet_filters_observers_and_only_carries_one_previous_interval(self):
        path = self.write_events('execute', [event('assistant', [{'type': 'text', 'text': 'real progress'}])])
        snapshot = self.snapshot()
        snapshot['operations'] = [
            {'task_id': 'task', 'model': 'model-a', 'events_path': str(path)},
            {'purpose': 'observation', 'events_path': '/must/not/read/observer', 'model': 'model-b'}]
        first = self.packet(snapshot)
        self.assertEqual(first['delta']['accepted_since_previous'], 0)
        self.assertIsNone(first['delta']['previous_snapshot_at'])
        self.assertEqual(len(first['operations']), 1)
        self.assertEqual(len(first['recent_events']), 1)
        snapshot['done'] = 1
        snapshot['tasks'][0]['status'] = 'done'
        second = self.packet(snapshot, first)
        self.assertEqual(second['delta']['accepted_since_previous'], 1)
        self.assertEqual(second['delta']['changed_tasks'], ['task'])
        self.assertNotIn('previous', second['previous'])
        self.assertEqual(second['previous']['recent_events'], first['recent_events'])
        third = self.packet(snapshot, second)
        self.assertEqual(third['delta']['accepted_since_previous'], 0)
        self.assertEqual(third['delta']['changed_tasks'], [])
        # A new manager has no baseline from a different controller lifetime.
        self.assertIsNone(observers.Manager(self.runner).previous)
        reset = self.packet(snapshot)
        self.assertEqual(reset['delta']['accepted_since_previous'], 0)

    def test_all_tasks_accepted_does_not_erase_final_integration_gate(self):
        snapshot = self.snapshot()
        snapshot.update(done=1, integration='running', ready=False)
        snapshot['tasks'][0]['status'] = 'done'
        current = self.packet(snapshot)
        self.assertEqual(current['integration'], 'running')
        self.assertFalse(current['ready'])
        self.assertIn('Итоговая', current['integration_scope'])
        complete = copy.deepcopy(current)
        complete.update(integration='done', ready=True)
        self.assertNotEqual(observers.fingerprint(current), observers.fingerprint(complete))

    def test_packet_never_reads_workspace_paths_and_rejects_oversized_input(self):
        product = self.root.parent / 'product'
        product.mkdir()
        secret = product / 'source.py'
        secret.write_text('PRODUCT_WORKSPACE_CONTENT')
        snapshot = self.snapshot()
        snapshot['operations'] = [{'task_id': 'task', 'events_path': str(secret)}]
        value = self.packet(snapshot)
        self.assertTrue(value['recent_events'][0]['unavailable'])
        self.assertNotIn('PRODUCT_WORKSPACE_CONTENT', json.dumps(value))
        self.assertEqual(secret.read_text(), 'PRODUCT_WORKSPACE_CONTENT')
        large = self.root / 'large.json'
        large.write_bytes(b' ' * (2 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            observers.read_small(large)

    def test_missing_nonempty_or_mcp_registry_cannot_start_audits(self):
        for init in [
                {'type': 'system', 'subtype': 'init', 'model': 'model-a'},
                {'type': 'system', 'subtype': 'init', 'model': 'model-a', 'tools': ['read_file']},
                {'type': 'system', 'subtype': 'init', 'model': 'model-a', 'tools': [], 'mcp_servers': ['server']}]:
            with self.subTest(init=init):
                path = self.write_events('probe', [init])
                manager = observers.Manager(self.runner)
                with patch.object(manager, 'session', return_value={
                        'ok': True, 'events_path': str(path), 'tool_calls': []}) as session:
                    result = manager.evaluate('progress', 'model-a', self.packet())
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(session.call_count, 1)
                self.assertFalse(manager.probed)

    def test_two_roles_receive_same_saved_packet_with_verified_models_and_profiles(self):
        value = self.packet()
        before = copy.deepcopy(value)
        transport, calls = self.fake_transport()
        simultaneous = threading.Barrier(2)
        def run(argv, cwd, prompt, log, **kwargs):
            if 'СНИМОК:\n' in prompt:
                simultaneous.wait(timeout=5)
            return transport(argv, cwd, prompt, log, **kwargs)
        with patch.object(observers, 'run_session', side_effect=run):
            self.manager.observe(value)
        self.assertEqual(value, before)
        self.assertEqual(len(calls), 4)  # one capability probe and one assessment per model
        actual = [c for c in calls if 'СНИМОК:\n' in c['prompt']]
        self.assertEqual(len(actual), 2)
        self.assertEqual({c['argv'][-1] for c in actual}, {'model-a', 'model-b'})
        self.assertEqual([json.loads(c['prompt'].split('СНИМОК:\n')[1]) for c in actual], [value, value])
        for call in calls:
            self.assertTrue(call['cwd'].is_relative_to(self.root / 'observer/sessions'))
            self.assertEqual(call['profile']['tools']['core'], ['__sdd_observer_no_tools__'])
            self.assertEqual(call['profile']['tools']['exclude'], observers.DENY_TOOLS)
            self.assertEqual(call['profile']['mcp']['allowed'], [])
            self.assertTrue(call['profile']['disableAllHooks'])
            self.assertEqual(call['profile']['context']['fileName'], 'OBSERVER-CONTEXT.md')
            self.assertEqual(call['kwargs']['approval_mode'], 'plan')
            self.assertFalse(call['kwargs']['stop_event'].is_set())
            self.assertIsNot(call['kwargs']['stop_event'], self.runner.stop)
        latest = json.loads((self.manager.root / 'latest.json').read_text())
        self.assertEqual(latest['status'], 'waiting')
        self.assertTrue(latest['advisory_only'])
        self.assertEqual(json.loads(Path(latest['snapshot']).read_text()), value)
        self.assertFalse(self.runner.stop.is_set())
        self.assertEqual(self.runner.state['tasks']['task']['status'], 'running')
        self.assertEqual(len(self.activities), 4)
        self.assertTrue(all(a[1]['metadata']['purpose'] == 'observation' for a in self.activities))

    def test_wrong_reported_model_disables_observation_without_changing_acceptance(self):
        transport, calls = self.fake_transport(reported_model='unexpected-model')
        state = copy.deepcopy(self.runner.state)
        with patch.object(observers, 'run_session', side_effect=transport):
            self.manager.observe(self.packet())
        self.assertEqual(len(calls), 2)  # no assessment after either failed capability probe
        self.assertTrue(self.manager.disabled)
        self.assertEqual(self.runner.state, state)
        self.assertFalse(self.runner.stop.is_set())
        latest = json.loads((self.manager.root / 'latest.json').read_text())
        self.assertEqual(latest['status'], 'unavailable')

    def test_unstructured_answer_is_kept_without_retry_or_main_state_change(self):
        prose = '**Проверки ещё идут.** Небольшая опечатка не блокирует выполнение.'
        transport, calls = self.fake_transport(response=prose)
        state = copy.deepcopy(self.runner.state)
        with patch.object(observers, 'run_session', side_effect=transport):
            self.manager.observe(self.packet())
        self.assertEqual(len(calls), 4)
        latest = json.loads((self.manager.root / 'latest.json').read_text())
        self.assertEqual(latest['status'], 'unknown')
        self.assertTrue(all(r['details'] == prose for r in latest['results'].values()))
        self.assertEqual(self.runner.state, state)
        self.assertFalse(self.runner.stop.is_set())
        self.assertFalse(self.manager.disabled)

    def test_one_observer_exception_is_isolated_and_not_retried(self):
        state = copy.deepcopy(self.runner.state)
        def evaluate(role, model, value):
            if role == 'scope':
                raise OSError('unavailable CLI')
            return {'status': 'waiting', 'summary': 'Tests running'}
        with patch.object(self.manager, 'evaluate', side_effect=evaluate) as run:
            self.manager.observe(self.packet())
        self.assertEqual(run.call_count, 2)
        self.assertTrue(self.manager.disabled)
        self.assertEqual(self.runner.state, state)
        self.assertFalse(self.runner.stop.is_set())
        latest = json.loads((self.manager.root / 'latest.json').read_text())
        self.assertEqual(latest['results']['progress']['status'], 'waiting')
        self.assertEqual(latest['results']['scope']['status'], 'unavailable')

    def test_profile_write_failure_prevents_cli_launch(self):
        with patch.object(observers, 'write_projection', return_value=False), \
                patch.object(observers, 'run_session') as run:
            with self.assertRaisesRegex(OSError, 'profile'):
                self.manager.session('progress', 'model-a', 'test')
        run.assert_not_called()

    def test_context_exit_cancels_observer_lifecycle_without_main_stop(self):
        entered = threading.Event()
        def loop():
            entered.set()
            self.manager.stop.wait(5)
        with patch.object(self.manager, 'loop', side_effect=loop):
            with self.manager:
                self.assertTrue(entered.wait(2))
        self.assertTrue(self.manager.stop.is_set())
        self.assertFalse(self.manager.thread.is_alive())
        self.assertFalse(self.runner.stop.is_set())

    def test_transport_observes_observer_cancellation_without_setting_main_stop(self):
        transport, calls = self.fake_transport()
        def cancel(argv, cwd, prompt, log, **kwargs):
            self.assertFalse(kwargs['stop_event'].is_set())
            self.manager.stop.set()
            self.assertTrue(kwargs['stop_event'].is_set())
            self.assertFalse(self.runner.stop.is_set())
            return transport(argv, cwd, prompt, log, **kwargs)
        with patch.object(observers, 'run_session', side_effect=cancel):
            self.manager.session('progress', 'model-a', 'test')
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.runner.stop.is_set())

    def test_main_cancellation_prevents_new_observer_sessions(self):
        self.runner.stop.set()
        with patch.object(observers, 'run_session') as run:
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                self.manager.session('progress', 'model-a', 'test')
        run.assert_not_called()

    def test_assessment_model_is_rechecked_after_successful_probe(self):
        good_transport, calls = self.fake_transport()
        def run(argv, cwd, prompt, log, **kwargs):
            result = good_transport(argv, cwd, prompt, log, **kwargs)
            if 'СНИМОК:\n' in prompt:
                path = Path(result['events_path'])
                init = json.loads(path.read_text())
                init['model'] = 'unexpected-model'
                path.write_text(json.dumps(init) + '\n')
            return result
        with patch.object(observers, 'run_session', side_effect=run):
            self.manager.observe(self.packet())
        self.assertEqual(len(calls), 4)
        self.assertTrue(self.manager.disabled)
        latest = json.loads((self.manager.root / 'latest.json').read_text())
        self.assertEqual(latest['status'], 'unavailable')
        self.assertFalse(self.runner.stop.is_set())

    def test_disabled_option_does_not_start_thread(self):
        self.runner.cfg['observers_enabled'] = False
        with patch.object(self.manager, 'loop') as loop:
            with self.manager:
                self.assertIsNone(self.manager.thread)
        loop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
