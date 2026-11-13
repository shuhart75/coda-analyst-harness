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
                    'name': name, 'input': {'file_path': path}}]}})+'\n')
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

    def test_rejection_stops_and_resume_does_not_call_model(self):
        f = fixtures.ImportTests('test_import_preserves_checkpoint_and_requires_review')
        f.setUp()
        self.addCleanup(f.doCleanups)
        fixtures.importer.import_plan(f.fixture.config, f.new, f.sha)
        runner = fixtures.importer.Runner(f.fixture.config)
        with patch.object(runner, 'require_preflight'), \
                patch.object(runner, 'session', side_effect=AssertionError('no planner')), \
                patch.object(runner, 'review', return_value={'verdict':'changes','findings':['fix task be']}) as review:
            with self.assertRaisesRegex(Blocked, 'automatic replanning stopped'):
                runner.plan()
            with self.assertRaisesRegex(Blocked, 'requires attention'):
                runner.plan()
        self.assertEqual(review.call_count, 1)
        rejected = read(runner.root / 'plan-rejected.json')
        self.assertEqual(rejected['candidate_sha256'], runner.state['plan_review_pending']['sha256'])
        self.assertFalse(runner.state['tasks'])

    def test_review_checks_returned_sha(self):
        f = fixtures.ImportTests('test_import_preserves_checkpoint_and_requires_review')
        f.setUp()
        self.addCleanup(f.doCleanups)
        root = f.fixture.root / 'review-copy'
        root.mkdir()
        sha = plan_review.prepare(root, f.fixture.packet, f.old, root / 'missing')
        events = root / 'events.jsonl'
        events.write_text(json.dumps({'message':{'content':[{'type':'tool_use','name':'read_file',
            'input':{'file_path':'.review-input/plan.json'}}]}})+'\n')
        report = {'verdict':'pass','findings':[], 'coverage':{'REQ-BE':'task be','REQ-FE':'task fe'},
                  'candidate_sha256':'wrong'}
        result = {'ok':True,'events_path':str(events),'text':json.dumps(report)}
        with patch.object(f.runner,'session',return_value=result):
            with self.assertRaisesRegex(Blocked,'SHA256 mismatch'):
                f.runner.review(root,'.','review',['REQ-BE','REQ-FE'],'plan-review')
        report['candidate_sha256']=sha
        result['text']=json.dumps(report)
        with patch.object(f.runner,'session',return_value=result):
            self.assertEqual(f.runner.review(root,'.','review',['REQ-BE','REQ-FE'],'plan-review')['verdict'],'pass')
