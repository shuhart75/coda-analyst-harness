import json
import unittest
from types import SimpleNamespace
from pathlib import Path
import tempfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from gantt_releases import apply_task_releases, milestone_overrides, rename_milestones


class GanttReleaseTests(unittest.TestCase):
    def test_partition_members_keep_release_and_remainder_stays_unknown(self):
        tasks = {k: SimpleNamespace(kind='real', release_label='') for k in ('a','b','qa','rest')}
        apply_task_releases(tasks,[{'task_id':'qa','members':['a'],'release':'REL-1'},
                                  {'task_id':'rest','members':['b'],'release':None}])
        self.assertEqual(tasks['a'].release_label, 'REL-1')
        self.assertEqual(tasks['qa'].release_label, 'REL-1')
        self.assertEqual(tasks['b'].release_label, 'релиз не уточнён')
        self.assertEqual(tasks['rest'].release_label, 'релиз не уточнён')

    def test_planned_and_deployed_milestones_require_exact_dates_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'releases.json'
            entries=[{'alias':'REL','release':'REL-1','state':'deployed','environment':'ПРОМ',
                      'date':'2026-10-20','analyst_confirmed':True,'evidence':'Analyst confirmed 20 October'},
                     {'alias':'NEXT','release':'REL-2','state':'planned','date':'2026-10-30',
                      'analyst_confirmed':True,'evidence':'Approved release plan'}]
            p.write_text(json.dumps({'schema_version':1,'releases':entries}))
            overrides=milestone_overrides(Path(tmp))
            seen=set()
            result=rename_milestones('[Old] as [REL] happens at 2026/10/20\n[Next] as [NEXT] happens at 2026/10/30\n', overrides, seen)
            self.assertIn('Релиз REL-1 · ПРОМ · факт',result)
            self.assertIn('Релиз REL-2 · план',result)
            self.assertEqual(seen,{'REL','NEXT'})
            with self.assertRaisesRegex(ValueError,'date differs'):
                rename_milestones('[Old] as [REL] happens at 2026/10/16',overrides,set())
            entries[0]['analyst_confirmed']=False
            p.write_text(json.dumps({'schema_version':1,'releases':entries}))
            with self.assertRaises(ValueError):milestone_overrides(Path(tmp))

    def test_independent_markers_and_stale_evidence(self):
        import hashlib
        from gantt_releases import apply_task_markers
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'execution').mkdir()
            registry = root/'execution/tasks.md'
            registry.write_text('fixture')
            tasks = {k: SimpleNamespace(kind='real', release_label='') for k in ('a','b','qa')}
            groups = [{'task_id':'qa','members':['a','b'],'release':'REL-1'}]
            proof = {'confirmed':True, 'source':'review.json', 'evidence':'Verified task history', 'date':'2026-10-15'}
            data = {'schema_version':1,'sources':{'execution/tasks.md':hashlib.sha256(registry.read_bytes()).hexdigest()},
                    'tasks':[{'task_id':'a','qa':proof}, {'task_id':'b','release':'REL-1','membership':proof,
                    'production':dict(proof, analyst_confirmed=True)}]}
            path = root/'execution/task-markers.json'
            path.write_text(json.dumps(data))
            apply_task_releases(tasks, groups)
            apply_task_markers(tasks, root)
            self.assertEqual(tasks['a'].release_label, 'REL-1 · QA ✓')
            self.assertEqual(tasks['b'].release_label, 'REL-1 · ПРОМ ✓')
            self.assertEqual(tasks['qa'].release_label, 'REL-1')
            data['tasks'][1]['production']['analyst_confirmed'] = False
            path.write_text(json.dumps(data))
            apply_task_releases(tasks, groups)
            with self.assertRaisesRegex(ValueError, 'analyst confirmation'):
                apply_task_markers(tasks, root)
            registry.write_text('changed membership')
            with self.assertRaisesRegex(ValueError, 'sources changed'):
                apply_task_markers(tasks, root)

    def test_marker_sources_belong_to_execution_save_scope(self):
        from execution_collaboration import path_kind
        scope = {'features':['example'], 'quarters':['2026-Q4']}
        self.assertEqual(path_kind('features/example/execution/task-markers.json', scope), 'execution-source')
        self.assertEqual(path_kind('planning/2026-Q4/gantt/releases.json', scope), 'execution-view-source')
        self.assertIsNone(path_kind('planning/2026-Q3/gantt/releases.json', scope))

    def test_rejects_unconfirmed_and_conflicting_marker_facts(self):
        import hashlib
        from gantt_releases import apply_task_markers
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'execution').mkdir()
            (root/'execution/tasks.md').write_text('registry')
            proof = {'confirmed':True, 'source':'review.json', 'evidence':'Reviewed', 'date':'2026-10-15'}
            data = {'schema_version':1,'sources':{'execution/tasks.md':hashlib.sha256(b'registry').hexdigest()}}
            bad = [
                {'task_id':'missing','qa':proof},
                {'task_id':'a','qa':dict(proof,confirmed=False)},
                {'task_id':'a','qa':dict(proof,date=None)},
                {'task_id':'a','release':'REL-2','membership':proof},
                {'task_id':'a','production':dict(proof,analyst_confirmed=True)},
            ]
            for entry in bad:
                with self.subTest(entry=entry):
                    data['tasks'] = [entry]
                    (root/'execution/task-markers.json').write_text(json.dumps(data))
                    with self.assertRaises(ValueError):
                        apply_task_markers({'a':SimpleNamespace(kind='real',release_label='REL-1')},root)
