import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

import test_workspace as fixtures
import workspace as ws
import snapshot_recovery as recovery


class SnapshotRaceTests(unittest.TestCase):
    setUp = fixtures.WorkspacesTest.setUp
    def mutate_on_copy(self, name, always=False):
        original = ws._copy_permissions
        calls = []
        def copy(source, dest):
            calls.append(dest)
            if always or len(calls) == 1:
                with (Path(source) / name).open('a') as stream:
                    stream.write('additional observation\n')
            return original(source, dest)
        return copy

    def test_transient_append_retains_failed_copy_then_copies_all_bytes(self):
        name = 'backend/observations.data'
        (self.repo / name).write_text('original observation\n')
        with patch.object(ws, '_copy_permissions', side_effect=self.mutate_on_copy(name)):
            result = ws.clone_snapshot(self.repo, self.root/'review', include_dirty=True, attempts=3)
        self.assertEqual(len(result['retry_reports']), 1)
        report = json.loads(Path(result['retry_reports'][0]).read_text())
        self.assertEqual(report['difference']['paths'], [name])
        self.assertFalse(report['usable'])
        self.assertEqual((self.root/'review'/name).read_bytes(), (self.repo/name).read_bytes())

    def test_continuous_writes_are_bounded_and_reported(self):
        with patch.object(ws, '_copy_permissions', side_effect=self.mutate_on_copy('backend/a.txt', True)):
            with self.assertRaises(ws.SnapshotChanged) as caught:
                ws.clone_snapshot(self.repo, self.root/'review', include_dirty=True, attempts=3)
        report = json.loads(Path(caught.exception.report).read_text())
        self.assertEqual(report['difference']['paths'], ['backend/a.txt'])
        self.assertEqual(len(list(self.root.glob('review-unstable-*'))), 2)

    def test_protected_edit_is_not_retried(self):
        (self.repo/'AGENTS.md').write_text('original rules\n')
        with patch.object(ws, '_copy_permissions', side_effect=self.mutate_on_copy('AGENTS.md')):
            with self.assertRaisesRegex(ws.WorkspaceError, 'Protected source'):
                ws.clone_snapshot(self.repo, self.root/'review', include_dirty=True, attempts=3)
        self.assertEqual(list(self.root.glob('review-unstable-*')), [])

    def prepare_recovery(self):
        root = self.root/'run'
        work = root/'workers'/'candidate'
        ws.clone_snapshot(self.repo, work, include_dirty=True)
        folder = root/'execution-cycles'/'scope'
        candidate = folder/'model'/'candidate.json'
        candidate.parent.mkdir(parents=True)
        value = {'scope':'scope', 'workspace':str(work), 'status':'running', 'stage':'review',
                 'workspace_snapshot':ws.snapshot(work), 'evidence_files':{}, 'checks':[{'ok':True}]}
        candidate.write_text(json.dumps(value))
        fatal = {'scope':'scope', 'type':'WorkspaceError', 'error':recovery.LEGACY_ERROR,
                 'frames':[{'file':'execution_cycle.py','function':'_review_copy'},
                           {'file':'workspace.py','function':'clone_snapshot'}]}
        (folder/'fatal.json').write_text(json.dumps(fatal))
        runner = SimpleNamespace(root=root, guard=Mock(), check_preserved_processes=Mock())
        return runner, folder, candidate, work

    def test_old_race_recovery_preserves_files_requires_new_checks(self):
        runner, folder, candidate, work = self.prepare_recovery()
        (work/'backend/a.txt').write_text('new ordinary content')
        self.assertTrue(recovery.recover(runner, folder, 'scope'))
        value = json.loads(candidate.read_text())
        self.assertEqual(value['stage'], 'checks')
        self.assertEqual(value['snapshot_recovery']['difference']['paths'], ['backend/a.txt'])
        self.assertTrue(Path(value['snapshot_recovery']['previous_candidate']).is_file())
        self.assertFalse((folder/'fatal.json').exists())
        self.assertEqual((work/'backend/a.txt').read_text(), 'new ordinary content')
        runner.guard.assert_called_once()
        runner.check_preserved_processes.assert_called_once()

    def test_old_race_cannot_hide_protected_changes(self):
        runner, folder, candidate, work = self.prepare_recovery()
        (work/'AGENTS.md').write_text('new rules')
        with self.assertRaisesRegex(RuntimeError, 'Protected candidate'):
            recovery.recover(runner, folder, 'scope')
        self.assertTrue((folder/'fatal.json').exists())
        self.assertEqual(json.loads(candidate.read_text())['stage'], 'review')

    def test_compound_failure_is_not_recovered(self):
        runner, folder, candidate, work = self.prepare_recovery()
        path=folder/'fatal.json'
        fatal=json.loads(path.read_text())
        fatal['related_failures']=[{'scope':'scope','type':'Blocked','error':'other safety failure'}]
        path.write_text(json.dumps(fatal))
        self.assertFalse(recovery.recover(runner, folder, 'scope'))
        runner.guard.assert_not_called()

    def test_changed_controller_evidence_blocks_recovery(self):
        runner, folder, candidate, work = self.prepare_recovery()
        from controller import digest
        evidence = runner.root/'check.log'
        evidence.write_text('PASS')
        value=json.loads(candidate.read_text())
        value['evidence_files']={str(evidence):digest(evidence)}
        candidate.write_text(json.dumps(value))
        evidence.write_text('changed')
        with self.assertRaisesRegex(RuntimeError, 'Saved controller evidence changed'):
            recovery.recover(runner, folder, 'scope')
        self.assertTrue((folder/'fatal.json').exists())

    def test_initial_clone_failure_is_not_review_recovery(self):
        runner, folder, candidate, work = self.prepare_recovery()
        path=folder/'fatal.json'
        fatal=json.loads(path.read_text())
        fatal['frames']=[{'file':'workspace.py','function':'clone_snapshot'}]
        path.write_text(json.dumps(fatal))
        self.assertFalse(recovery.recover(runner, folder, 'scope'))
        runner.guard.assert_not_called()

    def test_recovery_resumes_after_candidate_saved_before_fatal_archival(self):
        runner, folder, candidate, work = self.prepare_recovery()
        original = Path.rename
        def interrupted(path, target):
            if path == folder/'fatal.json':
                raise OSError('interrupted archive')
            return original(path, target)
        with patch.object(Path, 'rename', interrupted):
            with self.assertRaisesRegex(OSError, 'interrupted archive'):
                recovery.recover(runner, folder, 'scope')
        self.assertTrue((folder/'fatal.json').is_file())
        self.assertTrue(recovery.recover(runner, folder, 'scope'))
        self.assertFalse((folder/'fatal.json').exists())
        self.assertEqual(len(list(candidate.parent.glob('candidate-before-snapshot-recovery-*'))), 1)

    def test_capture_keeps_finite_evidence_while_writer_continues(self):
        name = 'backend/observations.data'
        (self.repo/name).write_text('original observation\n')
        with patch.object(ws, '_copy_permissions', side_effect=self.mutate_on_copy(name, True)):
            result = ws.clone_snapshot(self.repo, self.root/'frozen', include_dirty=True, capture_appends=True)
        self.assertEqual((self.root/'frozen'/name).read_text(), 'original observation\n')
        self.assertEqual(result['append_captures'][0]['path'], name)
        frozen = ws.snapshot(self.root/'frozen')
        with (self.repo/name).open('a') as stream:
            stream.write('next minute\n')
        self.assertEqual(ws.snapshot(self.root/'frozen'), frozen)
        self.assertIn('next minute', (self.repo/name).read_text())

    def test_capture_does_not_accept_rewritten_prefix_even_when_file_grows(self):
        original = ws._copy_permissions
        def rewrite(source, dest):
            (Path(source)/'backend/a.txt').write_text('rewritten content longer than original\n')
            return original(source, dest)
        with patch.object(ws, '_copy_permissions', side_effect=rewrite):
            with self.assertRaises(ws.SnapshotChanged):
                ws.clone_snapshot(self.repo, self.root/'frozen', include_dirty=True, capture_appends=True)

    def test_capture_handles_tracked_append_and_preserves_source_index(self):
        name = 'backend/a.txt'
        (self.repo/name).write_text('old and current observations\n')
        index=(self.repo/'.git/index').read_bytes()
        with patch.object(ws, '_copy_permissions', side_effect=self.mutate_on_copy(name, True)):
            result=ws.clone_snapshot(self.repo, self.root/'frozen', include_dirty=True, capture_appends=True)
        self.assertEqual(result['append_captures'][0]['path'],name)
        self.assertEqual((self.repo/'.git/index').read_bytes(),index)
        self.assertEqual((self.root/'frozen'/name).read_text(),'old and current observations\n')

    def test_capture_rejects_protected_append(self):
        (self.repo/'AGENTS.md').write_text('original rules\n')
        with patch.object(ws, '_copy_permissions', side_effect=self.mutate_on_copy('AGENTS.md', True)):
            with self.assertRaisesRegex(ws.WorkspaceError, 'Protected source'):
                ws.clone_snapshot(self.repo,self.root/'frozen',include_dirty=True,capture_appends=True)
