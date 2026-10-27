"""Read-only selection of current deliveries plus the next confirmed queue item.

Reuse full-quarter schedules. Selecting fewer rows must never free capacity or
move a task. Forecast starts and generated QA windows are not start evidence.
"""
from __future__ import annotations

from datetime import date
import json
import re

from actual_progress_scope import expanded_with_paths, load_forecast_scope
from project_layout import feature_root, delivery_title
from actual_progress_layout import load_layout

FOCUS_VIEW = 'actual-progress-focus'
CANCELLED = {'cancelled', 'canceled', 'superseded', 'deleted', 'отменен', 'отменён', 'отменена'}
DONE = {'done', 'closed', 'complete', 'completed', 'выполнен', 'выполнена', 'завершен', 'завершена'}
STARTED = {'in-progress', 'in_progress', 'in progress', 'doing', 'development', 'testing',
           'code_review', 'code review', 'review', 'разработка', 'тестирование', 'ревью кода'}
WAITING = {'planned', 'todo', 'open', 'backlog', 'not-started', 'not_started', 'created', 'создан', 'proposed', 'предложен'}


def remaining(task):
    return (task.kind != 'candidate' and task.status.lower() not in CANCELLED | DONE
            and not task.actual_finish and not task.completed_by and task.progress != 100
            and not getattr(task, 'production_confirmed', False))


def started(task):
    return (bool(task.actual_start) or (task.progress is not None and task.progress > 0)
            or task.status.lower() in STARTED)


def classify(tasks):
    pending = [task for task in tasks.values() if remaining(task)]
    if not pending:
        return 'closed'
    if any(started(task) for task in pending):
        return 'current'
    if all(task.status.lower() in WAITING for task in pending):
        return 'next'
    return 'unknown'


def select(entries):
    current = [entry for entry in entries if entry['state'] == 'current']
    waiting = next((entry for entry in entries if entry['state'] == 'next'), None)
    return current + ([waiting] if waiting else [])


def without_deployed_tasks(text, tasks, overlay):
    aliases = {'TASK_' + overlay.to_alias(task.task_id) for task in tasks.values()
               if getattr(task, 'production_confirmed', False)}
    return '\n\n'.join(block for block in text.split('\n\n')
                       if not any(f'as [{alias}]' in block for alias in aliases)).rstrip() + '\n'


def release_milestones(gantt_dir, selected, outputs):
    """Keep confirmed release associations, never infer membership from dates."""
    keys = {task.release_label.split(' · ')[0] for entry in selected
            for task in entry['tasks'].values()
            if task.kind != 'candidate' and task.status.lower() not in CANCELLED
            and not getattr(task, 'production_confirmed', False)} - {'', 'релиз не уточнён'}
    registry = gantt_dir / 'releases.json'
    bindings = {}
    if registry.exists():
        # Use the same validation as the full view before consuming identity/state.
        from gantt_releases import milestone_overrides
        milestone_overrides(gantt_dir)
        bindings = {item['alias']: item for item in json.loads(registry.read_text())['releases']}
    pattern = re.compile(r'^\[[^\]\n]+\] as \[([^\]\n]+)\] happens at (\d{4}/\d{2}/\d{2})\s*$')
    legacy = {}
    for name in ('common.puml', 'actual-progress.puml'):
        path = gantt_dir / 'preamble' / name
        if not path.exists():
            continue
        text, _ = expanded_with_paths(path, outputs)
        comments = []
        for line in text.splitlines():
            if line.startswith("'"):
                comments.append(line)
                continue
            match = pattern.fullmatch(line)
            if match:
                context = '\n'.join(comments)
                matches = [key for key in keys if re.search(
                    r'(?<![\w-])' + re.escape(key) + r'(?![\w-])', context)]
                deployed = re.search(r'установлен[аоы]?\s+в\s+ПРОМ|внедр[её]н[аоы]?\s+в\s+ПРОМ', context, re.I)
                if len(matches) == 1 and not deployed:
                    legacy[match[1]] = matches[0]
            comments = []
    full, _ = expanded_with_paths(gantt_dir / 'actual-progress.puml', outputs)
    lines = full.splitlines()
    result, days = [], []
    for line in lines:
        match = pattern.fullmatch(line)
        if not match:
            continue
        alias, day = match.groups()
        binding = bindings.get(alias)
        included = (binding['state'] == 'planned' and binding['release'] in keys
                    if binding else alias in legacy)
        if included:
            result.append(line)
            result.extend(item for item in lines if item.startswith(f'[{alias}] '))
            days.append(day)
    return result, sorted(set(days))


def prepare_focus(gantt_dir, overlay, outputs, sections, header, closed_days, order=None):
    root = gantt_dir.parents[2]
    scope = load_forecast_scope(root, gantt_dir.parent.name)
    layout = load_layout(gantt_dir)
    if layout:
        sections = [(section['feature'],
                     section['title'] if section['feature'] in scope.preserved else
                     delivery_title(root, scope.features.get(section['feature'], section['feature']), section['title']),
                     gantt_dir / section['include']) for section in layout.sections]
    else:
        known = {slug for slug, _, _ in sections}
        for slug, decision in scope.preserved.items():
            if slug in known:
                continue
            path = scope.includes[slug]
            text, _ = expanded_with_paths(path, outputs)
            parts = re.split(r'^-- (.+?) --\s*$', text, flags=re.MULTILINE)
            matches = [parts[i] for i in range(1, len(parts), 2)
                       if any(f'[{alias}]' in parts[i + 1] for alias in decision['aliases'])]
            if len(matches) != 1:
                # No independently named section means no safe focused selection.
                continue
            sections.append((slug, matches[0], path))
        sections = sorted(sections, key=lambda entry: (order or {}).get(entry[0], len(order or {})))
    entries = []
    for slug, title, path in sections:
        if slug in scope.exclusions:
            continue
        if slug in scope.preserved:
            tasks, state = {}, 'next'
        else:
            tasks = overlay.load_tasks(feature_root(root, scope.features.get(slug, slug)), root)
            state = classify(tasks)
        entries.append({'feature': slug, 'title': title, 'path': path, 'tasks': tasks, 'state': state})
    selected = select(entries)
    blocks = []
    for entry in selected:
        text, _ = expanded_with_paths(entry['path'], outputs)
        # A preserved FORECAST include can contain several independently named sections.
        parts = re.split(r'^-- (.+?) --\s*$', text, flags=re.MULTILINE)
        if len(parts) > 1:
            matches = [parts[i + 1] for i in range(1, len(parts), 2) if parts[i] == entry['title']]
            if len(matches) != 1:
                raise ValueError('Focused forecast section is ambiguous: ' + entry['title'])
            text = matches[0]
        text = without_deployed_tasks(text, entry['tasks'], overlay)
        label = 'Сейчас' if entry['state'] == 'current' else 'Следующая'
        blocks.append(f"-- {label}: {entry['title']} --\n" + text)
    starts = [date.fromisoformat(value.replace('/', '-')) for text in blocks
              for value in re.findall(r'\b(?:starts|happens at) (\d{4}[-/]\d{2}[-/]\d{2})', text)]
    milestones, highlighted_days = release_milestones(gantt_dir, selected, outputs)
    starts += [date.fromisoformat(day.replace('/', '-')) for day in highlighted_days]
    start = min(starts, default=overlay.harness_today())
    lines = header(gantt_dir, 'Текущие и следующая фича', start, closed_days, highlighted_days)
    lines += milestones
    lines += ["' Derived selection; dates/resources copied from full-quarter schedules."]
    lines += [f"' Focus {entry['state']}: {entry['feature']}" for entry in selected]
    unknown = [entry['feature'] for entry in entries if entry['state'] == 'unknown']
    if unknown:
        lines.append("' Start state unknown; not inferred from forecast dates: " + ', '.join(unknown))
    lines += blocks
    lines += ['legend', 'Сейчас — есть начатая незавершённая работа. Следующая — первая в очереди без активных задач.',
              'Завершённые задачи не определяют текущую фичу. Подтверждённо выпущенные задачи скрыты.']
    if not selected:
        lines.append('Нет текущих фич или подтверждённого следующего объёма.')
    if unknown:
        lines.append('Есть фичи с неподтверждённым состоянием начала; они не включены автоматически.')
    lines += ['end legend', '@endgantt']
    source = gantt_dir / (FOCUS_VIEW + '.puml')
    outputs[source] = '\n'.join(lines).rstrip() + '\n'
    target = gantt_dir / (FOCUS_VIEW + '-confluence.puml')
    outputs[target] = expanded_with_paths(source, outputs)[0]
    return entries, selected
