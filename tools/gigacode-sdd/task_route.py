"""Human task names and dependency routes; never derive UI copy from prompts."""
LEGACY_TITLES = {
    'sdd-backend-prepare': 'Подготовка backend',
    'sdd-frontend-prepare': 'Подготовка frontend',
    'backend-modular-code': 'Реализация backend',
    'backend-modular-its': 'Интеграционные тесты backend',
    'backend-regression-compat': 'Проверка обратной совместимости',
    'backend-stand-experiment': 'Подготовка стенда и эксперимент',
    'frontend-modular-page': 'Реализация страницы frontend',
    'frontend-server-e2e': 'Сквозная проверка frontend',
    'experiment-no-rebuild': 'Итоговый опыт без пересборки',
    'integration': 'Итоговая проверка всех требований',
}


def title(task):
    value = task.get('title')
    if isinstance(value, str) and value.strip() and len(value) <= 100 and not any(ord(c) < 32 or ord(c) == 127 for c in value):
        return value.strip()
    return LEGACY_TITLES.get(task['id'], task['id'])


def route(tasks, stopped=False):
    remaining = [t for t in tasks if t['status'] != 'done']
    if not remaining:
        return []
    by_id = {t['id']: t for t in tasks}
    pending, ordered = {t['id'] for t in remaining}, []
    while pending:
        ready = [t for t in remaining if t['id'] in pending and not (set(t.get('depends_on', t.get('waiting_for', []))) & pending)]
        if not ready:
            # Incomplete telemetry must not fabricate a dependency order.
            ready = [t for t in remaining if t['id'] in pending]
        ordered.extend(ready)
        pending.difference_update(t['id'] for t in ready)
    serial = all(ordered[i-1]['id'] in ordered[i].get('depends_on', ordered[i].get('waiting_for', []))
                 for i in range(1, len(ordered)))
    lines = ['Маршрут оставшихся задач:' if serial else 'Оставшиеся задачи и зависимости:']
    labels = {'running': ('▶', 'сейчас'), 'waiting-user': ('?', 'нужен ответ'),
              'blocked': ('!', 'заблокировано'), 'interrupted': ('■', 'остановлено'),
              'accepted': ('✓', 'проверено; ожидает интеграции'), 'pending': ('○', 'ожидает')}
    for i, task in enumerate(ordered):
        state = 'interrupted' if stopped and task['status'] == 'running' else task['status']
        mark, label = labels.get(state, ('○', state))
        line = ('  → ' if serial and i else '') + mark + ' ' + title(task) + ' [' + label + ']'
        if not serial:
            waiting = task.get('waiting_for', [d for d in task.get('depends_on', []) if d not in by_id or by_id[d]['status'] != 'done'])
            if waiting:
                line += '; после: ' + ', '.join(title(by_id.get(key, {'id': key})) for key in waiting)
            elif state == 'pending':
                line += '; зависимости выполнены'
        lines.append(line)
    return lines
