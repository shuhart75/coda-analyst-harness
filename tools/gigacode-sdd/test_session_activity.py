import json
from pathlib import Path
import tempfile
import unittest

from session_activity import SessionActivity


class ActivityTests(unittest.TestCase):
    def test_partial_lines_pending_calls_and_private_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'stdout.jsonl'
            path.write_bytes(b'')
            now = [0]
            reader = SessionActivity(path, clock=lambda: now[0])
            event = json.dumps({'type':'assistant','message':{'content':[
                {'type':'tool_use','id':'a','name':'shell','input':{'secret':'never-export'}}]}}).encode()
            path.write_bytes(event[:15])
            self.assertEqual(reader.poll()['events'], 0)
            with path.open('ab') as stream:
                stream.write(event[15:] + b'\n')
            self.assertEqual(reader.poll()['pending_calls'], 1)
            now[0] = 20
            summary = reader.poll()
            self.assertEqual(summary['idle_seconds'], 20)
            self.assertEqual(summary['oldest_pending_seconds'], 20)
            reader.save(summary)
            self.assertNotIn('never-export', (Path(directory)/'activity.json').read_text())
            with path.open('ab') as stream:
                stream.write(json.dumps({'type':'user','message':{'content':[
                    {'type':'tool_result','tool_use_id':'a','content':'secret'}]}}).encode()+b'\n')
                stream.write(b'{"type":"result","parent_tool_use_id":"child"}\n')
            summary = reader.poll()
            self.assertEqual(summary['pending_calls'], 0)
            self.assertEqual(summary['events'], 3)
            self.assertFalse(summary['terminal_result'])
            self.assertEqual(summary['idle_seconds'], 0)

    def test_oversized_event_does_not_hold_unbounded_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'stdout.jsonl'
            path.write_bytes(b'x' * (3 * 1024 * 1024) + b'\n{"type":"result"}\n')
            reader = SessionActivity(path)
            for _ in range(4):
                summary = reader.poll()
                self.assertLessEqual(len(reader.buffer), 1024 * 1024)
            self.assertTrue(summary['terminal_result'])


    def test_browser_dispatcher_shows_tool_name_without_form_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'stdout.jsonl'
            path.write_text(json.dumps({'message': {'content': [{'type': 'tool_use', 'id': 'b',
                'name': 'tool_call', 'input': {'name': 'mcp__playwright__browser_fill_form',
                'arguments': {'password': 'never-export'}}}]}})+'\n')
            summary = SessionActivity(path).poll()
            self.assertIn('mcp__playwright__browser_fill_form', json.dumps(summary))
            self.assertNotIn('never-export', json.dumps(summary))
