"""New documents transport and analyst evidence, with real isolated Git remotes."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_sdd_exchange as fixtures
import analyst_results
import grooming

SCRIPT = fixtures.legacy.ROOT / 'scripts/requirements-exchange.py'


class DocumentsExchangeTests(unittest.TestCase):
    for _name, _method in vars(fixtures.SddExchangeTests).items():
        if callable(_method) and not _name.startswith('test_'):
            locals()[_name] = _method
    del _name, _method

    def setUp(self):
        fixtures.SddExchangeTests.setUp(self)
        self.code_remote = self.remote
        self.remote = self.parent / 'documents.git'
        self.git(self.parent, 'init', '--bare', str(self.remote))
        self.git(self.project, 'init', '-b', 'main')
        self.git(self.project, 'config', 'user.name', 'Analyst')
        self.git(self.project, 'config', 'user.email', 'analyst@example.invalid')
        self.git(self.project, 'add', '--all')
        self.git(self.project, 'commit', '-m', 'Analytical source')
        self.git(self.project, 'remote', 'add', 'origin', str(self.remote))
        self.git(self.project, 'push', '-u', 'origin', 'main')
        self.git(self.remote, 'symbolic-ref', 'HEAD', 'refs/heads/main')

    def prepare(self, success=True):
        return self.cli(SCRIPT, 'prepare', self.project, 'demo', '--analyst', 'owner', success=success)

    def enable_code_exchange(self):
        # Compatibility name used by decision-delivery cases: destination is documents.
        pass

    def publish(self):
        self.authorize()
        before = self.code_snapshot()
        result = self.prepare()
        self.assertEqual(result['destination_role'], 'analytics')
        self.assertFalse(result['publication_confirmed'])
        self.assertEqual(self.prepare()['request_commit'], result['request_commit'])
        self.assertEqual(self.code_snapshot(), before)
        self.accept(result)
        result = self.prepare()
        self.assertTrue(result['publication_confirmed'])
        self.state('mark-published', '--revision', '1', '--manifest', result['manifest'], '--destination-role', 'analytics')
        # Preserve local preparation state while bringing the exact accepted packet into the checkout.
        self.git(self.project, 'fetch', 'origin')
        self.git(self.project, 'checkout', 'origin/main', '--', 'requirements-exchange')
        self.git(self.project, 'add', '--all')
        self.git(self.project, 'commit', '-m', 'Record preparation')
        self.git(self.project, 'merge', '--no-edit', 'origin/main')
        return result

    def test_documents_only_publication_requires_merge_and_preserves_code(self):
        before = self.git(self.code_remote, 'show-ref')
        result = self.publish()
        manifest = self.manifest(result)
        self.assertEqual(manifest['schema_version'], 6)
        self.assertEqual(manifest['revisions'][0]['returns_contract_version'], 3)
        self.assertFalse(manifest['requested_returns']['required'])
        self.assertEqual(self.git(self.code_remote, 'show-ref'), before)
        validated = self.cli(SCRIPT, 'validate', self.project/'requirements-exchange')
        self.assertEqual(validated['processing'][0]['state'], 'awaiting-analysis')
        self.assertFalse((self.code/'requirements-exchange').exists())

    def test_missing_documents_remote_never_falls_back_to_code(self):
        self.authorize()
        self.git(self.project, 'remote', 'remove', 'origin')
        self.assertIn('origin', self.prepare(success=False))
        self.assertFalse((self.code/'requirements-exchange').exists())

    def test_failed_documents_push_never_claims_publication(self):
        self.authorize()
        hook = self.remote/'hooks/pre-receive'
        hook.write_text('#!/bin/sh\nexit 1\n'); hook.chmod(0o755)
        self.prepare(success=False)
        self.assertFalse((self.project/'requirements-exchange').exists())
        self.assertFalse((self.code/'requirements-exchange').exists())
        self.assertEqual(json.loads((self.feature/'requirements-state.json').read_text())['delivery_stages']['revisions'], [])

    def add_change(self):
        root = self.code/'backend/openspec/changes/archive/2026-11-23-example'
        root.mkdir(parents=True)
        (root/'proposal.md').write_text('RSCON-999 is only a dependency.\n')
        (root/'tasks.md').write_text('- [x] 1.1 Work\n')
        self.git(self.code, 'add', '--all'); self.git(self.code, 'commit', '-m', 'RSCON-123: implement')
        introduced = self.git(self.code, 'rev-parse', 'HEAD')
        (root/'design.md').write_text('Later explanation\n')
        self.git(self.code, 'add', '--all'); self.git(self.code, 'commit', '-m', 'RSCON-456: add explanation')
        return root, introduced

    def test_evidence_pins_branch_excludes_other_refs_and_late_file_additions(self):
        directory, introduced = self.add_change()
        selected = self.git(self.code, 'rev-parse', 'HEAD')
        self.git(self.code, 'checkout', '-b', 'unmerged')
        (self.code/'unmerged.py').write_text('unmerged')
        self.git(self.code, 'add', '--all'); self.git(self.code, 'commit', '-m', 'RSCON-123: unmerged')
        other = self.git(self.code, 'rev-parse', 'HEAD')
        self.git(self.code, 'checkout', 'main')
        before = self.code_snapshot()
        result = analyst_results.collect(self.code, selected, ['backend/openspec'], {'BE-1': {'key': 'RSCON-123', 'evidence': 'confirmed grooming'}})
        self.assertEqual(result['changes'][0]['branch_entry_commit'], introduced)
        self.assertEqual(result['changes'][0]['task_key'], 'RSCON-123')
        self.assertEqual([c['commit'] for c in result['commits']], [introduced])
        self.assertNotIn(other, json.dumps(result))
        self.assertEqual(self.code_snapshot(), before)

    def test_typo_and_missing_key_remain_unresolved(self):
        directory, introduced = self.add_change()
        self.git(self.code, 'commit', '--allow-empty', '-m', 'RSON-8559: typo')
        result = analyst_results.collect(self.code, self.git(self.code, 'rev-parse', 'HEAD'), ['backend/openspec'], {'BE-1': {'key': 'RSCON-8559', 'evidence': 'confirmed'}})
        self.assertEqual(result['commits'], [])

    def test_analyst_report_is_immutable_and_unknown_scenarios_cannot_pass(self):
        self.publish()
        directory, introduced = self.add_change()
        code_head = self.git(self.code, 'rev-parse', 'HEAD')
        source = analyst_results.collect(self.code, code_head, ['backend/openspec'], {})
        root = self.project/'requirements-exchange/demo'
        manifest = json.loads((root/'manifest.json').read_text()); entry = manifest['revisions'][0]
        report = {'schema_version':1, 'kind':'analyst-implementation-review', 'feature':'demo', 'revision':1,
                  'requirements_sha256':entry['sha256'], 'sdd_sha256':entry['sdd_input']['sha256'],
                  'bindings':{}, 'evidence':[source], 'scenarios':[
                      {'task_id':'', 'requirement':'REQ-DEMO-001', 'scenario':'успешное действие',
                       'implementation':'unknown','conformity':'unknown','verification':'not-run',
                       'merge':'unknown','deployment':'unknown', 'expected':'Show result',
                       'actual':'Not established','limitations':'No runtime evidence','evidence':[]}]}
        analyst_results.validate_report(root, entry, report)
        broken = copy.deepcopy(report); broken['scenarios'][0]['verification']='passed'
        with self.assertRaises(ValueError):
            analyst_results.validate_report(root, entry, broken)
        name = grooming.fingerprint(report)
        path = root/f'revisions/001/analysis/{name}.json';path.parent.mkdir();path.write_text(json.dumps(report));(path.parent/'current').write_text(name+'\n')
        self.assertEqual(analyst_results.processing_status(root, entry)['state'], 'analysis-present')
        path.write_text(json.dumps(broken))
        self.assertEqual(analyst_results.processing_status(root, entry)['state'], 'invalid')

    def write_context(self, mode='requirements'):
        branch = self.git(self.project, 'branch', '--show-current')
        if branch == 'main':
            self.git(self.project, 'switch', '-c', 'feature/demo-review')
            branch = 'feature/demo-review'
        (self.runtime/'workspace.json').write_text(json.dumps({'roles':{'analytics':{'path':str(self.project)}}}))
        (self.runtime/'collaboration.json').write_text(json.dumps({'mode':'multi-user-branches','active_work':{'feature':'demo','branch':branch,'status':'active'}}))
        (self.runtime/'active-mode.md').write_text('mode: '+mode+'\n')

    def test_groomed_tasks_are_preserved_and_story_link_targets_documents(self):
        import test_grooming
        import grooming_tracker
        test_grooming.GroomingExchangeTests.enable_grooming(self)
        sent = self.publish()
        batch = grooming_tracker.prepare(self.project, 'demo', 'https://example.test/team/documents')
        self.assertEqual(batch['tasks'][0]['local_id'], self.plan['tasks'][0]['id'])
        self.assertIn('documents?path=', batch['tasks'][0]['requirements_url'])
        self.assertIn(sent['published_commit'], batch['tasks'][0]['description'])
        with self.assertRaises(ValueError):
            grooming_tracker.prepare(self.project, 'demo', 'https://example.test/team/coda')
        root = self.project/'requirements-exchange/demo'
        entry = self.manifest(sent)['revisions'][0]
        with self.assertRaises(ValueError):
            analyst_results.bound_tasks(root, entry, {})
        bindings = {t['id']:{'key':f'RSCON-{123+i}','evidence':'Verified Story journal'} for i,t in enumerate(self.plan['tasks'])}
        self.assertEqual(len(analyst_results.bound_tasks(root, entry, bindings)), len(self.plan['tasks']))

    def test_record_analysis_review_close_without_receipt(self):
        self.complete_analysis_cycle()

    def test_archive_preserves_analysis_and_blocks_further_writes(self):
        from unittest.mock import patch
        with patch.dict(os.environ, self.environment):
            self.complete_analysis_cycle(archive=True)

    def complete_analysis_cycle(self, archive=False):
        import hashlib
        import release_exchange
        self.publish()
        self.add_change()
        sha = self.git(self.code, 'rev-parse', 'HEAD')
        source = analyst_results.collect(self.code, sha, ['backend/openspec'], {}, sha)
        root = self.project/'requirements-exchange/demo'
        entry = json.loads((root/'manifest.json').read_text())['revisions'][0]
        log = self.project/'test-result.txt'; log.write_text('1 scenario passed\n')
        artifact = {'path':'test-result.txt','content':log.read_text(),'sha256':hashlib.sha256(log.read_bytes()).hexdigest(),
                    'kind':'test-result','source_commit':sha,'environment':'test fixture','procedure':'fixture scenario check','result':'passed'}
        report = {'schema_version':1,'kind':'analyst-implementation-review','feature':'demo','revision':1,
                  'requirements_sha256':entry['sha256'],'sdd_sha256':entry['sdd_input']['sha256'],
                  'bindings':{},'evidence':[source],'external_evidence':[artifact],
                  'scenarios':[{'task_id':'','requirement':'REQ-DEMO-001','scenario':'успешное действие',
                    'implementation':'full','conformity':'matches','verification':'passed','merge':'merged','deployment':'unknown',
                    'expected':'Show result','actual':'Result is shown','limitations':'Deployment not inspected',
                    'evidence':[{'snapshot':grooming.fingerprint(source),'path':'source.py','reason':'Implementation inspected'},
                                {'artifact':artifact['sha256'],'reason':'Scenario execution log'}]}]}
        path = self.parent/'report.json';path.write_text(json.dumps(report))
        script = fixtures.legacy.ROOT/'scripts/analyst_results.py'
        self.assertIn('workspace.json', self.cli(script,'record-analysis',self.project,'demo','--report',path,success=False))
        self.write_context()
        recorded = self.cli(script,'record-analysis',self.project,'demo','--report',path)
        self.assertEqual(self.cli(script,'record-analysis',self.project,'demo','--report',path)['return_id'], recorded['return_id'])
        review = {'schema_version':1,'return_id':recorded['return_id'],'requirements_sha256':entry['sha256'],
                  'items':[{'requirement':'REQ-DEMO-001','implementation':'full','conformity':'matches','verification':'passed',
                            'acceptance':'accepted','actual_behavior':'Result is shown','accepted_behavior':'Result is shown',
                            'reason':'Analyst accepts the verified behavior','evidence':[recorded['path']],
                            'baseline':{'action':'none'},'follow_up':{'action':'none'}}]}
        review_path = self.parent/'decision.json';review_path.write_text(json.dumps(review))
        self.cli(SCRIPT,'record-processed',self.project,'demo','--return-id',recorded['return_id'],
                 '--decision','reviewed','--review-file',review_path,'--analyst','owner')
        self.state('close-stage','--return-id',recorded['return_id'],'--note','Accept delivered scope','--analyst-confirmed')
        # The release closure validator has no summary/receipt dependency.
        publication, closure = release_exchange._closure(self.project,'demo',root)
        self.assertEqual(closure['return_id'], recorded['return_id'])
        self.assertFalse((root/'revisions/001/returns').exists())
        if archive:
            import baseline_releases as baseline
            self.write_context('release-finalization')
            current = self.project/'baseline/current'
            candidate = self.project/'releases/release-one/baseline-candidate'
            for directory, value in ((current, 'before'), (candidate, 'after')):
                for section in baseline.SECTIONS:
                    (directory/section).mkdir(parents=True)
                    (directory/section/'README.md').write_text(value)
            observed = baseline.observe(self.project, {'release_id':'release-one', 'membership_complete':True,
                'membership_evidence':'Reviewed full membership', 'tasks':[{'id':'task-one','closed':True,'evidence':'Confirmed'}]})
            baseline.prepare(self.project, 'release-one', candidate, {'scope_hash':observed['scope_hash'],
                'base_hash':baseline.tree_hash(current), 'sections':baseline.review_hashes(candidate),
                'domain_review':{aspect:{'status':'updated','evidence':'Reviewed '+aspect} for aspect in baseline.DOMAIN_ASPECTS},
                'consistency_evidence':'Reviewed deployed behavior'})
            baseline.promote(self.project, 'release-one', observed['scope_hash'],
                {'version':'one','environment':'production','evidence':'Explicit deployment confirmation'}, analyst_confirmed=True)
            original = {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            release_exchange.review_exchange(self.project, 'release-one', {'scope_hash':observed['scope_hash'],
                'deliveries':[{'delivery_key':'demo','disposition':'complete','task_ids':['task-one'],
                              'packet_hash':baseline.tree_hash(root),'evidence':'Reviewed analysis and closed scope'}], 'other_tasks':[]})
            archived = release_exchange.archived_root(self.project, 'demo')
            self.assertEqual({p.relative_to(archived):p.read_bytes() for p in archived.rglob('*') if p.is_file()}, original)
            self.assertEqual(release_exchange._closure(self.project, 'demo', archived)[1]['return_id'], recorded['return_id'])
            self.assertIn('Archived delivery is immutable', self.cli(script,'record-analysis',self.project,'demo','--report',path,success=False))
            return
        # Changing evidence cannot silently replace the report used by closure.
        report['scenarios'][0]['actual']='Changed observation'
        path.write_text(json.dumps(report))
        self.assertIn('supersede',self.cli(script,'record-analysis',self.project,'demo','--report',path,success=False))
        report['supersedes']=Path(recorded['path']).stem;path.write_text(json.dumps(report))
        newer=self.cli(script,'record-analysis',self.project,'demo','--report',path)
        self.assertNotEqual(newer['return_id'],recorded['return_id'])
        with self.assertRaises(ValueError):
            release_exchange._closure(self.project,'demo',root)

    def test_explicit_migration_keeps_old_code_packet_and_creates_new_contract(self):
        fixtures.SddExchangeTests.enable_code_exchange(self)
        self.authorize()
        old = self.cli(fixtures.legacy.SCRIPT,'prepare',self.project,'demo','--analyst','owner')
        documents_remote=self.remote;self.remote=self.code_remote
        self.accept(old);self.remote=documents_remote
        old=self.cli(fixtures.legacy.SCRIPT,'prepare',self.project,'demo','--analyst','owner')
        self.state('mark-published','--revision','1','--manifest',old['manifest'],'--destination-role','code')
        self.git(self.code,'pull','--ff-only')
        original=(self.code/'requirements-exchange/demo/revisions/001/requirements.md').read_bytes()
        self.authorize()
        self.assertIn('documents-migration',self.prepare(success=False))
        self.write_context()
        migrated=self.cli(SCRIPT,'migrate-code-exchange',self.project,'demo','--reason','Move accepted history','--analyst-confirmed')
        self.assertEqual(migrated['status'],'copied')
        self.assertEqual(self.cli(SCRIPT,'migrate-code-exchange',self.project,'demo','--reason','Move accepted history','--analyst-confirmed')['status'],'already-copied')
        # Local copy alone is not enough: new transport verifies accepted target history.
        self.assertIn('documents-migration',self.prepare(success=False))
        self.git(self.project,'add','--all');self.git(self.project,'commit','-m','Preserve migrated exchange')
        self.git(self.project,'push','origin','HEAD:main')
        self.authorize()
        new=self.prepare()
        self.assertEqual(new['revision'],2)
        entries=self.manifest(new)['revisions']
        self.assertEqual(entries[0]['returns_contract_version'],2)
        self.assertEqual(entries[1]['returns_contract_version'],3)
        self.assertEqual((self.code/'requirements-exchange/demo/revisions/001/requirements.md').read_bytes(),original)
