import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from controller import Blocked, digest, file_manifest, save
import operator_input as oi


class OperatorInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'input').mkdir()
        (root / 'input' / 'requirements.md').write_text('REQ-A mandatory behavior')
        save(root / 'plan.json', {'tasks': [{'id': 'task', 'requirements': ['REQ-A']}]})
        self.messages = []
        self.runner = SimpleNamespace(
            root=root, main=root / 'work/main',
            state={'tasks': {'task': {'status': 'done', 'workspace': 'retained-wip'}}},
            cfg={'requirements': ['REQ-A'], 'requirements_sha256': digest(root / 'input/requirements.md'),
                 'packet_files': file_manifest(root / 'input')},
            progress=self.messages.append)
        self.runner.persist = lambda: save(root / 'state.json', self.runner.state)
        self.git = patch('workspace.git', return_value='HEAD-A')
        self.snap = patch('workspace.snapshot', return_value={'status': ''})
        self.git.start(); self.snap.start()
        self.addCleanup(self.git.stop); self.addCleanup(self.snap.stop)

    def ready(self):
        oi.reconcile(self.runner)
        review = {'verdict': 'pass', 'findings': [], 'coverage': {'REQ-A': 'behavior test passed'}}
        checks = [{'ok': True, 'returncode': 0, 'timed_out': False}]
        self.runner.state.update(main_head='HEAD-A', plan_sha256=digest(self.runner.root / 'plan.json'),
                                 integration={'status': 'done', 'integrated_head': 'HEAD-A',
                                              'checks': checks, 'review': review})
        evidence = {'revision': self.runner.state['operator_revision']['id'], 'head': 'HEAD-A',
                    'checks': checks, 'review': review}
        oi.mark_reviewed(self.runner, evidence)
        return evidence

    def test_initial_binding_preserves_legacy_work_and_repeat_is_noop(self):
        tasks = copy.deepcopy(self.runner.state['tasks'])
        self.assertFalse(oi.reconcile(self.runner))
        self.assertTrue(self.runner._operator_bound)
        self.assertEqual(self.runner.state['tasks'], tasks)
        self.assertNotIn('needs_instruction_review', self.runner.state)
        pointer = copy.deepcopy(self.runner.state['operator_revision'])
        self.runner._operator_bound = False
        self.assertFalse(oi.reconcile(self.runner))
        self.assertTrue(self.runner._operator_bound)
        self.assertEqual(pointer, self.runner.state['operator_revision'])
        self.assertEqual(len(list((self.runner.root/'operator-instructions').glob('*.json'))), 1)

    def test_changed_instructions_preserve_work_and_all_revision_bytes(self):
        self.ready()
        oi.report(self.runner, 'complete', required_complete=True)
        old = copy.deepcopy(self.runner.state['operator_revision'])
        path = self.runner.root / 'operator-instructions' / old['file']
        raw = path.read_bytes()
        (self.runner.root / 'decision.md').write_text('Reuse the existing stand when faster.')
        self.assertTrue(oi.reconcile(self.runner))
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.runner.state['tasks']['task']['status'], 'done')
        self.assertEqual(self.runner.state['tasks']['task']['workspace'], 'retained-wip')
        self.assertEqual(self.runner.state['needs_instruction_review']['scope'], 'all')
        self.assertNotIn('instruction_verification', self.runner.state)
        self.assertFalse(json.loads((self.runner.root/'readiness.json').read_text())['required_complete'])
        self.assertIn('Reuse the existing stand', oi.prompt(self.runner))
        with self.assertRaisesRegex(Blocked, 'readiness'):
            oi.report(self.runner, 'complete', required_complete=True)

    def test_mid_session_instruction_change_cannot_be_accepted(self):
        oi.reconcile(self.runner)
        (self.runner.root/'decision.md').write_text('New instruction')
        with self.assertRaises(oi.InstructionChanged): oi.check_current(self.runner)
        with self.assertRaises(oi.InstructionChanged): oi.prompt(self.runner)
        self.assertNotIn('needs_instruction_review', self.runner.state)

    def test_changed_revision_file_is_not_trusted(self):
        oi.reconcile(self.runner)
        path=self.runner.root/'operator-instructions'/self.runner.state['operator_revision']['file']
        path.write_text('{}')
        with self.assertRaisesRegex(Blocked, 'revision changed'): oi.check_current(self.runner)

    def test_crash_after_revision_write_reuses_same_revision(self):
        oi.reconcile(self.runner)
        before=copy.deepcopy(self.runner.state)
        (self.runner.root/'decision.md').write_text('Changed')
        with patch.object(self.runner, 'persist', side_effect=OSError('crash')):
            with self.assertRaises(OSError): oi.reconcile(self.runner)
        pointer=copy.deepcopy(self.runner.state['operator_revision'])
        self.runner.state=before
        self.assertTrue(oi.reconcile(self.runner))
        self.assertEqual(self.runner.state['operator_revision'],pointer)
        self.assertEqual(len(list((self.runner.root/'operator-instructions').glob('*.json'))),2)

    def test_ready_requires_task_and_integration_and_instruction_proof(self):
        self.ready()
        valid=copy.deepcopy(self.runner.state)
        mutations = [lambda s:s['tasks']['task'].update(status='pending'),
                     lambda s:s['integration'].update(status='accepted'),
                     lambda s:s['integration'].update(integrated_head='old'),
                     lambda s:s['integration']['checks'][0].update(ok=False),
                     lambda s:s['integration']['review'].update(coverage={}),
                     lambda s:s.pop('instruction_verification'),
                     lambda s:s.update(needs_instruction_review={'scope':'all'})]
        for change in mutations:
            with self.subTest(change=change):
                self.runner.state=copy.deepcopy(valid);change(self.runner.state)
                with self.assertRaisesRegex(Blocked,'readiness'):
                    oi.report(self.runner,'complete',required_complete=True)
        self.runner.state=valid
        self.assertEqual(oi.report(self.runner,'complete',required_complete=True)['status'],'complete')

    def test_ready_rejects_changed_packet_or_plan(self):
        self.ready()
        (self.runner.root/'input/requirements.md').write_text('changed requirements')
        with self.assertRaisesRegex(Blocked,'readiness'):
            oi.report(self.runner,'complete',required_complete=True)

    def test_partial_status_and_optional_require_evidence(self):
        self.ready()
        partial=oi.report(self.runner,'running')
        self.assertFalse(partial['required_complete'])
        with self.assertRaisesRegex(Blocked,'ready status'):
            oi.report(self.runner,'preview-ready')
        with self.assertRaisesRegex(Blocked,'Optional finding'):
            oi.report(self.runner,'complete',required_complete=True,optional_findings=['polish'])
        optional={'id':'polish','status':'optional','text':'Optional wording improvement',
                  'reason':'Not part of required behavior','requirements':[],
                  'outside_required_scope':True,
                  'evidence':[{'path':'.review-input/packet/requirements.md','quote':'REQ-A mandatory behavior'}]}
        result=oi.report(self.runner,'complete',required_complete=True,optional_findings=[optional])
        self.assertEqual(result['status'],'preview-ready')
        self.assertIn('можно смотреть',self.messages[-1])
        self.assertEqual(json.loads((self.runner.root/'readiness.json').read_text()),result)

    def test_wrong_verification_does_not_clear_pending_revision(self):
        evidence=self.ready()
        (self.runner.root/'decision.md').write_text('Changed')
        oi.reconcile(self.runner)
        with self.assertRaisesRegex(Blocked,'Instruction verification'):
            oi.mark_reviewed(self.runner,evidence)
        self.assertIn('needs_instruction_review',self.runner.state)


if __name__ == '__main__': unittest.main()
