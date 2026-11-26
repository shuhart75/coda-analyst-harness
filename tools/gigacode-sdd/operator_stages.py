"""Observed lifecycle and session detail, shared by terminal and web projections."""
from datetime import datetime
import math
import time


def timestamp(stamp, *, now=None):
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or not math.isfinite(stamp):
        return 'время неизвестно'
    now = time.time() if now is None else now
    try:
        text = datetime.fromtimestamp(stamp).astimezone().strftime('%d.%m.%Y %H:%M:%S %z')
    except (ValueError, OverflowError, OSError):
        return 'время неизвестно'
    seconds = now-stamp
    if seconds < -60:
        return text + ' (в будущем относительно часов сервера; проверьте часы)'
    minutes = int(max(0, seconds)//60)
    age = 'меньше минуты назад' if minutes == 0 else f'{minutes} мин. назад' if minutes < 60 else f'{minutes//60} ч {minutes%60} мин. назад'
    return text + ' (' + age + ')'


def stages(value):
    tasks = value.get('tasks', [])
    done = bool(tasks) and all(t.get('status') == 'done' for t in tasks)
    ops = value.get('operations', [])
    active = value.get('status') not in ('blocked', 'interrupted', 'complete') and not value.get('telemetry_stale') and not value.get('telemetry_incomplete')
    def phase(key, title, status, description):
        return {'id': key, 'title': title, 'status': status, 'description': description}
    preflight = value.get('preflight_passed')
    plan = value.get('plan_pinned')
    result = [phase('preflight', 'Проверка возможностей и окружения',
                    'done' if preflight else 'running' if active and any(o.get('task_id') == 'preflight' for o in ops) else 'unknown',
                    'Проверка CLI, моделей, разрешений и доступных инструментов.'),
              phase('plan', 'Планирование и проверка плана',
                    'done' if plan else 'running' if active and any('plan' in str(o.get('task_id')) for o in ops) else 'pending',
                    'Состав задач, зависимости, области изменений и обязательные проверки.'),
              phase('tasks', 'Выполнение задач', 'done' if done else 'blocked' if any(t.get('status') == 'blocked' for t in tasks) else 'interrupted' if tasks and value.get('status') == 'interrupted' else 'running' if active and tasks else 'pending',
                    f"Принято {sum(t.get('status') == 'done' for t in tasks)} из {len(tasks)} задач. Разработка, проверки и независимое ревью каждой задачи.")]
    integration = value.get('integration') or 'pending'
    if value.get('status') == 'interrupted' and integration == 'running':
        integration = 'interrupted'
    result.append(phase('integration', 'Итоговая приёмка всего решения', integration,
                        'Проверка всех требований на общем результате, исправление межкомпонентных дефектов и независимое ревью.'))
    checks = value.get('integration_checks') or []
    if checks:
        passed = sum(c.get('returncode') == 0 and not c.get('timed_out') for c in checks)
        result[-1]['description'] += f' Сохранённый результат проверок: {passed} из {len(checks)} успешны.'
    if value.get('integration_attempts') is not None:
        result[-1]['description'] += ' Попыток исполнения: ' + str(value['integration_attempts']) + '.'
    if value.get('integration_error'):
        result[-1]['error'] = value['integration_error']
    result.append(phase('readiness', 'Подтверждение готовности результата', 'done' if value.get('ready') else 'pending',
                        'Закрепление проверенного результата и разрешение экспорта. Принятие задач само по себе не означает готовность.'))
    return result
