"""Small user-facing projection; activity never substitutes for acceptance."""
import json
from pathlib import Path
import time
import operator_questions as oq

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
    from controller import save
    name = oq.fingerprint([task['id'], value['model']])[:24]
    save(runner.root / 'progress' / (name + '.json'), {
        'task': task['id'], 'model': value['model'], 'stage': value['stage'],
        'status': value['status'], 'resource_wait': value.get('resource_wait'),
        'checks': [{'ok': c.get('ok'), 'log': c.get('log')} for c in value.get('checks', [])],
        'updated_at': time.time()})


def snapshot(root, state=None):
    from controller import read
    root = Path(root)
    state = state if state is not None else read(root / 'state.json')
    plan = read(root / 'plan.json') if (root / 'plan.json').is_file() else {'tasks': []}
    progress = [read(p) for p in sorted((root / 'progress').glob('*.json'))] if (root / 'progress').exists() else []
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
    return {'status': state.get('status'), 'tasks': rows,
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
    from controller import save
    value = snapshot(runner.root, runner.state)
    previous = None if force else getattr(runner, '_last_progress_snapshot', None)
    rendered = render(value, previous)
    save(runner.root / 'progress.json', value)
    if rendered:
        runner.progress(rendered)
    runner._last_progress_snapshot = value
    return value
