import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from gantt_releases import apply_task_releases, apply_registry_markers
from baseline_releases import digest
from test_actual_progress_sources import OVERLAY


class TaskFactsTests(unittest.TestCase):
    def task(self, key, status='completed', progress=100):
        return SimpleNamespace(task_id=key, tracker_key=key, kind='real', status=status,
                               progress=progress, actual_finish='2026-10-15', release_label='')

    def test_qa_is_group_fact_not_development_percentage(self):
        tasks={key:self.task(key) for key in ('A','B','QA')}
        groups=[{'task_id':'QA','members':['A','B'],'release':'REL-1'}]
        apply_task_releases(tasks,groups)
        apply_registry_markers(tasks,groups)
        self.assertTrue(all('QA ✓' in t.release_label for t in tasks.values()))
        tasks['B'].status='in_progress'
        apply_task_releases(tasks,groups)
        apply_registry_markers(tasks,groups)
        self.assertTrue(all('QA ✓' not in t.release_label for t in tasks.values()))
        self.assertTrue(all('ПРОМ' not in t.release_label for t in tasks.values()))

    def test_production_requires_promoted_exact_membership_and_environment(self):
        tasks={key:self.task(key) for key in ('A','B','QA')}
        groups=[{'task_id':'QA','members':['A','B'],'release':'REL-1'}]
        observation={'release_id':'REL-1','membership_complete':True,'membership_evidence':'Accepted',
                     'tasks':[{'id':'A','closed':True,'evidence':'Accepted'}]}
        entry={'status':'promoted','observation':observation,'scope_hash':digest(observation),
               'deployment':{'version':'v1','environment':'ПРОМ','evidence':'Analyst confirmation'}}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'releases').mkdir()
            path=root/'releases/baseline-status.json'
            for state, environment, marked in [('candidate','ПРОМ',False),('promoted','TEST',False),('promoted','ПРОМ',True)]:
                entry['status']=state;entry['deployment']['environment']=environment
                path.write_text(json.dumps({'schema_version':1,'releases':{'REL-1':entry}}))
                apply_task_releases(tasks,groups);apply_registry_markers(tasks,groups,root)
                self.assertEqual('ПРОМ ✓' in tasks['A'].release_label,marked)
                self.assertNotIn('ПРОМ ✓',tasks['B'].release_label)
                self.assertNotIn('ПРОМ ✓',tasks['QA'].release_label)
            entry['scope_hash']='stale'
            path.write_text(json.dumps({'schema_version':1,'releases':{'REL-1':entry}}))
            with self.assertRaises(ValueError):apply_registry_markers(tasks,groups,root)

    def test_label_uses_tracker_key_and_distinguishes_local_identity(self):
        task=OVERLAY.Task('OLD-1/BE','NEW-2','[BE] Work','real','BE',1,'B1','','','','','completed',100,[])
        self.assertEqual(OVERLAY.role_prefixed_summary(task),'NEW-2 · BE Work')
        for summary in ('NEW-2 [BE] Work', 'BE NEW-2 Work', 'NEW-2/BE Work'):
            task.summary = summary
            self.assertEqual(OVERLAY.role_prefixed_summary(task), 'NEW-2 · BE Work')
        task.tracker_key='';task.task_id='QA-REST';task.role='QA';task.summary='QA Work'
        self.assertEqual(OVERLAY.role_prefixed_summary(task),'лок. QA-REST · QA Work')

    def test_generator_reads_accepted_group_and_baseline_without_marker_file(self):
        from test_actual_progress_sources import ActualProgressSourcesTests
        fixture=ActualProgressSourcesTests()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.write_tasks([
            '| | ITEM-100 | List | real | FE | 5 | F1 | 2026-09-01 | 2026-09-04 | 2026-09-01 | 2026-09-04 | completed | 100 | STORY-COHORT |',
            '| QA-COHORT | | Cross-feature check | real | QA | 2 | Q2 | 2026-09-03 | 2026-09-09 | 2026-09-03 | 2026-09-09 | completed | 100 | STORY-COHORT |'])
        (fixture.feature/'execution/qa-groups.json').write_text(json.dumps({'schema_version':1,'total_estimate':2,
            'groups':[{'task_id':'QA-COHORT','members':['ITEM-100/FE'],'release':'REL-1','estimate':2}]}))
        observation={'release_id':'REL-1','membership_complete':True,'membership_evidence':'Accepted',
                     'tasks':[{'id':'ITEM-100','closed':True,'evidence':'Accepted'}]}
        entry={'status':'promoted','observation':observation,'scope_hash':digest(observation),
               'deployment':{'version':'v1','environment':'ПРОМ','evidence':'Analyst confirmation'}}
        (fixture.root/'releases').mkdir()
        (fixture.root/'releases/baseline-status.json').write_text(json.dumps({'schema_version':1,'releases':{'REL-1':entry}}))
        fixture.quarter()
        text=fixture.target.read_text()
        self.assertIn('ITEM-100 · FE List · REL-1 · QA ✓ · ПРОМ ✓',text)
        self.assertIn('лок. QA-COHORT · QA Cross-feature check · REL-1 · QA ✓ · ПРОМ ✓',text)
