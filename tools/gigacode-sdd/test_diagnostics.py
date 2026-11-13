import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import diagnostics
import test_controller as fixtures
from controller import Blocked


class BundleTests(unittest.TestCase):
    def test_complete_atomic_bundle_excludes_workspaces_and_links(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'runs/id'
            root.mkdir(parents=True)
            config = root / 'config.json'
            config.write_text('{}')
            (root / 'state.json').write_text('{"status":"initialized"}')
            (root / 'logs/session').mkdir(parents=True)
            (root / 'logs/session/stdout.jsonl').write_text('event')
            (root / 'work').mkdir()
            (root / 'work/secret').write_text('excluded')
            (root / 'logs/link').symlink_to(root / 'work', target_is_directory=True)
            (root / 'logs/secret').symlink_to(root / 'work/secret')
            out = diagnostics.collect(config, error='review rejected')
            self.assertEqual(out, Path(folder) / 'diagnostics/latest.zip')
            with zipfile.ZipFile(out) as z:
                summary = json.loads(z.read('summary.json'))
                self.assertEqual(summary['error'], 'review rejected')
                self.assertEqual(z.read('run/logs/session/stdout.jsonl'), b'event')
                self.assertFalse(any('secret' in n for n in z.namelist()))
                self.assertIsNone(z.testzip())
            before = out.read_bytes()
            with patch.object(diagnostics.os, 'replace', side_effect=OSError('disk')):
                self.assertIsNone(diagnostics.report(config))
            self.assertEqual(out.read_bytes(), before)
            self.assertEqual(list(out.parent.glob('.latest-*')), [])

    def test_runner_failure_is_preserved_and_state_is_in_bundle(self):
        fixture = fixtures.ControllerE2E('test_init_preserves_source_and_rejects_changed_packet')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with self.assertRaisesRegex(Blocked, 'specific failure'):
            with fixture.runner.locked():
                raise Blocked('specific failure')
        with zipfile.ZipFile(fixture.runner.root / 'diagnostics/latest.zip') as z:
            self.assertEqual(json.loads(z.read('summary.json'))['error'], 'specific failure')
            self.assertEqual(z.read('run/state.json'), fixture.runner.state_path.read_bytes())

    def test_manual_collection_does_not_launch_agents_or_change_state(self):
        import bootstrap
        from platform_support import FileLock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / 'config.json'
            config.write_text('{}')
            state = root / 'state.json'
            state.write_text('{"status":"initialized"}')
            before = state.read_bytes()
            with patch.object(bootstrap, 'launch', side_effect=AssertionError('no agents')):
                self.assertEqual(bootstrap.main(['--diagnostics-config', str(config)]), 0)
                self.assertEqual(state.read_bytes(), before)
                with FileLock(root / 'run.lock'):
                    self.assertEqual(bootstrap.main(['--diagnostics-config', str(config)]), 2)
