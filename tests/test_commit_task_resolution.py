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
    def request(self, kind='delivery', owner='demo'):
        return tasks.request('delivery' if kind == 'delivery' else 'quarter', owner,
                             kind, owner, 'RSCON-900', 'Analyst confirms epic')

    def card(self, state, key='RSCON-123', **changes):
        return {**state['spec'], 'key': key, 'status': 'open', **changes}

    def search(self, state, cards, complete=True):
        return tasks.ingest_search(state, cards, items_pointer='', fields=FIELDS,
                                  closed_values=['closed'], complete=complete, step=state['step_id'])

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
                        '--fields', str(fields), '--closed-value', 'closed', '--complete')
            arguments = root / 'args.json'
            arguments.write_text(json.dumps({name: state['spec'][name] for name in ('summary', 'suit', 'epic')}))
            state = run('begin-create', '--step', state['step_id'], '--tool', 'fixture.create',
                        '--arguments', str(arguments), '--fields', str(fields))
            self.assertEqual(run('status')['status'], 'create-outcome-unknown')
            response.write_text(json.dumps([self.card(state)]))
            state = run('search-result', '--step', state['step_id'], '--response', str(response),
                        '--fields', str(fields), '--closed-value', 'closed', '--complete')
            self.assertEqual(state['status'], 'resolved')
            self.assertEqual(len(state['attempts']), 1)
            self.assertEqual(len(state['evidence']), 2)

    def test_response_archive_is_verbatim(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            response = root / 'raw.json'
            response.write_bytes(b'{ "a": 1 }\n')
            state = self.request()
            tasks.save_state(root / 'state.json', state, response)
            self.assertEqual(Path(state['evidence'][0]['path']).read_bytes(), response.read_bytes())
