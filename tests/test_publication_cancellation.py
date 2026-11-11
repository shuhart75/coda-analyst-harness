"""Cancellation and replacement against real local Git remotes."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from test_requirements_exchange import RequirementsExchangeTests as Helpers, SCRIPT, requirements


class PublicationCancellationTests(unittest.TestCase):
    prepare_project = Helpers.prepare_project
    prepare_code_repository = Helpers.prepare_code_repository
    git = Helpers.git
    authorize = Helpers.authorize
    command = Helpers.command
    prepare = Helpers.prepare

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project, self.feature = self.prepare_project(self.root)
        self.remote, self.code = self.prepare_code_repository(self.root)
        self.env = dict(os.environ, CODA_ANALYST_STATE_ROOT=str(self.root/'state'),
                        ANALYST_HARNESS_STATE_ROOT=str(self.root/'state'))
        self.first = self.prepare(self.project, '--code-root', str(self.code), env=self.env)
        self.state = self.feature/'requirements-state.json'
        self.original = self.state.read_bytes()
        self.cached = Path(self.first['manifest']).read_bytes()
        self.head = self.git(self.code, 'rev-parse', 'HEAD')

    def cancel(self, *, expected=None, confirm=True):
        args = [sys.executable, str(SCRIPT), 'cancel-publication', str(self.project), 'demo',
                '--revision', '1', '--expected-commit', expected or self.first['request_commit'],
                '--reason', 'Заменить устаревший комплект']
        if confirm:
            args.append('--analyst-confirmed')
        return subprocess.run(args, env=self.env, text=True, capture_output=True)

    def cancel_ok(self):
        r = self.cancel()
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        return json.loads(r.stdout)

    def changed(self):
        (self.feature/'requirements.md').write_text(requirements('Система должна показать новый результат.'))
        self.authorize(self.project)

    def prepare_raw(self):
        return subprocess.run([sys.executable, str(SCRIPT), 'prepare', str(self.project), 'demo',
            '--analyst', 'ivan', '--code-root', str(self.code)], env=self.env, text=True, capture_output=True)

    def test_cancel_replace_and_accept_preserves_history(self):
        result = self.cancel_ok()
        self.assertEqual(self.cancel_ok()['status'], 'already-cancelled')
        state = json.loads(self.state.read_text())
        old = json.loads(self.original)['delivery_stages']['revisions'][0]
        current = state['delivery_stages']['revisions'][0]
        self.assertEqual(old, {k:v for k,v in current.items() if k != 'publication_cancellation'})
        self.assertEqual(Path(self.first['manifest']).read_bytes(), self.cached)
        snapshot = Path(result['snapshot'])
        self.changed()
        r = self.prepare_raw()
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        second = json.loads(r.stdout)
        self.assertEqual(second['revision'], 2)
        manifest = json.loads(Path(second['manifest']).read_text())
        self.assertEqual([e['state'] for e in manifest['revisions']], ['cancelled', 'sent'])
        self.assertEqual(self.git(self.remote, 'rev-parse', self.first['request_branch']), self.first['request_commit'])
        self.assertEqual(self.git(self.remote, 'rev-parse', 'main'), self.head)
        self.assertEqual(self.git(self.code, 'rev-parse', 'HEAD'), self.head)
        self.assertEqual(self.git(self.code, 'status', '--porcelain'), '')
        actual = subprocess.check_output(['git', '-C', str(self.remote), 'show',
            second['request_commit']+':requirements-exchange/demo/revisions/001/requirements.md'])
        self.assertEqual(actual, (snapshot/'requirements.md').read_bytes())
        # Simulate human acceptance without touching the ordinary code clone.
        self.git(self.remote, 'update-ref', 'refs/heads/main', second['request_commit'])
        r = self.prepare_raw()
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        accepted = json.loads(r.stdout)
        self.assertTrue(accepted['publication_confirmed'])
        self.assertEqual(accepted['revision'], 2)

    def test_confirmation_and_exact_commit_required(self):
        for kwargs in ({'confirm': False}, {'expected': '0'*40}):
            r = self.cancel(**kwargs)
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(self.state.read_bytes(), self.original)

    def test_merged_candidate_cannot_be_cancelled(self):
        self.git(self.remote, 'update-ref', 'refs/heads/main', self.first['request_commit'])
        self.assertNotEqual(self.cancel().returncode, 0)
        self.assertEqual(self.state.read_bytes(), self.original)

    def test_changed_branch_cannot_be_cancelled(self):
        self.git(self.remote, 'update-ref', 'refs/heads/'+self.first['request_branch'], self.head)
        self.assertNotEqual(self.cancel().returncode, 0)
        self.assertEqual(self.state.read_bytes(), self.original)

    def test_tampered_archive_blocks_retry_and_prepare(self):
        result = self.cancel_ok()
        (Path(result['snapshot'])/'requirements.md').write_text('tampered')
        self.assertNotEqual(self.cancel().returncode, 0)
        self.changed()
        self.assertNotEqual(self.prepare_raw().returncode, 0)

    def test_late_merge_with_deleted_branch_blocks_prepare(self):
        self.cancel_ok()
        self.git(self.remote, 'update-ref', 'refs/heads/main', self.first['request_commit'])
        self.git(self.remote, 'update-ref', '-d', 'refs/heads/'+self.first['request_branch'])
        self.changed()
        r = self.prepare_raw()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('Отменён', r.stdout)

    def test_changed_cancelled_branch_blocks_prepare(self):
        self.cancel_ok()
        self.git(self.remote, 'update-ref', 'refs/heads/'+self.first['request_branch'], self.head)
        self.changed()
        self.assertNotEqual(self.prepare_raw().returncode, 0)

    def test_deleted_cancelled_branch_allows_new_input(self):
        self.cancel_ok()
        self.git(self.remote, 'update-ref', '-d', 'refs/heads/'+self.first['request_branch'])
        self.changed()
        r = self.prepare_raw()
        self.assertEqual(r.returncode, 0, r.stdout+r.stderr)
        self.assertEqual(json.loads(r.stdout)['revision'], 2)

    def test_same_input_cannot_reuse_deleted_cancelled_branch(self):
        self.cancel_ok()
        self.git(self.remote, 'update-ref', '-d', 'refs/heads/'+self.first['request_branch'])
        self.assertNotEqual(self.prepare_raw().returncode, 0)

    def test_squashed_candidate_cannot_be_cancelled(self):
        tree = self.git(self.remote, 'rev-parse', self.first['request_commit']+'^{tree}')
        env = dict(os.environ, GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='t@example.com',
                   GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='t@example.com')
        commit = subprocess.check_output(['git', '-C', str(self.remote), 'commit-tree', tree,
                    '-p', self.head, '-m', 'Accept candidate'], env=env, text=True).strip()
        self.git(self.remote, 'update-ref', 'refs/heads/main', commit)
        self.assertNotEqual(self.cancel().returncode, 0)
        self.assertEqual(self.state.read_bytes(), self.original)


# Do not collect the helper TestCase imported above.
del Helpers

import test_sdd_exchange as sdd_helpers


class SddCancellationTests(unittest.TestCase):
    # Reuse fixture operations, without inheriting the existing test suite.
    for _name, _method in vars(sdd_helpers.SddExchangeTests).items():
        if callable(_method) and not _name.startswith('test_'):
            locals()[_name] = _method
    del _name, _method

    def test_sdd_only_replacement_then_mark_published(self):
        self.enable_code_exchange()
        self.authorize()
        first = self.prepare()
        old = self.manifest(first)['revisions'][0]
        self.cli(SCRIPT, 'cancel-publication', self.project, 'demo', '--revision', '1',
                 '--expected-commit', first['request_commit'], '--reason', 'Обновлены материалы',
                 '--analyst-confirmed')
        self.change_proposal()
        self.authorize()
        second = self.prepare()
        entries = self.manifest(second)['revisions']
        self.assertEqual(second['revision'], 2)
        self.assertEqual(entries[0], dict(old, state='cancelled'))
        self.assertEqual(entries[0]['sha256'], entries[1]['sha256'])
        self.assertNotEqual(entries[0]['sdd_input'], entries[1]['sdd_input'])
        self.accept(second)
        accepted = self.prepare()
        self.assertTrue(accepted['publication_confirmed'])
        self.state('mark-published', '--revision', '2', '--manifest', accepted['manifest'],
                   '--destination-role', 'code')
