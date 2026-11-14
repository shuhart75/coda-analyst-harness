import unittest
from unittest.mock import patch
import import_plan as importer
from controller import Blocked, digest, read, save, plan_candidate_path
import test_controller as fixtures
from platform_support import FileLock
from workspace import git, snapshot
from controller import git_controls


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ControllerE2E('test_start_reuses_completed_preflight_and_reaches_reviewed_plan')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner
        self.old = self.runner.root / 'plan-candidate.json'
        save(self.old, self.fixture.plan)
        self.sha = digest(self.old)
        self.new = self.fixture.root / 'corrected.json'
        save(self.new, self.fixture.plan)

    def test_import_preserves_checkpoint_and_requires_review(self):
        before = read(self.runner.state_path)
        before['preflight'] = {'fixture': 'preserved'}
        save(self.runner.state_path, before)
        target = importer.import_plan(self.fixture.config, self.new, self.sha)
        after = read(self.runner.state_path)
        self.assertEqual(after['preflight'], before['preflight'])
        self.assertEqual(after['tasks'], before.get('tasks', {}))
        self.assertEqual(after['status'], 'initialized')
        self.assertEqual(plan_candidate_path(self.runner.root, after), target)
        self.assertEqual(digest(self.old), self.sha)
        self.assertEqual(after['plan_candidate']['sha256'], digest(target))
        resumed = importer.Runner(self.fixture.config)
        with patch.object(resumed, 'require_preflight'), \
                patch.object(resumed, 'session', side_effect=AssertionError('planner must not run')), \
                patch('review_cycle.review_pair', side_effect=lambda r,p,*a: {'verdict': 'pass', 'findings': [], 'candidate_sha256':digest(p), 'coverage':{q:'verified' for q in r.cfg['requirements']}}) as review:
            resumed.plan()
        review.assert_called_once()
        self.assertEqual(read(resumed.state_path)['status'], 'planned')


    def test_import_after_auxiliary_sync_preserves_captured_copy(self):
        auxiliary = self.fixture.root / 'auxiliary'
        auxiliary.mkdir()
        git(auxiliary, 'init')
        git(auxiliary, 'config', 'user.email', 'test@example.test')
        git(auxiliary, 'config', 'user.name', 'Test')
        (auxiliary / 'file').write_text('old')
        git(auxiliary, 'add', 'file')
        git(auxiliary, 'commit', '-m', 'Initial')
        cfg = read(self.fixture.config)
        cfg['protected_snapshots'][str(auxiliary)] = snapshot(auxiliary)
        cfg['protected_git_controls'][str(auxiliary)] = git_controls(auxiliary)
        save(self.fixture.config, cfg)
        config_bytes = self.runner.config_path.read_bytes()
        captured = snapshot(self.runner.main)
        (auxiliary / 'file').write_text('synced')
        git(auxiliary, 'commit', '-am', 'Sync')
        external = snapshot(auxiliary)
        target = importer.import_plan(self.fixture.config, self.new, self.sha)
        self.assertTrue(target.is_file())
        self.assertEqual(snapshot(self.runner.main), captured)
        self.assertEqual(snapshot(auxiliary), external)
        self.assertEqual(self.runner.config_path.read_bytes(), config_bytes)

    def test_import_rejects_changed_working_copy_head(self):
        git(self.runner.main, '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
            'commit', '--allow-empty', '-m', 'Unexpected update')
        with self.assertRaisesRegex(Blocked, 'Working copy HEAD changed'):
            importer.import_plan(self.fixture.config, self.new, self.sha)

    def test_wrong_hash_and_existing_tasks_rejected(self):
        before = self.runner.state_path.read_bytes()
        with self.assertRaises(Blocked):
            importer.import_plan(self.fixture.config, self.new, 'wrong')
        self.assertEqual(self.runner.state_path.read_bytes(), before)
        state = read(self.runner.state_path)
        state['tasks'] = {'be': {'status': 'pending'}}
        save(self.runner.state_path, state)
        with self.assertRaises(Blocked):
            importer.import_plan(self.fixture.config, self.new, self.sha)

    def test_lock_and_interrupted_state_save_preserve_old_candidate(self):
        with FileLock(self.runner.root / 'run.lock'):
            with self.assertRaises(BlockingIOError):
                importer.import_plan(self.fixture.config, self.new, self.sha)
        before = self.runner.state_path.read_bytes()
        with patch.object(importer.Runner, 'persist', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                importer.import_plan(self.fixture.config, self.new, self.sha)
        self.assertEqual(self.runner.state_path.read_bytes(), before)
        self.assertEqual(digest(self.old), self.sha)
