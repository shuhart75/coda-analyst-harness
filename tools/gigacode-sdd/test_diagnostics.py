import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import re
import diagnostics
import test_controller as fixtures
from controller import Blocked


def read_sections(path):
    text = path.read_bytes().decode('utf-8')
    result = {}
    offset = text.index('=== FILE ')
    while offset < len(text):
        end = text.index('\n', offset)
        match = re.fullmatch(r'=== FILE (.+) CHARS ([0-9]+) ===', text[offset:end])
        name, length = json.loads(match[1]), int(match[2])
        start = end + 1
        result[name] = text[start:start + length]
        offset = start + length
        trailer = '\n=== END FILE ===\n\n'
        assert text[offset:offset + len(trailer)] == trailer
        offset += len(trailer)
    return result


class BundleTests(unittest.TestCase):
    def test_complete_atomic_bundle_excludes_workspaces_and_links(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'runs/id'
            root.mkdir(parents=True)
            config = root / 'config.json'
            config.write_text('{}')
            (root / 'state.json').write_text('{"status":"initialized"}')
            (root / 'logs/session').mkdir(parents=True)
            (root / 'logs/session/stdout.jsonl').write_bytes('событие\r\n=== END FILE ===\n'.encode('utf-8'))
            (root / 'logs/binary').write_bytes(b'\x00\xffSECRET_BINARY')
            (root / 'logs/control').write_bytes(b'\x1b[31mCONTROL')
            (root / 'work').mkdir()
            (root / 'work/secret').write_text('excluded')
            (root / 'logs/link').symlink_to(root / 'work', target_is_directory=True)
            (root / 'logs/secret').symlink_to(root / 'work/secret')
            out = diagnostics.collect(config, error='review rejected')
            self.assertEqual(out, Path(folder) / 'diagnostics/latest-diagnostics.txt')
            sections = read_sections(out)
            summary = json.loads(sections['summary.json'])
            self.assertEqual(summary['error'], 'review rejected')
            self.assertEqual(sections['run/logs/session/stdout.jsonl'], 'событие\r\n=== END FILE ===\n')
            self.assertFalse(any('secret' in n for n in sections))
            self.assertNotIn('run/logs/binary', sections)
            self.assertNotIn('run/logs/control', sections)
            self.assertNotIn(bytes([0, 255]), out.read_bytes())
            self.assertFalse(summary['files']['run/logs/binary']['included'])
            self.assertFalse(summary['files']['run/logs/control']['included'])
            self.assertFalse(list(out.parent.glob('*.zip')))
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
        sections = read_sections(fixture.runner.root / 'diagnostics/latest-diagnostics.txt')
        self.assertEqual(json.loads(sections['summary.json'])['error'], 'specific failure')
        self.assertEqual(sections['run/state.json'].encode('utf-8'), fixture.runner.state_path.read_bytes())

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
