"""Tool payloads are data, not permission events; audit old receipts narrowly."""
import json
from pathlib import Path
import tempfile
import unittest

from transport import parse_events, revalidate_saved_result


class EventStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.events = Path(self.temp.name)/'stdout.jsonl'

    def stream(self, error=False, structured=False):
        rows = [{'message': {'content': [
            {'type': 'tool_use', 'id': 'read', 'name': 'read_file'},
            {'type': 'tool_result', 'tool_use_id': 'read', 'is_error': error,
             'content': json.dumps({'historical_error': 'permission denied; requires approval',
                                    'permission_denials': [{'tool_use_id': 'historical'}]})}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'pass'}]
        if structured:
            rows[-1]['permission_denials'] = [{'tool_name': 'read_file', 'tool_use_id': 'read'}]
        self.events.write_text('\n'.join(map(json.dumps, rows)))
        result = parse_events(self.events, _legacy_denial_text=True)
        result['returncode'] = 0
        return result

    def test_successful_payload_is_not_a_denial(self):
        self.stream()
        result = parse_events(self.events)
        self.assertTrue(result['ok'])
        self.assertEqual(result['permission_denials'], [])

    def test_failed_call_and_structured_denial_are_retained(self):
        for error, structured in ((True, False), (False, True), (True, True)):
            with self.subTest(error=error, structured=structured):
                old = self.stream(error, structured)
                result = revalidate_saved_result(old, self.events)
                self.assertFalse(result['ok'])
                self.assertTrue(result['permission_denials'])

    def test_legacy_false_denial_recovers_without_mutating_evidence(self):
        old = self.stream()
        before = json.dumps(old), self.events.read_bytes()
        result = revalidate_saved_result(old, self.events)
        self.assertTrue(result['ok'])
        self.assertEqual(result['errors'], [])
        self.assertEqual(before, (json.dumps(old), self.events.read_bytes()))

    def test_process_and_other_errors_are_never_cleared(self):
        for extra in ({'errors': ['Session process timeout; partial events preserved']},
                      {'timed_out': True}, {'cancelled': True}, {'returncode': -15},
                      {'cleanup': {'preserved': True}}):
            with self.subTest(extra=extra):
                old = self.stream()
                if 'errors' in extra:
                    old['errors'] += extra['errors']
                else:
                    old.update(extra)
                self.assertFalse(revalidate_saved_result(old, self.events)['ok'])

    def test_unknown_saved_denial_or_changed_text_is_rejected(self):
        for key, value in (('permission_denials', [{'tool_use_id': 'invented'}]), ('text', 'changed')):
            old = self.stream()
            old[key] = value
            with self.assertRaises(ValueError):
                revalidate_saved_result(old, self.events)
