from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from tracker_lifecycle import AnalystCompletion, HistoryEvent, StatusRules, TaskHistory, calculate_feature, calculate_task


def moment(day: int, offset: str = '+03:00') -> datetime:
    return datetime.fromisoformat(f'2026-09-{day:02d}T10:00:00{offset}')


class TrackerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.people = {'analyst': 'AN', 'developer': 'FE', 'other-developer': 'FE', 'backend': 'BE', 'tester': 'QA'}
        self.rules = StatusRules(
            not_started=frozenset({'created'}), development_started=frozenset({'development'}),
            development_completed=frozenset({'ready_for_test'}), qa_started=frozenset({'testing'}),
            qa_completed=frozenset({'done', 'ready_ift', 'ready_psi', 'ift', 'psi'}),
        )

    def history(self, events=(), current_assignee='analyst', current_status='created', complete=True, key='ST-1'):
        return TaskHistory(key, 'FE', moment(20), current_assignee, current_status, tuple(events), complete)

    def assigned(self, identity, day, old, new):
        return HistoryEvent(identity, moment(day), assignee=(old, new))

    def status(self, identity, day, old, new):
        return HistoryEvent(identity, moment(day), status=(old, new))

    def task(self, history):
        before = copy.deepcopy((history, self.people, self.rules))
        result = calculate_task(history, self.people, self.rules)
        self.assertEqual((history, self.people, self.rules), before)
        return result

    def finished(self, key='ST-1', finish=5):
        return self.history([
            self.assigned('dev', 2, 'analyst', 'developer'),
            self.assigned('qa', 3, 'developer', 'tester'),
            self.status('done', finish, 'created', 'done'),
        ], 'tester', 'done', key=key)

    def feature(self, histories, keys=('ST-1',), confirmed=True, decision=None):
        return calculate_feature('example', tuple(histories), self.people, self.rules, keys, confirmed, decision)

    def test_todo_assigned_to_analyst_does_not_create_analysis_work(self):
        result = self.task(self.history())
        self.assertEqual(result['development']['state'], 'not-started')
        self.assertIsNone(result['development']['started_at'])
        self.assertNotIn('AN', result)
        self.assertNotIn('estimate', result)

    def test_assignment_starts_development_even_while_status_remains_created(self):
        result = self.task(self.history([self.assigned('start', 2, 'analyst', 'developer')], 'developer'))
        self.assertEqual(result['development']['state'], 'in-progress')
        self.assertEqual(result['development']['started_at'], moment(2).isoformat())
        self.assertEqual(result['qa']['state'], 'not-started')

    def test_backend_assignment_uses_confirmed_role(self):
        history = replace(self.history([self.assigned('start', 2, 'analyst', 'backend')], 'backend'), development_role='BE')
        self.assertEqual(self.task(history)['development']['started_at'], moment(2).isoformat())

    def test_handoff_ends_development_and_starts_qa_without_status_change(self):
        history = self.history([self.assigned('start', 2, 'analyst', 'developer'), self.assigned('qa', 4, 'developer', 'tester')], 'tester')
        result = self.task(history)
        self.assertEqual(result['development']['finished_at'], moment(4).isoformat())
        self.assertEqual(result['qa']['started_at'], moment(4).isoformat())
        self.assertEqual(result['qa']['state'], 'in-progress')

    def test_returns_and_repeated_handoffs_preserve_development_end_and_qa_start(self):
        events = [self.assigned('start', 2, 'analyst', 'developer'), self.assigned('qa', 4, 'developer', 'tester'),
                  self.assigned('return', 5, 'tester', 'developer'), self.assigned('again', 6, 'developer', 'tester')]
        result = self.task(self.history(events, 'tester'))
        self.assertEqual(result['development']['state'], 'completed')
        self.assertEqual(result['development']['finished_at'], moment(4).isoformat())
        self.assertEqual(result['qa']['started_at'], moment(4).isoformat())
        self.assertEqual(result['qa']['state'], 'in-progress')
        self.assertIsNone(result['qa']['finished_at'])

    def test_change_between_developers_does_not_restart_development(self):
        result = self.task(self.history([self.assigned('start', 2, 'analyst', 'developer'),
            self.assigned('change', 3, 'developer', 'other-developer')], 'other-developer'))
        self.assertEqual(result['development']['started_at'], moment(2).isoformat())

    def test_explicit_status_rules_also_supply_transitions(self):
        history = self.history([self.status('start', 2, 'created', 'development'),
            self.status('end', 3, 'development', 'ready_for_test'),
            self.status('qa', 4, 'ready_for_test', 'testing'), self.status('done', 5, 'testing', 'done')], 'tester', 'done')
        result = self.task(history)
        self.assertEqual(result['development']['started_at'], moment(2).isoformat())
        self.assertEqual(result['development']['finished_at'], moment(3).isoformat())
        self.assertEqual(result['qa']['started_at'], moment(4).isoformat())
        self.assertEqual(result['qa']['finished_at'], moment(5).isoformat())

    def test_all_five_confirmed_qa_statuses_close_testing(self):
        for status in self.rules.qa_completed:
            with self.subTest(status=status):
                history = self.history([self.status('done', 5, 'created', status)], 'tester', status)
                result = self.task(history)
                self.assertEqual(result['qa']['state'], 'completed')
                self.assertEqual(result['qa']['finished_at'], moment(5).isoformat())
                self.assertIsNone(result['development']['finished_at'])

    def test_transitions_inside_terminal_group_do_not_shift_qa_end(self):
        history = self.finished()
        history = replace(history, events=(*history.events, self.status('ift', 8, 'done', 'ift')), current_status='ift')
        self.assertEqual(self.task(history)['qa']['finished_at'], moment(5).isoformat())

    def test_overlapping_terminal_rules_do_not_backfill_development_end_from_qa_end(self):
        rules = replace(self.rules, development_completed=frozenset({'ready_for_test', 'done'}))
        history = self.history([self.status('done', 5, 'created', 'done')], 'tester', 'done')
        result = calculate_task(history, self.people, rules)
        self.assertIsNone(result['development']['finished_at'])
        self.assertEqual(result['development']['completed_by'], moment(5).isoformat())
        self.assertEqual(result['qa']['finished_at'], moment(5).isoformat())

    def test_return_after_terminal_status_continues_qa_without_reopening_development(self):
        history = self.finished()
        history = replace(history, events=(*history.events, self.assigned('return', 7, 'tester', 'developer')), current_assignee='developer')
        result = self.task(history)
        self.assertEqual(result['qa']['state'], 'in-progress')
        self.assertIsNone(result['qa']['finished_at'])
        self.assertEqual(result['development']['finished_at'], moment(3).isoformat())

    def test_status_leaving_terminal_group_requires_new_qa_completion(self):
        history = self.finished()
        events = (*history.events, self.status('back', 7, 'done', 'testing'))
        active = self.task(replace(history, events=events, current_status='testing'))
        self.assertEqual(active['qa']['state'], 'in-progress')
        self.assertEqual(active['development']['finished_at'], moment(3).isoformat())
        final = self.task(replace(history, events=(*events, self.status('done-again', 9, 'testing', 'done'))))
        self.assertEqual(final['qa']['finished_at'], moment(9).isoformat())

    def test_snapshot_without_history_proves_state_but_not_dates(self):
        result = self.task(self.history(current_assignee='tester', current_status='done', complete=False))
        self.assertEqual(result['qa']['state'], 'completed')
        self.assertIsNone(result['qa']['finished_at'])
        self.assertIsNone(result['development']['finished_at'])
        self.assertEqual(result['qa']['completed_by'], moment(20).isoformat())

    def test_incomplete_history_exposes_bounds_instead_of_first_dates(self):
        result = self.task(replace(self.finished(), complete=False))
        self.assertIsNone(result['development']['started_at'])
        self.assertIsNone(result['development']['finished_at'])
        self.assertEqual(result['development']['completed_by'], moment(3).isoformat())
        self.assertIsNone(result['qa']['started_at'])
        self.assertIsNone(result['qa']['finished_at'])

    def test_return_in_partial_history_does_not_invent_a_development_end_date(self):
        result = self.task(self.history([self.assigned('return', 5, 'tester', 'developer')], 'developer', complete=False))
        self.assertEqual(result['development']['state'], 'completed')
        self.assertIsNone(result['development']['finished_at'])
        self.assertEqual(result['development']['completed_by'], moment(5).isoformat())
        self.assertEqual(result['qa']['state'], 'in-progress')
        self.assertIsNone(result['qa']['started_at'])

    def test_unknown_role_and_unknown_code_are_not_guessed(self):
        result = self.task(self.history([self.assigned('start', 2, 'analyst', 'unknown-person')], 'unknown-person', 'unmapped'))
        self.assertEqual(result['development']['state'], 'unknown')
        self.assertIn('participant-role-unknown:unknown-person', result['limitations'])
        self.assertIn('current-status-unmapped:unmapped', result['limitations'])

    def test_names_are_not_silently_substituted_for_status_codes(self):
        rules = replace(self.rules, qa_completed=frozenset({'Выполнен'}))
        result = calculate_task(self.history(current_status='done'), self.people, rules)
        self.assertEqual(result['qa']['state'], 'unknown')
        self.assertIn('current-status-unmapped:done', result['limitations'])

    def test_duplicate_identical_events_are_idempotent_but_conflicts_are_rejected(self):
        history = self.finished()
        self.assertEqual(self.task(history), self.task(replace(history, events=(*history.events, history.events[0]))))
        with self.assertRaisesRegex(ValueError, 'Conflicting duplicate'):
            self.task(replace(history, events=(*history.events, replace(history.events[0], at=moment(8)))))

    def test_invalid_event_order_dates_continuity_and_roles_fail_closed(self):
        start = self.assigned('start', 2, 'analyst', 'developer')
        invalid = [
            [replace(start, at=moment(2).replace(tzinfo=None))],
            [replace(start, at=moment(21))],
            [start, self.assigned('earlier', 1, 'developer', 'tester')],
            [start, self.assigned('same-time', 2, 'developer', 'tester')],
            [start, self.assigned('gap', 3, 'analyst', 'tester')],
            [self.assigned('wrong-role', 2, 'analyst', 'backend')],
        ]
        for events in invalid:
            with self.subTest(events=events), self.assertRaises(ValueError):
                self.task(self.history(events, 'tester'))
        with self.assertRaisesRegex(ValueError, 'snapshot'):
            self.task(self.history([start], 'tester'))

    def test_feature_completion_requires_every_confirmed_qa_member(self):
        done = self.finished()
        waiting = self.history(key='ST-2')
        active = self.feature([done, waiting], ('ST-1', 'ST-2'))
        self.assertEqual(active['qa']['state'], 'in-progress')
        complete = self.feature([done, self.finished('ST-2', 8)], ('ST-1', 'ST-2'))
        self.assertEqual(complete['qa']['state'], 'completed')
        self.assertEqual(complete['qa']['started_at'], moment(3).isoformat())
        self.assertEqual(complete['qa']['finished_at'], moment(8).isoformat())
        self.assertFalse(complete['planning_application_allowed'])
        self.assertFalse(complete['writes_performed'])
        self.assertNotIn('estimate', complete['qa'])

    def test_unknown_or_empty_scope_and_missing_history_cannot_close_feature(self):
        for keys, confirmed in [(('ST-1', 'ST-2'), True), ((), True), (('ST-1',), False)]:
            result = self.feature([self.finished()], keys, confirmed)
            self.assertNotEqual(result['qa']['state'], 'completed')
            self.assertIsNone(result['qa']['finished_at'])

    def test_feature_start_is_unknown_when_one_member_start_is_unknown(self):
        result = self.feature([self.finished(), replace(self.finished('ST-2'), complete=False)], ('ST-1', 'ST-2'))
        self.assertIsNone(result['qa']['started_at'])
        self.assertIsNone(result['qa']['finished_at'])
        self.assertEqual(result['qa']['state'], 'completed')

    def test_feature_date_comparison_uses_instants_not_offset_strings(self):
        first = self.finished()
        second = self.finished('ST-2')
        second = replace(second, events=(*second.events[:-1], replace(second.events[-1], at=moment(5, '+01:00'))))
        result = self.feature([first, second], ('ST-1', 'ST-2'))
        self.assertEqual(result['qa']['finished_at'], moment(5, '+01:00').isoformat())

    def test_analyst_can_close_qa_without_inventing_date_or_changing_task_states(self):
        decision = AnalystCompletion('analyst-message', moment(20), True)
        result = self.feature([self.history()], ('ST-1', 'ST-2'), False, decision)
        self.assertEqual(result['qa']['state'], 'completed')
        self.assertIsNone(result['qa']['finished_at'])
        self.assertIsNone(result['qa']['finished_on'])
        self.assertEqual(result['qa']['completed_by'], moment(20).isoformat())
        self.assertEqual(result['tasks']['ST-1']['development']['state'], 'not-started')
        self.assertEqual(result['qa']['completion_source'], 'analyst-message')

    def test_analyst_date_is_explicit_and_confirmation_does_not_erase_known_finish(self):
        decision = AnalystCompletion('analyst-message', moment(20), True, date(2026, 9, 19))
        result = self.feature([self.history()], decision=decision)
        self.assertEqual(result['qa']['finished_on'], '2026-09-19')
        result = self.feature([self.finished()], decision=replace(decision, finished_on=None))
        self.assertEqual(result['qa']['finished_at'], moment(5).isoformat())

    def test_invalid_or_stale_analyst_confirmation_is_rejected(self):
        base = AnalystCompletion('analyst-message', moment(20), True)
        for decision in [replace(base, analyst_confirmed=False), replace(base, source=''),
                         replace(base, finished_on=date(2026, 9, 21)), replace(base, confirmed_at=moment(19)),
                         replace(base, finished_on=date(2026, 9, 1))]:
            with self.subTest(decision=decision), self.assertRaises(ValueError):
                self.feature([self.finished()], decision=decision)

    def test_invalid_scope_and_rules_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate task'):
            self.feature([self.finished(), self.finished()])
        with self.assertRaisesRegex(ValueError, 'unique'):
            self.feature([self.finished()], ('ST-1', 'ST-1'))
        with self.assertRaises(ValueError):
            calculate_task(self.history(), self.people, replace(self.rules, qa_completed=frozenset({'created'})))

    def test_combined_status_and_assignment_are_one_atomic_event(self):
        history = self.history([
            HistoryEvent('start', moment(2), ('analyst', 'developer'), ('created', 'development')),
            HistoryEvent('qa', moment(3), ('developer', 'tester'), ('development', 'testing')),
        ], 'tester', 'testing')
        result = self.task(history)
        self.assertEqual(result['development']['finished_at'], moment(3).isoformat())
        self.assertEqual(result['qa']['started_at'], moment(3).isoformat())
        self.assertEqual(len(result['evidence']), 2)

    def test_conflicting_return_and_completion_in_same_event_are_rejected(self):
        history = self.history([HistoryEvent('conflict', moment(2), ('tester', 'developer'), ('testing', 'done'))], 'developer', 'done')
        with self.assertRaisesRegex(ValueError, 'same event'):
            self.task(history)

    def test_status_chain_and_snapshot_must_agree(self):
        for events, current in [
            ([self.status('start', 2, 'created', 'development')], 'done'),
            ([self.status('start', 2, 'created', 'development'), self.status('gap', 3, 'created', 'done')], 'done'),
        ]:
            with self.subTest(events=events), self.assertRaises(ValueError):
                self.task(self.history(events, 'developer', current))
        with self.assertRaises(ValueError):
            self.task(replace(self.history(), observed_at=moment(20).replace(tzinfo=None)))

    def test_feature_qa_can_start_while_other_development_is_still_running(self):
        developing = self.history([self.assigned('start', 2, 'analyst', 'developer')], 'developer', key='ST-2')
        result = self.feature([self.finished(), developing], ('ST-1', 'ST-2'))
        self.assertEqual(result['qa']['state'], 'in-progress')
        self.assertEqual(result['qa']['started_at'], moment(3).isoformat())
        self.assertIsNone(result['qa']['finished_at'])


if __name__ == '__main__':
    unittest.main()
