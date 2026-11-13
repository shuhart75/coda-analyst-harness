import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import review_cycle
import plan_review
import test_controller as fixtures
from controller import Blocked, Runner, digest, save
from transport import parse_events


class ReviewCycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ControllerE2E('test_init_preserves_source_and_rejects_changed_packet')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner
        self.candidate = self.runner.root / 'plan-candidate.json'
        save(self.candidate, self.fixture.plan)
        self.counts = {}
        self.lock = threading.Lock()

    def result(self, root, model, *, extra=None, report=None, model_reported=None):
        events = self.runner.logdir('plan-review') / 'stdout.jsonl'
        events.parent.mkdir(parents=True)
        report = report or {'verdict': 'pass', 'findings': [], 'candidate_sha256': 'typing-error',
                            'coverage': {r: 'specific test evidence' for r in self.runner.cfg['requirements']}}
        rows = [{'type':'system','subtype':'init','model':model_reported or model,'cwd':str(root)},
                {'type':'assistant','message':{'content':[{'type':'tool_use','id':'read','name':'read_file',
                   'input':{'file_path':'.review-input/plan.json'}}]}},
                {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'read',
                   'is_error':False,'content':'Read candidate'}]}}]
        if extra:
            rows.extend(extra)
        rows.append({'type':'result','subtype':'success','is_error':False,'result':json.dumps(report)})
        events.write_text('\n'.join(map(json.dumps, rows))+'\n')
        result = parse_events(events)
        result['returncode'] = 0
        return result

    def fake(self, root, cwd, prompt, role, name, model=None):
        with self.lock:
            self.counts[model] = self.counts.get(model, 0) + 1
        return self.result(root, model)

    def pair(self):
        return review_cycle.review_pair(self.runner, self.candidate, 'test plan', self.runner.cfg['requirements'])

    def test_parallel_reviews_cached_on_restart_and_bound_by_controller(self):
        barrier = threading.Barrier(2)
        def simultaneous(*args, **kwargs):
            barrier.wait(timeout=5)
            return self.fake(*args, **kwargs)
        with patch.object(self.runner, 'session', side_effect=simultaneous):
            report = self.pair()
        self.assertEqual(report['verdict'], 'pass')
        self.assertEqual(report['candidate_sha256'], digest(self.candidate))
        self.assertEqual(self.counts, {m:1 for m in review_cycle.MODELS})
        self.runner = Runner(self.fixture.config)
        with patch.object(self.runner, 'session', side_effect=AssertionError('no repeated models')):
            self.assertEqual(self.pair(), report)
        ledger = json.loads((self.runner.root/'review-ledger.json').read_text())
        self.assertEqual(len(ledger['rounds']), 1)

    def test_only_failed_model_retries_and_budget_survives_restart(self):
        bad = review_cycle.MODELS[1]
        def mixed(root, cwd, prompt, role, name, model=None):
            self.fake(root,cwd,prompt,role,name,model)
            return self.result(root,model,model_reported='unexpected' if model == bad else model)
        with patch.object(self.runner, 'session', side_effect=mixed):
            with self.assertRaisesRegex(Blocked, 'budget exhausted'):
                self.pair()
        self.assertEqual(self.counts, {review_cycle.MODELS[0]:1,bad:2})
        self.runner = Runner(self.fixture.config)
        with patch.object(self.runner,'session',side_effect=AssertionError('no fresh budget')):
            with self.assertRaisesRegex(Blocked,'budget exhausted'):
                self.pair()

    def test_json_error_repaired_in_fresh_copy_without_repeating_peer(self):
        roots = []
        def malformed(root,cwd,prompt,role,name,model=None):
            result=self.fake(root,cwd,prompt,role,name,model)
            if model == review_cycle.MODELS[0]:
                roots.append(root)
                if self.counts[model] == 1:
                    result['text']='not JSON'
                else:
                    self.assertIn('Previous review draft',prompt)
            return result
        with patch.object(self.runner,'session',side_effect=malformed):
            self.assertEqual(self.pair()['verdict'],'pass')
        self.assertEqual(len(set(roots)),2)
        self.assertEqual(self.counts[review_cycle.MODELS[1]],1)

    def test_empty_external_search_is_recoverable_nonempty_is_not(self):
        root=self.fixture.root/'review'
        root.mkdir()
        sha=plan_review.prepare(root,self.runner.root/'input',self.candidate,root/'missing')
        def extra(text,error=False):
            return [{'type':'assistant','message':{'content':[{'type':'tool_use','id':'glob',
                'name':'glob','input':{'path':'../other-run','pattern':'**/*.java'}}]}},
                {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'glob',
                    'is_error':error,'content':text}]}}]
        result=self.result(root,review_cycle.MODELS[0],extra=extra('No files found'))
        report,evidence=review_cycle.validate_result(root,result,sha,self.runner.cfg['requirements'])
        self.assertEqual(report['verdict'],'pass')
        self.assertEqual(len(evidence['notices']),1)
        for body,error in [('foreign/file.java',False),('Permission denied',True),('partial data with error',True)]:
            with self.subTest(body=body), self.assertRaisesRegex(plan_review.ProtocolError,'outside'):
                review_cycle.validate_result(root,self.result(root,review_cycle.MODELS[0],extra=extra(body,error)),sha,self.runner.cfg['requirements'])
        result['cancelled']=True
        result['ok']=False
        with self.assertRaisesRegex(plan_review.ProtocolError,'transport'):
            review_cycle.validate_result(root,result,sha,self.runner.cfg['requirements'])

    def test_changed_cache_is_not_accepted_or_silently_rerun(self):
        with patch.object(self.runner,'session',side_effect=self.fake):self.pair()
        record=next((self.runner.root/'review-cycles').glob('*/*/attempt-1.json'))
        events=Path(json.loads(record.read_text())['result']['events_path'])
        events.write_text(events.read_text()+'\n')
        with patch.object(self.runner,'session',side_effect=AssertionError('no rerun')):
            with self.assertRaisesRegex(Blocked,'evidence changed'):self.pair()

    def test_new_candidate_does_not_reuse_old_pass(self):
        with patch.object(self.runner,'session',side_effect=self.fake):self.pair()
        value=json.loads(self.candidate.read_text());value['tasks'][0]['prompt']+=' More detail.';save(self.candidate,value)
        with patch.object(self.runner,'session',side_effect=self.fake):self.pair()
        self.assertEqual(self.counts,{m:2 for m in review_cycle.MODELS})

    def test_previous_findings_require_explicit_evidence_of_resolution(self):
        root=self.fixture.root/'review';root.mkdir()
        sha=plan_review.prepare(root,self.runner.root/'input',self.candidate,root/'missing')
        prior=[{'id':'issue','text':'missing test'}]
        report={'verdict':'pass','findings':[],'coverage':{r:'evidence' for r in self.runner.cfg['requirements']}}
        with self.assertRaisesRegex(plan_review.ProtocolError,'prior finding'):
            review_cycle.validate_result(root,self.result(root,'model',report=report),sha,self.runner.cfg['requirements'],prior=prior)
        report['prior_findings']={'issue':{'status':'resolved','evidence':'task be checks now run the missing behavior test'}}
        self.assertEqual(review_cycle.validate_result(root,self.result(root,'model',report=report),sha,self.runner.cfg['requirements'],prior=prior)[0]['verdict'],'pass')

    def test_changes_go_to_planner_but_unchanged_output_has_finite_budget(self):
        self.runner.state['preflight']={'config_sha256':digest(self.fixture.config),'passed':True}
        report={'verdict':'changes','findings':['task be lacks a behavior test'], 'coverage':{'REQ-BE':'gap','REQ-FE':'covered'}}
        with patch('review_cycle.review_pair',return_value=report), patch.object(self.runner,'session',return_value={'ok':True,'text':json.dumps(self.fixture.plan)}) as planner:
            with self.assertRaises(Blocked):self.runner.plan()
            count=planner.call_count
            self.assertLessEqual(count,review_cycle.MAX_REPAIRS)
            self.assertIn('task be lacks a behavior test',planner.call_args.args[2])
        resumed=Runner(self.fixture.config)
        with patch.object(resumed,'session',side_effect=AssertionError('no budget reset')):
            with self.assertRaisesRegex(Blocked,'budget exhausted'):resumed.plan()

    def test_external_blocker_does_not_trigger_plan_rewrites(self):
        self.runner.state['preflight']={'config_sha256':digest(self.fixture.config),'passed':True}
        report={'verdict':'blocked','findings':['Business choice required'], 'coverage':{'REQ-BE':'gap','REQ-FE':'covered'}}
        with patch('review_cycle.review_pair',return_value=report), patch.object(self.runner,'session',return_value={'ok':True,'text':json.dumps(self.fixture.plan)}) as planner:
            with self.assertRaisesRegex(Blocked,'analyst decision'):self.runner.plan()
            self.assertEqual(planner.call_count,1)
        with patch.object(self.runner,'session',side_effect=AssertionError('no model calls')):
            with self.assertRaisesRegex(Blocked,'analyst decision'):self.runner.plan()

    def test_input_changed_during_review_is_not_accepted(self):
        changed = threading.Event()
        def mutate(root,cwd,prompt,role,name,model=None):
            result = self.fake(root,cwd,prompt,role,name,model)
            if model == review_cycle.MODELS[0]:
                (self.runner.root/'decision.md').write_text('New decision')
                changed.set()
            else:
                changed.wait(timeout=5)
            return result
        with patch.object(self.runner,'session',side_effect=mutate):
            with self.assertRaisesRegex(Blocked,'input changed'):self.pair()

    def test_legacy_findings_are_carried_to_both_models(self):
        prior = json.loads(self.candidate.read_text())
        old_path=self.runner.root/'plan-candidate-legacy.json'
        save(old_path,prior)
        save(self.runner.root/'plan-rejected.json',{'candidate_sha256':digest(old_path),'plan':prior,
            'review':{'verdict':'changes','findings':['task be needs explicit check']}})
        prior['tasks'][0]['prompt']+=' Explicit check added.'
        save(self.candidate,prior)
        seen=[]
        def answer(root,cwd,prompt,role,name,model=None):
            issues=json.loads((root/'.review-input/previous-findings.json').read_text())
            seen.append(issues)
            report={'verdict':'pass','findings':[], 'coverage':{r:'evidence' for r in self.runner.cfg['requirements']},
                'prior_findings':{i['id']:{'status':'resolved','evidence':'task be checks now cover behavior'} for i in issues}}
            return self.result(root,model,report=report)
        with patch.object(self.runner,'session',side_effect=answer):self.pair()
        self.assertEqual(len(seen),2)
        self.assertTrue(all(x[0]['text']=='task be needs explicit check' for x in seen))
