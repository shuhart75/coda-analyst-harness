import json
import shutil
import unittest
from pathlib import Path
import test_execution_collaboration as fixture
import test_workspace as test_collaboration
from test_actual_progress_sources import HEADER, SEPARATOR


class ExecutionCreationTests(unittest.TestCase):
    for name in ('create_seed','configure_identity','prepare_workspace','git','collaboration','raw','change','expand','scope'):
        locals()[name] = getattr(fixture.ExecutionCollaborationTests, name)

    def setUp(self):
        fixture.ExecutionCollaborationTests.setUp(self)
        index={'schema_version':1,'deliveries':{}}
        for key in ('registry','other'):
            path=f'quarters/2026-Q3/features/{key}/deliveries/first'
            (self.project/path).parent.mkdir(parents=True,exist_ok=True)
            shutil.move(str(self.project/'features'/key),str(self.project/path))
            index['deliveries'][key]={'feature_id':key,'delivery_id':'first','quarter':'2026-Q3','path':path}
        shutil.move(str(self.gantt),str(self.project/'quarters/2026-Q3/gantt'))
        self.gantt=self.project/'quarters/2026-Q3/gantt'
        self.change('delivery-index.json',json.dumps(index))
        self.change('features/README.md','# Features\n\n[Registry](registry/README.md)\n')
        self.change('quarters/2026-Q3/plan-state.md','Status: draft\n')
        self.git(self.project,'add','--','features','planning','quarters','delivery-index.json')
        self.git(self.project,'commit','-m','Prepare accepted quarter layout')
        self.initial_head=self.git(self.project,'rev-parse','HEAD')
        self.index=index
        self.scope('registry')  # Existing early registration must be recoverable.
        for key in ('alpha','beta'):
            path=f'quarters/2026-Q3/features/{key}/deliveries/first'
            self.index['deliveries'][key]={'feature_id':key,'delivery_id':'first','quarter':'2026-Q3','path':path}
            self.change(path+'/README.md','# Delivery\n')
            self.change(path+'/execution/tasks.md','\n'.join([HEADER,SEPARATOR,
                '| TASK/BE | TRACK-1 | Work | real | BE | 1 | B1 | 2026-09-01 | 2026-09-01 | | | planned | 0 | STORY-ONE |','']))
            for name in ('README.md','deliveries.md','requirements.md','backlog.md'):
                self.change(f'features/{key}/{name}','# New feature material\n')
        self.change('delivery-index.json',json.dumps(self.index))
        self.change('features/README.md','# Features\n\n[Registry](registry/README.md)\n[Alpha](alpha/README.md)\n[Beta](beta/README.md)\n')
        for view in ('quarter-plan','commander-plan'):
            for suffix in ('','-confluence'):
                self.change(f'quarters/2026-Q3/gantt/{view}{suffix}.puml','@startgantt\n@endgantt\n')

    def register(self, expected=0):
        result=self.raw('set-execution-scope','--feature','registry','--feature','alpha','--feature','beta',
                        '--quarter','2026-Q3','--include-planning','--include-creation',
                        '--reason','Analyst requested two new deliveries with execution','--analyst-confirmed')
        self.assertEqual(result.returncode,expected,result.stdout+result.stderr)
        return result

    def test_recover_early_scope_save_two_features_and_all_exports_atomically(self):
        self.assertNotEqual(self.raw('save-preview').returncode,0)
        before=self.git(self.project,'status','--porcelain')
        self.register()
        self.assertEqual(self.git(self.project,'status','--porcelain'),before)
        preview=json.loads(self.raw('save-preview').stdout)
        self.assertEqual(preview['status'],'execution-save-preview')
        paths={p['path'] for p in preview['paths']}
        for path in ('delivery-index.json','features/README.md','features/alpha/requirements.md',
                     'features/beta/backlog.md','quarters/2026-Q3/gantt/quarter-plan-confluence.puml'):
            self.assertIn(path,paths)
        command=preview['save_command'];command[command.index('--message')+1]='Save two deliveries and execution'
        result=test_collaboration.run(*command,env=self.environment)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.git(self.project,'status','--porcelain'),'')
        self.assertEqual(self.git(self.project,'rev-parse','HEAD^'),self.initial_head)
        self.change('features/alpha/requirements.md','# Modified existing requirements\n')
        self.assertIn('outside confirmed execution scope',self.raw('save-preview').stdout)

    def test_registration_cannot_modify_existing_bindings_or_index_entries(self):
        before=self.state_path.read_bytes()
        self.index['deliveries']['other']['delivery_id']='changed'
        self.change('delivery-index.json',json.dumps(self.index))
        self.assertIn('existing delivery identities',self.register(1).stdout)
        self.assertEqual(self.state_path.read_bytes(),before)
        self.index['deliveries']['other']['delivery_id']='first'
        self.change('delivery-index.json',json.dumps(self.index))
        self.change('features/README.md','# Features\n[Alpha](alpha/README.md)\n')
        self.assertIn('existing feature index',self.register(1).stdout)

    def test_identity_drift_and_other_features_cannot_enter_save(self):
        self.register()
        self.index['deliveries']['alpha']['title']='Unconfirmed identity metadata'
        self.change('delivery-index.json',json.dumps(self.index))
        self.assertIn('identities changed',self.raw('save-preview').stdout)
        del self.index['deliveries']['alpha']['title']
        self.change('delivery-index.json',json.dumps(self.index))
        self.change('features/other/requirements.md','# Existing feature is outside creation grant\n')
        self.assertIn('outside confirmed execution scope',self.raw('save-preview').stdout)
        self.assertEqual(self.git(self.project,'diff','--cached','--name-only'),'')

    def test_missing_and_stale_planned_exports_block_save(self):
        self.register()
        path=self.gantt/'commander-plan-confluence.puml'
        path.write_text('stale')
        self.assertIn('Confluence export differs from commander-plan',self.raw('save-preview').stdout)
        path.unlink()
        self.assertIn('Missing source or Confluence export',self.raw('save-preview').stdout)
        self.assertEqual(self.git(self.project,'diff','--cached','--name-only'),'')

    def test_new_delivery_of_existing_owner_keeps_creation_grant_narrow(self):
        old_path = self.index['deliveries']['beta']['path']
        new_path = 'quarters/2026-Q3/features/other/deliveries/second'
        self.index['deliveries']['beta'].update(feature_id='other', delivery_id='second', path=new_path)
        shutil.move(str(self.project / old_path), str(self.project / new_path))
        shutil.rmtree(self.project / 'features/beta')
        self.change('delivery-index.json', json.dumps(self.index))
        self.change('features/README.md', '# Features\n\n[Registry](registry/README.md)\n[Alpha](alpha/README.md)\n')
        self.register()
        preview = self.raw('save-preview')
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        self.change('features/other/requirements.md', '# Existing owner requirements\n')
        self.assertIn('outside confirmed execution scope', self.raw('save-preview').stdout)

    def test_re_registration_cannot_drop_tracker_runs(self):
        state=json.loads(self.state_path.read_text())
        state['active_work']['execution_scope']['run_ids']=['retained-run']
        self.state_path.write_text(json.dumps(state))
        before=self.state_path.read_bytes()
        self.assertIn('Keep every registered tracker run',self.register(1).stdout)
        self.assertEqual(self.state_path.read_bytes(),before)
