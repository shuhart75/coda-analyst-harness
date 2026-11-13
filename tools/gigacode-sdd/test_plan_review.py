import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import plan_review
import test_import_plan as fixtures
from controller import Blocked, read, save


class BoundaryTests(unittest.TestCase):
    def test_pinned_read_and_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'work'
            root.mkdir()
            packet = Path(tmp) / 'input'
            packet.mkdir()
            candidate = Path(tmp) / 'candidate.json'
            candidate.write_text('{}')
            sha = plan_review.prepare(root, packet, candidate, Path(tmp) / 'decision')
            events = Path(tmp) / 'events'
            def write(path, name='read_file'):
                events.write_text(json.dumps({'message': {'content': [{'type': 'tool_use',
                    'id': 'read', 'name': name, 'input': {'file_path': path}},
                    {'type': 'tool_result', 'tool_use_id': 'read', 'is_error': False, 'content': 'read'}]}})+'\n')
            write('.review-input/plan.json')
            plan_review.audit(root, events, sha)
            write('../candidate.json')
            with self.assertRaisesRegex(ValueError, 'outside'):
                plan_review.audit(root, events, sha)
            write('.review-input/plan.json', 'agent')
            with self.assertRaisesRegex(ValueError, 'disallowed'):
                plan_review.audit(root, events, sha)
            write('backend/missing.txt')
            with self.assertRaisesRegex(ValueError, 'did not read'):
                plan_review.audit(root, events, sha)
            (root / '.review-input/plan.json').write_text('{"changed":true}')
            with self.assertRaisesRegex(ValueError, 'changed'):
                plan_review.audit(root, events, sha)

    def test_only_exact_noops_are_tolerated(self):
        for command in ('true', 'echo no'):
            self.assertTrue(plan_review.harmless_shell_call({'name':'run_shell_command',
                'input':{'command':command,'description':'no-op'}}))
        for command in ('true; touch x', 'echo no > x', 'true && pwd', '$(true)',
                        'echo `pwd`', 'true\npwd', ' true', 'echo no ', 'pwd', 'cat secret'):
            self.assertFalse(plan_review.harmless_shell_call({'name':'run_shell_command',
                'input':{'command':command}}))
        self.assertFalse(plan_review.harmless_shell_call({'name':'run_shell_command',
            'input':{'command':'true','cwd':'..'}}))

    def test_review_binds_candidate_without_trusting_model_sha(self):
        f = fixtures.ImportTests('test_import_preserves_checkpoint_and_requires_review')
        f.setUp()
        self.addCleanup(f.doCleanups)
        root = f.fixture.root / 'review-copy'
        root.mkdir()
        sha = plan_review.prepare(root, f.fixture.packet, f.old, root / 'missing')
        events = root / 'events.jsonl'
        events.write_text(json.dumps({'message':{'content':[{'type':'tool_use','name':'read_file',
            'id':'candidate', 'input':{'file_path':'.review-input/plan.json'}},
            {'type':'tool_result','tool_use_id':'candidate','is_error':False,'content':'read'}]}})+'\n')
        report = {'verdict':'pass','findings':[], 'coverage':{'REQ-BE':'task be','REQ-FE':'task fe'},
                  'candidate_sha256':'wrong'}
        result = {'ok':True,'events_path':str(events),'text':json.dumps(report)}
        with patch.object(f.runner,'session',return_value=result):
            bound = f.runner.review(root,'.','review',['REQ-BE','REQ-FE'],'plan-review')
            self.assertEqual(bound['candidate_sha256'], sha)
        with events.open('a') as stream:
            stream.write(json.dumps({'message':{'content':[{'type':'tool_use','id':'noop',
                'name':'run_shell_command','input':{'command':'true'}},
                {'type':'tool_result','tool_use_id':'noop','is_error':False,'content':'ok'}]}})+'\n')
        report['candidate_sha256']=sha
        result['text']=json.dumps(report)
        with patch.object(f.runner,'session',return_value=result):
            accepted = f.runner.review(root,'.','review',['REQ-BE','REQ-FE'],'plan-review')
            self.assertEqual(accepted['verdict'],'pass')
            self.assertNotIn('protocol_warnings', accepted)
            self.assertFalse((root/'review-audit.json').exists())
        with events.open('a') as stream:
            stream.write(json.dumps({'message':{'content':[{'type':'tool_use','id':'bad','name':'run_shell_command',
                'input':{'command':'true; touch x'}},
                {'type':'tool_result','tool_use_id':'bad','is_error':False,'content':'ok'}]}})+'\n')
        with patch.object(f.runner,'session',return_value=result):
            with self.assertRaisesRegex(Blocked,'disallowed tool'):
                f.runner.review(root,'.','review',['REQ-BE','REQ-FE'],'plan-review')
