from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import commit_tasks as tasks
from commit_message_policy import keys_for_paths, validate_binding
from analysis_task_fixture import task_binding

FIELDS = {name: '/' + name for name in ('key', 'summary', 'suit', 'epic', 'status')}


class TaskResolutionTests(unittest.TestCase):
    def test_actual_link_list_envelope_proves_epic_without_synthetic_card_fields(self):
        raw = {'result': [{'type': 'text', 'text': 'Links for RSCON-123 (1 total, page 0, has_next: False)\\n\\n1. RSCON-900 **decomposition** -> RSCON-123'}]}
        memberships = tasks.link_epics([raw], '/result/0/text')
        state = self.request()
        card = self.card(state)
        del card['epic']
        fields = {k: v for k, v in FIELDS.items() if k != 'epic'}
        with self.assertRaises(ValueError):
            tasks.ingest_search(state, [card], items_pointer='', fields=fields,
                closed_values=['closed'], complete=True, step=state['step_id'])
        tasks.ingest_search(state, [card], items_pointer='', fields=fields,
            closed_values=['closed'], complete=True, step=state['step_id'], memberships=memberships)
        self.assertEqual(state['binding']['epic'], 'RSCON-900')
        created = self.request()
        self.search(created, [])
        tasks.begin_create(created, 'mcp.create', {'summary': created['spec']['summary'], 'suit': 'task'},
                           created['step_id'], FIELDS, True)
        tasks.ingest_created(created, card, item_pointer='', fields=fields,
                             step=created['step_id'], memberships=memberships)
        self.assertEqual(created['status'], 'resolved')
        reversed_links = tasks.link_epics([{'text': 'Links for RSCON-123 (1 total, page 0, has_next: False)\n1. RSCON-123 **decomposition** -> RSCON-900'}], '/text')
        self.assertEqual(reversed_links['RSCON-123'], set())

    def test_link_list_rejects_incomplete_malformed_and_conflicting_evidence(self):
        valid = 'Links for RSCON-123 (1 total, page 0, has_next: False)\n1. RSCON-900 **decomposition** -> RSCON-123'
        for text in (valid.replace('False', 'True'), valid.replace('page 0', 'page 1'),
                     valid.replace('1 total', '2 total'), valid.replace('1.', '2.'),
                     valid.replace('-> RSCON-123', '-> RSCON-456')):
            with self.subTest(text=text), self.assertRaises(ValueError):
                tasks.link_epics([{'text': text}], '/text')
        with self.assertRaises(ValueError):
            tasks.link_epics([{'text': valid}, {'text': valid.replace('RSCON-900', 'RSCON-901')}], '/text')
        state = self.request()
        with self.assertRaises(ValueError):
            tasks.ingest_search(state, [self.card(state)], items_pointer='', fields=FIELDS,
                closed_values=['closed'], complete=True, step=state['step_id'],
                memberships={'RSCON-123': {'RSCON-901'}})

    def test_cli_archives_card_and_link_evidence_without_rewriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = self.request()
            tasks.save_state(root / 'state.json', state)
            card = self.card(state)
            del card['epic']
            (root / 'cards.json').write_text(json.dumps([card], indent=3))
            (root / 'fields.json').write_text(json.dumps({k: v for k, v in FIELDS.items() if k != 'epic'}))
            (root / 'links.json').write_text(json.dumps({'text': 'Links for RSCON-123 (1 total, page 0, has_next: False)\n\n1. RSCON-900 **decomposition** -> RSCON-123'}, indent=4))
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/commit-taskctl.py'), 'search-result',
                '--state', str(root / 'state.json'), '--step', state['step_id'], '--response', str(root / 'cards.json'),
                '--fields', str(root / 'fields.json'), '--closed-value', 'closed', '--complete',
                '--links-response', str(root / 'links.json'), '--links-text-pointer', '/text'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            saved = json.loads(result.stdout)
            self.assertEqual(saved['status'], 'resolved')
            self.assertEqual([Path(item['path']).read_bytes() for item in saved['evidence']],
                             [(root / 'cards.json').read_bytes(), (root / 'links.json').read_bytes()])

    def request(self, kind='delivery', owner='demo'):
        return tasks.request('delivery' if kind == 'delivery' else 'quarter', owner,
                             kind, owner, 'RSCON-900', 'Analyst confirms epic')

    def card(self, state, key='RSCON-123', **changes):
        return {**state['spec'], 'key': key, 'status': 'open', **changes}

    def search(self, state, cards, complete=True):
        return tasks.ingest_search(state, cards, items_pointer='', fields=FIELDS,
                                  closed_values=['closed'], complete=complete, step=state['step_id'], thematic=True)

    def test_existing_task_reused_without_creation(self):
        state = self.request()
        self.search(state, [self.card(state)])
        self.assertEqual(state['status'], 'resolved')
        self.assertEqual(state['binding']['key'], 'RSCON-123')
        self.assertEqual(state['attempts'], [])

    def test_missing_task_creation_is_bound_to_epic_and_real_response(self):
        state = self.request()
        self.search(state, [])
        arguments = {name: state['spec'][name] for name in ('summary', 'suit', 'epic')}
        with self.assertRaises(ValueError):
            tasks.begin_create(state, 'mcp.create', {**arguments, 'epic': 'RSCON-999'}, state['step_id'], FIELDS)
        tasks.begin_create(state, 'mcp.create', arguments, state['step_id'], FIELDS)
        self.assertEqual(state['status'], 'create-outcome-unknown')
        with self.assertRaises(ValueError):
            tasks.begin_create(state, 'mcp.create', arguments, state['step_id'], FIELDS)
        with self.assertRaises(ValueError):
            tasks.ingest_created(state, self.card(state, epic='RSCON-999'), item_pointer='', fields=FIELDS, step=state['step_id'])
        tasks.ingest_created(state, self.card(state), item_pointer='', fields=FIELDS, step=state['step_id'])
        self.assertEqual(state['binding']['epic'], 'RSCON-900')

    def test_separate_epic_link_requires_verified_readback(self):
        state = self.request()
        self.search(state, [])
        arguments = {name: state['spec'][name] for name in ('summary', 'suit')}
        tasks.begin_create(state, 'mcp.create', arguments, state['step_id'], FIELDS, True)
        with self.assertRaises(ValueError):
            tasks.ingest_created(state, self.card(state, epic=None), item_pointer='', fields=FIELDS, step=state['step_id'])
        self.assertEqual(state['status'], 'create-outcome-unknown')
        tasks.ingest_created(state, self.card(state), item_pointer='', fields=FIELDS, step=state['step_id'])
        self.assertEqual(state['status'], 'resolved')

    def test_lost_response_requires_search_and_never_blind_retry(self):
        state = self.request()
        self.search(state, [])
        arguments = {name: state['spec'][name] for name in ('summary', 'suit', 'epic')}
        tasks.begin_create(state, 'mcp.create', arguments, state['step_id'], FIELDS)
        found = deepcopy(state)
        self.search(found, [self.card(found)])
        self.assertEqual(found['status'], 'resolved')
        self.search(state, [])
        self.assertEqual(state['status'], 'creation-reconciliation-required')
        with self.assertRaises(ValueError):
            tasks.begin_create(state, 'mcp.create', arguments, state['step_id'], FIELDS)

    def test_incomplete_search_and_stale_step_do_not_authorize_creation(self):
        state = self.request()
        before = deepcopy(state)
        with self.assertRaises(ValueError):
            self.search(state, [], complete=False)
        self.assertEqual(state, before)
        with self.assertRaises(ValueError):
            tasks.ingest_search(state, [], items_pointer='', fields=FIELDS,
                                closed_values=['closed'], complete=True, step='stale')
        self.assertEqual(state, before)

    def test_duplicate_or_closed_task_requires_explicit_selection(self):
        for cards in ('duplicate', 'closed', 'different-title'):
            state = self.request()
            candidates = ([self.card(state), self.card(state, 'RSCON-456')] if cards == 'duplicate'
                          else [self.card(state, status='closed')] if cards == 'closed'
                          else [self.card(state, summary='[SA][PR] old title')])
            self.search(state, candidates)
            self.assertEqual(state['status'], 'analyst-selection-required')
            with self.assertRaises(ValueError):
                tasks.select(state, 'RSCON-123', '')
            tasks.select(state, 'RSCON-123', 'Analyst explicitly selects this task')
            self.assertEqual(state['status'], 'resolved')

    def test_other_epic_or_issue_type_is_not_a_match(self):
        state = self.request()
        self.search(state, [self.card(state, epic='RSCON-999'), self.card(state, suit='story')])
        self.assertEqual(state['status'], 'create-ready')

    def test_legacy_an_binding_does_not_authorize_new_commit(self):
        with self.assertRaises(ValueError):
            validate_binding(dict(schema_version=1, provider='sbertrek', role='AN',
                                  key='RSCON-123', confirmation='old'))

    def test_baseline_for_two_deliveries_requires_both_baseline_keys(self):
        first = task_binding(kind='baseline')
        first['owner'] = {'type': 'delivery', 'id': 'a'}
        second = {**first, 'key': 'RSCON-456', 'epic': 'RSCON-901', 'owner': {'type': 'delivery', 'id': 'b'}}
        registry = dict(schema_version=1, tasks={'a': first, 'b': second}, paths={'baseline/current/': ['a', 'b']})
        files = {'work-task-bindings.json': json.dumps(registry)}
        self.assertEqual(keys_for_paths(['baseline/current/domain.md'], files.get), {'RSCON-123', 'RSCON-456'})
        registry['tasks']['a'] = task_binding()
        files['work-task-bindings.json'] = json.dumps(registry)
        with self.assertRaises(ValueError):
            keys_for_paths(['baseline/current/domain.md'], files.get)

    def test_unowned_context_is_not_assigned_to_current_quarter(self):
        with self.assertRaisesRegex(ValueError, 'поставку.*квартал'):
            keys_for_paths(['context/overview.md'], lambda _: None)
        files = {'context/analysis-task.json': json.dumps(task_binding(kind='context', owner_id='2027-Q1'))}
        self.assertEqual(keys_for_paths(['context/overview.md'], files.get), {'RSCON-123'})

    def test_quarter_epic_is_asked_at_start_and_preserves_other_quarters(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            subprocess.run(['git', 'init', '-b', 'feature/planning', str(project)], check=True, capture_output=True)
            command = [sys.executable, str(ROOT / 'scripts/commit-taskctl.py'), 'quarter-epic',
                       '--project', str(project), '--quarter', '2027-Q1', '--as-of', '2026-11-06']
            def run(*extra):
                result = subprocess.run(command + list(extra), capture_output=True, text=True)
                return result, json.loads(result.stdout)
            _, payload = run()
            self.assertEqual(payload['status'], 'analyst-epic-required')
            result, payload = run('--key', 'RSCON-900', '--confirmation', 'Confirmed quarter epic')
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(payload['status'], 'ready')
            _, payload = run()
            self.assertEqual(payload['epic']['key'], 'RSCON-900')
            command[command.index('2027-Q1')] = '2027-Q2'
            result, _ = run('--key', 'RSCON-900', '--confirmation', 'Duplicate epic')
            self.assertNotEqual(result.returncode, 0)
            registry = json.loads((project / 'planning/task-bindings.json').read_text())
            self.assertNotIn('2027-Q2', registry['epics'])

    def test_explicit_reconciliation_can_authorize_creation(self):
        state = self.request()
        self.search(state, [self.card(state, summary='[SA][PR] another delivery')])
        with self.assertRaises(ValueError):
            tasks.confirm_create(state, '')
        tasks.confirm_create(state, 'Analyst confirms candidate belongs to another delivery; create this task')
        self.assertEqual(state['status'], 'create-ready')
        self.assertEqual(len(state['decisions']), 1)

    def test_work_mapping_cannot_replace_delivery_task_with_context(self):
        files = {'work-task-bindings.json': json.dumps(dict(schema_version=1,
                 tasks={'wrong': task_binding(kind='context')},
                 paths={'features/demo/': ['wrong']}))}
        with self.assertRaises(ValueError):
            keys_for_paths(['features/demo/requirements.md'], files.get)

    def test_cli_resumes_unknown_creation_using_read_only_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / 'state.json'
            fields = root / 'fields.json'
            fields.write_text(json.dumps(FIELDS))
            def run(command, *arguments):
                result = subprocess.run([sys.executable, str(ROOT / 'scripts/commit-taskctl.py'), command,
                                         '--state', str(state_path), *arguments], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return json.loads(result.stdout)
            state = run('request', '--owner-type', 'delivery', '--owner-id', 'demo', '--kind', 'delivery',
                        '--title', 'demo', '--epic', 'RSCON-900', '--confirmation', 'Confirmed epic')
            response = root / 'raw.json'
            response.write_text('[]')
            state = run('search-result', '--step', state['step_id'], '--response', str(response),
                        '--fields', str(fields), '--closed-value', 'closed', '--complete', '--thematic')
            arguments = root / 'args.json'
            arguments.write_text(json.dumps({name: state['spec'][name] for name in ('summary', 'suit', 'epic')}))
            state = run('begin-create', '--step', state['step_id'], '--tool', 'fixture.create',
                        '--arguments', str(arguments), '--fields', str(fields))
            self.assertEqual(run('status')['status'], 'create-outcome-unknown')
            response.write_text(json.dumps([self.card(state)]))
            state = run('search-result', '--step', state['step_id'], '--response', str(response),
                        '--fields', str(fields), '--closed-value', 'closed', '--complete', '--thematic')
            self.assertEqual(state['status'], 'resolved')
            self.assertEqual(len(state['attempts']), 1)
            self.assertEqual(len(state['evidence']), 2)

    def test_delivery_context_cannot_be_redirected_to_quarter_epic(self):
        files = {'features/demo/context/analysis-task.json': json.dumps(task_binding(kind='context'))}
        with self.assertRaisesRegex(ValueError, 'эпик поставки'):
            keys_for_paths(['features/demo/context/overview.md'], files.get)
        bound = task_binding(kind='context')
        bound['owner'] = {'type': 'delivery', 'id': 'demo'}
        files['features/demo/context/analysis-task.json'] = json.dumps(bound)
        self.assertEqual(keys_for_paths(['features/demo/context/overview.md'], files.get), {'RSCON-123'})

    def test_baseline_and_context_use_same_epic_as_their_owner(self):
        binding = task_binding(kind='context')
        files = {'context/analysis-task.json': json.dumps(binding),
                 'planning/task-bindings.json': json.dumps(dict(schema_version=1, quarters={},
                     epics={'2026-Q3': dict(key='RSCON-901')}))}
        with self.assertRaisesRegex(ValueError, 'том же эпике'):
            keys_for_paths(['context/overview.md'], files.get)
        binding['epic'] = 'RSCON-901'
        files['context/analysis-task.json'] = json.dumps(binding)
        self.assertEqual(keys_for_paths(['context/overview.md'], files.get), {'RSCON-123'})
        binding['owner'] = {'type': 'delivery', 'id': 'demo'}
        files = {'features/demo/analysis-task.json': json.dumps(task_binding()),
                 'features/demo/context/analysis-task.json': json.dumps(binding)}
        with self.assertRaisesRegex(ValueError, 'том же эпике'):
            keys_for_paths(['features/demo/context/overview.md'], files.get)

    def test_response_archive_is_verbatim(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            response = root / 'raw.json'
            response.write_bytes(b'{ "a": 1 }\n')
            state = self.request()
            tasks.save_state(root / 'state.json', state, response)
            self.assertEqual(Path(state['evidence'][0]['path']).read_bytes(), response.read_bytes())

    def test_prefix_rejected_and_unused_request_can_be_corrected(self):
        for prefix in tasks.PREFIXES.values():
            with self.assertRaisesRegex(ValueError, 'без служебного префикса'):
                tasks.request('delivery', 'demo', 'delivery', prefix + ' demo', 'RSCON-900', 'yes')
        state = self.request()
        old = deepcopy(state)
        tasks.revise_request(state, 'Corrected', state['step_id'])
        self.assertEqual(state['corrections'], [old['spec']])
        self.assertNotEqual(state['step_id'], old['step_id'])
        self.assertEqual(state['spec']['owner'], old['spec']['owner'])
        for field in ('attempts', 'evidence', 'candidates', 'binding'):
            used = deepcopy(old)
            used[field] = ['recorded']
            before = deepcopy(used)
            with self.assertRaises(ValueError):
                tasks.revise_request(used, 'New', used['step_id'])
            self.assertEqual(used, before)

    def test_prefix_only_search_cannot_authorize_delivery_creation(self):
        state = self.request()
        tasks.ingest_search(state, [], items_pointer='', fields=FIELDS,
                            closed_values=[], complete=True, step=state['step_id'])
        self.assertEqual(state['status'], 'related-search-required')
        with self.assertRaises(ValueError):
            tasks.confirm_create(state, 'yes')
        self.search(state, [self.card(state, summary='Legacy simulation PoC')])
        self.assertEqual(state['status'], 'analyst-selection-required')
        self.assertEqual(state['related_candidates'][0]['summary'], 'Legacy simulation PoC')
        with self.assertRaises(ValueError):
            tasks.select(state, 'RSCON-123', 'reuse')
        tasks.confirm_create(state, 'Separate work: different scope confirmed by analyst')
        self.assertEqual(state['status'], 'create-ready')

    def test_status_metadata_only_required_for_in_scope_prefixed_candidates(self):
        state = self.request()
        tasks.ingest_search(state, [self.card(state, epic='RSCON-999')], items_pointer='',
                            fields=FIELDS, closed_values=[], complete=True, thematic=True, step=state['step_id'])
        self.assertEqual(state['status'], 'create-ready')
        state = self.request()
        with self.assertRaisesRegex(ValueError, 'закрытые состояния'):
            tasks.ingest_search(state, [self.card(state)], items_pointer='', fields=FIELDS,
                                closed_values=[], complete=True, step=state['step_id'])

    def test_old_create_ready_state_requires_thematic_search(self):
        state = self.request()
        state['status'] = 'create-ready'
        args = {name: state['spec'][name] for name in ('summary', 'suit', 'epic')}
        with self.assertRaisesRegex(ValueError, 'тематический поиск'):
            tasks.begin_create(state, 'fixture.create', args, state['step_id'], FIELDS)
        self.search(state, [])
        tasks.begin_create(state, 'fixture.create', args, state['step_id'], FIELDS)
        self.assertEqual(len(state['attempts']), 1)

    def test_cli_parses_text_envelope_and_archives_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = self.request()
            tasks.save_state(root / 'state.json', state)
            raw = json.dumps({'content': [{'text': json.dumps([self.card(state)])}]}, indent=4).encode()
            (root / 'response.json').write_bytes(raw)
            (root / 'fields.json').write_text(json.dumps(FIELDS))
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/commit-taskctl.py'), 'search-result',
                '--state', str(root / 'state.json'), '--step', state['step_id'],
                '--response', str(root / 'response.json'), '--response-text-pointer', '/content/0/text',
                '--fields', str(root / 'fields.json'), '--complete', '--closed-value', 'closed'],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            saved = json.loads(result.stdout)
            self.assertEqual(saved['status'], 'resolved')
            self.assertEqual(Path(saved['evidence'][0]['path']).read_bytes(), raw)
