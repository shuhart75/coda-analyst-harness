import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import observer_findings as findings
import observers


class FindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.packet = {'snapshot_at': time.time(), 'tasks': [{'id': 'test', 'status': 'running'}],
                       'done': 0, 'previous': {'done': 0}}
        self.raw = {'task_id': 'test', 'key': 'repeat-error', 'state': 'open', 'confidence': 'high',
                    'problem': 'Одна ошибка повторилась.', 'recommendation': 'Проверить другую гипотезу.',
                    'expected_result': 'Новая проверка отличает две причины.', 'evidence': ['/tasks/0/status']}

    def save(self, raw=None, role='progress'):
        values, errors = findings.normalize([raw or self.raw], self.packet)
        self.assertFalse(errors)
        return findings.update(self.root, {role: {'status': 'concern', 'findings': values}}, self.packet, 'report.json')

    def test_refs_are_verified_and_observer_claims_are_not_evidence(self):
        for ref in ['/tasks/3/status', '/tasks/-1', '/open_findings/items/0', '/missing', None]:
            values, errors = findings.normalize([{**self.raw, 'evidence': [ref]}], self.packet)
            self.assertEqual(values, [])
            self.assertTrue(errors)
        values, errors = findings.normalize([self.raw], self.packet)
        self.assertFalse(errors)
        self.assertEqual(values[0]['evidence'][0]['excerpt'], '"running"')
        self.assertEqual(findings.normalize([{**self.raw, 'task_id': 'other'}], self.packet)[0], [])

    def test_restart_failure_and_silence_keep_unresolved_findings(self):
        first = self.save()['items'][0]
        # Functions reload from disk, so no Manager object carries memory implicitly.
        state = findings.update(self.root, {'progress': {'status': 'unavailable'},
            'scope': {'status': 'progress', 'findings': []}}, self.packet, 'next.json')
        self.assertEqual(state['items'][0]['id'], first['id'])
        self.assertEqual(state['items'][0]['state'], 'open')
        advice, items = findings.guidance(self.root, 'test')
        self.assertIn('not instructions', advice)
        self.assertEqual(len(items), 1)
        self.assertEqual(findings.guidance(self.root, 'other'), ('', []))

    def test_delivery_is_not_resolution_and_revision_can_be_delivered_again(self):
        self.save()
        _, items = findings.guidance(self.root, 'test')
        findings.delivered(self.root, items, 'attempt-1', 'model-a', {'ok': True})
        self.assertEqual(findings.guidance(self.root, 'test'), ('', []))
        again = self.save()['items'][0]
        self.assertEqual(again['observations'], 2)
        self.assertEqual(again['state'], 'open')
        self.assertEqual(again['delivery']['attempt'], 'attempt-1')
        self.save({**self.raw, 'recommendation': 'Проверить третью гипотезу.'})
        self.assertEqual(len(findings.guidance(self.root, 'test')[1]), 1)

    def test_resolution_requires_current_evidence_and_same_role(self):
        self.save()
        closed = {**self.raw, 'state': 'resolved', 'problem': 'Ошибка устранена.'}
        invalid = {**closed, 'evidence': ['/previous/done']}
        self.assertTrue(findings.normalize([invalid], self.packet)[1])
        self.save(closed, role='scope')
        self.assertEqual(len(findings.context(self.root)['items']), 1)
        self.packet['done'] = 1
        self.save({**closed, 'evidence': ['/done']})
        self.assertEqual(findings.context(self.root)['items'], [])
        self.assertEqual(findings.read(self.root)['items'][0]['state'], 'resolved')

    def test_stale_and_low_confidence_findings_are_visible_but_not_sent(self):
        self.save({**self.raw, 'confidence': 'low'})
        self.assertEqual(findings.guidance(self.root, 'test'), ('', []))
        self.save()
        with patch.object(findings.time, 'time', return_value=self.packet['snapshot_at']+901):
            self.assertEqual(findings.guidance(self.root, 'test'), ('', []))
        self.assertEqual(len(findings.context(self.root)['items']), 1)

    def test_failed_invocation_keeps_advice_for_retry(self):
        self.save()
        _, items = findings.guidance(self.root, 'test')
        findings.delivered(self.root, items, 'attempt-1', 'm', {'ok': False})
        self.assertEqual(len(findings.guidance(self.root, 'test')[1]), 1)

    def test_packet_memory_is_bounded_without_losing_ledger(self):
        for n in range(findings.LIMIT):
            self.save({**self.raw, 'key': 'problem-'+str(n), 'problem': 'X'*400,
                       'recommendation': 'Y'*400, 'expected_result': 'Z'*400})
        memory = findings.packet_context(self.root)
        self.assertLess(len(json.dumps(memory, ensure_ascii=False)), 17000)
        self.assertGreater(memory['omitted'], 0)
        self.assertEqual(len(findings.context(self.root)['items']), findings.LIMIT)

    def test_corrupt_ledger_does_not_block_execution_or_get_overwritten(self):
        path = self.root/'observer/findings.json'
        path.parent.mkdir()
        path.write_text('{broken')
        self.assertEqual(findings.guidance(self.root, 'test'), ('', []))
        with self.assertRaises(OSError):
            self.save()
        self.assertEqual(path.read_text(), '{broken')

    def test_dimensions_are_not_votes(self):
        status, summary = observers.combined({'progress': {'status': 'progress'}, 'scope': {'status': 'concern'}})
        self.assertEqual(status, 'concern')
        self.assertNotIn('расходятся', summary)

    def test_report_memory_and_receipts_enter_packet_after_restart(self):
        self.save()
        _, selected = findings.guidance(self.root, 'test')
        findings.delivered(self.root, selected, 'attempt', 'm')
        (self.root/'plan.json').write_text(json.dumps({'tasks': [{'id': 'test', 'prompt': 'Do tests'}]}))
        snapshot = {**self.packet, 'status': 'running', 'total': 1, 'questions': [],
                    'operations': [], 'service_stop': {}, 'telemetry_stale': False, 'telemetry_incomplete': False}
        with patch('run_status.snapshot', return_value=snapshot):
            packet = observers.packet(self.root)
        self.assertEqual(packet['open_findings']['items'][0]['delivery']['attempt'], 'attempt')
        self.assertNotIn('history', packet['open_findings']['items'][0])


if __name__ == '__main__':
    unittest.main()
