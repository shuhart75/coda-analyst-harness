"""Decision delivery exercises real Git transport and the deployed receiver."""
import json
import subprocess
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
import test_sdd_exchange as helpers


class AnalystDeliveryTests(unittest.TestCase):
    for _name, _method in vars(helpers.SddExchangeTests).items():
        if callable(_method) and not _name.startswith('test_'):
            locals()[_name] = _method
    del _name, _method

    def send(self, success=True, confirm=True):
        return self.cli(helpers.legacy.SCRIPT, 'send-by-decision', self.project, 'demo',
                        '--reason', 'Передать как есть по моему решению', '--analyst', 'owner',
                        *(['--analyst-confirmed'] if confirm else []), success=success)

    def receiver(self, sent, *, apply=False, success=True):
        operation = json.loads(Path(sent['operation']).read_text())
        exchange = Path(operation['checkout'])/'requirements-exchange'
        # The ordinary code root is changed only here by the developer fixture.
        return self.cli(exchange/'sdd-receive.py', '--packet', exchange/'demo',
                        '--revision', sent['revision'], '--code-root', self.code,
                        *(['--apply'] if apply else []), success=success)

    def test_no_audit_dirty_source_stale_pins_and_apply(self):
        self.enable_code_exchange()
        (self.code/'source.py').write_text('new code\n')
        before = self.code_snapshot()
        first = self.send()
        self.assertFalse(first['publication_confirmed'])
        self.assertEqual(first['delivery_decision']['checks']['audit'], 'not-performed')
        self.assertEqual(before, self.code_snapshot())
        result = self.receiver(first, apply=True)
        self.assertEqual(result['source']['status'], 'not-performed')
        self.assertTrue(result['applied'])
        self.assertEqual(self.receiver(first, apply=True)['status'], 'already-current')
        state = json.loads((self.feature/'requirements-state.json').read_text())
        self.assertNotEqual(state.get('delivery_audit', {}).get('state'), 'confirmed')

    def test_pending_candidate_is_replaced_without_cancellation_dialogue(self):
        self.enable_code_exchange()
        self.authorize()
        old = self.prepare()
        old_manifest = Path(old['manifest']).read_bytes()
        old_state = json.loads((self.feature/'requirements-state.json').read_text())
        self.change_proposal()
        sent = self.send()
        self.assertEqual(sent['revision'], 2)
        self.assertEqual(Path(old['manifest']).read_bytes(), old_manifest)
        current = json.loads((self.feature/'requirements-state.json').read_text())
        self.assertEqual(old_state['delivery_stages']['revisions'][0]['entry'],current['delivery_stages']['revisions'][0]['entry'])
        self.assertEqual(self.git(self.remote,'rev-parse',old['request_branch']),old['request_commit'])
        self.assertEqual(current['delivery_audit'], old_state['delivery_audit'])

    def test_retry_and_acceptance_need_no_mark_published_audit(self):
        self.enable_code_exchange()
        first = self.send()
        self.assertEqual(self.send()['request_commit'], first['request_commit'])
        self.accept(first)
        accepted = self.send()
        self.assertTrue(accepted['publication_confirmed'])
        self.assertEqual(accepted['status'], 'published')
        state = json.loads((self.feature/'requirements-state.json').read_text())
        self.assertTrue(state['delivery_stages']['revisions'][0]['publication_confirmed'])

    def test_explicit_decision_is_required(self):
        self.enable_code_exchange()
        before = self.code_snapshot()
        self.assertIn('решение',self.send(success=False,confirm=False))
        self.assertEqual(self.code_snapshot(),before)

    def test_stale_package_checksum_and_traceability_are_not_gates(self):
        self.enable_code_exchange()
        package = self.sdd/'package.json'
        content=json.loads(package.read_text());content['requirements_sha256']='0'*64
        content['changes'][0]['coverage']=[];package.write_text(json.dumps(content))
        (self.feature/'requirements.md').write_text('# Новый текущий текст\n')
        materials=self.feature/'materials';materials.mkdir()
        (materials/'SHA256SUMS').write_text('старый список\n')
        (materials/'guide.md').write_text('Материалы как есть\n')
        sent=self.send()
        result=self.receiver(sent)
        self.assertEqual(result['status'],'ready')
        op=json.loads(Path(sent['operation']).read_text());copied=Path(op['checkout'])/'requirements-exchange/demo/revisions/001'
        self.assertEqual((copied/'sdd/package.json').read_bytes(), package.read_bytes())
        self.assertEqual((copied/'materials/SHA256SUMS').read_bytes(),(materials/'SHA256SUMS').read_bytes())

    def test_corrupted_transport_blocks_receiver(self):
        self.enable_code_exchange();sent=self.send()
        op=json.loads(Path(sent['operation']).read_text())
        path=Path(op['checkout'])/'requirements-exchange/demo/revisions/001/requirements.md'
        path.write_text('corrupted')
        self.assertIn('сумма',self.receiver(sent,success=False))
        self.assertIn('снимок',self.send(success=False))

    def test_path_escape_is_rejected_by_receiver(self):
        self.enable_code_exchange()
        package=self.sdd/'package.json';content=json.loads(package.read_text())
        content['changes'][0]['change_id']='../../escape';package.write_text(json.dumps(content))
        sent=self.send()
        self.assertIn('адрес',self.receiver(sent,apply=True,success=False))
        self.assertFalse((self.code/'escape').exists())

    def test_symlinks_are_not_transported(self):
        self.enable_code_exchange()
        (self.sdd/'secret').symlink_to(self.feature/'requirements.md')
        self.assertIn('regular',self.send(success=False))

    def test_next_revision_uses_separate_receiver_change(self):
        self.enable_code_exchange();first=self.send();result=self.receiver(first,apply=True)
        original=Path(result['changes'][0]['path']);(original/'design.md').write_text('Developer work')
        self.change_proposal();second=self.send();other=self.receiver(second,apply=True)
        self.assertEqual(second['revision'],2)
        self.assertNotEqual(other['changes'][0]['path'],str(original))
        self.assertEqual((original/'design.md').read_text(),'Developer work')

    def test_push_failure_has_durable_retry(self):
        self.enable_code_exchange()
        hook=self.remote/'hooks/pre-receive';hook.write_text('#!/bin/sh\nexit 1\n');hook.chmod(0o755)
        self.assertIn('Снимок сохранён',self.send(success=False))
        journals=list((self.runtime/'decision-transfers').glob('*/*.json'))
        self.assertEqual(len(journals),1)
        commit=json.loads(journals[0].read_text())['request_commit']
        hook.unlink();sent=self.send()
        self.assertEqual(sent['request_commit'],commit)
        self.assertEqual(sent['revision'],1)

    def test_cancelled_history_and_snapshot_are_preserved(self):
        self.enable_code_exchange();self.authorize();old=self.prepare()
        self.cli(helpers.legacy.SCRIPT,'cancel-publication',self.project,'demo','--revision','1',
                 '--expected-commit',old['request_commit'],'--reason','Заменить','--analyst-confirmed')
        before=json.loads((self.feature/'requirements-state.json').read_text())['delivery_stages']['revisions'][0]
        self.change_proposal();sent=self.send()
        after=json.loads((self.feature/'requirements-state.json').read_text())['delivery_stages']['revisions'][0]
        self.assertEqual(after['publication_cancellation'],before['publication_cancellation'])
        self.assertEqual(after['entry'],before['entry'])
        self.assertEqual(sent['revision'],2)

    def test_strict_receiver_does_not_accept_a_missing_decision(self):
        self.enable_code_exchange();sent=self.send()
        op=json.loads(Path(sent['operation']).read_text());path=Path(op['checkout'])/'requirements-exchange/demo/manifest.json'
        manifest=json.loads(path.read_text());manifest['revisions'][0].pop('delivery_decision');path.write_text(json.dumps(manifest))
        self.assertIn('explicit bound',self.receiver(sent,success=False))

    def test_first_transfer_needs_no_state_registration(self):
        self.enable_code_exchange();(self.feature/'requirements-state.json').unlink()
        sent=self.send();self.assertEqual(sent['revision'],1)
        self.assertEqual(self.receiver(sent)['status'],'ready')

    def test_target_product_files_and_existing_revision_bytes_survive(self):
        self.enable_code_exchange();first=self.send();self.accept(first);self.send()
        self.change_proposal();second=self.send()
        self.assertEqual(second['revision'],2)
        op=json.loads(Path(second['operation']).read_text());clone=Path(op['checkout'])
        prior=clone/'requirements-exchange/demo/revisions/001/requirements.md'
        self.assertEqual(prior.read_bytes(),(self.feature/'requirements.md').read_bytes())
        changed=self.git(clone,'diff','--name-only','origin/main','HEAD').splitlines()
        self.assertTrue(all(name.startswith('requirements-exchange/') for name in changed))
        self.assertEqual(self.receiver(second)['status'],'ready')

    def test_ignored_material_files_are_sent_as_exact_bytes(self):
        self.enable_code_exchange()
        materials=self.feature/'materials';materials.mkdir()
        (materials/'.gitignore').write_text('*.json\n')
        (materials/'data.json').write_text('{"value": 1}\n')
        sent=self.send()
        blob=subprocess.check_output(['git','-C',str(self.remote),'show',sent['request_commit']+
            ':requirements-exchange/demo/revisions/001/materials/data.json'])
        self.assertEqual(blob,(materials/'data.json').read_bytes())

    def test_new_input_after_failed_push_preserves_reserved_history(self):
        self.enable_code_exchange()
        hook=self.remote/'hooks/pre-receive';hook.write_text('#!/bin/sh\nexit 1\n');hook.chmod(0o755)
        self.send(success=False);self.change_proposal();hook.unlink()
        sent=self.send();self.assertEqual(sent['revision'],2)
        state=json.loads((self.feature/'requirements-state.json').read_text())
        self.assertEqual([r['entry']['revision'] for r in state['delivery_stages']['revisions']],[1,2])
        self.assertIn('replacement_by_decision',state['delivery_stages']['revisions'][0])
