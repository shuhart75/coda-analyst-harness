"""Timeout research survives retries without replacing independent final evidence."""
import json
from pathlib import Path, PureWindowsPath
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from controller import Blocked, Runner, digest
from progress import SESSION_IDENTITY, session_identity
import review_cycle
import review_research
import test_review_cycle as fixtures
from transport import parse_events
from workspace import snapshot, clone_snapshot


class ResearchTests(unittest.TestCase):
    setUp = fixtures.ReviewCycleTests.setUp
    result = fixtures.ReviewCycleTests.result
    pair = fixtures.ReviewCycleTests.pair

    def partial(self, root, model):
        (root/'research.txt').write_text('confirmed local observation')
        extra = [{'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'research', 'name': 'read_file',
             'input': {'file_path': str(root/'research.txt')}}]}},
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'research', 'is_error': False,
                 'content': 'confirmed local observation'}]}},
            {'type': 'assistant', 'message': {'content': [
                {'type': 'text', 'text': 'Coverage checked; only final synthesis remains.'}]}}]
        result = self.result(root, model, extra=extra)
        events = Path(result['events_path'])
        events.write_text('\n'.join(events.read_text().splitlines()[:-1])+'\n')
        result = parse_events(events)
        result.update(returncode=-15, errors=result['errors'] + ['Session process timeout'])
        return result

    def test_timeout_retry_uses_observations_and_requires_final_verdict(self):
        bad = review_cycle.MODELS[1]
        def answer(root, cwd, prompt, role, name, model=None):
            self.counts[model] = self.counts.get(model, 0) + 1
            self.assertIn(digest(self.candidate)[:12], SESSION_IDENTITY.get())
            if model == bad and self.counts[model] == 1:
                return self.partial(root, model)
            notebook = root/'.review-input/previous-research.json'
            if model == bad:
                data = json.loads(notebook.read_text())
                self.assertEqual(data['candidate_sha256'], digest(self.candidate))
                self.assertEqual(data['model'], bad)
                self.assertIn('confirmed local observation', notebook.read_text())
                self.assertIn('Coverage checked', notebook.read_text())
                self.assertIn('FINALIZE_REVIEW', prompt)
                self.assertIn(str(min(900, self.runner.cfg['session_timeout'])) + '-second', prompt)
                self.assertEqual(len(data['sources']), 1)
                self.assertNotIn(str(root.parent), json.dumps(data['items']))
            else:
                self.assertFalse(notebook.exists())
            return self.result(root, model)
        with patch.object(self.runner, 'session', side_effect=answer):
            self.assertEqual(self.pair()['verdict'], 'pass')
        self.assertEqual(self.counts, {review_cycle.MODELS[0]: 1, bad: 2})
        self.assertEqual(SESSION_IDENTITY.get(), '')

    def test_interrupted_retry_preserves_research_on_restart(self):
        model = review_cycle.MODELS[1]
        def answer(root, cwd, prompt, role, name, model=None):
            if model == review_cycle.MODELS[0]:
                return self.result(root, model)
            self.runner.stop.set()
            return self.partial(root, model)
        with patch.object(self.runner, 'session', side_effect=answer):
            with self.assertRaises(Blocked):
                self.pair()
        self.runner = Runner(self.fixture.config)
        def resume(root, cwd, prompt, role, name, model=None):
            if model == review_cycle.MODELS[1]:
                self.assertTrue((root/'.review-input/previous-research.json').exists())
                self.assertIn('FINALIZE_REVIEW', prompt)
            return self.result(root, model)
        with patch.object(self.runner, 'session', side_effect=resume):
            self.assertEqual(self.pair()['verdict'], 'pass')

    def test_tampering_and_foreign_candidate_cannot_supply_research(self):
        import plan_review
        root = self.runner.root/'planners/research'
        clone_snapshot(self.runner.main, root)
        expected = plan_review.prepare(root, self.runner.root/'input', self.candidate, self.runner.root/'decision.md')
        model = review_cycle.MODELS[0]
        result = self.partial(root, model)
        record = {'attempt': 1, 'model': model, 'workspace': str(root), 'result': result,
                  'events_sha256': digest(result['events_path']), 'workspace_snapshot': snapshot(root, normalize_settings=True)}
        self.assertTrue(review_research.collect(self.runner, [record], expected, model)['items'])
        self.assertFalse(review_research.collect(self.runner, [record], 'different', model)['items'])
        self.assertFalse(review_research.collect(self.runner, [record], expected, 'different')['items'])
        Path(result['events_path']).write_text('{}\n')
        with self.assertRaisesRegex(Blocked, 'research changed'):
            review_research.collect(self.runner, [record], expected, model)

    def test_windows_paths_and_item_truncation(self):
        root = PureWindowsPath('C:/review/old')
        self.assertEqual(review_research.normalize({'path': str(root/'a')}, root), {'path': '.\\a'})
        value = review_research._bounded('я' * 100000)
        self.assertTrue(value['truncated'])
        self.assertIn('sha256', value)
        self.assertLess(len(json.dumps(value).encode()), review_research.MAX_BYTES)

    def test_notebook_is_bounded_and_does_not_copy_previous_notebook(self):
        import plan_review
        root = self.runner.root/'planners/bounded'
        clone_snapshot(self.runner.main, root)
        expected = plan_review.prepare(root, self.runner.root/'input', self.candidate, self.runner.root/'decision.md')
        (root/'.review-input/previous-research.json').write_text('recursive notebook')
        extra = [{'type': 'assistant', 'message': {'content': [
            {'type': 'text', 'text': str(i) + 'я' * 20000} for i in range(80)]}},
            {'message': {'content': [
                {'type': 'tool_use', 'id': 'prior', 'name': 'read_file',
                 'input': {'file_path': '.review-input/previous-research.json'}},
                {'type': 'tool_result', 'tool_use_id': 'prior', 'is_error': False,
                 'content': 'RECURSIVE_NOTEBOOK_MARKER'}]}}]
        model = review_cycle.MODELS[0]
        result = self.result(root, model, extra=extra)
        record = {'attempt': 1, 'model': model, 'workspace': str(root), 'result': result,
                  'events_sha256': digest(result['events_path']), 'workspace_snapshot': snapshot(root, normalize_settings=True)}
        notebook = review_research.collect(self.runner, [record], expected, model)
        text = json.dumps(notebook, ensure_ascii=False)
        self.assertLessEqual(len(text.encode('utf-8')), review_research.MAX_BYTES)
        self.assertGreater(notebook['omitted_items'], 0)
        self.assertNotIn('RECURSIVE_NOTEBOOK_MARKER', text)

    def test_parallel_identities_are_isolated_and_reset_on_failure(self):
        barrier = threading.Barrier(2)
        def worker(value):
            try:
                with session_identity(value):
                    barrier.wait(timeout=5)
                    self.assertEqual(SESSION_IDENTITY.get(), value)
                    raise ValueError()
            except ValueError:
                return SESSION_IDENTITY.get()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(worker, ['candidate-a #1', 'candidate-b #4'])), ['', ''])
