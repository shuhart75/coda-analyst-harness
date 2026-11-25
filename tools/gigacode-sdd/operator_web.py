"""Local operator UI. Python stdlib only; never starts the SDD controller."""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit

import operator_questions as questions
import operator_view
from operator_display import render_question
from run_status import snapshot, observation_lines, operation_label

ASSETS = Path(__file__).with_name('operator_ui')
MAX_BODY = 150000


class OperatorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config, port=0):
        self.root = Path(config).resolve(strict=True).parent
        if Path(config).name != 'config.json' or not (self.root/'state.json').is_file():
            raise ValueError('Expected config.json of an initialized run')
        self.token = secrets.token_urlsafe(32)
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = 'http://127.0.0.1:' + str(self.server_port)
        self.logger = logging.getLogger('operator-web-' + str(self.server_port))
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        log = self.root/'logs'/'operator-web.log'
        log.parent.mkdir(exist_ok=True)
        if log.is_symlink():
            self.server_close()
            raise ValueError('Web log cannot be a symlink')
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.log_stream = os.fdopen(fd, 'a', encoding='utf-8')
        self.log_handler = logging.StreamHandler(self.log_stream)
        self.log_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        self.logger.addHandler(self.log_handler)

    def server_close(self):
        super().server_close()
        if hasattr(self, 'log_handler'):
            self.logger.removeHandler(self.log_handler)
            self.log_handler.close()
            self.log_stream.close()

    def view(self):
        state = snapshot(self.root)
        # One mailbox read supplies both pending questions and history.
        records = questions.questions(self.root)
        state['questions'] = [q for q in records if not q.get('answer') and not q.get('resolved')]
        view = operator_view.project(state)
        view['questions'] = []
        view['answers'] = []
        for question in records:
            item = {key: question.get(key) for key in ('id', 'task', 'topic', 'question', 'options', 'manual_check')}
            item['revision'] = questions.question_revision(question)
            item['context'] = render_question(self.root, question)
            if question.get('answer'):
                item['answer'] = {key: question['answer'].get(key) for key in ('text', 'explanation', 'answered_at')}
                view['answers'].append(item)
            elif not question.get('resolved'):
                view['questions'].append(item)
        view['answers'].sort(key=lambda q: q['answer'].get('answered_at') or 0, reverse=True)
        for operation, raw in zip(view['execution']['operations'], state.get('operations', [])):
            operation['label'] = operation_label(raw, state.get('tasks', []))
        view['observers'] = {'lines': observation_lines(state),
                             'snapshot_at': (state.get('observation') or {}).get('snapshot_at')}
        view['capabilities']['answer_questions'] = True
        view['run']['name'] = self.root.name
        view['run']['log_path'] = str(self.root/'logs'/'controller.log')
        view['run']['requires_resume'] = state.get('status') != 'running'
        return view


class Handler(BaseHTTPRequestHandler):
    server_version = 'GigaCodeOperator/1'

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # Never log URLs, bearer tokens, questions or submitted answers.
        self.server.logger.info('http %s', self.command)

    def send_data(self, status, body, content_type='application/json; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def json(self, status, value):
        self.send_data(status, json.dumps(value, ensure_ascii=False).encode())

    def authorized(self, *, api=False, mutation=False):
        if self.headers.get('Host') != urlsplit(self.server.origin).netloc:
            self.json(403, {'error': 'Недопустимый адрес сервера.'})
            return False
        origin = self.headers.get('Origin')
        if (origin is not None and origin != self.server.origin) or (mutation and origin != self.server.origin):
            self.json(403, {'error': 'Запрос разрешён только из локального интерфейса.'})
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.json(403, {'error': 'Запрос с другого сайта отклонён.'})
            return False
        if api and not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + self.server.token):
            self.json(401, {'error': 'Откройте полную ссылку, которую вывел сервер в терминале.'})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self.authorized(api=path.startswith('/api/')):
            return
        try:
            if path == '/api/state':
                self.json(200, self.server.view())
            elif path == '/api/log':
                log = self.server.root/'logs'/'controller.log'
                if log.is_symlink() or not log.resolve().is_relative_to(self.server.root):
                    self.json(409, {'error': 'Журнал недоступен по ожидаемому пути.'})
                    return
                if not log.is_file():
                    self.json(200, {'text': 'Журнал пока не создан.', 'truncated': False})
                    return
                with log.open('rb') as stream:
                    stream.seek(0, 2)
                    size = stream.tell()
                    stream.seek(max(0, size-65536))
                    content = stream.read(65536).decode('utf-8', errors='replace')
                self.json(200, {'text': content, 'truncated': size > 65536})
            else:
                static = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                          '/style.css': ('style.css', 'text/css; charset=utf-8')}
                if path not in static:
                    self.json(404, {'error': 'Страница не найдена.'})
                    return
                name, content_type = static[path]
                self.send_data(200, (ASSETS/name).read_bytes(), content_type)
        except (ValueError, KeyError, OSError, RuntimeError):
            self.server.logger.exception('Unable to read operator state')
            self.json(503, {'error': 'Не удалось прочитать состояние. Последний показанный снимок может быть устаревшим.'})

    def do_POST(self):
        if not self.authorized(api=True, mutation=True):
            return
        if self.path != '/api/answers':
            self.json(404, {'error': 'Команда не поддерживается.'})
            return
        try:
            size = int(self.headers.get('Content-Length', '-1'))
            if self.headers.get('Transfer-Encoding') or not 0 <= size <= MAX_BODY:
                self.json(413, {'error': 'Недопустимый размер ответа.'})
                return
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                self.json(415, {'error': 'Ожидается JSON.'})
                return
            payload = json.loads(self.rfile.read(size))
            if (not isinstance(payload, dict) or set(payload) != {'id', 'revision', 'text', 'explanation'}
                    or not all(isinstance(payload[k], str) for k in payload)
                    or not re.fullmatch(r'[0-9a-f]{24}', payload['id'])
                    or not re.fullmatch(r'[0-9a-f]{64}', payload['revision'])):
                self.json(400, {'error': 'Некорректный формат ответа.'})
                return
            try:
                result = questions.answer(self.server.root, payload['id'], payload['text'], payload['explanation'],
                                          idempotent=True, expected_revision=payload['revision'])
            except ValueError:
                self.json(409, {'error': 'Вопрос изменился, уже закрыт или имеет другой ответ. Обновите данные; ваш текст сохранён в форме.'})
                return
            self.json(200, {'question_id': result['question'], 'saved': True, 'answer': {
                k: result.get(k) for k in ('text', 'explanation', 'answered_at')}})
        except (ValueError, UnicodeError):
            self.json(400, {'error': 'Не удалось прочитать ответ.'})
        except (OSError, RuntimeError):
            self.server.logger.exception('Unable to save operator answer')
            self.json(503, {'error': 'Ответ не подтверждён. Повторите отправку; уже сохранённый ответ не будет записан дважды.'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--port', type=int, default=0, help='Local port; 0 chooses an available port')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('Invalid port')
    server = OperatorServer(args.config.expanduser(), args.port)
    print(server.origin + '/#token=' + server.token, flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
