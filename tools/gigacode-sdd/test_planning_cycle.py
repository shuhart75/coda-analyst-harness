"""Portfolios recover audited generation and never spend failures on a user stop."""
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import controller
import operator_input
import planning_cycle
import review_cycle
import test_controller as fixtures


class PlanningCycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ControllerE2E('test_init_preserves_source_and_rejects_changed_packet')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner
        operator_input.reconcile(self.runner)
        self.runner.state['preflight'] = {'config_sha256':controller.digest(self.fixture.config),'passed':True}
        self.folder = self.runner.root / 'planning-cycles' / 'test'
        self.model = review_cycle.MODELS[0]

    def generate(self):
        return planning_cycle.generate(self.runner,self.model,'PLAN_SDD_WORK\n',None,'',self.folder)

    def test_three_interruptions_do_not_exhaust_generation(self):
        for _ in range(3):
            with patch.object(self.runner,'session',side_effect=controller.Interrupted()):
                with self.assertRaises(controller.Interrupted): self.generate()
        record = next(self.folder.glob('*/generation.json'))
        self.assertEqual(controller.read(record)['failures'],0)
        candidate = self.generate()
        self.assertEqual(controller.read(candidate),self.fixture.plan)

    def test_crash_after_audited_session_reuses_output_without_model_call(self):
        original = self.runner.session
        class Crash(BaseException): pass
        def crash(*args,**kwargs):
            original(*args,**kwargs)
            raise Crash()
        with patch.object(self.runner,'session',side_effect=crash):
            with self.assertRaises(Crash):self.generate()
        with patch.object(self.runner,'session',side_effect=AssertionError('completed session must not repeat')):
            candidate=self.generate()
        self.assertEqual(controller.read(candidate),self.fixture.plan)

    def test_parallel_candidates_select_pass_not_fewer_unresolved_defects(self):
        barrier=threading.Barrier(2)
        paths={}
        for index,model in enumerate(review_cycle.MODELS):
            path=self.runner.root/f'candidate-{index}.json'
            plan=json.loads(json.dumps(self.fixture.plan))
            plan['tasks'][0]['prompt']+=f' Alternative {index}'
            controller.save(path,plan); paths[model]=path
        def generate(runner,model,*args):
            barrier.wait(timeout=5)
            return paths[model]
        def evaluate(runner,path):
            passed=path==paths[review_cycle.MODELS[1]]
            return {'verdict':'pass' if passed else 'changes','findings':[] if passed else ['Missing behavior'],
                    'coverage':{r:'verified' for r in self.runner.cfg['requirements']},
                    'candidate_sha256':controller.digest(path)}
        with patch('planning_cycle.generate',side_effect=generate),patch('planning_cycle.evaluate',side_effect=evaluate):
            self.runner.plan()
        self.assertEqual(self.runner.state['plan_selection']['sha256'],controller.digest(paths[review_cycle.MODELS[1]]))
        self.assertEqual(self.runner.state['status'],'planned')
