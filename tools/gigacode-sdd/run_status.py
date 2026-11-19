"""Small user-facing projection; activity never substitutes for acceptance."""
import json
from pathlib import Path
import time
import operator_questions as oq
from progress_io import read_projection, write_projection

STAGES = {'execute': 'разработка', 'checks': 'обязательные проверки', 'review': 'независимое ревью',
          'adjudicate': 'разбор замечаний', 'accepted': 'вариант проверен'}
STATUSES = {'pending': 'ожидает запуска', 'done': 'принято', 'running': 'в работе',
            'blocked': 'заблокировано', 'waiting-user': 'нужен ответ', 'interrupted': 'остановлено',
            'waiting-retry': 'пауза перед повтором', 'complete': 'завершено'}

SERVICE_STATUSES = {'awaiting-approval': 'нужно разрешение на запуск', 'starting': 'запускается',
                    'ready': 'готов', 'failed': 'ошибка запуска', 'stopped': 'остановлен',
                    'released': 'остановлен и освобождён'}

def candidate_label(value):
    phase = STAGES.get(value['stage'], value['stage'])
    if value['status'] in ('waiting-user', 'waiting-retry', 'interrupted'):
        phase = STATUSES[value['status']] + ' (' + phase + ')'
    return value['model'] + ': ' + phase


def candidate(runner, task, value):
    name = oq.fingerprint([task['id'], value['model']])[:24]
    write_projection(runner.root / 'progress' / (name + '.json'), {
        'task': task['id'], 'model': value['model'], 'stage': value['stage'],
        'status': value['status'], 'resource_wait': value.get('resource_wait'),
        'checks': [{'ok': c.get('ok'), 'log': c.get('log')} for c in value.get('checks', [])],
        'updated_at': time.time()})


def snapshot(root, state=None):
    from controller import read
    root = Path(root)
    state = state if state is not None else read(root / 'state.json')
    plan = read(root / 'plan.json') if (root / 'plan.json').is_file() else {'tasks': []}
    problems = []
    progress = [read_projection(p, problems) for p in sorted((root / 'progress').glob('*.json'))] if (root / 'progress').exists() else []
    progress = [p for p in progress if all(k in p for k in ('task', 'model', 'stage', 'status'))]
    rows = []
    for task in plan['tasks']:
        rec = state.get('tasks', {}).get(task['id'], {})
        rows.append({'id': task['id'], 'status': rec.get('status', 'pending'),
            'goal': task.get('acceptance', {}).get('deliverables') or [],
            'waiting_for': [d for d in task['depends_on'] if state.get('tasks', {}).get(d, {}).get('status') != 'done'],
            'error': rec.get('error'), 'result': rec.get('workspace'),
            'deferred_evidence': rec.get('review', {}).get('deferred_evidence', []),
            'candidates': [p for p in progress if p['task'] == task['id']]})
    questions = oq.questions(root, pending_only=True)
    services = []
    for path in sorted((root / 'managed-services').glob('*/service.json')):
        value = read(path)
        services.append({'id': value['spec']['id'], 'status': value['status'],
                         'health': value['spec']['health'], 'log': value.get('log')})
    readiness = read(root / 'readiness.json') if (root / 'readiness.json').is_file() else {}
    runtime = read_projection(root / 'console-status.json', problems) if (root / 'console-status.json').is_file() else {}
    stale = bool(runtime) and state.get('status') == 'running' and time.time()-runtime.get('observed_at', 0) > 90
    return {'operations': runtime.get('operations', []), 'telemetry_stale': stale,
            'telemetry_incomplete': bool(problems),
            'status': state.get('status'), 'tasks': rows,
            'done': sum(r['status'] == 'done' for r in rows), 'total': len(rows),
            'questions': questions, 'answer_command': oq.command(root), 'services': services,
            'integration': state.get('integration', {}).get('status'),
            'ready': readiness.get('required_complete') is True and state.get('status') == 'complete'}


def _sections(value):
    """Only visible facts participate in updates, never timestamps or prompts."""
    sections = {}
    for row in value['tasks']:
        line = row['id'] + ': ' + STATUSES.get(row['status'], row['status'])
        if row['status'] == 'pending':
            line += ('; после ' + ', '.join(row['waiting_for'])) if row['waiting_for'] else '; готова к запуску'
        lines = [line]
        if row['status'] in ('running', 'waiting-user'):
            for p in row['candidates']:
                label = candidate_label(p)
                wait = p.get('resource_wait') or {}
                if wait.get('state') == 'waiting':
                    label += '; ждёт освобождения ресурса'
                lines.append('  Вариант ' + label)
        if row['status'] in ('blocked', 'waiting-user') and row['error']:
            lines.append('  Причина: ' + str(row['error']).replace('\n', ' ')[:500])
        for item in row['deferred_evidence']:
            lines.append('  Проверка впереди: ' + item['requirement'] + ' → ' + item['owner'] + ': ' + item['evidence'])
        sections['task:' + row['id']] = '\n'.join(lines)
    for service in value['services']:
        sections['service:' + service['id']] = 'Стенд ' + service['id'] + ': ' + SERVICE_STATUSES.get(service['status'], service['status'])
    if value.get('integration'):
        sections['integration'] = 'Итоговая проверка всех требований: ' + STATUSES.get(value['integration'], value['integration'])
    questions = ['Нужны ответы: ' + str(len(value['questions']))]
    for q in value['questions']:
        questions.append(q['task'] + ': ' + q['question'])
    if value['questions']:
        questions.append('Для ответа выполните в другом окне:\n' + value['answer_command'])
    sections['questions'] = '\n'.join(questions) if value['questions'] else ''
    return sections


def _heading(value):
    return ('Прогресс: принято ' + str(value['done']) + '/' + str(value['total']) +
            '; состояние: ' + STATUSES.get(value['status'], str(value['status'])) +
            '; итоговая приёмка: ' + ('пройдена' if value['ready'] else 'ещё не пройдена'))


def render(value, previous=None):
    """Full map on request/start; subsequent output contains only changes."""
    sections = _sections(value)
    before = _sections(previous) if previous is not None else {}
    changed = [text for key, text in sections.items() if text and before.get(key) != text]
    if previous is not None and before.get('questions') and not sections['questions']:
        changed.append('Открытых вопросов пользователю больше нет.')
    if previous is not None and not changed and _heading(previous) == _heading(value):
        return ''
    return '\n'.join([_heading(value)] + changed)


def publish(runner, force=False):
    try:
        value = snapshot(runner.root, runner.state)
    except (OSError, ValueError) as exc:
        # Display I/O is not an execution gate. Authoritative checks stay strict.
        value = dict(getattr(runner, '_last_progress_snapshot', None) or {
            'tasks': [], 'done': 0, 'total': 0, 'questions': [], 'services': [], 'integration': None})
        value.update(status=runner.state.get('status'), ready=False, telemetry_incomplete=True)
        runner.progress('Сводка временно недоступна: ' + str(exc))
    previous = None if force else getattr(runner, '_last_progress_snapshot', None)
    rendered = render(value, previous)
    if not write_projection(runner.root / 'progress.json', value):
        value['telemetry_incomplete'] = True
    if rendered:
        runner.progress(rendered)
    display = getattr(runner, '_display', None)
    if display is not None and hasattr(display, 'summary'):
        display.summary(value)
        pending = [(q['id'], q['question']) for q in value['questions']]
        if pending and pending != getattr(runner, '_console_questions', None):
            display.notice('Нужен ваш ответ. Для просмотра вопросов и ответа выполните в другом окне:\n' + value['answer_command'])
        runner._console_questions = pending
    runner._last_progress_snapshot = value
    return value

def dashboard(value, include_time=True, include_command=True):
    """Answer operator questions without equating activity with correctness."""
    from progress import elapsed
    tasks = value.get('tasks', [])
    operations = value.get('operations', [])
    questions = value.get('questions', [])
    blocked = [t['id'] for t in tasks if t['status'] == 'blocked']
    near_limit = any(op.get('timeout_seconds') and op['elapsed_seconds'] >= .8*op['timeout_seconds'] for op in operations)
    quiet = any(op.get('idle_seconds') is not None and op['idle_seconds'] >= 300 for op in operations)
    if value.get('console_error'):
        health = 'Запуск остановлен с ошибкой. Нужна проверка журнала.'
    elif value.get('ready'):
        health = 'Завершено. Итоговая проверка пройдена.'
    elif value.get('status') == 'interrupted':
        health = 'Работа остановлена. Для продолжения запустите мастер.'
    elif questions:
        health = 'Нужен ваш ответ: вопросов ' + str(len(questions)) + '.'
        if operations:
            health += ' Независимая работа продолжается.'
    elif blocked or value.get('integration') == 'blocked' or value.get('status') == 'blocked':
        health = 'Есть блокировка. Требуется вмешательство; подробности в журнале.'
    elif value.get('telemetry_incomplete'):
        health = 'Сводка временно неполна. Обновление повторится автоматически.'
    elif value.get('telemetry_stale'):
        health = 'Сводка устарела. Текущее состояние процесса неизвестно.'
    elif near_limit:
        health = 'Долгий этап: близок лимит времени. Автоматический контроль продолжается.'
    elif quiet:
        health = 'Давно нет новых событий. Автоматический контроль продолжается.'
    else:
        health = 'В работе. Запросов на вмешательство нет.'
    done, total = value.get('done', 0), value.get('total', 0)
    lines = [health, f'Принято: {done} из {total} задач.' if total else 'План задач ещё не закреплён.']
    if value.get('ready'):
        current = ['Сейчас: работа завершена.']
    elif value.get('status') == 'interrupted' or value.get('console_error'):
        current = ['Сейчас: исполнение остановлено.']
    elif value.get('telemetry_stale'):
        current = ['Сейчас: нет свежих данных об исполнении.']
    elif operations:
        current = []
        for op in operations[:3]:
            line = 'Сейчас: '
            if include_time:
                line += elapsed(op['elapsed_seconds']) + ' · '
            line += op['title']
            if op.get('timeout_seconds') and op['elapsed_seconds'] >= .8*op['timeout_seconds']:
                line += ' (лимит ' + elapsed(op['timeout_seconds']) + ')'
            current.append(line)
        if len(operations) > 3:
            current.append('Других активных операций: ' + str(len(operations)-3))
    elif blocked:
        current = ['Заблокировано: ' + ', '.join(blocked[:3])]
    elif questions:
        current = ['Сейчас: ожидание ответа.']
    else:
        active = [t['id'] for t in tasks if t['status'] == 'running']
        current = ['Сейчас: ' + (', '.join(active[:3]) + '; подготовка следующего шага.' if active else 'подготовка следующего шага.')]
    lines.extend(current)
    remaining = max(0, total-done)
    if value.get('ready'):
        lines.append('Осталось: 0 задач. Обязательные проверки завершены.')
    elif total:
        lines.append(('Осталось: ' + str(remaining) + ' задач и итоговая проверка.' if remaining else
                      'Осталась итоговая проверка.') + ' Оценки времени пока нет.')
    else:
        lines.append('Остаток будет известен после проверки плана.')
    if questions and include_command:
        lines.append('Для ответа выполните в другом окне:\n' + value['answer_command'])
    return '\n'.join(lines)
