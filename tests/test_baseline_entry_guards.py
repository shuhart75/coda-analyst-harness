from pathlib import Path
import json
import subprocess
import sys
import unittest

import test_baseline_releases as fixtures
import baseline_releases as baseline


class BaselineEntryGuardTests(unittest.TestCase):
    setUp = fixtures.BaselineReleaseTests.setUp
    prepare = fixtures.BaselineReleaseTests.prepare
    release_review = fixtures.BaselineReleaseTests.release_review

    def snapshot(self):
        return {p.relative_to(self.project): p.read_bytes() for p in self.project.rglob('*')
                if p.is_file() and '.git' not in p.relative_to(self.project).parts}

    def test_wrong_or_missing_mode_blocks_preparation_and_promotion_without_writes(self):
        entry = self.prepare()
        before = self.snapshot()
        for mode in ('execution-update', 'requirements', '', 'release-finalization\nmode: requirements'):
            (self.state / 'active-mode.md').write_text('mode: ' + mode + '\n')
            for operation in (lambda: baseline.prepare(self.project, 'release-one', self.candidate, self.release_review(entry)),
                              lambda: baseline.promote(self.project, 'release-one', entry['scope_hash'], self.deployment, True)):
                with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'Switch to release-finalization'):
                    operation()
                self.assertEqual(self.snapshot(), before)
        (self.state / 'active-mode.md').unlink()
        with self.assertRaisesRegex(ValueError, 'Switch to release-finalization'):
            baseline.preflight(self.project)
        self.assertEqual(self.snapshot(), before)

    def test_main_unregistered_branch_and_pending_git_operation_block_before_writes(self):
        before = self.snapshot()
        for branch in ('main', 'master', 'feature/foreign'):
            subprocess.run(['git', '-C', str(self.project), 'symbolic-ref', 'HEAD', 'refs/heads/' + branch], check=True)
            with self.subTest(branch=branch), self.assertRaisesRegex(ValueError, 'collaboration'):
                baseline.observe(self.project, self.observation)
            self.assertEqual(self.snapshot(), before)
        subprocess.run(['git', '-C', str(self.project), 'symbolic-ref', 'HEAD', 'refs/heads/feature/baseline'], check=True)
        for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply'):
            path = self.project / '.git' / marker
            path.touch()
            try:
                with self.subTest(marker=marker), self.assertRaisesRegex(ValueError, 'pending Git operation'):
                    baseline.observe(self.project, self.observation)
                self.assertEqual(self.snapshot(), before)
            finally:
                path.unlink()

    def test_preflight_is_readonly_and_cli_blocks_before_reading_mutation_inputs(self):
        before = self.snapshot()
        script = Path(__file__).resolve().parents[1] / 'scripts/baselinectl.py'
        args = [sys.executable, str(script), '--project', str(self.project)]
        ready = subprocess.run([*args, 'preflight'], capture_output=True, text=True)
        self.assertEqual(ready.returncode, 0, ready.stdout + ready.stderr)
        self.assertEqual(json.loads(ready.stdout)['status'], 'baseline-preflight-ready')
        self.assertEqual(self.snapshot(), before)
        (self.state / 'active-mode.md').write_text('mode: execution-update\n')
        blocked = subprocess.run([*args, 'prepare', '--release', 'release-one', '--candidate', str(self.candidate),
                                  '--review', str(self.project / 'missing.json')], capture_output=True, text=True)
        self.assertEqual(blocked.returncode, 1)
        self.assertIn('Switch to release-finalization', blocked.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(subprocess.run([*args, 'scan'], capture_output=True).returncode, 0)

    def test_observation_allowed_during_execution_without_authorizing_baseline_write(self):
        (self.state / 'active-mode.md').write_text('mode: execution-update\n')
        entry = baseline.observe(self.project, self.observation)
        baseline.defer(self.project, 'release-one', 'Await deployment decision')
        with self.assertRaisesRegex(ValueError, 'Switch to release-finalization'):
            baseline.prepare(self.project, 'release-one', self.candidate, self.release_review(entry))
        (self.state / 'active-mode.md').write_text('mode: release-finalization\n')
        prepared = self.prepare()
        baseline.promote(self.project, 'release-one', prepared['scope_hash'], self.deployment, True)
        self.assertEqual(baseline.tree_hash(self.current), baseline.tree_hash(self.candidate))
