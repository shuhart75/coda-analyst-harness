"""Acceptance must bind exact reviewed inputs; explicit imports supersede stale cycles."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from controller import Blocked, Runner, digest, save
from import_plan import import_plan
import test_controller as fixtures


class PlanningIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ControllerE2E('test_init_preserves_source_and_rejects_changed_packet')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner
        self.runner.state['preflight'] = {'config_sha256': digest(self.fixture.config), 'passed': True}
        self.runner.persist()
        self.initial = self.runner.root/'plan-candidate.json'
        save(self.initial, self.fixture.plan)
        self.candidate = import_plan(self.fixture.config, self.initial, digest(self.initial))
        self.runner = Runner(self.fixture.config)

    def report(self, path, verdict='pass'):
        return {'verdict': verdict, 'findings': [] if verdict == 'pass' else ['Confirmed external decision'],
                'candidate_sha256': digest(path),
                'coverage': {r: 'Pinned task and acceptance check evidence' for r in self.runner.cfg['requirements']}}

    def test_candidate_mutated_after_review_is_not_accepted(self):
        def review(runner, path):
            result = self.report(path)
            altered = json.loads(path.read_text())
            altered['tasks'][0]['prompt'] += ' Unreviewed change.'
            save(path, altered)
            return result
        with patch('planning_cycle.evaluate', side_effect=review), \
             patch('planning_cycle.generate', side_effect=AssertionError('import needs no generation')):
            with self.assertRaises(Blocked):
                self.runner.plan()
        self.assertEqual(self.runner.state['tasks'], {})
        self.assertFalse((self.runner.root/'plan.json').exists())

    def test_workspace_mutated_after_review_is_not_accepted(self):
        def review(runner, path):
            result = self.report(path)
            (runner.main/'unreviewed.txt').write_text('Changed while another reviewer was finishing')
            return result
        with patch('planning_cycle.evaluate', side_effect=review), \
             patch('planning_cycle.generate', side_effect=AssertionError('import needs no generation')):
            with self.assertRaises(Blocked):
                self.runner.plan()
        self.assertEqual(self.runner.state['tasks'], {})
        self.assertTrue((self.runner.main/'unreviewed.txt').exists())

    def test_new_import_after_external_blocker_is_reviewed_and_accepted(self):
        with patch('planning_cycle.evaluate', side_effect=lambda runner, path: self.report(path, 'blocked')), \
             patch('planning_cycle.generate', side_effect=AssertionError('external blocker must not trigger generation')):
            with self.assertRaisesRegex(Blocked, 'analyst decision'):
                self.runner.plan()
        corrected = self.fixture.root/'corrected.json'
        proposal = json.loads(self.candidate.read_text())
        proposal['tasks'][0]['prompt'] += ' Uses an existing available component.'
        save(corrected, proposal)
        target = import_plan(self.fixture.config, corrected, digest(self.candidate))
        self.runner = Runner(self.fixture.config)
        seen = []
        def review(runner, path):
            seen.append(Path(path))
            return self.report(path)
        with patch('planning_cycle.evaluate', side_effect=review), \
             patch('planning_cycle.generate', side_effect=AssertionError('must review explicitly imported replacement')):
            self.runner.plan()
        self.assertEqual(seen, [target])
        self.assertEqual(self.runner.state['status'], 'planned')
        self.assertEqual(json.loads((self.runner.root/'plan.json').read_text()), proposal)

    def test_one_external_verdict_and_one_protocol_failure_do_not_latch_global_blocker(self):
        import review_cycle
        self.runner.state.pop('plan_candidate', None)
        self.runner.persist()
        alternative = self.runner.root/'plan-candidate-alternative.json'
        proposal = json.loads(self.candidate.read_text())
        proposal['tasks'][0]['prompt'] += ' Alternative implementation.'
        save(alternative, proposal)
        def generate(runner, model, prompt, previous, feedback, folder):
            return self.candidate if model == review_cycle.MODELS[0] else alternative
        def incomplete(runner, path):
            if path == self.candidate:
                return self.report(path, 'blocked')
            raise Blocked('Reviewer protocol attempts exhausted')
        with patch('planning_cycle.generate', side_effect=generate), patch('planning_cycle.evaluate', side_effect=incomplete):
            with self.assertRaises(Blocked):
                self.runner.plan()
        portfolio = json.loads((self.runner.root/'planning-portfolio.json').read_text())
        cycle = next(iter(portfolio['scopes'].values()))
        self.assertNotIn('external_blocker', cycle)
        self.runner = Runner(self.fixture.config)
        with patch('planning_cycle.generate', side_effect=generate), \
             patch('planning_cycle.evaluate', side_effect=lambda runner, path: self.report(path)):
            self.runner.plan()
        self.assertEqual(self.runner.state['status'], 'planned')
