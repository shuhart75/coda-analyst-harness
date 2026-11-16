import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import re
import diagnostics
import compact_diagnostics
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
            (root / 'execution-cycles/test').mkdir(parents=True)
            (root / 'execution-cycles/test/candidate.patch').write_text('GIT binary patch\nliteral 10\nENCODED_BINARY_SECRET')
            (root / 'work').mkdir()
            (root / 'work/secret').write_text('excluded')
            (root / 'logs/link').symlink_to(root / 'work', target_is_directory=True)
            (root / 'logs/secret').symlink_to(root / 'work/secret')
            out = diagnostics.collect(config, error='review rejected', full=True)
            self.assertEqual(out, Path(folder) / 'diagnostics/full-diagnostics.txt')
            sections = read_sections(out)
            summary = json.loads(sections['summary.json'])
            self.assertEqual(summary['error'], 'review rejected')
            self.assertEqual(sections['run/logs/session/stdout.jsonl'], 'событие\r\n=== END FILE ===\n')
            self.assertFalse(any('secret' in n for n in sections))
            self.assertNotIn('run/logs/binary', sections)
            self.assertNotIn('run/logs/control', sections)
            self.assertNotIn('run/execution-cycles/test/candidate.patch', sections)
            self.assertNotIn('ENCODED_BINARY_SECRET', ''.join(v for k,v in sections.items() if k.startswith('run/')))
            self.assertNotIn(bytes([0, 255]), out.read_bytes())
            self.assertFalse(summary['files']['run/logs/binary']['included'])
            self.assertFalse(summary['files']['run/logs/control']['included'])
            self.assertFalse(list(out.parent.glob('*.zip')))
            before = out.read_bytes()
            with patch.object(diagnostics.os, 'replace', side_effect=OSError('disk')):
                self.assertIsNone(diagnostics.report(config, full=True))
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
                compact = root/'diagnostics/latest-diagnostics.txt'
                self.assertEqual(json.loads(read_sections(compact)['summary.json'])['format'], 'compact-v1')
                before_compact = compact.read_bytes()
                self.assertEqual(bootstrap.main(['--diagnostics-config', str(config), '--diagnostics-full']), 0)
                self.assertTrue((root/'diagnostics/full-diagnostics.txt').is_file())
                self.assertEqual(compact.read_bytes(), before_compact)
                with FileLock(root / 'run.lock'):
                    self.assertEqual(bootstrap.main(['--diagnostics-config', str(config)]), 2)

    def test_compact_deduplicates_reports_drops_old_history_and_preserves_sources(self):
        import os
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)/'runs/id'; root.mkdir(parents=True)
            config = root/'config.json'
            config.write_text(json.dumps({'requirements': ['REQ-1'], 'protected_snapshots': {'huge': 'SNAPSHOT' * 100000}}))
            (root/'state.json').write_text('{"status":"initialized"}')
            body = '{"verdict":"changes","findings":["UNIQUE_CURRENT_FINDING"]}'
            for i in range(20):
                log = root/'logs'/('session-%02d' % i); log.mkdir(parents=True)
                payload = body if i == 19 else 'OLD_HISTORY_MARKER'
                rows = [{'type': 'system', 'subtype': 'init', 'model': 'test-model'},
                        {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'bad',
                         'is_error': True, 'content': 'CURRENT_PATH_ERROR' if i == 19 else 'old'}]}},
                        {'type': 'result', 'result': payload, 'is_error': False}]
                (log/'stdout.jsonl').write_text('\n'.join(map(json.dumps, rows)))
                os.utime(log, (100+i, 100+i))
            events = root/'logs/session-19/stdout.jsonl'
            attempt = root/'review-cycles/current/reviewer/attempt-1.json'; attempt.parent.mkdir(parents=True)
            attempt.write_text(json.dumps({'status': 'protocol-error', 'error': 'quote mismatch',
                              'workspace_snapshot': {'huge': 'SNAPSHOT' * 100000},
                              'result': {'text': body, 'events_path': str(events)}}))
            (root/'work').mkdir(); (root/'work/private').write_text('PRIVATE_WORKSPACE')
            (root/'logs/session-19/private-link').symlink_to(root/'work/private')
            sources = [p for p in root.rglob('*') if p.is_file() and not p.is_symlink()]
            before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
            out = diagnostics.collect(config, error='current failure')
            sections = read_sections(out); text = out.read_text()
            self.assertLess(out.stat().st_size, compact_diagnostics.MAX_REPORT_BYTES)
            self.assertEqual(text.count('UNIQUE_CURRENT_FINDING'), 1)
            self.assertIn('CURRENT_PATH_ERROR', text)
            self.assertNotIn('PRIVATE_WORKSPACE', text)
            self.assertNotIn('SNAPSHOT', text)
            self.assertFalse(any(k.startswith('runner/') for k in sections))
            self.assertFalse(any('session-00/' in k for k in sections))
            self.assertEqual(before, {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
            old = out.read_bytes()
            with patch.object(compact_diagnostics.os, 'replace', side_effect=OSError('disk')):
                self.assertIsNone(diagnostics.report(config))
            self.assertEqual(out.read_bytes(), old)
            diagnostics.collect(config, error='new failure')
            self.assertNotIn('current failure', out.read_text())
            self.assertEqual(list(out.parent.glob('.latest-*')), [])
            self.assertEqual(len(list(out.parent.glob('*.txt'))), 1)

    def test_compact_size_cap_handles_unicode_large_payloads_and_marks_omissions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); config = root/'config.json'; config.write_text('{}')
            for i in range(24):
                path = root/'review-cycles/current/reviewer'/('attempt-%d.json' % i)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({'status': 'protocol-error', 'error': 'failure',
                    'result': {'text': str(i) + 'Данные' * 100000}}, ensure_ascii=False), encoding='utf-8')
            result = diagnostics.collect(config, error='current error')
            self.assertLessEqual(result.stat().st_size, compact_diagnostics.MAX_REPORT_BYTES)
            sections = read_sections(result)
            summary = json.loads(sections['summary.json'])
            self.assertEqual(summary['error'], 'current error')
            self.assertGreater(summary['omitted_count'], 0)
            self.assertIn('truncated', result.read_text(encoding='utf-8'))

    def test_compact_survives_malformed_session_and_candidate_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); config = root/'config.json'; config.write_text('{}')
            (root/'state.json').write_text('{"plan_candidate": []}')
            log = root/'logs/current'; log.mkdir(parents=True)
            (log/'stdout.jsonl').write_text('{broken\n{"type": {}}\n')
            result = diagnostics.collect(config, error='malformed session')
            sections = read_sections(result)
            self.assertIn('malformed_event', sections['compact/logs/current/events.json'])
            self.assertEqual(json.loads(sections['summary.json'])['error'], 'malformed session')
