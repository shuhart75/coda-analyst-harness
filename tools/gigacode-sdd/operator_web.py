"""Local operator UI with fixed, guarded controller actions. Python stdlib only."""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import secrets
import threading
import hashlib
from urllib.parse import urlsplit

import operator_questions as questions
import operator_view
from operator_display import render_question
from run_status import snapshot, observation_lines, operation_label

ASSETS = Path(__file__).with_name('operator_ui')
MAX_BODY = 150000


class OperatorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config=None, port=0):
        from bootstrap import app_root, known_runs
        from operator_actions import Actions
        self.state_lock = threading.RLock()
        from operator_project import ProjectLinks
        self.project_links = ProjectLinks()
        if config is None:
            runs = known_runs(app_root())
            config = runs[0]['config'] if runs else None
        self.config = Path(config).resolve(strict=True) if config else None
        self.root = self.config.parent if self.config else app_root()/'operator'
        self.root.mkdir(parents=True, exist_ok=True)
        if self.config and (self.config.name != 'config.json' or not (self.root/'state.json').is_file()):
            raise ValueError('Expected config.json of an initialized run')
        self.actions = Actions(app_root()/'operator-control')
        # Reopening a server must not select an old wizard result over --config.
        self.adopted = {op['id'] for op in self.actions.operations() if op.get('config')}
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
        self.project_links.close()
        if hasattr(self, 'log_handler'):
            self.logger.removeHandler(self.log_handler)
            self.log_handler.close()
            self.log_stream.close()

    def run_key(self):
        return hashlib.sha256(str(self.config).encode()).hexdigest()

    def runs(self, selected_state=None):
        from bootstrap import app_root
        from controller import read
        paths = set((app_root()/'runs').glob('*/config.json'))
        if self.config:
            paths.add(self.config)
        result = []
        for path in sorted(paths, reverse=True):
            if path.is_symlink():
                continue
            try:
                config = read(path)
                state = selected_state if path == self.config and selected_state is not None else read(path.parent/'state.json')
                key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
                result.append({'key': key, 'name': path.parent.name, 'source': config.get('source', ''),
                               'status': state.get('status'), 'config': str(path.resolve())})
            except (OSError, ValueError, KeyError):
                continue
        return result

    def management(self):
        from operator_actions import revision
        import operator_control
        operations = self.actions.operations()
        for op in reversed(operations):
            if op['action'] == 'new' and op.get('config') and op['id'] not in self.adopted:
                path = Path(op['config'])
                if path.is_file() and (path.parent/'state.json').is_file():
                    self.config, self.root = path.resolve(), path.resolve().parent
                    self.adopted.add(op['id'])
        from controller import read
        self._snapshot_state = read(self.root/'state.json') if self.config else None
        runtime = operator_control.status(self.root) if self.config else {'active': False, 'controllable': False}
        return {'revision': revision(self.config), 'runtime': runtime, 'operations': operations,
                'runs': self.runs(self._snapshot_state), 'selected': self.run_key(),
                'startup': next((op for op in operations if op['action'] in ('resume', 'restart', 'new')
                                 and op.get('config') == str(self.config)), None)}

    def view(self):
        management = self.management()
        state = snapshot(self.root, state=self._snapshot_state) if self.config else {'status': 'idle', 'tasks': [], 'operations': []}
        # One mailbox read supplies both pending questions and history.
        records = questions.questions(self.root) if self.config else []
        from browser_workflow import obligations
        browser_checks = obligations(self.root) if self.config else []
        delegated_ids = {item['id'] for item in browser_checks}
        state['questions'] = [q for q in records if not q.get('answer') and not q.get('resolved') and q['id'] not in delegated_ids]
        view = operator_view.project(state)
        view['browser_checks'] = browser_checks
        view['project_links'] = self.project_links.view(self.run_key(), records)
        view['questions'] = []
        view['answers'] = []
        for question in records:
            item = {key: question.get(key) for key in ('id', 'task', 'topic', 'question', 'options', 'manual_check')}
            item['revision'] = questions.question_revision(question)
            item['context'] = render_question(self.root, question)
            if question.get('answer'):
                item['answer'] = {key: question['answer'].get(key) for key in ('text', 'explanation', 'answered_at')}
                view['answers'].append(item)
            elif not question.get('resolved') and question['id'] not in delegated_ids:
                view['questions'].append(item)
        view['answers'].sort(key=lambda q: q['answer'].get('answered_at') or 0, reverse=True)
        for operation, raw in zip(view['execution']['operations'], state.get('operations', [])):
            operation['label'] = operation_label(raw, state.get('tasks', []))
        view['observers'] = {'lines': observation_lines(state),
                             'findings': (state.get('observation') or {}).get('findings', []),
                             'results': (state.get('observation') or {}).get('results', {}),
                             'snapshot_at': (state.get('observation') or {}).get('snapshot_at')}
        view['capabilities']['answer_questions'] = bool(self.config)
        view['capabilities']['manage_controller'] = True
        view['management'] = management
        view['run']['key'] = self.run_key()
        view['run']['name'] = self.root.name if self.config else 'Запуск не выбран'
        view['run']['log_path'] = str(self.root/'logs'/'controller.log')
        view['run']['requires_resume'] = not management['runtime']['active']
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
                with self.server.state_lock:
                    self.json(200, self.server.view())
            elif path.startswith('/api/artifact/'):
                artifact = self.server.actions.artifact(path.rsplit('/', 1)[-1])
                self.send_data(200, artifact.read_bytes(), 'application/octet-stream')
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
        if self.path not in ('/api/answers', '/api/actions', '/api/wizard/reply', '/api/wizard/cancel', '/api/select'):
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
            with self.server.state_lock:
                if self.path != '/api/answers':
                    try:
                        if self.path == '/api/actions':
                            result = self.server.actions.submit(self.server.config, payload)
                        elif self.path == '/api/wizard/reply':
                            result = self.server.actions.reply(payload)
                        elif self.path == '/api/wizard/cancel':
                            if not isinstance(payload, dict) or set(payload) != {'operation'}:
                                raise ValueError('Некорректный запрос отмены.')
                            result = self.server.actions.cancel(payload['operation'])
                        else:
                            if not isinstance(payload, dict) or set(payload) != {'key'}:
                                raise ValueError('Некорректный выбор запуска.')
                            selected = next((r for r in self.server.runs() if r['key'] == payload['key']), None)
                            if not selected:
                                raise ValueError('Запуск не найден.')
                            self.server.config = Path(selected['config'])
                            self.server.root = self.server.config.parent
                            result = {'selected': selected['key']}
                        self.json(200, result)
                    except (ValueError, KeyError, TypeError) as exc:
                        self.json(409, {'error': str(exc)})
                    return
                if not self.server.config or self.headers.get('X-Operator-Run') != self.server.run_key():
                    self.json(409, {'error': 'Выбранный запуск изменился. Обновите страницу перед ответом.'})
                    return
                answer_root = self.server.root
            if (not isinstance(payload, dict) or set(payload) != {'id', 'revision', 'text', 'explanation'}
                    or not all(isinstance(payload[k], str) for k in payload)
                    or not re.fullmatch(r'[0-9a-f]{24}', payload['id'])
                    or not re.fullmatch(r'[0-9a-f]{64}', payload['revision'])):
                self.json(400, {'error': 'Некорректный формат ответа.'})
                return
            try:
                result = questions.answer(answer_root, payload['id'], payload['text'], payload['explanation'],
                                          idempotent=True, expected_revision=payload['revision'])
            except ValueError:
                self.json(409, {'error': 'Вопрос изменился, уже закрыт или имеет другой ответ. Обновите данные; ваш текст сохранён в форме.'})
                return
            self.json(200, {'question_id': result['question'], 'saved': True, 'answer': {
                k: result.get(k) for k in ('text', 'explanation', 'answered_at')}})
        except (ValueError, UnicodeError):
            self.json(400, {'error': 'Не удалось прочитать запрос.'})
        except (OSError, RuntimeError):
            self.server.logger.exception('Unable to complete operator request')
            self.json(503, {'error': 'Операция не подтверждена. Повторите отправку той же команды; сохранённая квитанция предотвращает дублирование.'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='Saved run; omit to open the launcher')
    parser.add_argument('--port', type=int, default=0, help='Local port; 0 chooses an available port')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('Invalid port')
    server = OperatorServer(args.config.expanduser() if args.config else None, args.port)
    print(server.origin + '/#token=' + server.token, flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
