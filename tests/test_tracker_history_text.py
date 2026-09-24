from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from tracker_history import decode_history
from tracker_history_text import decode_text_records, validate_participants
from tracker_lifecycle import StatusRules, calculate_task


def text_mapping():
    return json.loads((Path(__file__).resolve().parents[1] /
                       'templates/tracker-history-text-mapping.json').read_text())


def history_text():
    return (
        'History (2 entries, page 0, has_next: False)\n\n'
        "- **external_id='actor'** [2026-08-02T12:00:00+00:00] UPDATE Assignee (`assigned_to`) "
        "`{'externalId': 'dev'}` → `{'externalId': 'qa'}`\n"
        "- **external_id='dev'** [2026-08-01T12:00:00+00:00] CREATE"
    )


class TextHistoryTests(unittest.TestCase):
    def decode(self, source, mapping=None):
        entry = {'provider': 'sbertrek', 'key': 'TASK-1', 'role': 'BE',
                 'call': {'arguments': {'key': 'TASK-1'}}, 'mapping': mapping or text_mapping(),
                 'snapshot': {'mapping': {'key': '/key', 'assignee': '/assignee', 'status': '/status'}}}
        return decode_history(source, entry, datetime.fromisoformat('2026-08-10T12:00:00+00:00'),
                              {'key': 'TASK-1', 'assignee': 'qa', 'status': 'created'})

    def test_handoff_without_invented_initial_assignment(self):
        history, limits = self.decode(history_text())
        self.assertTrue(history.complete)
        self.assertEqual(limits, [])
        self.assertEqual(len(history.events), 1)
        self.assertEqual(history.events[0].assignee, ('dev', 'qa'))
        result = calculate_task(history, {'dev': 'BE', 'qa': 'QA'},
                                StatusRules(not_started=frozenset({'created'})))
        self.assertIsNone(result['development']['started_at'])
        self.assertEqual(result['development']['finished_at'], '2026-08-02T12:00:00+00:00')

    def test_bare_create_is_not_assignment(self):
        source = 'History (1 entries, page 0, has_next: False)\n' + history_text().splitlines()[-1]
        history, _ = self.decode(source)
        self.assertEqual(history.events, ())

    def test_wrong_nested_path_does_not_invent_initial_assignment(self):
        with self.assertRaisesRegex(ValueError, 'JSON pointer not found'):
            self.decode(history_text().replace("{'externalId': 'dev'}", "{'otherId': 'dev'}"))

    def test_full_record_coverage_and_header_count(self):
        for source in (history_text() + '\ntruncated record',
                       history_text().replace('2 entries', '3 entries'),
                       history_text().replace('UPDATE Assignee', 'BROKEN Assignee')):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.decode(source)

    def test_incomplete_page_and_missing_dates(self):
        for source in (history_text().replace('has_next: False', 'has_next: True'),
                       history_text().replace('page 0', 'page 1')):
            history, limits = self.decode(source)
            self.assertFalse(history.complete)
            self.assertIn('history-completeness-not-proven', limits)
        history, limits = self.decode(history_text().replace(' [2026-08-02T12:00:00+00:00]', ''))
        self.assertFalse(history.complete)
        self.assertEqual(history.events, ())
        self.assertIn('history-event-timestamps-not-returned', limits)

    def test_no_parser_preserves_old_pending_behavior(self):
        history, limits = self.decode(history_text(), {'request_key': '/key', 'text': ''})
        self.assertEqual(history.events, ())
        self.assertIn('history-text-requires-dated-source', limits)

    def test_literal_not_executed_and_patterns_validated(self):
        with self.assertRaisesRegex(ValueError, 'Invalid before value'):
            self.decode(history_text().replace("{'externalId': 'dev'}", "__import__('os').getcwd()"))
        for name, value in (('record_start_pattern', '(?m)^'), ('record_pattern', '.*'),
                            ('header_pattern', 'invalid')):
            mapping = text_mapping()
            mapping['text_parser'][name] = value
            with self.subTest(name=name), self.assertRaises(ValueError):
                decode_text_records(history_text(), mapping)

    def test_reverse_order_and_nested_status(self):
        source = history_text().replace('2 entries', '3 entries') + (
            "\n- **actor** [2026-08-01T15:00:00+03:00] UPDATE Status (`workflow_status`) "
            "`{'code': 'todo'}` → `{'code': 'development'}`")
        history, _ = self.decode(source)
        self.assertEqual(history.events[0].status, ('todo', 'development'))
        self.assertLess(history.events[0].at, history.events[1].at)

    def test_participant_role_and_resource_are_consistent(self):
        for team_id in ('QA1', 'QA2', 'Q2'):
            configured = {'qa': {'team_id': team_id}}
            validate_participants({'qa': 'QA'}, configured)
            with self.assertRaisesRegex(ValueError, 'contradicts'):
                validate_participants({'qa': 'BE'}, configured)
        with self.assertRaisesRegex(ValueError, 'lifecycle role'):
            validate_participants({'qa': 'QA2'}, {})
        with self.assertRaisesRegex(ValueError, 'contradicts'):
            validate_participants({'qa': 'QA'}, {'qa': {'role': 'BE'}})
