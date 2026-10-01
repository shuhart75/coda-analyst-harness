from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import layoutctl
from workspace import install_commit_message_hook


class LayoutSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        p = self.root / 'features/demo/requirements.md'
        p.parent.mkdir(parents=True)
        p.write_text('Original business requirements\n')
        self.handoff_source = 'features/demo/handoffs/initial-delivery/AGENTS.md'
        handoff = self.root / self.handoff_source
        handoff.parent.mkdir(parents=True)
        handoff.write_text('Immutable historical receiver contract\n')
        self.git('add', '--', 'features/demo/requirements.md', self.handoff_source)
        self.git('commit', '-qm', 'Initial content')
        self.head = self.git('rev-parse', 'HEAD')
        self.git('switch', '-qc', 'codex/layout')
        proposal = layoutctl.plan(self.root, {'demo': {'quarter': '2026-Q4'}})
        layoutctl.apply(self.root, proposal, True)
        install_commit_message_hook(self.root, ROOT / 'scripts/commit_message_policy.py')
        self.paths = {'features/demo/requirements.md': None, self.handoff_source: None}
        for p in self.root.rglob('*'):
            if p.is_file() and '.git' not in p.parts:
                self.paths[p.relative_to(self.root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True, stderr=subprocess.DEVNULL).strip()

    def save(self, **kwargs):
        return layoutctl.save(self.root, kwargs.get('head', self.head), kwargs.get('paths', self.paths), kwargs.get('message', 'Migrate quarterly delivery layout'), kwargs.get('push', False))

    def test_save_commits_exact_reviewed_paths_leaves_unrelated_work(self):
        (self.root / 'unrelated.txt').write_text('Do not commit\n')
        result = self.save()
        self.assertEqual(result['status'], 'committed')
        self.assertNotEqual(result['commit'], self.head)
        self.assertNotIn('unrelated.txt', self.git('ls-files').splitlines())
        self.assertEqual(self.git('branch', '--show-current'), 'codex/layout')
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')

    def test_changed_reviewed_bytes_do_not_stage(self):
        (self.root / 'delivery-index.json').write_text('{}')
        with self.assertRaises((ValueError, KeyError)):
            self.save()
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.head)

    def test_changed_historical_receiver_instructions_refused(self):
        target = next(p for p in self.paths if p.startswith('quarters/') and p.endswith('/AGENTS.md'))
        (self.root / target).write_text('Changed receiver contract\n')
        self.paths[target] = hashlib.sha256((self.root / target).read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'Local settings'):
            self.save()
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')

    def test_new_receiver_instructions_are_not_historical(self):
        target = 'quarters/2026-Q4/features/demo/deliveries/demo/handoffs/new/AGENTS.md'
        p = self.root / target
        p.parent.mkdir(parents=True)
        p.write_text('New instructions\n')
        self.paths[target] = hashlib.sha256(p.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'Local settings'):
            self.save()
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')

    def test_foreign_staged_content_preserved_and_save_refused(self):
        (self.root / 'unrelated.txt').write_text('Keep staged\n')
        self.git('add', '--', 'unrelated.txt')
        with self.assertRaisesRegex(ValueError, 'empty index'):
            self.save()
        self.assertEqual(self.git('diff', '--cached', '--name-only'), 'unrelated.txt')

    def test_main_and_stale_head_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'HEAD'):
            self.save(head='f' * 40)
        self.git('branch', '-m', 'main-copy')
        self.git('branch', '-D', 'main')
        self.git('branch', '-m', 'main')
        with self.assertRaisesRegex(ValueError, 'review branch'):
            self.save()

    def test_local_settings_and_wildcard_paths_refused(self):
        local = self.root / '.gigacode/settings.json'
        local.parent.mkdir()
        local.write_text('{}')
        for name in ('.gigacode/settings.json', '.git/config', 'features/*', '../outside'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.save(paths={**self.paths, name: hashlib.sha256(b'{}').hexdigest()})
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')

    def test_reports_require_registered_release_directory(self):
        for name in ('baseline-review.md', 'migration-report.md'):
            (self.root / name).write_text('# Report\n')
            with self.assertRaisesRegex(ValueError, 'non-analytical path'):
                self.save(paths={**self.paths, name: hashlib.sha256(b'# Report\n').hexdigest()})
        report = self.root / 'releases/layout-migration/migration-report.md'
        report.parent.mkdir(parents=True)
        report.write_text('# Report\n')
        self.assertEqual(self.save(paths={**self.paths, report.relative_to(self.root).as_posix():
                                         hashlib.sha256(report.read_bytes()).hexdigest()})['status'], 'committed')

    def test_missing_managed_hook_and_invalid_message_refused(self):
        hook = self.root / '.git/hooks/commit-msg'
        hook.write_text('#!/bin/sh\nexit 0\n')
        with self.assertRaisesRegex(ValueError, 'managed commit-msg'):
            self.save()
        with self.assertRaisesRegex(ValueError, 'Сообщение коммита'):
            self.save(message='Fix DEMO-123')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.head)

    def test_missing_source_or_fake_deletion_refused(self):
        without = dict(self.paths)
        del without['features/demo/requirements.md']
        with self.assertRaisesRegex(ValueError, 'every tracked'):
            self.save(paths=without)
        with self.assertRaisesRegex(ValueError, 'missing tracked'):
            self.save(paths={**self.paths, 'features/nonexistent.md': None})

    def test_incomplete_migration_journal_refused(self):
        path = self.root / 'migration-layout.json'
        journal = json.loads(path.read_text());journal['moves'] = []
        path.write_text(json.dumps(journal))
        self.paths['migration-layout.json'] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'complete tracked|Local settings'):
            self.save()
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')

    def test_failed_push_retains_commit_and_reports_retry(self):
        result = self.save(push=True)
        self.assertEqual(result['status'], 'committed-push-failed')
        self.assertEqual(result['commit'], self.git('rev-parse', 'HEAD'))
        self.assertIn('ordinary git push', result['next_action'])
        with self.assertRaisesRegex(ValueError, 'HEAD changed'):
            self.save()

    def test_push_targets_only_review_branch(self):
        remote = Path(self.temp.name) / 'remote.git'
        subprocess.check_call(['git', 'init', '-q', '--bare', str(remote)])
        self.git('remote', 'add', 'origin', str(remote))
        result = self.save(push=True)
        self.assertEqual(result['status'], 'committed-and-pushed')
        refs = subprocess.check_output(['git', '-C', str(remote), 'for-each-ref', '--format=%(refname)'], text=True).splitlines()
        self.assertEqual(refs, ['refs/heads/codex/layout'])


if __name__ == '__main__':
    unittest.main()
