from __future__ import annotations

import copy
from datetime import datetime
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from tracker_history import decode_history
from tracker_history_extraction import decode_text_extraction
from tracker_lifecycle import StatusRules, calculate_task


def extraction_fixture():
    lines = [
        'Records: 4; offset: 0; more: False',
        '2026-08-03T12:00:00+00:00 assignee changed dev -> qa',
        '2026-08-02T12:00:00+00:00 assigned_to CREATE -> dev',
        '2026-08-01T12:00:00+00:00 DELETE assigned_to analyst',
        '2026-07-31T12:00:00+00:00 CREATE by actor',
    ]
    text = '\n'.join(lines)

    def span(quote, start=0, hashed=False):
        offset = text.index(quote, start)
        return {'start': offset, 'end': offset + len(quote),
                **({'sha256': hashlib.sha256(quote.encode()).hexdigest()} if hashed else {'quote': quote})}

    segments = [{'kind': 'context', 'source': span(lines[0], hashed=True), 'reason': 'Pagination header'}]
    for line, operation, field, before, after in (
        (lines[1], 'change', 'assignee', 'dev', 'qa'),
        (lines[2], 'add', 'assigned_to', None, 'dev'),
        (lines[3], 'remove', 'assigned_to', 'analyst', None),
    ):
        start = text.index(line)
        verb = {'change': 'changed', 'add': 'CREATE', 'remove': 'DELETE'}[operation]
        segments.append({'kind': 'assignment', 'source': span(line, hashed=True), 'field': span(field, start),
                         'at': span(line.split()[0], start),
                         'before': span(before, start) if before else None,
                         'after': span(after, start) if after else None,
                         'operation': {'kind': operation, 'source': span(verb, start)}})
    segments.append({'kind': 'metadata', 'source': span(lines[4], hashed=True),
                     'reason': 'Bare creation names its actor, not its assignee'})
    extraction = {'schema_version': 1, 'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                  'segments': segments,
                  'pagination': {'count': span('4'), 'start': span('0'), 'has_next': span('False')}}
    return text, extraction


class TextExtractionTests(unittest.TestCase):
    def decode(self, text, extraction):
        entry = {'provider': 'sbertrek', 'key': 'TASK-1', 'role': 'BE',
                 'call': {'arguments': {'key': 'TASK-1'}},
                 'mapping': {'request_key': '/key', 'text': '', 'text_extraction': extraction},
                 'snapshot': {'mapping': {'key': '/key', 'assignee': '/assignee', 'status': '/status'}}}
        return decode_history(text, entry, datetime.fromisoformat('2026-08-10T12:00:00+00:00'),
                              {'key': 'TASK-1', 'assignee': 'qa', 'status': 'created'})

    def test_mixed_formats_create_delete_handoff_and_bare_creation(self):
        text, extraction = extraction_fixture()
        history, limits = self.decode(text, extraction)
        self.assertEqual(limits, [])
        self.assertTrue(history.complete)
        self.assertEqual([event.assignee for event in history.events],
                         [('analyst', None), (None, 'dev'), ('dev', 'qa')])
        calculation = calculate_task(history, {'analyst': 'AN', 'dev': 'BE', 'qa': 'QA'},
                                     StatusRules(not_started=frozenset({'created'})))
        self.assertEqual(calculation['development']['started_at'], '2026-08-02T12:00:00+00:00')
        self.assertEqual(calculation['development']['finished_at'], '2026-08-03T12:00:00+00:00')

    def test_tampering_omission_overlap_and_cross_event_quotes_rejected(self):
        text, original = extraction_fixture()
        for mutate in (
            lambda value: value.update(text_sha256='bad'),
            lambda value: value['segments'].pop(2),
            lambda value: value['segments'].append(value['segments'][-1]),
            lambda value: value['segments'][1]['after'].update(quote='invented'),
            lambda value: value['segments'][1].update(before=value['segments'][3]['before']),
            lambda value: value['segments'][3]['operation'].update(kind='change'),
        ):
            extraction = copy.deepcopy(original)
            mutate(extraction)
            with self.subTest(extraction=extraction), self.assertRaises(ValueError):
                self.decode(text, extraction)

    def test_unresolved_record_clears_timeline_and_requests_collection(self):
        text, extraction = extraction_fixture()
        extraction['segments'][3].update(kind='unresolved', reason='Meaning of removal is unclear')
        history, limits = self.decode(text, extraction)
        self.assertFalse(history.complete)
        self.assertEqual(history.events, ())
        self.assertIn('history-text-extraction-unresolved', limits)
        self.assertNotIn('history-event-timestamps-not-returned', limits)

    def test_missing_event_date_cannot_leave_partial_exact_history(self):
        text, extraction = extraction_fixture()
        extraction['segments'][1]['at'] = None
        history, limits = self.decode(text, extraction)
        self.assertEqual(history.events, ())
        self.assertIn('history-event-timestamps-not-returned', limits)

    def test_coverage_does_not_invent_server_completeness(self):
        text, extraction = extraction_fixture()
        del extraction['pagination']
        history, limits = self.decode(text, extraction)
        self.assertFalse(history.complete)
        self.assertIn('history-completeness-not-proven', limits)
        self.assertEqual(len(history.events), 3)

    def test_count_and_context_evidence_checked(self):
        text, original = extraction_fixture()
        extraction = copy.deepcopy(original)
        extraction['pagination']['count'] = extraction['pagination']['start']
        with self.assertRaisesRegex(ValueError, 'record count'):
            decode_text_extraction(text, extraction)
        extraction = copy.deepcopy(original)
        extraction['pagination']['count'] = extraction['segments'][1]['at']
        with self.assertRaisesRegex(ValueError, 'source context'):
            decode_text_extraction(text, extraction)

    def test_snapshot_gap_remains_a_limitation(self):
        text, extraction = extraction_fixture()
        history, _ = self.decode(text, extraction)
        from dataclasses import replace
        calculation = calculate_task(replace(history, current_assignee='dev'), {'analyst': 'AN', 'dev': 'BE', 'qa': 'QA'},
                                     StatusRules(not_started=frozenset({'created'})))
        self.assertIn('assignment-snapshot-gap', calculation['limitations'])
        self.assertIsNone(calculation['development']['finished_at'])

    def test_example_matches_executable_fixture(self):
        import json
        example = json.loads((Path(__file__).resolve().parents[1] /
                              'templates/tracker-history-extraction.example.json').read_text())
        self.assertEqual((example['example_source_text'], example['mapping']['text_extraction']),
                         extraction_fixture())

    def test_status_change_in_different_presentation(self):
        text = 'On 2026-08-03T12:00:00+00:00 the status changed from todo to qa.'

        def reference(value):
            start = text.index(value)
            return {'start': start, 'end': start + len(value), 'quote': value}

        checksum = hashlib.sha256(text.encode()).hexdigest()
        extraction = {'schema_version': 1, 'text_sha256': checksum, 'segments': [{
            'kind': 'status', 'source': {'start': 0, 'end': len(text), 'sha256': checksum},
            'field': reference('status'), 'at': reference('2026-08-03T12:00:00+00:00'),
            'before': reference('todo'), 'after': reference('qa'),
            'operation': {'kind': 'change', 'source': reference('changed')},
        }]}
        history, limits = self.decode(text, extraction)
        self.assertEqual(history.events[0].status, ('todo', 'qa'))
        self.assertIsNone(history.events[0].assignee)
        self.assertIn('history-completeness-not-proven', limits)

    def test_pagination_without_start_does_not_prove_completeness(self):
        text, extraction = extraction_fixture()
        del extraction['pagination']['start']
        history, limits = self.decode(text, extraction)
        self.assertFalse(history.complete)
        self.assertIn('history-completeness-not-proven', limits)
