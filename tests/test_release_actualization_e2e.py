from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_execution_collaboration
import test_tracker_history
import test_tracker_release
import test_trackerctl


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / 'fixtures/release-actualization/expected.json'
FIELDS = ('Task ID', 'SberTrek', 'Jira', 'Summary', 'Kind', 'Role', 'Estimate (дн)',
          'Executor', 'Planned Start', 'Planned Finish', 'Actual Start', 'Actual Finish',
          'Status', 'Progress %', 'Related Stories')
NETWORK_GUARD = '''import sys
def deny_network(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.gethostbyname", "socket.sendto"}:
        raise RuntimeError("Offline release test: network access denied")
sys.addaudithook(deny_network)
'''


class ReleaseActualizationEndToEndTests(unittest.TestCase):
    prepare_workspace = test_execution_collaboration.ExecutionCollaborationTests.prepare_workspace
    create_seed = test_execution_collaboration.ExecutionCollaborationTests.create_seed
    configure_identity = test_execution_collaboration.ExecutionCollaborationTests.configure_identity
    git = test_execution_collaboration.ExecutionCollaborationTests.git
    collaboration = test_execution_collaboration.ExecutionCollaborationTests.collaboration
    run_tool = test_trackerctl.DirectTrackerWorkflowTests.run_tool
    write = test_trackerctl.DirectTrackerWorkflowTests.write
    sber_issue = test_trackerctl.DirectTrackerWorkflowTests.sber_issue
    call = test_tracker_history.AdaptiveHistoryTests.call
    ingest = test_tracker_history.AdaptiveHistoryTests.ingest
    linked_call = test_tracker_release.ReleaseWorkflowTests.linked_call

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='release-actualization-test-')
        self.addCleanup(temporary.cleanup)
        self.prepare_lab(Path(temporary.name))

    def prepare_lab(self, root):
        self.root = root.resolve()
        self.expected = json.loads(FIXTURE.read_text(encoding='utf-8'))
        guard = self.root / 'offline'
        guard.mkdir()
        (guard / 'sitecustomize.py').write_text(NETWORK_GUARD, encoding='utf-8')
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(('GIT_', 'ANALYST_HARNESS_', 'CODA_ANALYST_'))}
        environment.update(HOME=str(self.root), XDG_CONFIG_HOME=str(self.root / 'config'),
                           GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_ALLOW_PROTOCOL='file', GIT_TERMINAL_PROMPT='0',
                           GIT_AUTHOR_NAME='Offline Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                           GIT_COMMITTER_NAME='Offline Test', GIT_COMMITTER_EMAIL='test@example.invalid',
                           PYTHONPATH=str(guard), PYTHONDONTWRITEBYTECODE='1',
                           HARNESS_TODAY=self.expected['today'])
        self.environment_patch = patch.dict(os.environ, environment, clear=True)
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)
        self.workspace, self.project, self.remote, self.environment = self.prepare_workspace(self.root)
        self.state = self.workspace / '.workspace-state'
        os.environ.update(self.environment)
        self.collab('migrate', '--analyst', 'offline')
        self.collab('start', '--feature', 'registry')
        (self.state / 'active-mode.md').write_text('mode: execution-update\n', encoding='utf-8')
        self.registry = self.project / 'features/registry/execution/tasks.md'
        self.groups_path = self.registry.with_name('qa-groups.json')
        self.gantt = self.project / 'planning/2026-Q3/gantt'
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()
        self.rows = {}
        self.prepare_sources()
        self.git(self.project, 'add', '--', 'features', 'planning')
        self.git(self.project, 'commit', '-m', 'Prepare offline release scenario')
        self.initial_head = self.git(self.project, 'rev-parse', 'HEAD')
        self.initial_main = self.git(self.remote, 'rev-parse', 'main')
        self.write(self.state / 'tracker-config.json', {
            'schema_version': 4, 'primary_provider': 'sbertrek', 'setup_complete': True,
            'jira_enabled': False, 'projects': {'sbertrek': ['LAB'], 'jira': []},
            'development_issue_types': ['story', 'task'], 'participants': {'sbertrek': {}, 'jira': {}},
            'status_rules': {'sbertrek': {'completed': ['done'], 'excluded': ['cancelled']},
                             'jira': {'completed': [], 'excluded': []}}})

    def collab(self, *args):
        return self.collaboration(self.workspace, self.environment, *args)

    def command(self, *args, expected=0):
        result = subprocess.run(args, cwd=self.root, env=self.environment,
                                text=True, capture_output=True, timeout=90)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def row(self, task_id, *, estimate='2', status='unknown', progress='unknown'):
        role = task_id.split('/')[-1] if '/' in task_id else 'QA'
        return dict(zip(FIELDS, (task_id, task_id.split('/')[0] if '/' in task_id else '-', '-',
                                'Synthetic ' + task_id, 'real', role, estimate,
                                {'BE': 'B1', 'FE': 'F1', 'QA': 'Q1'}[role],
                                '2026-09-01', '2026-09-02', '-', '-', status, progress, 'STORY-REGISTRY')))

    def write_registry(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('| ' + ' | '.join(FIELDS) + ' |\n|' + '---|' * len(FIELDS) + '\n'
                        + ''.join('| ' + ' | '.join(row[field] for field in FIELDS) + ' |\n'
                                  for row in rows), encoding='utf-8')

    def prepare_sources(self):
        for task_id in self.expected['members'] + self.expected['remainder']:
            self.rows[task_id] = self.row(task_id)
        self.rows['QA-REGISTRY'] = self.row('QA-REGISTRY', estimate='8.307692')
        self.rows['QA-REST'] = self.row('QA-REST', estimate='0.692308')
        self.write_registry(self.registry, self.rows.values())
        broken = copy.deepcopy(self.expected['groups'])
        broken['groups'][0]['estimate'] = '8.307692'
        broken['groups'][1].update(members=['LAB-201/BE'], estimate='0.692308')
        self.write(self.groups_path, broken)
        other = self.row('LAB-301/FE', status='planned', progress='0')
        other['Related Stories'] = 'STORY-OTHER'
        self.other_registry = self.project / 'features/other/execution/tasks.md'
        self.write_registry(self.other_registry, [other, {**self.row('QA-OTHER', estimate='1'),
                                                       'Related Stories': 'STORY-OTHER'}])
        for feature, members in (('registry', list(self.rows)), ('other', ['LAB-301/FE', 'QA-OTHER'])):
            mapping = self.project / f'features/{feature}/planning/actualization.md'
            mapping.parent.mkdir(parents=True, exist_ok=True)
            mapping.write_text('| Story ID | Summary | Baseline Start | Baseline Duration (дн) | Actualization State | Mapping Mode | Replaced By | Residual Virtual Tasks | Depends On |\n'
                               '|---|---|---|---|---|---|---|---|---|\n'
                               f'| STORY-{feature.upper()} | Synthetic delivery | 2026-09-01 | 5 | materialized | explicit | {", ".join(members)} | | |\n', encoding='utf-8')
            include = self.gantt / f'includes/actual-progress/FEATURE-{feature}.puml'
            include.parent.mkdir(parents=True, exist_ok=True)
            include.write_text(f'[Old {feature}] as [OLD_{feature.upper()}] happens at 2026/11/06\n', encoding='utf-8')
        (self.project / 'planning/team.md').write_text('| Role | Resources |\n|---|---|\n| BE | B1 |\n| FE | F1 |\n| QA | Q1 |\n', encoding='utf-8')
        (self.gantt / 'actual-progress.puml').write_text('@startgantt\n!include includes/actual-progress/FEATURE-registry.puml\n!include includes/actual-progress/FEATURE-other.puml\n@endgantt\n', encoding='utf-8')
        (self.gantt / 'actual-progress-confluence.puml').write_text('Initial derived export\n', encoding='utf-8')
        for name in ('quarter-plan', 'commander-plan'):
            (self.gantt / f'{name}.puml').write_text('@startgantt\n[Approved plan] starts 2026/09/01\n@endgantt\n', encoding='utf-8')

    def evidence(self, path):
        return {'file': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'quote': path.read_text(encoding='utf-8')}

    def history_entry(self, task_id):
        key, role = task_id.split('/')
        lines = ['2026-08-01T12:00:00+00:00 CREATE by actor']
        if key != 'LAB-112':
            lines.append('2026-08-02T12:00:00+00:00 assigned_to CREATE -> dev')
        lines.append('2026-08-03T12:00:00+00:00 assignee changed dev -> qa')
        text = f'Records: {len(lines)}; offset: 0; more: False\n' + '\n'.join(lines)

        def span(value, start=0):
            offset = text.index(value, start)
            return {'start': offset, 'end': offset + len(value), 'quote': value}

        def segment(value):
            reference = span(value)
            reference.pop('quote')
            return {**reference, 'sha256': hashlib.sha256(value.encode()).hexdigest()}

        segments = [{'kind': 'context', 'source': segment(text.splitlines()[0]), 'reason': 'Synthetic pagination'},
                    {'kind': 'metadata', 'source': segment(lines[0]), 'reason': 'Actor is not an assignee'}]
        for line in lines[1:]:
            start = text.index(line)
            creation = 'CREATE' in line
            segments.append({'kind': 'assignment', 'source': segment(line),
                             'field': span('assigned_to' if creation else 'assignee', start),
                             'at': span(line.split()[0], start), 'before': None if creation else span('dev', start),
                             'after': span('dev' if creation else 'qa', start),
                             'operation': {'kind': 'add' if creation else 'change',
                                           'source': span('CREATE' if creation else 'changed', start)}})
        source = self.inputs / f'{key}-history.txt'
        source.write_text(text, encoding='utf-8')
        snapshot = self.write(self.inputs / f'{key}-snapshot.json', {'key': key, 'assignee': 'qa', 'status': 'created'})
        call = {**self.call('sbertrek'), 'arguments': {'key': key}}
        return {'provider': 'sbertrek', 'key': key, 'role': role, 'response_file': str(source), 'format': 'text',
                'sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'call': call,
                'mapping': {'request_key': '/key', 'text': '', 'text_extraction': {
                    'schema_version': 1, 'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                    'segments': segments, 'pagination': {'count': span(str(len(lines))),
                                                        'start': span('0'), 'has_next': span('False')}}},
                'snapshot': {'response_file': str(snapshot), 'sha256': hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                             'call': {**call, 'tool': 'synthetic-snapshot-reader'},
                             'mapping': {'key': '/key', 'assignee': '/assignee', 'status': '/status'}}}

    def collect(self, *, decision_text=None):
        self.run_tool(self.state, 'config-status')
        run = self.run_tool(self.state, 'begin', '--adaptive', '--scope-kind', 'release',
                            '--scope-provider', 'sbertrek', '--scope-id', self.expected['release'],
                            '--label', 'Offline synthetic release', '--scope-source', 'analyst', '--intent', 'update-planning')
        keys = [task.split('/')[0] for task in self.expected['members']]
        call = self.linked_call(run, {'owner': 'REL-1', 'edges': [{'type': 'ships', 'target': key} for key in keys],
                                      'size': len(keys), 'offset': 0})
        cards = [self.sber_issue(task.split('/')[0], summary=('Synthetic role exception' if task == 'LAB-112/BE'
                                else f'[{task.split("/")[1]}] Synthetic delivery')) for task in self.expected['members']]
        self.ingest(run, {'issues': cards}, call)
        self.run_id = run['run_id']
        self.run_tool(self.state, 'reconcile', '--run-id', self.run_id)
        self.collab('set-execution-scope', '--feature', 'registry', '--quarter', '2026-Q3',
                    '--run-id', self.run_id, '--reason', 'Synthetic release decision', '--analyst-confirmed')
        self.decision = self.inputs / 'decision.txt'
        self.decision.write_text(decision_text or 'Synthetic approval: LAB-112 is BE in registry. Keep nonmembers unchanged. '
                                 'Approve twelve release members and three remainder members; QA shares 7.2 and 1.8. '
                                 'Accept development dates from expected.json; unknown start and estimate stay unknown.', encoding='utf-8')
        supplement = self.write(self.inputs / 'supplement.json', {'issues': [
            self.sber_issue('LAB-201'), self.sber_issue('LAB-202', summary='[FE] Protected form', status='cancelled'),
            self.sber_issue('LAB-203', summary='Protected actions without prefix', status='cancelled')]})
        from tracker_workflow import tql_units
        self.manifest = {
            'schema_version': 1, 'analyst_confirmed': True, 'decision_source': 'Synthetic fixture approval',
            'provider': 'sbertrek', 'participants': {'dev': 'BE', 'qa': 'QA'},
            'status_rules': {'not_started': ['created'], 'qa_completed': [], 'cancelled': ['cancelled']},
            'responses': [self.history_entry(task) for task in self.expected['members']],
            'release_scope_decisions': {'schema_version': 1, 'provider': 'sbertrek', 'release': 'REL-1',
                'analyst_confirmed': True, 'source': self.evidence(self.decision),
                'tasks': [{'key': 'LAB-112', 'feature': 'registry', 'role': 'BE'}]},
            'supplemental_responses': [{'keys': ['LAB-201', 'LAB-202', 'LAB-203'], 'response_file': str(supplement),
                'sha256': hashlib.sha256(supplement.read_bytes()).hexdigest(),
                'call': self.call('sbertrek', tql_units(['LAB-201', 'LAB-202', 'LAB-203']))}]}

    def review(self, name, *, confirmed=False):
        value = copy.deepcopy(self.manifest)
        value.update(expected_head=self.initial_head, reviewed_registries={
            self.registry.relative_to(self.project).as_posix(): hashlib.sha256(self.registry.read_bytes()).hexdigest()})
        if confirmed:
            value['qa_confirmations'] = [{'feature': 'registry', 'task_id': task_id, 'kind': 'reviewed-fields',
                'analyst_confirmed': True, 'fields': fields, 'source': self.evidence(self.decision)}
                for task_id, fields in self.expected['qa_fields'].items()]
        path = self.write(self.inputs / f'{name}.json', value)
        return self.run_tool(self.state, 'history-review', '--run-id', self.run_id,
                             '--project-root', str(self.project), '--manifest', str(path))

    def qa_check(self, review, *, expected=0):
        return self.run_tool(self.state, 'qa-application-check', '--project-root', str(self.project),
                             '--review-file', review['review_file'], expected=expected)

    def generate(self, *, expected=0):
        return self.command(sys.executable, str(ROOT / 'scripts/sync-quarter-gantt.py'),
                            str(self.gantt), '--actual-only', expected=expected)

    def snapshot(self, root):
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*')
                if path.is_file() and '.git' not in path.relative_to(root).parts}

    def test_offline_release_from_collection_to_save_preview(self):
        protected = {task: copy.deepcopy(self.rows[task]) for task in self.expected['remainder']}
        source_before = self.snapshot(self.project)
        self.collect()
        inputs_before = self.snapshot(self.inputs)
        collection_before = {path: path.read_bytes() for path in self.state.glob('*-response.json')}
        collection_before[self.state / 'links.json'] = (self.state / 'links.json').read_bytes()
        reconciled = self.state / 'tracker-runs' / self.run_id / 'reconciled.json'
        reconciled_before = reconciled.read_bytes()
        blocked = self.review('diagnostic')
        blocked_bytes = Path(blocked['review_file']).read_bytes()
        partition = blocked['feature_qa_proposals'][0]['partition']
        self.assertFalse(partition['ready'])
        self.assertEqual(partition['reason'], 'qa-membership-changed-review-partition')
        self.assertEqual(partition['added'], self.expected['missing_coverage'])
        self.assertEqual(partition['removed'], [])
        self.assertEqual(self.snapshot(self.project), source_before)
        rejected = self.qa_check(blocked, expected=2)
        self.assertIn('Resolve QA partition blockers', rejected['error'])
        old_outputs = self.snapshot(self.gantt)
        failed_generation = self.generate(expected=1)
        self.assertIn('QA', failed_generation.stdout + failed_generation.stderr)
        self.assertEqual(self.snapshot(self.gantt), old_outputs)
        preflight = self.run_tool(self.state, 'application-preflight', '--project-root', str(self.project), '--feature', 'registry')
        self.assertEqual(preflight['status'], 'tracker-application-preflight-ready')
        self.run_tool(self.state, 'application-preflight', '--project-root', str(self.project),
                      '--feature', 'other', expected=2)
        self.write(self.groups_path, self.expected['groups'])
        self.rows['QA-REGISTRY']['Estimate (дн)'] = '7.2'
        self.rows['QA-REST']['Estimate (дн)'] = '1.8'
        self.write_registry(self.registry, self.rows.values())
        approved = self.review('approved', confirmed=True)
        approved_bytes = Path(approved['review_file']).read_bytes()
        self.assertEqual(approved['qa_application_blockers'], [])
        self.assertEqual(approved['pending_history'], [])
        self.assertEqual(approved['feature_qa_proposals'][0]['partition']['document'], self.expected['groups'])
        for task in self.expected['members']:
            development = approved['features'][0]['tasks'][task]['development']
            self.assertEqual(development['state'], 'completed', task)
            self.assertEqual(development['finished_at'], '2026-08-03T12:00:00+00:00', task)
            self.assertEqual(development['started_at'], None if task == 'LAB-112/BE'
                             else '2026-08-02T12:00:00+00:00', task)
        self.assertNotIn('Completed By', FIELDS)
        self.assertEqual(self.qa_check(approved, expected=2)['status'], 'qa-application-mismatch')
        for task in self.expected['members']:
            fields = self.expected['development']['finish_only' if task == 'LAB-112/BE' else 'regular']
            self.rows[task].update(fields)
        for task, fields in self.expected['qa_fields'].items():
            self.rows[task].update(fields)
        self.write_registry(self.registry, self.rows.values())
        verified = self.qa_check(approved)
        self.assertEqual(verified['status'], 'qa-application-verified')
        registry_before = self.registry.read_bytes()
        self.rows['LAB-202/FE']['Status'] = 'cancelled'
        self.write_registry(self.registry, self.rows.values())
        self.assertEqual(self.qa_check(approved, expected=2)['status'], 'qa-application-mismatch')
        self.rows['LAB-202/FE'] = protected['LAB-202/FE']
        self.registry.write_bytes(registry_before)
        self.generate()
        outputs = self.snapshot(self.gantt)
        self.rows['LAB-112/BE'].update(Status='in-progress', **{'Progress %': 'unknown', 'Actual Finish': '-'})
        self.write_registry(self.registry, self.rows.values())
        try:
            rejected_forecast = self.generate(expected=1)
            self.assertIn('LAB-112/BE', rejected_forecast.stderr)
            self.assertEqual(self.snapshot(self.gantt), outputs)
        finally:
            self.rows['LAB-112/BE'].update(self.expected['development']['finish_only'])
            self.registry.write_bytes(registry_before)
        self.generate()
        self.assertEqual(self.snapshot(self.gantt), outputs)
        overlay = (self.gantt / 'includes/actual-progress/FEATURE-registry.puml').read_text(encoding='utf-8')
        self.assertIn('[TASK_LAB_112_BE] happens at 2026/08/03', overlay)
        self.assertNotIn('[TASK_LAB_112_BE] ends', overlay)
        self.assertNotIn('as [TASK_LAB_112_BE] on', overlay)
        self.assertEqual(self.registry.read_bytes(), registry_before)
        self.assertEqual(self.rows['LAB-112/BE']['Actual Start'], '-')
        self.assertEqual(self.rows['LAB-112/BE']['Estimate (дн)'], '-')
        export = self.gantt / 'actual-progress-confluence.puml'
        complete_export = export.read_bytes()
        export.write_text('Incomplete export\n', encoding='utf-8')
        try:
            refused = self.command(sys.executable, str(ROOT / 'scripts/collaboration.py'),
                                   '--root', str(self.workspace), 'save-preview',
                                   '--review-file', approved['review_file'], expected=1)
            self.assertIn('Confluence export differs', refused.stdout + refused.stderr)
        finally:
            export.write_bytes(complete_export)
        before_preview = self.snapshot(self.project)
        preview = self.collab('save-preview', '--review-file', approved['review_file'])
        self.assertEqual(preview['qa_verified_runs'], [self.run_id])
        self.assertEqual(preview['confluence_verified'], ['2026-Q3'])
        self.assertFalse(preview['writes_performed'])
        self.assertFalse(preview['content_approved'])
        self.assertEqual(self.snapshot(self.project), before_preview)
        paths = {item['path']: item['kind'] for item in preview['paths']}
        self.assertEqual(paths['planning/2026-Q3/gantt/includes/actual-progress/FEATURE-other.puml'], 'generated-quarter-view')
        self.assertEqual({path for path, kind in paths.items() if kind == 'execution-source'},
                         {'features/registry/execution/tasks.md', 'features/registry/execution/qa-groups.json'})
        for path, content in source_before.items():
            if path.startswith('features/other/') or path.endswith(('quarter-plan.puml', 'commander-plan.puml')):
                self.assertEqual((self.project / path).read_bytes(), content, path)
        for path, content in inputs_before.items():
            self.assertEqual((self.inputs / path).read_bytes(), content, path)
        self.assertEqual(reconciled.read_bytes(), reconciled_before)
        for path, content in collection_before.items():
            self.assertEqual(path.read_bytes(), content, str(path))
        self.assertEqual(Path(blocked['review_file']).read_bytes(), blocked_bytes)
        self.assertEqual(Path(approved['review_file']).read_bytes(), approved_bytes)
        self.assertEqual(self.git(self.project, 'rev-parse', 'HEAD'), self.initial_head)
        self.assertEqual(self.git(self.remote, 'rev-parse', 'main'), self.initial_main)
        self.assertEqual(self.git(self.project, 'diff', '--cached', '--name-only'), '')
        repeated = self.review('repeated', confirmed=True)
        self.assertEqual(repeated['qa_application_blockers'], [])
        self.assertEqual(self.review('repeated-again', confirmed=True)['review_file'], repeated['review_file'])
        self.assertEqual(self.snapshot(self.project), before_preview)
        self.assertEqual(len(list((self.state / 'tracker-runs').iterdir())), 1)

    def test_network_transports_are_denied(self):
        denied = self.command('git', 'ls-remote', 'https://example.invalid/repository.git', expected=128)
        self.assertIn('not allowed', denied.stderr)
        denied = self.command(sys.executable, '-c', 'import socket; socket.getaddrinfo("example.invalid", 443)', expected=1)
        self.assertIn('Offline release test: network access denied', denied.stderr)
        self.assertEqual(Path(self.git(self.project, 'remote', 'get-url', 'origin')).resolve(), self.remote.resolve())


if __name__ == '__main__':
    unittest.main()
