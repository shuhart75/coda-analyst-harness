from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from controller import save
import operator_questions as oq
from operator_web import OperatorServer


class OperatorWebTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        save(self.root/'config.json', {'requirements_sha256': 'pinned'})
        save(self.root/'plan.json', {'tasks': [{'id': 'be', 'title': 'Backend', 'depends_on': []}]})
        save(self.root/'state.json', {'status': 'running', 'tasks': {'be': {'status': 'running'}}})
        save(self.root/'console-status.json', {'observed_at': time.time(), 'operations': []})
        self.server = OperatorServer(self.root/'config.json')
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()

    def request(self, method, path, payload=None, headers=None, auth=True):
        values = {'Authorization': 'Bearer '+self.server.token} if auth else {}
        if method == 'POST':
            values.update({'Origin': self.server.origin, 'Content-Type': 'application/json'})
        values.update(headers or {})
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=json.dumps(payload) if payload is not None else None, headers=values)
            response = connection.getresponse()
            body = response.read()
            data = json.loads(body) if response.getheader('Content-Type').startswith('application/json') else body.decode()
            return response.status, data, dict(response.getheaders())
        finally:
            connection.close()

    def question(self):
        with patch('desktop_notifications.notify_question'):
            key = oq.request(self.root, 'be', 'source', 'Какой источник?', options=['Отложить'], details='Нужен реальный пример.')
        q = oq.questions(self.root)[0]
        return {'id': key, 'revision': oq.question_revision(q), 'text': 'Отложить', 'explanation': ''}

    def test_page_works_without_npm_and_sensitive_reads_need_token(self):
        status, html, headers = self.request('GET', '/', auth=False)
        self.assertEqual(status, 200)
        self.assertIn('Ход работы', html)
        self.assertNotIn(self.server.token, html)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(self.request('GET', '/api/state', auth=False)[0], 401)
        self.assertEqual(self.request('GET', '/api/log', auth=False)[0], 401)
        status, data, _ = self.request('GET', '/api/state')
        self.assertEqual(status, 200)
        self.assertEqual(data['run']['total_tasks'], 1)

    def test_foreign_origin_host_and_fetch_site_are_rejected(self):
        payload = self.question()
        for headers in ({'Origin': 'https://foreign.example'}, {'Host': 'foreign.example'},
                        {'Origin': ''}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.request('POST', '/api/answers', payload, headers=headers)[0], 403)
        self.assertEqual(oq.questions(self.root, pending_only=True)[0]['id'], payload['id'])

    def test_duplicate_delivery_returns_same_durable_answer_without_rewriting(self):
        payload = self.question()
        before = (self.root/'state.json').read_bytes()
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.request('POST', '/api/answers', payload), range(2)))
        self.assertTrue(all(r[0] == 200 for r in responses))
        self.assertEqual(responses[0][1], responses[1][1])
        self.assertEqual(len(list((self.root/'operator-questions').glob('*-answer.json'))), 1)
        self.assertEqual(before, (self.root/'state.json').read_bytes())
        state = self.request('GET', '/api/state')[1]
        self.assertEqual(state['questions'], [])
        self.assertEqual(state['answers'][0]['answer']['text'], 'Отложить')
        payload['text'] = 'Другой ответ'
        self.assertEqual(self.request('POST', '/api/answers', payload)[0], 409)
        self.assertEqual(oq.response(self.root, payload['id'])['text'], 'Отложить')

    def test_changed_context_or_plan_cannot_reuse_displayed_revision(self):
        payload = self.question()
        with patch('desktop_notifications.notify_question'):
            oq.request(self.root, 'be', 'source', 'Какой источник?', options=['Отложить'], details='Условия изменились.')
        self.assertEqual(self.request('POST', '/api/answers', payload)[0], 409)
        self.assertIsNone(oq.response(self.root, payload['id']))
        payload['revision'] = oq.question_revision(oq.questions(self.root)[0])
        save(self.root/'plan.json', {'tasks': [], 'revision': 2})
        self.assertEqual(self.request('POST', '/api/answers', payload)[0], 409)

    def test_questions_render_context_and_manual_check_without_execution(self):
        payload = self.question()
        data = self.request('GET', '/api/state')[1]
        self.assertIn('Нужен реальный пример.', data['questions'][0]['context'])
        self.assertEqual(data['questions'][0]['revision'], payload['revision'])
        self.assertTrue(data['capabilities']['answer_questions'])
        self.assertFalse(data['capabilities']['stop_processes'])

    def test_log_endpoint_is_bounded_and_cannot_select_arbitrary_files(self):
        log = self.root/'logs'/'controller.log'
        log.write_text('a'*70000+'конец', encoding='utf-8')
        status, body, _ = self.request('GET', '/api/log')
        self.assertEqual(status, 200)
        self.assertTrue(body['truncated'])
        self.assertTrue(body['text'].endswith('конец'))
        self.assertLessEqual(len(body['text'].encode()), 65540)
        self.assertEqual(self.request('GET', '/../../config.json')[0], 404)
        self.assertEqual(self.request('POST', '/api/stop', {})[0], 404)

    def test_invalid_body_does_not_write_answer(self):
        payload = self.question()
        for value in ([], {'id': payload['id']}, {**payload, 'id': '../state'}, {**payload, 'text': ''}):
            self.assertIn(self.request('POST', '/api/answers', value)[0], (400, 409))
        self.assertIsNone(oq.response(self.root, payload['id']))


if __name__ == '__main__':
    unittest.main()
