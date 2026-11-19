"""Durable task-scoped mailbox. The second terminal never writes controller state."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import sys
import threading
import time

from platform_support import FileLock

_LOCK = threading.RLock()


class WaitingForOperator(RuntimeError):
    def __init__(self, message, question_ids=()):
        super().__init__(message)
        self.question_ids = list(question_ids)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@contextmanager
def locked(root):
    folder = Path(root) / 'operator-questions'
    folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink():
        raise ValueError('Question mailbox must not be a symlink')
    with _LOCK:
        deadline = time.monotonic() + 10
        while True:
            lock = FileLock(folder / 'mailbox.lock')
            try:
                lock.__enter__()
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)
        try:
            yield folder
        finally:
            lock.__exit__(None, None, None)


def binding(root):
    from controller import read, digest
    root = Path(root)
    config = read(root / 'config.json')
    return {'requirements': config['requirements_sha256'],
            'plan': digest(root / 'plan.json') if (root / 'plan.json').is_file() else None}


def command(root):
    # Use the installed launcher, not a guessed Downloads path.
    argv = [sys.executable, str(Path(__file__).resolve().with_name('bootstrap.py')),
            '--answers-config', str(Path(root).resolve() / 'config.json')]
    if os.name == 'nt':
        return '& ' + ' '.join("'" + a.replace("'", "''") + "'" for a in argv)
    return shlex.join(argv)


def _load(path):
    if path.is_symlink():
        raise ValueError('Question/answer must not be a symlink')
    return json.loads(path.read_text(encoding='utf-8'))


def request(root, task, topic, question, *, options=None, details=None, generation='', model=None, action=None):
    from controller import save
    if not all(isinstance(v, str) and v.strip() for v in (task, topic, question)):
        raise ValueError('Question needs a task, topic and readable question')
    if len(question) > 16000 or len(topic) > 200:
        raise ValueError('Question is too long')
    options = options or []
    if not isinstance(options, list) or any(not isinstance(v, str) or not v.strip() for v in options):
        raise ValueError('Invalid question options')
    bound = binding(root)
    key = fingerprint({'binding': bound, 'task': task, 'topic': topic.strip().casefold(),
                       'generation': generation})[:24]
    with locked(root) as folder:
        path = folder / (key + '.json')
        if path.exists():
            value = _load(path)
            if action is not None and value.get('action') != action:
                raise ValueError('Question action changed; preserve the previous decision')
            variant = {'model': model, 'question': question, 'details': details}
            if variant not in value['requests']:
                value['requests'].append(variant)
                save(path, value)
        else:
            value = {'id': key, 'binding': bound, 'task': task, 'topic': topic,
                     'question': question, 'options': options, 'created_at': time.time(),
                     'requests': [{'model': model, 'question': question, 'details': details}]}
            if action is not None:
                value['action'] = action
            save(path, value)
    return key


def questions(root, pending_only=False):
    if not (Path(root) / 'operator-questions').exists():
        return []
    bound = binding(root)
    with locked(root) as folder:
        result = []
        for path in sorted(folder.glob('*.json')):
            if path.name.endswith(('-answer.json', '-resolved.json')):
                continue
            value = _load(path)
            if value.get('binding') != bound:
                continue
            answer_path = folder / (value['id'] + '-answer.json')
            resolution = folder / (value['id'] + '-resolved.json')
            value['answer'] = _load(answer_path) if answer_path.exists() else None
            value['resolved'] = _load(resolution) if resolution.exists() else None
            if not pending_only or not (value['answer'] or value['resolved']):
                result.append(value)
        return result


def answer(root, question_id, text, explanation=''):
    from controller import save
    if not isinstance(text, str) or not text.strip() or len(text) > 32000:
        raise ValueError('Answer must be nonempty and at most 32000 characters')
    current = {q['id']: q for q in questions(root)}
    if question_id not in current:
        raise ValueError('Question is stale or belongs to different inputs')
    with locked(root) as folder:
        path = folder / (question_id + '-answer.json')
        if path.exists() or (folder / (question_id + '-resolved.json')).exists():
            raise ValueError('Question already answered/resolved; previous decision preserved')
        value = {'question': question_id, 'binding': current[question_id]['binding'],
                 'text': text.strip(), 'explanation': explanation.strip(), 'answered_at': time.time()}
        if current[question_id].get('action') is not None:
            value['action_sha256'] = fingerprint(current[question_id]['action'])
        save(path, value)
    return value


def replies(root, task):
    return [{'id': q['id'], 'topic': q['topic'], 'question': q['question'], 'answer': q['answer']}
            for q in questions(root) if q['answer'] and (task is None or q['task'] == task)]


def response(root, key):
    return next((q['answer'] for q in questions(root) if q['id'] == key), None)


def resolve_task(root, task):
    from controller import save
    for q in questions(root, pending_only=True):
        if q['task'] == task:
            with locked(root) as folder:
                save(folder / (q['id'] + '-resolved.json'),
                     {'reason': 'Task accepted through a verified candidate', 'resolved_at': time.time()})


def interact(config, ui):
    root = Path(config).resolve().parent
    pending = questions(root, pending_only=True)
    if not pending:
        ui.notify('Вопросов без ответа нет. Работающий контроллер не запускается повторно.')
        return
    for q in pending:
        from operator_display import render_question
        ui.notify(render_question(root, q))
        if q['options']:
            choices = [*q['options'], 'Свой ответ', 'Ответить позже']
            selected = ui.choose('Ваш ответ:', choices)
            if selected == len(choices)-1:
                continue
            text = choices[selected] if selected < len(q['options']) else ui._read('Ответ: ')
        else:
            text = ui._read('Ответ (пустая строка: позже): ')
            if not text.strip():
                continue
        explanation = ui._read('Пояснение (необязательно): ')
        ui.confirm('Сохранить ответ?\n' + text + ('\n' + explanation if explanation else ''), 'Сохранить')
        answer(root, q['id'], text, explanation)
        ui.notify('Ответ сохранён. Контроллер продолжит зависимую работу и заново проверит результат. '
                  'Если запуск остановлен, выберите продолжение в обычном мастере.')


class InvalidControlRequest(ValueError):
    """An explicit request exists, but choosing an action would be ambiguous."""


def control_result(text):
    """Extract explicit control objects, independently of review verdict parsing.

    Prose and diff blocks are presentation. They never become executable actions.
    Multiple distinct requests or a malformed explicit request require clarification.
    """
    from controller import parse_json, Blocked
    kinds = ('operator_question', 'scope_request', 'service_request', 'service_action')
    documents, invalid = [], False
    blocks = list(re.finditer(r'^[ \t]*```([^\n`]*)\n(.*?)^[ \t]*```[ \t]*$', text, re.M | re.S))
    if blocks:
        for block in blocks:
            if block[1].strip().lower() not in ('', 'json'):
                continue
            try:
                documents.append(json.loads(block[2]))
            except ValueError:
                invalid |= any(re.search(r'"' + kind + r'"\s*:', block[2]) for kind in kinds)
    else:
        try:
            documents.append(parse_json(text))
        except (Blocked, ValueError, TypeError):
            invalid = any(re.search(r'"' + kind + r'"\s*:', text) for kind in kinds)
    requests = {}
    for document in documents:
        if not isinstance(document, dict):
            continue
        for kind in kinds:
            if kind in document:
                if not isinstance(document[kind], dict):
                    invalid = True
                else:
                    requests[fingerprint([kind, document[kind]])] = (kind, document[kind])
    if invalid or len(requests) > 1:
        raise InvalidControlRequest('Explicit request is malformed or contains competing actions')
    if not requests:
        return None
    kind, original = next(iter(requests.values()))
    if kind != 'operator_question':
        return kind, original
    value = dict(original)
    question = value.get('question') or value.get('request') or value.get('message')
    if not isinstance(question, str) or not question.strip():
        raise InvalidControlRequest('Explicit question has no readable question text')
    topic = value.get('topic') or value.get('subject')
    if not isinstance(topic, str) or not topic.strip():
        topic = 'question-' + fingerprint(question)[:20]
    value.update(topic=topic, question=question)
    extra = {k: v for k, v in original.items() if k not in ('question', 'request', 'message', 'topic', 'subject', 'options', 'details')}
    if extra:
        value['details'] = {'details': original.get('details'), 'context': extra}
    return kind, value


def control_or_question(text):
    try:
        return control_result(text)
    except InvalidControlRequest as exc:
        return 'operator_question', {
            'topic': 'clarify-control-' + fingerprint(text)[:20],
            'question': 'Модель запросила решение, но запрос неоднозначен. Как следует продолжить?',
            'details': {'diagnostic': str(exc), 'model_output': text[:32000]}}


def register(runner, task, value, control):
    """Store questions without interpreting model patches as executable code."""
    import scope_repair
    kind, request_data = control
    if scope_repair.is_request(kind, request_data):
        try:
            return scope_repair.prepare(runner, task, value, request_data)
        except ValueError as exc:
            return request(runner.root, task['id'], 'scope-unavailable-' + fingerprint(request_data)[:20],
                'Нужное исправление выходит за область задачи и не может быть автоматически разрешено. '
                'Как изменить план или устранить внешнюю причину?',
                details={'reason': str(exc), 'model_request': request_data,
                         'rule': 'Этот ответ не расширяет область записи; защищённые пути и независимые задачи сохраняют свои границы.'},
                model=value['model'])
    try:
        return request(runner.root, task['id'], request_data.get('topic'), request_data.get('question'),
            options=request_data.get('options'), details=request_data.get('details'),
            generation=value.get('answer_generation', ''), model=value['model'])
    except (ValueError, TypeError) as exc:
        return request(runner.root, task['id'], 'clarify-question-' + fingerprint(request_data)[:20],
            'Модель запросила решение. Уточните, как следует продолжить.',
            details={'diagnostic': str(exc), 'model_request': request_data}, model=value['model'])


PROMPT = '''If a required business/environment fact needs a human answer, do not guess or spin.
Finish independent permitted work, then return JSON
{"operator_question":{"topic":"stable-short-topic","question":"clear question in Russian",
"options":["choice 1","choice 2"],"details":"why this answer is needed"}}.
Use the same topic for the same decision across attempts. A question is not a failure or acceptance.
For a necessary repair outside task.writes, report an explicit request:
{"scope_request":{"paths":["exact/repository-relative/existing-file"],"reason":"defect and failed check"}}.
Do not edit these files yet. The controller can propose a narrowly approved repair of an accepted
predecessor's file, preserve the pinned plan, then rerun all checks and independent review.
Do not ask for permission to bypass corporate policy. Answers cannot waive requirements/checks.
For a necessary persistent local service, do NOT start it from an agent shell. Return
{"service_request":{"id":"stable-service-name","cwd":"repository-relative directory",
"argv":["installed-executable","argument"],"ports":[8082],
"health":{"url":"http://127.0.0.1:8082/health","status":200},
"artifacts":["path/to/built-artifact"],"reason":"purpose and data scope"}}.
The controller shows the exact launch to the operator once, owns the process and reuses healthy services.
Use foreground processes only, never daemonize. Paths to artifacts must be relative to the declared cwd;
the controller freezes them in a separate service copy. Include scripts/config needed for launch in artifacts.
To restart a previously approved service with the SAME frozen artifacts, return
{"service_action":{"id":"service-name","action":"restart","reason":"required experiment step"}}.
Use action="stop" to release an owned service before requesting an approved replacement; its files remain.
Never replace frozen artifacts inside a pinned no-rebuild experiment.
Do not repeat a completed action without new work/evidence. No rebuild occurs during restart.
Use only approved local tooling and isolated test data. Never propose disabling managed browser/security
policy or using an existing PoC database for destructive experiments. Use Nexus for dependencies.
'''
