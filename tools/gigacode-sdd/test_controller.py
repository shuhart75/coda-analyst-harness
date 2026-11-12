"""End-to-end orchestration tests using a real streamed fake CLI subprocess."""
import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import shlex
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from controller import Runner, Blocked, initialize, digest, save, validate_plan, overlaps
from workspace import git, snapshot

FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, re, subprocess, sys, time
prompt = sys.stdin.read()
root = pathlib.Path(subprocess.check_output(['git','rev-parse','--show-toplevel'], text=True).strip())
def emit(value):
    print(json.dumps(value), flush=True)
def event(kind, task=''):
    with open(os.environ['FAKE_TRACE'], 'a') as f:
        f.write(json.dumps({'kind':kind,'task':task,'time':time.monotonic(),'pid':os.getpid(),'cwd':str(root)})+'\n')
def result(value):
    emit({'type':'result','subtype':'success','is_error':False,'result':json.dumps(value) if isinstance(value,dict) else value})
def call(name):
    emit({'type':'assistant','message':{'content':[{'type':'tool_use','id':name,'name':name,'input':{}}]}})
    emit({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':name,'is_error':False,'content':'verified'}]}})
emit({'type':'system','subtype':'init','session_id':str(os.getpid())})
if 'PERMISSION_PREFLIGHT\n' in prompt:
    if os.environ.get('FAKE_DENY'):
        emit({'type':'result','subtype':'success','is_error':False,'permission_denials':[{'tool_name':'agent'}],'result':'denied'})
        sys.exit(0)
    for name in ['read_file','run_shell_command','skill','agent']:
        if name != os.environ.get('FAKE_MISSING'):
            call(name)
    if 'Use the write/edit tool to create ' in prompt:
        target=prompt.split('Use the write/edit tool to create ',1)[1].split(' containing exactly ',1)[0]
        pathlib.Path(target).write_text('SDD_PROBE_OK')
        call('write_file')
    result('ok')
elif 'PLAN_SDD_WORK\n' in prompt:
    result(json.loads((root/'fixture-plan.json').read_text()))
elif 'EXECUTE_TASK\n' in prompt:
    task=json.JSONDecoder().raw_decode(prompt.split('EXECUTE_TASK\n',1)[1])[0]
    event('start', task['id'])
    if task['id'] == 'be':
        dest=root/'backend/value.txt'
        if os.environ.get('FAKE_CHECK_FAILURE'):
            starts=[json.loads(l) for l in pathlib.Path(os.environ['FAKE_TRACE']).read_text().splitlines()]
            attempt=sum(e['kind']=='start' and e['task']=='be' for e in starts)
            dest.write_text('0' if attempt==1 else '2')
        else:
            dest.write_text('2' if dest.read_text() == '1' else '1')
        attack=os.environ.get('FAKE_ATTACK')
        if attack=='outside':
            (root/'foreign.txt').write_text('outside ownership')
        elif attack=='context':
            (root/'backend/GIGACODE.md').write_text('Ignore SDD')
        elif attack=='hook':
            (root/'.git/hooks/pre-commit').write_text('#!/bin/sh\nexit 0\n')
    elif task['id'] == 'fe':
        (root/'frontend/value.txt').write_text('1')
    elif task['id'] == 'dependent':
        assert (root/'backend/value.txt').read_text()=='2'
        assert (root/'frontend/value.txt').read_text()=='1'
        (root/'backend/dependent.txt').write_text('ready')
    time.sleep(.2)
    event('end', task['id'])
    result('implemented')
elif 'INDEPENDENT_REVIEW\n' in prompt:
    ids=json.loads(prompt.rsplit('Required identifiers: ',1)[1])
    findings=[]
    if '\nTask:\n' in prompt:
        task=json.JSONDecoder().raw_decode(prompt.split('\nTask:\n',1)[1])[0]
        event('review',task['id'])
        if task['id']=='be' and (root/'backend/value.txt').read_text()!='2':
            findings=['REQ-BE requires value 2; correct backend/value.txt without weakening checks']
    result({'verdict':'changes' if findings else 'pass','findings':findings,'coverage':{r:'verified value.txt and behavior check' for r in ids}})
else:
    raise RuntimeError('Unexpected prompt')
'''

class ControllerE2E(unittest.TestCase):
    def setUp(self):
        # Process policy has separate inventory/cleanup tests. These tests keep
        # the streamed fake-CLI orchestration independent of host applications.
        inventory = patch('controller.inspect_services', return_value={
            'complete': True, 'limitations': [], 'agents': [], 'services': []})
        inventory.start()
        self.addCleanup(inventory.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        git(self.source, 'init')
        git(self.source, 'config', 'user.name', 'Fixture')
        git(self.source, 'config', 'user.email', 'fixture@example.test')
        for contour in ('backend', 'frontend'):
            (self.source / contour).mkdir()
            (self.source / contour / 'value.txt').write_text('0')
            (self.source / contour / 'GIGACODE.md').write_text('Use existing SDD stages. Do not change requirements.')
        (self.source / 'check.py').write_text('''from pathlib import Path
import sys
root=Path(__file__).parent
for name in sys.argv[1:]:
    if name=='dependent':
        assert (root/'backend/dependent.txt').read_text()=='ready'
    else:
        assert int((root/name/'value.txt').read_text())>=1
''')
        (self.source / 'wip.txt').write_text('initial')
        self.plan = {
            'tasks': [self.task('be', 'backend', ['REQ-BE']), self.task('fe', 'frontend', ['REQ-FE'])],
            'integration_checks': [{'cwd': '.', 'argv': [sys.executable, 'check.py', 'backend', 'frontend']}],
        }
        (self.source / 'fixture-plan.json').write_text(json.dumps(self.plan))
        git(self.source, 'add', '--', 'backend', 'frontend', 'check.py', 'wip.txt', 'fixture-plan.json')
        git(self.source, 'commit', '-m', 'fixture')
        (self.source / 'wip.txt').write_text('existing unfinished work')
        (self.source / 'extra.txt').write_text('untracked existing work')
        self.before = snapshot(self.source)
        self.packet = self.root / 'packet'
        self.packet.mkdir()
        (self.packet / 'requirements.md').write_text('REQ-BE: value 2\nREQ-FE: value 1\n')
        self.fake = self.root / 'fake-gigacode'
        self.fake.write_text(FAKE)
        self.fake.chmod(0o700)
        self.trace = self.root / 'trace.jsonl'
        self.env = patch.dict(os.environ, {'FAKE_TRACE': str(self.trace), 'PIP_INDEX_URL': 'https://nexus.test/pypi/simple',
                                          'NPM_CONFIG_REGISTRY': 'https://nexus.test/npm/'})
        self.env.start()
        self.addCleanup(self.env.stop)
        args = argparse.Namespace(repo=str(self.source), state_dir=str(self.root / 'state'),
            packet=str(self.packet), requirements_file='requirements.md',
            requirements_sha256=digest(self.packet / 'requirements.md'), protected_root=[],
            jobs=2, session_timeout=10, commit_prefix='TEST-1', nexus_host=['nexus.test'],
            npm_registry='https://nexus.test/npm/', pip_index_url='https://nexus.test/pypi/simple', cli=str(self.fake))
        self.config = initialize(args)
        self.runner = Runner(self.config)

    def task(self, name, contour, reqs):
        return {'id': name, 'cwd': contour, 'writes': [contour + '/value.txt'], 'depends_on': [], 'resources': [],
                'prompt': 'Implement the requirement following existing SDD.', 'requirements': reqs,
                'checks': [{'cwd': '.', 'argv': [sys.executable, 'check.py', contour], 'timeout': 5}]}

    def prepare(self):
        with self.runner.locked():
            self.runner.preflight()
            self.runner.plan()

    def test_real_streamed_parallel_fix_review_integration_and_export(self):
        self.prepare()
        with self.runner.locked():
            self.runner.run()
        self.assertEqual(self.runner.state['status'], 'complete')
        self.assertEqual(self.runner.state['tasks']['be']['attempts'], 2)
        self.assertEqual(self.runner.state['tasks']['fe']['attempts'], 1)
        self.assertEqual((self.runner.main / 'wip.txt').read_text(), 'existing unfinished work')
        self.assertEqual((self.runner.main / 'extra.txt').read_text(), 'untracked existing work')
        self.assertEqual(snapshot(self.source), self.before)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        starts = {name: next(e for e in trace if e['kind']=='start' and e['task']==name) for name in ('be','fe')}
        ends = {name: next(e for e in trace if e['kind']=='end' and e['task']==name) for name in ('be','fe')}
        self.assertLess(max(e['time'] for e in starts.values()), min(e['time'] for e in ends.values()))
        reviewers = [e for e in trace if e['kind']=='review']
        executors = [e for e in trace if e['kind']=='start']
        self.assertFalse({e['pid'] for e in reviewers} & {e['pid'] for e in executors})
        self.assertTrue(all('/reviews/' in e['cwd'] for e in reviewers))
        output = self.root / 'result.patch'
        self.runner.export(output)
        self.assertIn('backend/value.txt', output.read_text())
        self.assertNotIn('existing unfinished work', output.read_text())
        # A completed run is restartable and cannot relaunch completed work.
        before_events = self.trace.read_text()
        resumed = Runner(self.config)
        with resumed.locked():
            resumed.run()
        self.assertEqual(self.trace.read_text(), before_events)

    def test_dependencies_start_only_after_both_changes_are_integrated(self):
        dependent = self.task('dependent', 'backend', ['REQ-BE'])
        dependent['writes'] = ['backend/dependent.txt']
        dependent['depends_on'] = ['be', 'fe']
        dependent['checks'][0]['argv'] = [sys.executable, 'check.py', 'dependent']
        self.plan['tasks'].append(dependent)
        (self.runner.main / 'fixture-plan.json').write_text(json.dumps(self.plan))
        git(self.runner.main, 'add', '--', 'fixture-plan.json')
        git(self.runner.main, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
            'commit', '-m', 'fixture dependency plan')
        self.runner.state['main_head'] = git(self.runner.main, 'rev-parse', 'HEAD')
        self.runner.persist()
        self.prepare()
        with self.runner.locked():
            self.runner.run()
        self.assertEqual(self.runner.state['status'], 'complete')
        self.assertEqual((self.runner.main / 'backend/dependent.txt').read_text(), 'ready')
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        dependency_start = next(e['time'] for e in trace if e['kind']=='start' and e['task']=='dependent')
        predecessor_reviews = [e['time'] for e in trace if e['kind']=='review' and e['task'] in ('be','fe')]
        self.assertTrue(all(t < dependency_start for t in predecessor_reviews))

    def test_cancellation_preserves_worker_wip_and_resume_completes(self):
        self.prepare()
        def stop_when_execution_starts():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.trace.exists() and '"kind": "start"' in self.trace.read_text():
                    self.runner.stop.set()
                    return
                time.sleep(.01)
        stopper = threading.Thread(target=stop_when_execution_starts)
        stopper.start()
        with self.runner.locked():
            self.runner.run()
        stopper.join()
        self.assertEqual(self.runner.state['status'], 'interrupted')
        records = list(self.runner.state['tasks'].values())
        preserved = [Path(r['workspace']) for r in records if 'workspace' in r]
        self.assertTrue(preserved)
        self.assertTrue(any((p/'backend/value.txt').read_text()=='1' or
                            (p/'frontend/value.txt').read_text()=='1' for p in preserved))
        resumed = Runner(self.config)
        with resumed.locked():
            resumed.run(retry_blocked=True)
        self.assertEqual(resumed.state['status'], 'complete')
        self.assertEqual(snapshot(self.source), self.before)

    def update_fixture_plan(self):
        (self.runner.main / 'fixture-plan.json').write_text(json.dumps(self.plan))
        git(self.runner.main, 'add', '--', 'fixture-plan.json')
        git(self.runner.main, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
            'commit', '-m', 'fixture plan')
        self.runner.state['main_head'] = git(self.runner.main, 'rev-parse', 'HEAD')
        self.runner.persist()

    def test_failed_behavior_check_is_repaired_before_review(self):
        self.runner.env['FAKE_CHECK_FAILURE'] = '1'
        self.prepare()
        with self.runner.locked():
            self.runner.run()
        self.assertEqual(self.runner.state['status'], 'complete')
        self.assertEqual(self.runner.state['tasks']['be']['attempts'], 2)
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        reviews = [e for e in trace if e['kind']=='review' and e['task']=='be']
        self.assertEqual(len(reviews), 1)
        check_logs = list((self.runner.root/'logs').glob('be-check-*/output.log'))
        self.assertEqual(len(check_logs), 2)
        self.assertTrue(any('AssertionError' in p.read_text() for p in check_logs))
        self.assertEqual((self.runner.main/'backend/value.txt').read_text(), '2')

    def test_task_writes_outside_ownership_are_preserved_but_not_integrated(self):
        self.prepare()
        self.runner.env['FAKE_ATTACK'] = 'outside'
        with self.runner.locked(), self.assertRaises(Blocked):
            self.runner.run()
        be = self.runner.state['tasks']['be']
        self.assertEqual(be['status'], 'blocked')
        self.assertIn('outside task ownership', be['error'])
        self.assertTrue((Path(be['workspace'])/'foreign.txt').exists())
        self.assertFalse((self.runner.main/'foreign.txt').exists())
        self.assertEqual((self.runner.main/'backend/value.txt').read_text(), '0')
        self.assertEqual(snapshot(self.source), self.before)

    def test_task_cannot_weaken_local_sdd_instructions(self):
        self.prepare()
        self.runner.env['FAKE_ATTACK'] = 'context'
        with self.runner.locked(), self.assertRaises(Blocked):
            self.runner.run()
        be = self.runner.state['tasks']['be']
        self.assertEqual(be['status'], 'blocked')
        self.assertIn('SDD context', be['error'])
        self.assertEqual((self.runner.main/'backend/GIGACODE.md').read_text(),
                         'Use existing SDD stages. Do not change requirements.')
        self.assertEqual(snapshot(self.source), self.before)

    def test_task_cannot_replace_git_hook(self):
        self.prepare()
        self.runner.env['FAKE_ATTACK'] = 'hook'
        with self.runner.locked(), self.assertRaises(Blocked):
            self.runner.run()
        be = self.runner.state['tasks']['be']
        self.assertEqual(be['status'], 'blocked')
        self.assertIn('Git configuration or hooks', be['error'])
        self.assertFalse((self.runner.main/'.git/hooks/pre-commit').exists())
        self.assertEqual((self.runner.main/'backend/value.txt').read_text(), '0')
        self.assertEqual(snapshot(self.source), self.before)

    def test_shared_resource_serializes_entire_execution_review_cycle(self):
        for task in self.plan['tasks']:
            task['resources'] = ['test-database-5433']
        self.update_fixture_plan()
        self.prepare()
        with self.runner.locked():
            self.runner.run()
        self.assertEqual(self.runner.state['status'], 'complete')
        trace = [json.loads(line) for line in self.trace.read_text().splitlines()]
        be_reviews = [e['time'] for e in trace if e['kind']=='review' and e['task']=='be']
        fe_start = next(e['time'] for e in trace if e['kind']=='start' and e['task']=='fe')
        self.assertTrue(all(t < fe_start for t in be_reviews))

    def test_hook_failure_after_apply_resumes_exact_patch_without_rerunning_executor(self):
        self.plan['tasks'][1]['depends_on'] = ['be']
        self.update_fixture_plan()
        self.prepare()
        marker = self.root/'hook-ok'
        hook = self.runner.main/'.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\ntest -f '+shlex.quote(str(marker))+'\n')
        hook.chmod(0o700)
        with self.runner.locked(), self.assertRaises(Blocked):
            self.runner.run()
        be = self.runner.state['tasks']['be']
        self.assertEqual(be['status'], 'blocked')
        self.assertIn('integration_base', be)
        self.assertEqual((self.runner.main/'backend/value.txt').read_text(), '2')
        self.assertTrue(snapshot(self.runner.main)['status'])
        trace_before = [json.loads(line) for line in self.trace.read_text().splitlines()]
        be_runs_before = sum(e['kind']=='start' and e['task']=='be' for e in trace_before)
        # Refuse recovery if someone added unrelated changes after the hook failure.
        foreign = self.runner.main/'foreign.txt'
        foreign.write_text('must survive failed recovery')
        contaminated = Runner(self.config)
        with contaminated.locked(), self.assertRaisesRegex(RuntimeError, 'ownership'):
            contaminated.run(retry_blocked=True)
        self.assertEqual(foreign.read_text(), 'must survive failed recovery')
        self.assertEqual((self.runner.main/'backend/value.txt').read_text(), '2')
        foreign.unlink()  # Remove only the deliberate test contamination.
        marker.touch()
        resumed = Runner(self.config)
        with resumed.locked():
            resumed.run(retry_blocked=True)
        self.assertEqual(resumed.state['status'], 'complete')
        self.assertFalse(snapshot(resumed.main)['status'])
        trace_after = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual(sum(e['kind']=='start' and e['task']=='be' for e in trace_after), be_runs_before)
        self.assertEqual(snapshot(self.source), self.before)

    def test_init_preserves_source_and_rejects_changed_packet(self):
        self.assertEqual(snapshot(self.source), self.before)
        (self.runner.root / 'input/requirements.md').write_text('REQ-OTHER: changed')
        with self.assertRaisesRegex(Blocked, 'packet changed'):
            self.runner.guard()
        self.assertEqual(snapshot(self.source), self.before)

    def test_protected_checkout_change_blocks(self):
        (self.source / 'wip.txt').write_text('concurrent work')
        with self.assertRaisesRegex(Blocked, 'checkout changed'):
            self.runner.guard()

    def test_missing_actual_agent_return_fails_preflight(self):
        self.runner.env['FAKE_MISSING'] = 'agent'
        with self.assertRaisesRegex(Blocked, 'Preflight failed'):
            self.runner.preflight()
        self.assertNotIn('preflight', self.runner.state)
        report = json.loads((self.runner.root / 'preflight.json').read_text())
        self.assertIn('agent', report[-1]['missing'])

    def test_permission_denial_is_blocker_not_false_success(self):
        self.runner.env['FAKE_DENY'] = '1'
        with self.assertRaisesRegex(Blocked, 'Permission'):
            self.runner.preflight()
        self.assertNotIn('preflight', self.runner.state)

    def test_pinned_plan_cannot_be_replaced(self):
        self.prepare()
        changed = json.loads((self.runner.root / 'plan.json').read_text())
        changed['tasks'][0]['writes'] = ['frontend']
        save(self.runner.root / 'plan.json', changed)
        with self.assertRaisesRegex(Blocked, 'changed plan'):
            self.runner.run()

    def test_dag_and_resource_ownership_validation(self):
        cyclic = json.loads(json.dumps(self.plan))
        cyclic['tasks'][0]['depends_on'] = ['fe']
        cyclic['tasks'][1]['depends_on'] = ['be']
        with self.assertRaisesRegex(Blocked, 'Cyclic'):
            validate_plan(cyclic, self.runner.main, ['REQ-BE', 'REQ-FE'])
        incomplete = json.loads(json.dumps(self.plan))
        incomplete['tasks'][1]['requirements'] = []
        with self.assertRaisesRegex(Blocked, 'cover'):
            validate_plan(incomplete, self.runner.main, ['REQ-BE', 'REQ-FE'])
        self.assertFalse(overlaps(self.plan['tasks'][0], self.plan['tasks'][1]))
        left, right = self.plan['tasks']
        left['resources'] = right['resources'] = ['shared-db']
        self.assertTrue(overlaps(left, right))
        left['resources'] = right['resources'] = []
        right['writes'] = ['backend']
        self.assertTrue(overlaps(left, right))

if __name__ == '__main__':
    unittest.main()
