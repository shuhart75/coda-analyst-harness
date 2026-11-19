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
            'goal': task.get('acceptance', {}).get('deliverables') or [task['prompt']],
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


def render(value):
    lines = ['План: принято ' + str(value['done']) + '/' + str(value['total']) +
             '; состояние: ' + STATUSES.get(value['status'], str(value['status'])) +
             '; итоговая приёмка: ' + ('пройдена' if value['ready'] else 'ещё не пройдена')]
    for row in value['tasks']:
        line = row['id'] + ': ' + STATUSES.get(row['status'], row['status'])
        if row['status'] == 'pending' and row['waiting_for']:
            line += '; после ' + ', '.join(row['waiting_for'])
        if row['status'] == 'running':
            line += '; ' + '; '.join(candidate_label(p) +
                (' (ждёт ресурс)' if (p.get('resource_wait') or {}).get('state') == 'waiting' else '')
                for p in row['candidates'])
        if row['status'] in ('blocked', 'waiting-user') and row['error']:
            line += '; ' + str(row['error']).replace('\n', ' ')[:500]
        lines.append(line)
        goal = '; '.join(row['goal']).replace('\n', ' ')
        lines.append('  Результат задачи: ' + goal[:240] + ('…' if len(goal) > 240 else ''))
        for item in row['deferred_evidence']:
            lines.append('  Проверка впереди: ' + item['requirement'] + ' → ' + item['owner'] + ': ' + item['evidence'])
    for service in value['services']:
        lines.append('Стенд ' + service['id'] + ': ' + service['status'] + '; ' + str(service['health']))
    if value.get('integration'):
        lines.append('Итоговая проверка всех требований: ' + STATUSES.get(value['integration'], value['integration']))
    if value['questions']:
        lines.append('Нужны ответы: ' + str(len(value['questions'])))
        for q in value['questions']:
            lines.append(q['task'] + ': ' + q['question'])
        lines.append('Для ответа выполните в другом окне:\n' + value['answer_command'])
    return '\n'.join(lines)


def publish(runner, force=False):
    from controller import save
    value = snapshot(runner.root, runner.state)
    rendered = render(value)
    save(runner.root / 'progress.json', value)
    if force or getattr(runner, '_last_progress_text', None) != rendered:
        runner.progress(rendered)
        runner._last_progress_text = rendered
    return value
