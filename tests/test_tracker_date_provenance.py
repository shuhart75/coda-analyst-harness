from __future__ import annotations

import copy
from datetime import datetime
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from tracker_history import decode_history
from tracker_lifecycle import StatusRules, calculate_task


class TrackerDateProvenanceTests(unittest.TestCase):
    def calculate(self, source):
        entry = {'provider': 'sbertrek', 'key': 'TASK-1', 'role': 'BE', 'mapping': {
            'key': '/key', 'events': '/events', 'at': '/at', 'field': '/field',
            'assignment_field': 'assigned_to', 'status_field': 'status',
            'from': '/before', 'to': '/after', 'assignee': '/assignee', 'status': '/status',
            'start': '/start', 'total': '/total'}}
        observed = datetime.fromisoformat('2026-08-10T12:00:00+00:00')
        history, limits = decode_history(source, entry, observed)
        rules = StatusRules(not_started=frozenset({'created'}),
                            development_started=frozenset({'development'}),
                            qa_completed=frozenset({'done'}), cancelled=frozenset({'cancelled'}))
        return history, limits, calculate_task(history, {'dev': 'BE', 'qa': 'QA'}, rules)

    def source(self, status, assignee='dev', events=()):
        return {'key': 'TASK-1', 'assignee': assignee, 'status': status,
                'created': '2026-08-01T09:00:00+00:00',
                'updated': '2026-08-06T11:50:00+00:00',
                'events': list(events), 'start': 0, 'total': len(events)}

    def test_cancelled_snapshot_does_not_create_a_dated_event(self):
        source = self.source('cancelled')
        before = copy.deepcopy(source)
        history, limits, result = self.calculate(source)
        self.assertEqual(history.events, ())
        self.assertEqual(limits, [])
        self.assertEqual(result['development']['state'], 'cancelled')
        for phase in ('development', 'qa'):
            for field in ('started_at', 'finished_at', 'started_by', 'completed_by'):
                self.assertIsNone(result[phase][field], (phase, field))
        self.assertEqual(source, before)

    def test_snapshot_bounds_use_observation_not_card_updated(self):
        for status, assignee, phase, field in (
            ('development', 'dev', 'development', 'started_by'),
            ('done', 'qa', 'qa', 'completed_by'),
        ):
            with self.subTest(status=status):
                history, _, result = self.calculate(self.source(status, assignee))
                self.assertEqual(result[phase][field], history.observed_at.isoformat())
                self.assertIsNone(result[phase]['started_at'])
                self.assertIsNone(result[phase]['finished_at'])

    def test_card_metadata_does_not_shift_exact_handoff(self):
        events = [
            {'at': '2026-08-02T10:00:00+00:00', 'field': 'assigned_to', 'before': None, 'after': 'dev'},
            {'at': '2026-08-03T11:00:00+00:00', 'field': 'assigned_to', 'before': 'dev', 'after': 'qa'},
        ]
        source = self.source('created', 'qa', events)
        _, _, original = self.calculate(source)
        source.update(created='2026-07-01T00:00:00+00:00', updated='2026-08-09T23:00:00+00:00')
        _, _, repeated = self.calculate(source)
        self.assertEqual(repeated, original)
        self.assertEqual(original['development']['started_at'], events[0]['at'])
        self.assertEqual(original['development']['finished_at'], events[1]['at'])

    def test_undated_transition_cannot_borrow_card_timestamps(self):
        for status in ('done', 'cancelled'):
            with self.subTest(status=status):
                source = self.source(status, events=[{'field': 'status', 'before': 'created', 'after': status}])
                history, limits, result = self.calculate(source)
                self.assertEqual(history.events, ())
                self.assertIn('history-event-timestamps-not-returned', limits)
                self.assertFalse(history.complete)
                for phase in ('development', 'qa'):
                    self.assertIsNone(result[phase]['started_at'])
                    self.assertIsNone(result[phase]['finished_at'])


if __name__ == '__main__':
    unittest.main()
