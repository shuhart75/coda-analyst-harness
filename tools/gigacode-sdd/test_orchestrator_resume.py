"""Real subprocess regressions for stop, operator changes and acceptance lifecycle."""
import json
from pathlib import Path
import threading
import time
import unittest

from controller import Runner, Blocked, Interrupted, digest, save
from workspace import git, snapshot
import test_controller as fixtures


class OrchestratorResumeTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.ControllerE2E('test_init_preserves_source_and_rejects_changed_packet')
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.fx.fake.write_text(fixtures.FAKE.replace('time.sleep(.2)',
            "time.sleep(float(os.environ.get('LIFECYCLE_DELAY', '.2')))").replace(
                "dest.write_text('2' if dest.read_text() == '1' else '1')",
                "dest.write_text('2')"), encoding='utf-8')
        self.runner = self.fx.runner
        # The lifecycle starts with an already independently accepted fixture plan.
        # No real models, source checkout changes or permission probes are needed.
        with self.runner.locked():
            save(self.runner.root / 'plan.json', self.fx.plan)
            self.runner.state.update(status='planned',
                preflight={'passed': True, 'config_sha256': digest(self.fx.config)},
                plan_sha256=digest(self.runner.root / 'plan.json'),
                tasks={t['id']: {'status':'pending', 'attempts':0} for t in self.fx.plan['tasks']})
            self.runner.persist()

    def trace(self):
        if not self.fx.trace.exists():
            return []
        result=[]
        for line in self.fx.trace.read_text(encoding='utf-8').splitlines():
            try: result.append(json.loads(line))
            except ValueError: pass  # A concurrent process may still finish its last line.
        return result

    def complete(self):
        with self.runner.locked():
            self.runner.run()
        self.assertEqual(self.runner.state['status'], 'complete')
        self.assertTrue(json.loads((self.runner.root/'readiness.json').read_text())['required_complete'])

    def stop_on_execution(self, runner, task, offset=0):
        runner.env['LIFECYCLE_DELAY'] = '1'
        reached = threading.Event()
        def stop():
            until=time.monotonic()+20
            while time.monotonic()<until:
                if any(e['kind']=='start' and (task is None or e['task']==task) for e in self.trace()[offset:]):
                    reached.set(); runner.stop.set(); return
                time.sleep(.01)
        thread=threading.Thread(target=stop, daemon=True)
        thread.start()
        try:
            with runner.locked():
                try: runner.run()
                except Interrupted: pass
        finally:
            thread.join(timeout=21)
        self.assertTrue(reached.is_set(), 'The real fake CLI never reached the expected execution')
        self.assertEqual(runner.state['status'], 'interrupted')

    def test_stop_then_plain_resume_keeps_wip_without_retry_blocked(self):
        self.stop_on_execution(self.runner, None)
        workspaces={key:value['workspace'] for key,value in self.runner.state['tasks'].items()
                    if value.get('workspace')}
        self.assertTrue(workspaces)
        self.assertTrue(all(Path(path).is_dir() for path in workspaces.values()))
        resumed=Runner(self.fx.config)
        with resumed.locked(): resumed.run()
        self.assertEqual(resumed.state['status'],'complete')
        # Alternate winners may be chosen, but no interrupted workspace is deleted.
        self.assertTrue(all(Path(path).is_dir() for path in workspaces.values()))
        self.assertEqual(snapshot(self.fx.source),self.fx.before)

    def test_new_decision_blocks_export_and_revalidates_only_integration(self):
        self.complete()
        prior=len(self.trace())
        completed_tasks={k:v['integrated_head'] for k,v in self.runner.state['tasks'].items()}
        (self.runner.root/'decision.md').write_text('Reuse the existing isolated stand; retain all requirements.', encoding='utf-8')
        resumed=Runner(self.fx.config)
        with resumed.locked():
            with self.assertRaises(Blocked): resumed.export(self.fx.root/'premature.patch')
            self.assertFalse((self.fx.root/'premature.patch').exists())
            self.assertIn('needs_instruction_review',resumed.state)
            resumed.run()
        self.assertEqual(resumed.state['status'],'complete')
        self.assertNotIn('needs_instruction_review',resumed.state)
        starts=[e['task'] for e in self.trace()[prior:] if e['kind']=='start']
        self.assertTrue(starts)
        self.assertEqual(set(starts),{'integration'})
        self.assertEqual(completed_tasks,{k:v['integrated_head'] for k,v in resumed.state['tasks'].items()})
        self.assertEqual(resumed.state['instruction_verification']['revision'],resumed.state['operator_revision']['id'])

    def test_legacy_complete_is_revalidated_before_binding_new_readiness(self):
        self.complete()
        prior=len(self.trace())
        for key in ('operator_revision','instruction_verification','needs_instruction_review'):
            self.runner.state.pop(key,None)
        self.runner.state['integration'].pop('operator_revision',None)
        self.runner.persist()
        (self.runner.root/'decision.md').write_text('A clarification with no historical binding.', encoding='utf-8')
        resumed=Runner(self.fx.config)
        with resumed.locked():
            with self.assertRaises(Blocked): resumed.export(self.fx.root/'legacy-unverified.patch')
            resumed.run()
        starts=[e['task'] for e in self.trace()[prior:] if e['kind']=='start']
        self.assertTrue(starts, 'An old receipt must not become evidence for newly bound instructions')
        self.assertEqual(set(starts),{'integration'})
        self.assertEqual(resumed.state['status'],'complete')

    def test_stop_during_instruction_revalidation_reuses_its_working_copy(self):
        self.complete()
        (self.runner.root/'decision.md').write_text('Verify the existing implementation under this clarification.', encoding='utf-8')
        offset=len(self.trace())
        resumed=Runner(self.fx.config)
        self.stop_on_execution(resumed,'integration',offset)
        rec=resumed.state['integration']
        work=rec['workspace']
        revision=rec['operator_revision']
        history_length=len(resumed.state.get('integration_history',[]))
        self.assertTrue(Path(work).is_dir())
        preserved=[json.loads(p.read_text()) for p in (resumed.root/'execution-cycles').glob('*/*/candidate.json')]
        current_scope=next(c['scope'] for c in preserved if c['workspace']==work)
        preserved_roots={c['workspace'] for c in preserved if c['scope']==current_scope}
        next_offset=len(self.trace())
        again=Runner(self.fx.config)
        with again.locked(): again.run()
        self.assertEqual(again.state['status'],'complete')
        restarted_roots={e['cwd'] for e in self.trace()[next_offset:] if e['kind']=='start'}
        self.assertTrue(restarted_roots <= preserved_roots)
        self.assertEqual(len(again.state.get('integration_history',[])),history_length)
        self.assertEqual(again.state['integration']['operator_revision'],revision)
        self.assertTrue(Path(work).is_dir())
        # The existing execution portfolio stays keyed by its original workspace.
        cycles=[json.loads(p.read_text()) for p in (again.root/'execution-cycles').glob('*/*/candidate.json')]
        self.assertTrue(any(c['workspace']==work for c in cycles))
        self.assertEqual(snapshot(self.fx.source),self.fx.before)


if __name__ == '__main__': unittest.main()
