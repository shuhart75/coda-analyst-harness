"""Small user-facing projection; activity never substitutes for acceptance."""
import json
from collections import Counter
import task_route
from pathlib import Path
import time
import operator_questions as oq
from progress_io import read_projection, write_projection

STAGES = {'execute': 'разработка', 'checks': 'обязательные проверки', 'review': 'независимое ревью',
          'adjudicate': 'разбор спорного замечания', 'accepted': 'вариант проверен'}
STATUSES = {'unknown': 'нет подтверждения', 'pending': 'ожидает запуска', 'done': 'принято', 'running': 'в работе',
            'blocked': 'заблокировано', 'waiting-user': 'нужен ответ', 'interrupted': 'остановлено',
            'waiting-scope': 'ждёт пересекающуюся задачу', 'waiting-retry': 'пауза перед повтором', 'complete': 'завершено'}

SERVICE_STATUSES = {'awaiting-approval': 'нужно разрешение на запуск', 'starting': 'запускается',
                    'ready': 'готов', 'failed': 'ошибка запуска', 'stopped': 'остановлен',
                    'released': 'остановлен и освобождён'}

def solution_variants(count):
    if count % 10 == 1 and count % 100 != 11:
        return 'вариант решения'
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return 'варианта решения'
    return 'вариантов решения'


def candidate_label(value):
    phase = STAGES.get(value['stage'], value['stage'])
    if value['status'] == 'not-observed':
        return value['model'] + ': состояние ещё не получено'
    if value['status'] in ('stalled', 'quarantined', 'external-blocker'):
        return value['model'] + ': ' + {'stalled': 'приостановлен', 'quarantined': 'требует проверки',
                                      'external-blocker': 'ждёт внешних данных'}[value['status']]
    wait = value.get('resource_wait') or {}
    if value['status'] in ('pending', 'running') and wait.get('state') == 'waiting':
        return value['model'] + ': ждёт ресурс' + ('; занят вариантом ' + wait['holder'] if wait.get('holder') else '')
    if value['status'] in ('waiting-user', 'waiting-scope', 'waiting-retry', 'interrupted'):
        phase = STATUSES[value['status']] + ' (' + phase + ')'
    if value['status'] == 'pending':
        phase = 'ожидает запуска (' + phase + ')'
    return value['model'] + ': ' + phase


def candidate(runner, task, value):
    name = oq.fingerprint([task['id'], value['model']])[:24]
    history = []
    for item in value.get('history', [])[-3:]:
        if not isinstance(item, dict):
            continue
        detail = item.get('detail', '')
        detail = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        history.append({'kind': item.get('kind'), 'detail': detail[:1500], 'detail_truncated': len(detail) > 1500})
    write_projection(runner.root / 'progress' / (name + '.json'), {
        'task': task['id'], 'model': value['model'], 'stage': value['stage'],
        'status': value['status'], 'resource_wait': value.get('resource_wait'), 'history': history,
        'checks': [{'ok': c.get('ok'), 'log': c.get('log')} for c in value.get('checks', [])],
        'updated_at': time.time()})


def snapshot(root, state=None):
    from controller import read
    root = Path(root)
    state = state if state is not None else read(root / 'state.json')
    config = read(root / 'config.json') if (root / 'config.json').is_file() else {}
    models = config.get('execution_models') or config.get('plan_review_models')
    if not isinstance(models, list) or not models or not all(isinstance(m, str) and m for m in models):
        models = ['DeepSeek-V4.1-Flash-1017', 'GLM-5.3-Flash']
    plan = read(root / 'plan.json') if (root / 'plan.json').is_file() else {'tasks': []}
    problems = []
    progress = [read_projection(p, problems) for p in sorted((root / 'progress').glob('*.json'))] if (root / 'progress').exists() else []
    progress = [p for p in progress if all(k in p for k in ('task', 'model', 'stage', 'status'))]
    rows = []
    for task in plan['tasks']:
        rec = state.get('tasks', {}).get(task['id'], {})
        rows.append({'id': task['id'], 'title': task_route.title(task), 'depends_on': task['depends_on'], 'status': rec.get('status', 'pending'),
            'goal': task.get('acceptance', {}).get('deliverables') or [],
            'waiting_for': [d for d in task['depends_on'] if state.get('tasks', {}).get(d, {}).get('status') != 'done'],
            'error': rec.get('error'), 'result': rec.get('workspace'),
            'deferred_evidence': rec.get('review', {}).get('deferred_evidence', []),
            'candidates': [next((p for p in progress if p['task'] == task['id'] and p['model'] == model),
                 {'model': model, 'status': 'not-observed', 'stage': 'unknown'}) for model in models]})
    questions = oq.questions(root, pending_only=True)
    services = []
    for path in sorted((root / 'managed-services').glob('*/service.json')):
        value = read(path)
        services.append({'id': value['spec']['id'], 'status': value['status'],
                         'health': value['spec']['health'], 'log': value.get('log')})
    readiness = read(root / 'readiness.json') if (root / 'readiness.json').is_file() else {}
    runtime = read_projection(root / 'console-status.json', problems) if (root / 'console-status.json').is_file() else {}
    if state.get('status') == 'running' and not runtime:
        problems.append('runtime-status-unavailable')
    stale = bool(runtime) and state.get('status') not in ('blocked', 'interrupted', 'complete') and time.time()-runtime.get('observed_at', 0) > 90
    try:
        # A prompt plus a stream file is an invocation attempt, including failed
        # CLI launches. Do not count build checks, read prompts, or scan event logs.
        attempts = sum(1 for p in (root/'logs').glob('*/prompt.txt')
                       if not p.is_symlink() and not p.parent.is_symlink() and (p.parent/'stdout.jsonl').is_file())
    except OSError:
        attempts = None
        problems.append('session-attempt-count-unavailable')
    service_stop = read_projection(root/'service-stop.json', problems) if (root/'service-stop.json').is_file() else {}
    # Observer output is advisory. Its absence/failure cannot degrade execution telemetry.
    observation_path = root/'observer/latest.json'
    try:
        observation = read_projection(observation_path, []) if observation_path.is_file() else {}
    except OSError:
        observation = {}
    if not isinstance(observation, dict):
        observation = {}
    captured = observation.get('snapshot_at')
    if isinstance(captured, (int, float)):
        observation['stale'] = time.time()-captured > 900
    return {'service_stop': service_stop, 'session_attempts': attempts, 'jobs': config.get('jobs'),
            'observation': observation,
            'observer_health': read_projection(root/'observer/health.json', []) if (root/'observer/health.json').is_file() else {},
            'controller_events': runtime.get('controller_events', []),
            'operations': runtime.get('operations', []), 'telemetry_stale': stale,
            'telemetry_incomplete': bool(problems),
            'status': state.get('status'), 'tasks': rows,
            'done': sum(r['status'] == 'done' for r in rows), 'total': len(rows),
            'questions': questions, 'answer_command': oq.command(root), 'services': services,
            'preflight_passed': (state.get('preflight') or {}).get('passed') is True,
            'plan_pinned': bool(state.get('plan_sha256')),
            'integration_error': state.get('integration', {}).get('error'),
            'integration_attempts': state.get('integration', {}).get('attempts'),
            'integration_checks': [{'returncode': c.get('returncode'), 'timed_out': c.get('timed_out', False)}
                                   for c in state.get('integration', {}).get('checks', []) if isinstance(c, dict)],
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
        questions.append('Откройте отдельное окно терминала и выполните:\n' + value['answer_command'])
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
    runner._last_progress_snapshot = value
    return value

def model_label(model):
    return {'DeepSeek-V4.1-Flash-1017': 'DeepSeek', 'GLM-5.3-Flash': 'GLM'}.get(model, model or 'модель не указана')


def operation_label(op, tasks):
    """The actor and candidate author are distinct, explicit identities."""
    if op.get('purpose') == 'observation':
        return op.get('title', 'Аудитор') + ' · ' + model_label(op.get('model'))
    service_title = {'integration': 'Итоговая приёмка всего решения', 'preflight': 'Проверка возможностей и окружения',
                     'plan': 'Планирование и проверка плана'}.get(op.get('task_id'))
    if service_title:
        phase = op.get('phase') or 'подготовка'
        return service_title + ' · ' + model_label(op.get('model')) + ' · ' + STAGES.get(phase, phase)
    name = next((task_route.title(t) for t in tasks if t['id'] == op.get('task_id')), None)
    if not name or op.get('kind') != 'llm':
        return op.get('title', 'операция')
    actor, author = op.get('model'), op.get('candidate_model')
    phase = STAGES.get(op.get('phase'), op.get('phase', ''))
    subject = model_label(actor)
    if phase == 'разработка':
        action = ('разрабатывает свой вариант' if author and author == actor else
                  'разрабатывает вариант ' + model_label(author) if author else 'выполняет разработку')
    elif phase in ('независимое ревью', 'разбор спорного замечания', 'разбор замечаний'):
        action = ('проверяет вариант ' if phase == 'независимое ревью' else 'разбирает возражение по варианту ')
        action += model_label(author) if author else '(автор не указан)'
    else:
        action = phase
    return name + ' · ' + subject + ' ' + action


def _candidate_is_active(task, candidate, operations):
    if (candidate.get('resource_wait') or {}).get('state') == 'waiting':
        return False
    return candidate.get('status') == 'running' and any(
        op.get('task_id') == task['id'] and op.get('candidate_model') == candidate['model']
        for op in operations)


def observation_lines(value):
    """Advisory assessment stays separate from factual controller health."""
    observation = value.get('observation') or {}
    if not isinstance(observation, dict) or not observation:
        return []
    labels = {'progress': 'продвижение есть', 'waiting': 'ожидание результата',
              'concern': 'нужна проверка', 'unknown': 'данных недостаточно',
              'disagreement': 'оценки расходятся', 'unavailable': 'недоступны'}
    stamp = observation.get('snapshot_at')
    from operator_stages import timestamp
    captured = timestamp(stamp) if isinstance(stamp, (int, float)) else None
    stopped = value.get('status') in ('interrupted', 'complete', 'blocked') or bool(value.get('console_error') or value.get('service_stop'))
    stale = observation.get('stale') or (captured is not None and (time.time()-stamp > 900 or stamp-time.time() > 60))
    prefix = 'Предыдущее наблюдение аудиторов' if stale or stopped else 'Аудиторы'
    heading = prefix + (', снимок ' + captured if captured else ', время снимка неизвестно')
    if stopped:
        heading += ' (не описывает текущее исполнение)'
    elif stale:
        heading += ' (устарело)'
    heading += ': ' + labels.get(observation.get('status'), 'данных недостаточно') + '.'
    if observation.get('partial'):
        heading += ' Наблюдение частичное.'
    lines = [heading]
    def sentence(raw):
        if not isinstance(raw, str) or not raw.strip():
            return ''
        import unicodedata
        text = ' '.join(''.join(c if not unicodedata.category(c).startswith('C') else ' '
                               for c in raw).split())
        # Never expose a cut-off instruction or half a model conclusion.
        return text if len(text) <= 400 else 'Подробности в отчёте.'
    results = observation.get('results') or {}
    if observation.get('status') == 'disagreement' and isinstance(results, dict):
        for role, title in (('progress', 'Прогресс'), ('scope', 'Соответствие задаче')):
            result = results.get(role) or {}
            summary = sentence(result.get('summary')) if isinstance(result, dict) else ''
            if summary:
                lines.append(title + ': ' + summary)
    else:
        result = next((r for r in results.values() if r.get('status') != 'unavailable'), {}) if isinstance(results, dict) else {}
        if observation.get('status') == 'unavailable':
            result = {}
        summary = sentence(result.get('summary')) if isinstance(result, dict) else ''
        summary = summary or sentence(observation.get('summary'))
        if summary:
            lines.append('Основание: ' + summary)
    errors = observation.get('audit_errors') or {}
    if errors:
        failed = ', '.join(model_label(r.get('model')) + ': ' + (sentence(r.get('summary')) or 'ошибка аудита')
                           for r in errors.values())
        lines.append('Ошибка аудита: ' + failed + '.')
        if observation.get('retry_pending') and not stopped:
            lines.append('Следующее задание аудиторам запустится автоматически.')
    health = value.get('observer_health') or {}
    for role in ('progress', 'scope'):
        item = health.get(role, {})
        if not isinstance(item, dict) or not item:
            continue
        def clock(stamp):
            return timestamp(stamp) if stamp is not None else 'ещё не было'
        phase = {'starting': 'запускается', 'probe': 'проверка режима',
                 'assessment': 'оценивает снимок', 'waiting': 'ожидание следующего снимка'}.get(item.get('phase'), 'состояние неизвестно')
        line = ('Аудитор ' + model_label(item.get('model')) + ': попытка ' + str(item.get('attempt', '?'))
                + '; ' + ('исполнение остановлено' if stopped else phase)
                + '; последний успех ' + clock(item.get('last_success_at')))
        if item.get('last_success_at') and item.get('controller_started_at') and item['last_success_at'] < item['controller_started_at']:
            line += '; успех предыдущего запуска контроллера'
        if item.get('failure'):
            line += '; ' + (sentence(item['failure']) or 'ошибка аудита')
        if item.get('next_attempt_at') and not stopped:
            line += '; повтор не раньше ' + clock(item['next_attempt_at'])
        lines.append(line + '.')
    return lines


def health_summary(value):
    tasks = value.get('tasks', [])
    operations = value.get('operations', [])
    questions = value.get('questions', [])
    blocked = [t['id'] for t in tasks if t['status'] == 'blocked']
    execution_operations = [op for op in operations if op.get('purpose') != 'observation']
    near_limit = any(op.get('timeout_seconds') and op['elapsed_seconds'] >= .8*op['timeout_seconds'] for op in execution_operations)
    quiet = any(op.get('idle_seconds') is not None and op['idle_seconds'] >= 300 for op in execution_operations)
    if value.get('status') == 'waiting-user' and questions:
        health = 'Нужно решение по работающим сервисам. Процессы сохранены.' if any(
            q.get('task') == 'service-handoff' for q in questions) else 'Нужен ваш ответ: вопросов ' + str(len(questions)) + '.'
    elif value.get('service_stop'):
        health = value['service_stop']['reason']
    elif value.get('console_error'):
        health = 'Запуск остановлен с ошибкой. Нужна проверка журнала.'
    elif value.get('ready'):
        health = 'Завершено. Итоговая проверка пройдена.'
    elif value.get('status') == 'interrupted':
        health = 'Работа остановлена. Для продолжения запустите мастер.'
    elif questions:
        health = 'Нужен ваш ответ: вопросов ' + str(len(questions)) + '.'
        if execution_operations:
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
    return health


def dashboard(value, include_time=True, include_command=True, include_overview=True, compact=False):
    """Answer operator questions without equating activity with correctness."""
    from progress import elapsed
    tasks = value.get('tasks', [])
    operations = value.get('operations', [])
    questions = value.get('questions', [])
    blocked = [t['id'] for t in tasks if t['status'] == 'blocked']
    execution_operations = [op for op in operations if op.get('purpose') != 'observation']
    health = health_summary(value)
    done, total = value.get('done', 0), value.get('total', 0)
    lines = [health] + observation_lines(value)
    lines.append(f'Принято: {done} из {total} задач.' if total else 'План задач ещё не закреплён.')
    if value.get('ready'):
        current = ['Сейчас: работа завершена.']
    elif value.get('status') == 'interrupted' or value.get('console_error'):
        current = ['Сейчас: исполнение остановлено.']
    elif value.get('telemetry_stale'):
        current = ['Сейчас: нет свежих данных об исполнении.']
    elif operations:
        current = []
        displayed = execution_operations + [op for op in operations if op.get('purpose') == 'observation']
        for op in displayed[:3]:
            line = 'Сейчас: '
            if include_time:
                line += elapsed(op['elapsed_seconds']) + ' · '
            line += operation_label(op, tasks)
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
    if not compact and value.get('service_stop', {}).get('report'):
        lines.append('Отчёт об остановке: ' + value['service_stop']['report'])
    stopped = value.get('status') == 'interrupted' or bool(value.get('console_error'))
    if include_overview:
        from operator_stages import stages
        lines.append('Этапы: ' + ' → '.join(p['title'] + ' [' + STATUSES.get(p['status'], p['status']) + ']' for p in stages(value)))
        completed = [task_route.title(t) for t in tasks if t['status'] == 'done']
        if completed:
            lines.append('✓ ' + ' · '.join(completed))
        lines.extend(task_route.route(tasks, stopped=stopped))
    active_rows = [t for t in tasks if t['status'] in ('running', 'waiting-user', 'waiting-scope', 'blocked')]
    for task in ([] if compact else active_rows):
        for candidate in task.get('candidates', []):
            if not stopped and not value.get('telemetry_stale') and _candidate_is_active(task, candidate, operations):
                continue
            if candidate.get('status') == 'not-observed':
                continue
            label = candidate_label(candidate)
            prefix = '  Вариант ' if len(active_rows) == 1 else '  ' + task_route.title(task) + ' / '
            lines.append(prefix + ('сохранён: ' if stopped or value.get('telemetry_stale') else '') + label)
    if value.get('telemetry_stale') or value.get('telemetry_incomplete') or any('kind' not in op for op in operations):
        lines.append('LLM-сессии сейчас: число неизвестно, нет полного свежего статуса.')
    else:
        active_sessions = [] if stopped else [op for op in operations if op.get('kind') == 'llm']
        counts = Counter(op.get('model') or 'модель не указана' for op in active_sessions)
        line = 'LLM-сессии сейчас: ' + str(len(active_sessions))
        if counts:
            line += ' (' + ', '.join(model + ': ' + str(n) for model, n in sorted(counts.items())) + ')'
        lines.append(line + '.')
    if not stopped and not value.get('telemetry_stale'):
        candidates = [c for t in active_rows for c in t.get('candidates', [])]
        waiting_resource = sum(c.get('status') in ('running', 'pending') and
            (c.get('resource_wait') or {}).get('state') == 'waiting' for c in candidates)
        waiting_answer = sum(c.get('status') == 'waiting-user' for c in candidates)
        if waiting_resource or waiting_answer:
            if waiting_resource:
                noun = solution_variants(waiting_resource)
                verb = 'ждёт' if noun == 'вариант решения' else 'ждут'
                lines.append(f'В очереди: {waiting_resource} {noun} {verb} ресурс.')
            if waiting_answer:
                lines.append(f'Ответ пользователя ожидают: {waiting_answer} {solution_variants(waiting_answer)}.')
            else:
                lines.append('Ответ пользователя не требуется.')
        waiting_scope = sum(c.get('status') == 'waiting-scope' for c in candidates)
        if waiting_scope:
            lines.append(f'Ожидают завершения пересекающихся задач: {waiting_scope} {solution_variants(waiting_scope)}.')
    if not compact and include_overview and value.get('session_attempts') is not None:
        lines.append('Попыток запуска LLM по журналам прогона: ' + str(value['session_attempts']) + ' (включая повторы).')
    observation = value.get('observation') or {}
    if not compact and include_overview and observation.get('report'):
        lines.append('Отчёт аудиторов: ' + str(observation['report']))
    remaining = max(0, total-done)
    if value.get('ready'):
        lines.append('Осталось: 0 задач. Обязательные проверки завершены.')
    elif total:
        lines.append(('Осталось: ' + str(remaining) + ' задач и итоговая проверка.' if remaining else
                      'Осталась итоговая проверка.') + ' Оценки времени пока нет.')
    else:
        lines.append('Остаток будет известен после проверки плана.')
    if questions and include_command:
        lines.append('Откройте отдельное окно терминала и выполните:\n' + value['answer_command'])
    return '\n'.join(lines)
