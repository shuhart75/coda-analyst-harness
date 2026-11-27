import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import browser_workflow as browser
import operator_questions as questions
from controller import read, save

CHECK = {'url': 'http://127.0.0.1:3002/ui', 'artifact': 'build-a', 'requirements': ['REQ-UI'],
         'preconditions': 'Test account', 'steps': [{'action': 'Open page', 'expected': 'Table visible'}]}


class BrowserWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        save(self.root/'config.json', {'requirements_sha256': 'fixed'})
        save(self.root/'plan.json', {'tasks': []})
        patcher = patch('desktop_notifications.notify_question')
        self.notify = patcher.start()
        self.addCleanup(patcher.stop)
        self.runner = Mock(root=self.root, cfg={})

    def question(self, **kwargs):
        return questions.request(self.root, 'ui', 'browser', 'Check the page?', manual_check=CHECK, **kwargs)

    def test_migrate_only_unanswered_checks_without_manufacturing_answer(self):
        manual = self.question()
        business = questions.request(self.root, 'ui', 'source', 'Choose source')
        browser.activate(self.runner)
        self.assertEqual([q['id'] for q in questions.questions(self.root, pending_only=True)], [business])
        self.assertIsNone(questions.response(self.root, manual))
        self.assertEqual(browser.obligations(self.root)[0]['check'], CHECK)
        self.assertFalse(list((self.root/'operator-questions').glob('*-answer.json')))
        self.assertFalse(list((self.root/'operator-questions').glob('*-resolved.json')))

    def test_answered_check_preserved_and_disabled_mode_keeps_manual_gate(self):
        key = self.question()
        questions.answer(self.root, key, 'Failed', 'Actual mismatch')
        before = (self.root/'operator-questions'/(key+'-answer.json')).read_bytes()
        browser.activate(self.runner)
        self.assertEqual(browser.obligations(self.root), [])
        self.assertEqual((self.root/'operator-questions'/(key+'-answer.json')).read_bytes(), before)
        self.runner.cfg = {'browser_automation': False}
        browser.activate(self.runner)
        questions.request(self.root, 'ui', 'other-check', 'Check?', manual_check=CHECK)
        with self.assertRaises(questions.WaitingForOperator):
            questions.require_manual_answers(self.root)

    def test_new_automatic_check_does_not_notify_or_accept(self):
        browser.activate(self.runner)
        key = self.question()
        self.notify.assert_not_called()
        self.assertTrue(browser.assigned(self.root, key))
        self.assertEqual(questions.questions(self.root, pending_only=True), [])
        with self.assertRaises(ValueError):
            browser.complete(self.root, {'review': {'verdict': 'pass'}})
        browser.complete(self.root, {'review': {'verdict': 'pass'}, 'manual_review_state': questions.manual_state(self.root, 'integration')})
        self.assertEqual(browser.obligations(self.root), [])
        self.assertIsNone(questions.response(self.root, key))

    def test_failure_returns_only_that_obligation_to_operator(self):
        browser.activate(self.runner)
        key = self.question()
        self.assertFalse(browser.release(self.root, key, {'tool_results': []}))
        failure = {'events_path': 'private-session-log', 'resolved_tool_calls': {'a': 'mcp__playwright__browser_navigate'},
                   'tool_results': [{'tool_use_id': 'a', 'is_error': True}]}
        self.assertTrue(browser.release(self.root, key, failure))
        self.assertEqual([q['id'] for q in questions.questions(self.root, pending_only=True)], [key])
        browser.activate(self.runner)
        self.assertFalse(browser.assigned(self.root, key))
        self.notify.assert_called_once()

    def test_resume_keeps_checkpoints_and_does_not_repeat_same_transfer(self):
        browser.activate(self.runner)
        key = self.question()
        value = {'status': 'waiting-user', 'question_id': key, 'checks': ['preserved'], 'patch': 'kept'}
        self.assertTrue(browser.resume_candidate(self.root, {'id': 'ui'}, value))
        self.assertEqual((value['status'], value['stage']), ('pending', 'execute'))
        self.assertEqual(value['checks'], ['preserved'])
        self.assertEqual(value['patch'], 'kept')
        self.assertFalse(browser.resume_candidate(self.root, {'id': 'ui'}, value))

    def test_runtime_browser_reservation_does_not_change_plan_task(self):
        browser.activate(self.runner)
        task = {'id': 'ui', 'writes': ['frontend'], 'resources': ['database']}
        enhanced = browser.task_context(self.root, task)
        self.assertEqual(enhanced['resources'], ['database', 'sdd-browser'])
        self.assertEqual(task['resources'], ['database'])
        self.assertEqual(browser.task_context(self.root, {'id': 'be', 'writes': ['backend'], 'resources': []})['resources'], [])

    def test_capture_requires_audited_stream_successful_calls_and_candidate_png(self):
        work = self.root/'work'
        work.mkdir()
        png = work/'screen.png'
        png.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        events = [{'type': 'system', 'subtype': 'init', 'cwd': str(work)}]
        for i, (name, args) in enumerate((('navigate', {'url': CHECK['url']}), ('snapshot', {}),
                                        ('take_screenshot', {'filename': 'screen.png'}))):
            events += [{'message': {'content': [{'type': 'tool_use', 'id': str(i), 'name': 'tool_call',
                'input': {'name': 'mcp__playwright__browser_'+name, 'arguments': args}}]}},
                {'message': {'content': [{'type': 'tool_result', 'tool_use_id': str(i), 'is_error': False}]}}]
        path = self.root/'stdout.jsonl'
        def write():
            path.write_text('\n'.join(json.dumps(e) for e in events))
            return {'evidence_files': {str(path): hashlib.sha256(path.read_bytes()).hexdigest()}}
        value = write()
        self.assertEqual(browser.captures(work, value)[0]['screenshot'], 'screen.png')
        events[-1]['message']['content'][0]['is_error'] = True
        self.assertEqual(browser.captures(work, write()), [])
        events[-1]['message']['content'][0]['is_error'] = False
        value = write()
        path.write_text(path.read_text()+'\n')
        self.assertEqual(browser.captures(work, value), [])
        value = write()
        png.unlink()
        self.assertEqual(browser.captures(work, value), [])

    def test_repeated_answered_request_does_not_create_assignment(self):
        key = self.question()
        questions.answer(self.root, key, 'Failed')
        browser.activate(self.runner)
        self.question()
        self.assertFalse((self.root/'operator-questions'/(key+'-automation.json')).exists())

    def test_business_blocker_without_browser_obligation_is_not_restarted(self):
        browser.activate(self.runner)
        value = {'status': 'external-blocker', 'acceptance_wait': {'question_ids': ['decision']}}
        self.assertFalse(browser.resume_candidate(self.root, {'id': 'ui'}, value))
        self.assertEqual(value['status'], 'external-blocker')

    def test_browser_lease_is_exclusive_cancellable_and_released_after_error(self):
        import threading
        from contextlib import nullcontext
        from controller import Interrupted
        from platform_support import FileLock
        browser.activate(self.runner)
        self.runner.activity.side_effect = lambda *args, **kwargs: nullcontext()
        task = {'id': 'ui', 'resources': ['sdd-browser']}
        stop = threading.Event()
        with patch('bootstrap.app_root', return_value=self.root/'app'):
            with browser.session_lease(self.runner, task, stop):
                with self.assertRaises(BlockingIOError):
                    with FileLock(self.root/'app/browser-runtime/session.lock'):
                        pass
                stop.set()
                with self.assertRaises(Interrupted):
                    with browser.session_lease(self.runner, task, stop):
                        self.fail('Cancelled waiter entered')
            stop.clear()
            with self.assertRaisesRegex(ValueError, 'session failed'):
                with browser.session_lease(self.runner, task, stop):
                    raise ValueError('session failed')
            with browser.session_lease(self.runner, task, stop):
                pass
