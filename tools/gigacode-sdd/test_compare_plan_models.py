import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import compare_plan_models as experiment
from controller import read, save
from platform_support import FileLock
from workspace import git
import test_controller as controller_tests


FAKE_REVIEW = r'''
import json,sys,time,pathlib
assert '--fallback-model' not in sys.argv
assert sys.argv[sys.argv.index('--approval-mode')+1] == 'plan'
model=sys.argv[sys.argv.index('--model')+1]
prompt=sys.stdin.read()
assert 'Do not use subagents' in prompt
assert pathlib.Path('.review-input/packet/requirements.md').is_file()
assert pathlib.Path('.review-input/plan.json').is_file()
print(json.dumps({'type':'system','model':model,'timestamp':time.time()}),flush=True)
time.sleep(.35)
report={'verdict':'changes','findings':[{'id':'F1','requirements':['REQ-BE'],
 'claim':'Add restart evidence','kind':'plan-gap',
 'evidence':[{'path':'backend/value.txt','locator':'line 1','observation':'fixture evidence'}]}],
 'coverage':{'REQ-BE':'gap in be','REQ-FE':'task fe'},'limitations':[]}
print(json.dumps({'type':'result','subtype':'success','result':json.dumps(report),
                  'timestamp':time.time()}),flush=True)
'''


class ComparisonTests(unittest.TestCase):
    def test_parallel_sessions_pin_models_inputs_and_preserve_state(self):
        fixture = controller_tests.ControllerE2E('test_start_reuses_completed_preflight_and_reaches_reviewed_plan')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fake = fixture.root / 'review.py'
        fake.write_text(FAKE_REVIEW)
        config = read(fixture.config)
        config['cli'] = [sys.executable, str(fake)]
        save(fixture.config, config)
        save(fixture.runner.root / 'plan-candidate.json', fixture.plan)
        # Tracked legacy config receives different checkout mtimes in copies.
        (fixture.runner.main / '.env.test').write_text('TOKEN=fixture-private-value\n')
        git(fixture.runner.main, 'add', '--', '.env.test')
        git(fixture.runner.main, '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
            'commit', '-qm', 'fixture private config')
        before = fixture.runner.state_path.read_bytes()
        inventory = {'complete': True, 'services': [], 'agents': [], 'limitations': []}
        with patch.object(experiment, 'app_root', return_value=fixture.root / 'app'), \
                patch.object(experiment, 'inspect_services', return_value=inventory):
            status = experiment.experiment(fixture.config, list(experiment.MODELS), timeout=5)
        self.assertEqual(status, 0)
        self.assertEqual(fixture.runner.state_path.read_bytes(), before)
        output = next((fixture.root / 'app/experiments').iterdir())
        comparison = read(output / 'comparison.json')
        self.assertTrue(comparison['controlled_comparison_valid'])
        self.assertTrue(comparison['original_unchanged'])
        self.assertEqual(len(comparison['exact_text_matches']), 1)
        prompts = [(output / model / 'session/prompt.txt').read_bytes() for model in ('deepseek', 'glm')]
        self.assertEqual(prompts[0], prompts[1])
        intervals = []
        for label, model in zip(('deepseek', 'glm'), experiment.MODELS):
            result = read(output / label / 'result.json')
            self.assertEqual(result['models_reported'], [model])
            events = [json.loads(line) for line in (output / label / 'session/stdout.jsonl').read_text().splitlines()]
            intervals.append((events[0]['timestamp'], events[-1]['timestamp']))
        self.assertLess(max(v[0] for v in intervals), min(v[1] for v in intervals))
        with patch.object(experiment, 'app_root', return_value=fixture.root / 'app'), \
                patch.object(experiment, 'run_session') as run, \
                FileLock(fixture.runner.root / 'run.lock'):
            with self.assertRaises(BlockingIOError):
                experiment.experiment(fixture.config, list(experiment.MODELS))
            run.assert_not_called()

    def test_private_config_timestamps_ignored_but_content_changes_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = [Path(tmp) / name for name in ('a', 'b')]
            base = {'head': 'same', 'status': '', 'files': {
                '.env': {'redacted': True, 'size': 8, 'mtime_ns': 1, 'mode': 0o600}}}
            for index, root in enumerate(roots):
                root.mkdir()
                (root / '.env').write_text('TOKEN=aa')
                os.utime(root / '.env', ns=(index + 1, index + 1))
            def snap(root, **kwargs):
                value = json.loads(json.dumps(base))
                value['files']['.env']['mtime_ns'] = (root / '.env').stat().st_mtime_ns
                return value
            with patch.object(experiment, 'snapshot', side_effect=snap):
                self.assertNotEqual(snap(roots[0]), snap(roots[1]))
                left, right = [experiment.comparable_input(root) for root in roots]
                self.assertEqual(left, right)
                (roots[1] / '.env').write_text('TOKEN=bb')
                right = experiment.comparable_input(roots[1])
                self.assertNotEqual(left, right)
                diagnostic = experiment.input_differences(left, right, {}, {})
                self.assertEqual(diagnostic['workspace_paths'], ['.env'])
                self.assertNotIn('TOKEN', json.dumps(diagnostic))
                self.assertNotIn(left['files']['.env']['sha256'], json.dumps(diagnostic))

    def test_activity_handles_partial_events_and_distinguishes_subagent_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'stdout.jsonl'
            path.write_text('{"type":"system","model":"GLM')
            activity = experiment.Activity(path)
            activity.poll()
            self.assertEqual(activity.events, 0)
            with path.open('a') as stream:
                stream.write('-5.3-Flash"}\n')
                stream.write(json.dumps({'type':'system','model':'other','parent_tool_use_id':'a'})+'\n')
                stream.write(json.dumps({'type':'assistant','message':{'content':[
                    {'type':'tool_use','name':'agent','input':{'secret':'DO_NOT_PRINT'}}]}})+'\n')
            activity.poll()
            self.assertEqual(activity.models, {'GLM-5.3-Flash'})
            self.assertEqual(activity.child_models, {'other'})
            self.assertTrue(activity.subagents)
            self.assertNotIn('DO_NOT_PRINT', activity.summary())
            self.assertEqual(experiment.identity({'type':'assistant','model':'untrusted'}), [])

    def test_comparison_does_not_equate_wording_or_verify_claims(self):
        def report(verdict, claim):
            return {'review': {'verdict': verdict, 'findings': [
                {'claim':claim,'requirements':['REQ-1'],'evidence':[]} ] if claim else []}}
        compared = experiment.compare([report('changes','test missing'), report('changes','no test exists')])
        self.assertEqual(compared['exact_text_matches'], [])
        self.assertEqual(len(compared['same_requirement_candidates']), 1)
        self.assertEqual(compared['verified_defects'], [])
        self.assertFalse(compared['automatic_acceptance'])
        self.assertTrue(experiment.compare([report('pass',None), report('changes','gap')])['verdict_disagreement'])

    def test_review_schema_requires_evidence_and_full_coverage(self):
        with self.assertRaises(ValueError):
            experiment.valid_review(json.dumps({'verdict':'changes','findings':[{'claim':'gap'}],
                                               'coverage':{'REQ-1':'gap'}}), ['REQ-1'])
        with self.assertRaises(ValueError):
            experiment.valid_review('{"verdict":"pass","findings":[],"coverage":{}}', ['REQ-1'])


if __name__ == '__main__':
    unittest.main()
