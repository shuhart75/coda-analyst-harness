from __future__ import annotations

import copy
from contextlib import redirect_stdout
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_release_actualization_e2e as e2e
from tracker_registry import check_protected_rows, read_registry, require_new_registry_format
import execution_collaboration as ec


class ProtectedRegistryTests(unittest.TestCase):
    def test_anonymous_rows_preserve_multiplicity_and_known_ids_stay_unique(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'tasks.md'
            header = '| Task ID | Jira | SberTrek | Summary | Estimate |\n|---|---|---|---|---|\n'
            rows = '| | OLD-1 | | Same | 2 |\n' * 2 + '| LOCAL | OLD-2 | NEW-1 | Known | 3 |\n'
            path.write_text(header + rows)
            protected = [{'registry': 'tasks.md', 'task_id': row.get('Task ID', ''), 'registry_fields': row}
                         for row in read_registry(path)[0][0]]
            self.assertEqual(check_protected_rows(root, protected), [])
            for changed in [rows.replace('| | OLD-1 | | Same | 2 |\n', '', 1),
                            rows.replace('Same', 'Changed', 1), rows + '| | OLD-1 | | Same | 2 |\n',
                            rows + '| LOCAL | OLD-2 | NEW-1 | Known | 3 |\n']:
                path.write_text(header + changed)
                self.assertTrue(check_protected_rows(root, protected))
            path.write_text(header + rows.replace('| 3 |', '| 4 |'))
            self.assertTrue(check_protected_rows(root, protected))
            protected[-1]['allowed_fields'] = ['Estimate']
            self.assertEqual(check_protected_rows(root, protected), [])
            path.unlink()
            self.assertTrue(check_protected_rows(root, protected))

    def test_new_registry_cannot_use_legacy_columns_even_after_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = e2e.ReleaseActualizationEndToEndTests.git
            git(self, root, 'init', '-b', 'main')
            git(self, root, 'config', 'user.name', 'Test')
            git(self, root, 'config', 'user.email', 'test@example.invalid')
            path = root / 'old.md'
            raw = '| Jira | Summary | Kind | Role |\n|---|---|---|---|\n| LOCAL | Old | real | BE |\n'
            path.write_text(raw)
            git(self, root, 'add', '--', 'old.md')
            git(self, root, 'commit', '-m', 'Keep historical registry')
            require_new_registry_format(root, path)
            new = root / 'new.md'; new.write_text(raw)
            for staged in (False, True):
                if staged:
                    git(self, root, 'add', '--', 'new.md')
                with self.assertRaisesRegex(ValueError, 'New registry requires'):
                    require_new_registry_format(root, new)
            for raw in ['| Task ID | SberTrek | Jira |\n|---|---|---|\n| LOCAL | NEW-1 | OLD-1 |\n',
                        '| Task ID | SberTrek |\n|---|---|\n| | NEW-1 |\n']:
                new.write_text(raw)
                with self.assertRaises(ValueError):
                    require_new_registry_format(root, new)
            new.write_text('| Task ID | SberTrek |\n|---|---|\n| LOCAL | NEW-1 |\n')
            require_new_registry_format(root, new)


class DeliveryScopeTests(unittest.TestCase):
    def test_alias_normalization_and_explicit_delivery_keep_owner_and_quarter_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            deliveries = {}
            for key, owner, quarter in [('owner-poc', 'owner', '2026-Q4'), ('target-q4', 'target', '2026-Q4')]:
                relative = f'quarters/{quarter}/features/{owner}/deliveries/poc'
                (root / relative).mkdir(parents=True)
                deliveries[key] = {'feature_id': owner, 'delivery_id': 'poc', 'quarter': quarter, 'path': relative}
            index = root / 'delivery-index.json'
            index.write_text(json.dumps({'schema_version': 1, 'deliveries': deliveries}))
            (root / 'quarters/2026-Q4/gantt').mkdir()
            work = {'feature': 'owner', 'status': 'active', 'branch': 'feature/owner/analyst'}
            state = {'active_work': work}
            saved = []
            api = SimpleNamespace(root_path=lambda _: root, analytics_repository=lambda _: (root, None),
                load_state=lambda _: state, require_active_work=lambda *_: work,
                SLUG_PATTERN=re.compile('[a-z0-9-]+'), utc_now=lambda: 'now',
                write_state=lambda *args: saved.append(copy.deepcopy(args[-1])))
            def register(features, quarters=None):
                args = SimpleNamespace(root=str(root), feature=features, quarter=quarters or ['2026-Q4'],
                    run_id=[], reason='Confirmed fixture', analyst_confirmed=True)
                with patch.object(ec, 'require_execution_work'), redirect_stdout(StringIO()):
                    return ec.set_scope(args, api)
            for selected in [['owner', 'target-q4'], ['owner-poc', 'target-q4'], ['owner', 'owner-poc', 'target-q4']]:
                register(selected)
                self.assertEqual(work['execution_scope']['features'], ['owner-poc', 'target-q4'])
                self.assertEqual(work['feature'], 'owner')
            old = copy.deepcopy(saved)
            with self.assertRaisesRegex(ValueError, 'active owning feature'):
                register(['target-q4'])
            self.assertEqual(saved, old)
            relative = 'quarters/2026-Q3/features/owner/deliveries/earlier'
            (root / relative).mkdir(parents=True)
            (root / 'quarters/2026-Q3/gantt').mkdir()
            deliveries['owner-earlier'] = {'feature_id': 'owner', 'delivery_id': 'earlier', 'quarter': '2026-Q3', 'path': relative}
            index.write_text(json.dumps({'schema_version': 1, 'deliveries': deliveries}))
            with self.assertRaisesRegex(ValueError, 'Select a delivery'):
                register(['owner', 'target-q4'])
            register(['owner-poc', 'target-q4'])
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                register(['owner-poc', 'owner-earlier', 'target-q4'], ['2026-Q3', '2026-Q4'])
            with self.assertRaisesRegex(ValueError, 'registered quarter'):
                register(['owner-earlier', 'target-q4'])
            work['feature'] = 'owner-poc'
            with self.assertRaisesRegex(ValueError, 'active owning feature'):
                register(['owner-earlier', 'target-q4'], ['2026-Q3', '2026-Q4'])


class EpicResumeApplicationTests(unittest.TestCase):
    create_seed = e2e.ReleaseActualizationEndToEndTests.create_seed
    configure_identity = e2e.ReleaseActualizationEndToEndTests.configure_identity
    prepare_workspace = e2e.ReleaseActualizationEndToEndTests.prepare_workspace
    git = e2e.ReleaseActualizationEndToEndTests.git
    collaboration = e2e.ReleaseActualizationEndToEndTests.collaboration
    run_tool = e2e.ReleaseActualizationEndToEndTests.run_tool
    write = e2e.ReleaseActualizationEndToEndTests.write
    sber_issue = e2e.ReleaseActualizationEndToEndTests.sber_issue
    call = e2e.ReleaseActualizationEndToEndTests.call
    ingest = e2e.ReleaseActualizationEndToEndTests.ingest
    prepare_lab = e2e.ReleaseActualizationEndToEndTests.prepare_lab
    prepare_sources = e2e.ReleaseActualizationEndToEndTests.prepare_sources
    collab = e2e.ReleaseActualizationEndToEndTests.collab
    command = e2e.ReleaseActualizationEndToEndTests.command
    row = e2e.ReleaseActualizationEndToEndTests.row
    write_registry = e2e.ReleaseActualizationEndToEndTests.write_registry
    evidence = e2e.ReleaseActualizationEndToEndTests.evidence
    qa_check = e2e.ReleaseActualizationEndToEndTests.qa_check
    generate = e2e.ReleaseActualizationEndToEndTests.generate
    snapshot = e2e.ReleaseActualizationEndToEndTests.snapshot

    def test_saved_epic_resumes_offline_through_application_generation_and_save(self):
        temporary = tempfile.TemporaryDirectory(prefix='epic-resume-')
        self.addCleanup(temporary.cleanup)
        self.prepare_lab(Path(temporary.name))
        bindings = {}
        for feature, quarter in [('registry', '2026-Q4'), ('other', '2026-Q3')]:
            key = feature + ('-q4' if feature == 'registry' else '-q3')
            relative = f'quarters/{quarter}/features/{feature}/deliveries/poc'
            destination = self.project / relative
            destination.parent.mkdir(parents=True)
            (self.project / 'features' / feature).rename(destination)
            bindings[key] = {'feature_id': feature, 'delivery_id': 'poc', 'quarter': quarter, 'path': relative}
        moved_gantt = self.project / 'quarters/2026-Q4/gantt'
        self.gantt.rename(moved_gantt); self.gantt = moved_gantt
        self.feature = self.project / bindings['registry-q4']['path']
        self.registry = self.feature / 'slices/existing/execution/tasks.md'
        old = self.feature / 'execution/tasks.md'
        old.unlink()
        self.groups_path = self.feature / 'execution/qa-groups.json'
        self.keys = [f'NEW-{i+1}' for i in range(21)]
        roles = ['FE']*3 + ['BE']*13 + ['BE', 'FE', 'FE', 'FE', 'BE']
        rows = []
        for i, (key, role) in enumerate(zip(self.keys, roles)):
            row = self.row(key + '/' + role, status='created')
            row.update({'Executor': '', 'Planned Start': '', 'Planned Finish': '', 'Related Stories': ''})
            rows.append(row)
        qa = self.row('QA-REGISTRY', estimate='7', status='created')
        qa.update({'Executor': '', 'Planned Start': '', 'Planned Finish': '', 'Related Stories': ''})
        self.write_registry(self.registry, rows[:16] + [qa])
        group = {'schema_version': 1, 'provider': 'sbertrek', 'total_estimate': '7', 'groups': [
            {'task_id': 'QA-REGISTRY', 'members': [row['Task ID'] for row in rows[:16]], 'estimate': '7'}]}
        self.write(self.groups_path, group)
        mapping = self.feature / 'planning/actualization.md'
        mapping.write_text('| Story ID | Summary | Baseline Start | Baseline Duration (дн) | Actualization State | Mapping Mode | Replaced By | Residual Virtual Tasks | Depends On |\n|---|---|---|---|---|---|---|---|---|\n'
            '| STORY-REGISTRY | Synthetic delivery | 2026-10-01 | 5 | materialized | explicit | '+', '.join(r['Task ID'] for r in rows[:16]+[qa])+' | | |\n')
        (self.gantt / 'actual-progress.puml').write_text('@startgantt\n!include includes/actual-progress/FEATURE-registry-q4.puml\n@endgantt\n')
        for path in (self.gantt / 'includes/actual-progress').glob('*.puml'):
            path.unlink()
        (self.gantt / 'includes/actual-progress/FEATURE-registry-q4.puml').write_text('[Old] happens at 2026/10/01\n')
        self.write(self.project / 'delivery-index.json', {'schema_version': 1, 'deliveries': bindings})
        protected = []
        other = self.project / bindings['other-q3']['path']
        for i in range(8):
            path = other / f'slices/anonymous-{i}/execution/tasks.md'
            path.parent.mkdir(parents=True)
            count = 8 if i == 0 else 5
            path.write_text('| Task ID | Jira | Summary | Kind | Role |\n|---|---|---|---|---|\n'+
                ''.join(f'| | OLD-{i}-{j} | Preserved | real | BE |\n' for j in range(count)))
            protected.append(path)
        for i in range(19):
            path = other / f'slices/legacy-{i}/execution/tasks.md'
            path.parent.mkdir(parents=True)
            path.write_text('| Jira | Summary | Kind | Role |\n|---|---|---|---|\n'+f'| OLD-{i} | Historical | real | BE |\n')
            protected.append(path)
        self.git(self.project, 'add', '--', 'features', 'quarters', 'planning')
        self.git(self.project, 'add', '--', 'delivery-index.json')
        self.git(self.project, 'commit', '-m', 'Prepare epic recovery sources')
        self.initial_head = self.git(self.project, 'rev-parse', 'HEAD')
        preserved = {p:p.read_bytes() for p in protected}
        plans = {p:p.read_bytes() for p in self.gantt.glob('*-plan.puml')}
        run = self.run_tool(self.state, 'begin', '--adaptive', '--scope-kind', 'epic', '--scope-id', 'EPIC-1',
            '--label', 'Saved epic', '--scope-source', 'analyst', '--intent', 'update-planning')
        cards = [self.sber_issue(key, summary=f'[{role}] Task', roles={role:2, **({'QA':10} if i==0 else {})})
                 for i,(key,role) in enumerate(zip(self.keys,roles))]
        self.ingest(run, {'issues':cards})
        self.run_id = run['run_id']
        self.run_tool(self.state, 'reconcile', '--run-id', self.run_id)
        status = self.run_tool(self.state, 'result-status', '--run-id', self.run_id)
        self.collab('set-execution-scope', '--feature', 'registry-q4', '--quarter', '2026-Q4',
            '--run-id', self.run_id, '--reason', 'Confirmed epic scope', '--analyst-confirmed')
        answer = self.inputs/'answer.txt'; answer.write_text('Confirm five new owners, preserve current facts, extend QA membership and keep QA estimate seven.')
        decision = {'schema_version':1, 'provider':'sbertrek', 'run_id':self.run_id, 'scope':status['scope'],
            'analyst_confirmed':True, 'source':self.evidence(answer),
            'tasks':[{'key':key,'feature':'registry-q4'} for key in self.keys[16:]]}
        manifest = {'schema_version':1, 'analyst_confirmed':True, 'decision_source':'Confirmed fixture rules',
            'provider':'sbertrek', 'deliveries':['registry-q4'], 'scope_decisions':decision,
            'participants':{}, 'status_rules':{'not_started':['created'],'qa_completed':['done']}, 'responses':[]}
        for key,role in zip(self.keys,roles):
            raw = self.write(self.inputs/(key+'.json'), {'key':key,'status':'created','assignee':None,'events':[],'start':0,'total':0})
            manifest['responses'].append({'provider':'sbertrek','key':key,'role':role,'response_file':str(raw),
                'sha256':hashlib.sha256(raw.read_bytes()).hexdigest(), 'call':{**self.call('sbertrek'),'arguments':{'key':key}},
                'mapping':{'key':'/key','status':'/status','assignee':'/assignee','events':'/events','start':'/start','total':'/total',
                    'field':'/field','at':'/at','from':'/from','to':'/to','assignment_field':'assignee','status_field':'status'}})
        manifest_path = self.write(self.inputs/'manifest.json', manifest)
        args = ('history-review','--run-id',self.run_id,'--project-root',str(self.project),'--manifest',str(manifest_path))
        first = self.run_tool(self.state,*args)
        self.assertEqual(first['pending_history'],[])
        self.assertEqual(len(first['proposed_registrations']),5)
        self.run_tool(self.state,'application-preflight','--project-root',str(self.project),'--delivery','registry-q4')
        self.write_registry(old,rows[16:])
        canonical = old.read_text()
        old.write_text(canonical.replace('Task ID','Jira'))
        with self.assertRaisesRegex(ValueError,'New registry requires'):
            require_new_registry_format(self.project,old)
        bad = self.run_tool(self.state,'execution-preview','--run-id',self.run_id,'--project-root',str(self.project),'--delivery','registry-q4')
        self.assertIn('invalid-new-registry-format',{b['reason'] for b in bad['blockers']})
        old.write_text(canonical)
        group['groups'][0]['members'] = [r['Task ID'] for r in rows]
        self.write(self.groups_path,group)
        manifest.update(expected_head=self.initial_head,reviewed_registries={str(old.relative_to(self.project)):hashlib.sha256(old.read_bytes()).hexdigest()},
            development_decision={'kind':'keep-current','analyst_confirmed':True,'source':self.evidence(answer)},
            qa_confirmations=[{'feature':'registry-q4','task_id':'QA-REGISTRY','kind':'keep-current','fields':{},'analyst_confirmed':True,'source':self.evidence(answer)}])
        self.write(manifest_path,manifest)
        reviewed = self.run_tool(self.state,*args)
        self.assertEqual(reviewed['qa_application_blockers'],[])
        self.assertEqual(len([r for r in reviewed['application_scope']['protected_rows'] if not r['task_id']]),43)
        self.generation_review = reviewed['review_file']
        # New CLI processes resume the saved run with networking disabled by sitecustomize.
        run_dir = Path(status['paths']['reconciled']).parent
        raw_before = self.snapshot(run_dir)
        inputs_before = self.snapshot(self.inputs)
        dirty_before = self.git(self.project,'status','--porcelain')
        self.run_tool(self.state,'config-status')
        resumed = self.run_tool(self.state,'resume','--scope-kind','epic','--scope-provider','sbertrek','--scope-id','EPIC-1',
            '--intent','update-planning','--run-id',self.run_id)
        self.assertEqual(resumed['run_id'],self.run_id)
        self.assertTrue(resumed['planning_application_allowed'])
        self.assertEqual(self.snapshot(run_dir),raw_before)
        self.assertEqual(self.git(self.project,'status','--porcelain'),dirty_before)
        # A review/analyst checksum cannot authorize a newly written legacy table.
        old.write_text(canonical.replace('Task ID', 'Jira'))
        invalid = self.qa_check(reviewed, expected=2)
        self.assertIn('New registry requires', invalid['error'])
        gantt_before = self.snapshot(self.gantt)
        self.generate(expected=1)
        self.assertEqual(self.snapshot(self.gantt), gantt_before)
        self.command(sys.executable, str(e2e.ROOT / 'scripts/collaboration.py'), '--root', str(self.workspace),
                     'save-preview', '--review-file', reviewed['review_file'], expected=1)
        old.write_text(canonical)
        self.assertTrue(self.qa_check(reviewed)['development_verified'])
        protected[0].write_text(protected[0].read_text().replace('Preserved','Unauthorized',1))
        self.qa_check(reviewed,expected=2)
        gantt_before = self.snapshot(self.gantt)
        self.generate(expected=1)
        self.assertEqual(self.snapshot(self.gantt),gantt_before)
        protected[0].write_bytes(preserved[protected[0]])
        self.assertTrue(self.qa_check(reviewed)['development_verified'])
        self.generate()
        preview = self.collab('save-preview','--review-file',reviewed['review_file'])
        self.assertEqual(preview['qa_verified_runs'],[self.run_id])
        self.assertEqual(preview['confluence_verified'],['2026-Q4'])
        self.assertEqual(self.snapshot(self.inputs),inputs_before)
        self.assertEqual(self.snapshot(run_dir),raw_before)
        self.assertTrue(all(path.read_bytes()==data for path,data in {**preserved,**plans}.items()))
        command = preview['save_command'];command[command.index('--message')+1]='Save resumed epic execution'
        self.command(*command)
        self.assertEqual(self.git(self.project,'status','--porcelain'),'')
        self.assertFalse(self.run_tool(self.state,'result-status','--run-id',self.run_id)['final_response_allowed'])


if __name__ == '__main__':
    unittest.main()
