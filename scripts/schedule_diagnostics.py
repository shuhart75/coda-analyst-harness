"""Explain future resource capacity without modifying the computed schedule."""
from datetime import date, timedelta


def resource_report(tasks, schedules, team, closed, unavailable, today,
                    candidates, needs_forecast):
    horizon = max([today, *(item.finish for item in schedules.values() if item.finish)])
    days = []
    current = today
    resources = sorted({resource for group in team.values() for resource in group})
    while current <= horizon:
        if current.weekday() < 5 and current not in closed:
            for resource in resources:
                if current in unavailable.get(resource, set()):
                    days.append({'date': str(current), 'resource': resource, 'available': 0,
                                 'reason': 'resource-unavailable'})
                    continue
                load, work, future = 0.0, [], []
                for key, scheduled in schedules.items():
                    if scheduled.assignee == resource:
                        amount = (sum(value for day, value in scheduled.allocations if day == current)
                                  if scheduled.allocations else
                                  float(scheduled.start <= current <= (scheduled.finish or scheduled.start)))
                        if amount:
                            load += amount
                            work.append({'task': key, 'load': amount})
                    task = tasks[key]
                    if scheduled.start <= current or not needs_forecast(task) or task.role.upper() == 'QA':
                        continue
                    compatible, _ = candidates(task, team)
                    if resource not in compatible:
                        continue
                    reasons = []
                    if task.not_before and date.fromisoformat(task.not_before) > current:
                        reasons.append('not-before')
                    scope = key.removesuffix(task.task_id).rstrip('/')
                    for ref in task.depends_on:
                        local = scope + '/' + ref if scope else ref
                        dep = local if local in tasks else ref
                        end = (schedules[dep].finish if dep in schedules else
                               date.fromisoformat(tasks[dep].actual_finish or tasks[dep].completed_by))
                        if end >= current:
                            reasons.append('dependency:' + dep)
                    future.append({'task': key, 'scheduled_start': str(scheduled.start),
                                   'reasons': reasons or ['requires-contiguous-slot-or-other-resource-selected']})
                days.append({'date': str(current), 'resource': resource, 'available': 1,
                             'load': round(load, 6), 'free': round(max(0, 1 - load), 6),
                             'tasks': work, 'waiting': future,
                             'reason': 'allocated' if load >= 1 else 'waiting-work' if future else 'no-ready-work'})
        current += timedelta(days=1)
    return {'schema_version': 1, 'as_of': str(today), 'through': str(horizon),
            'scope': 'generated-execution-tasks; preserved forecast blocks are not recalculated', 'days': days}
