"""Check release-plan forecasts against exact release work in the full Gantt."""
from __future__ import annotations

from datetime import date
from importlib import import_module
import json
from pathlib import Path
import re

HEADERS = ['Релиз', 'Плановая дата', 'Прогноз готовности для заказчиков', 'Фича',
           'Что получат пользователи', 'Бизнес-ценность']
UNKNOWN = 'Не определён'


def plan_rows(body: str) -> list[dict]:
    rows, inside = [], False
    for line in body.splitlines():
        if not line.startswith('|'):
            if inside and rows:
                break
            continue
        cells = [cell.strip() for cell in re.split(r'(?<!\\)\|', line.strip())[1:-1]]
        if cells == HEADERS:
            if inside:
                raise ValueError('Duplicate release-plan table')
            inside = True
            continue
        if not inside or all(re.fullmatch(r':?-+:?', cell) for cell in cells):
            continue
        if len(cells) != len(HEADERS) or any(not cell for cell in cells):
            raise ValueError('Release-plan table requires six nonempty columns')
        row = dict(zip(('release', 'planned', 'forecast', 'feature', 'capabilities', 'value'), cells))
        if any((old['release'], old['feature']) == (row['release'], row['feature']) for old in rows):
            raise ValueError('Duplicate release-plan row')
        rows.append(row)
    if not rows:
        raise ValueError('Release-plan table is missing or empty')
    return rows


def regular_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError('Release-plan evidence requires a project-relative file')
    path = root / relative
    if path.resolve() != path.absolute() or not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError('Release-plan source is missing, linked or outside project: ' + relative)
    return path


def end_from_gantt(task, expanded: str, overlay) -> tuple[date | None, bool]:
    alias = re.escape('TASK_' + overlay.to_alias(task.task_id))
    declarations = re.findall(r'^\[[^\n]+\] as \[' + alias + r'\].*$', expanded, re.M)
    if len(declarations) != 1:
        return None, False
    endings = re.findall(r'^\[' + alias + r'\] ends (\d{4}[-/]\d{2}[-/]\d{2})\s*$', expanded, re.M)
    if len(endings) == 1:
        return date.fromisoformat(endings[0].replace('/', '-')), False
    if endings:
        raise ValueError('Duplicate Gantt task endings: ' + task.task_id)
    # A milestone may be an unknown remaining workload. Only a recorded
    # completion makes it usable; a completed-by bound stays a bound.
    if task.status.lower() not in {'completed', 'done', 'closed'}:
        return None, False
    fact = task.actual_finish or task.completed_by
    milestone = re.search(r' happens at (\d{4}[-/]\d{2}[-/]\d{2})\s*$', declarations[0])
    if not fact or not milestone or milestone[1].replace('/', '-') != fact:
        return None, False
    return date.fromisoformat(fact), not bool(task.actual_finish)


def forecast_rows(quarter: Path, body: str, expanded: str) -> tuple[dict, list[dict]]:
    from project_layout import feature_root, delivery_selection, layout

    project = quarter.parent.parent.resolve()
    binding_path = regular_file(project, str((quarter / 'release-plan-sources.json').relative_to(project)))
    bindings = json.loads(binding_path.read_text(encoding='utf-8'))
    if not isinstance(bindings, dict) or bindings.get('schema_version') != 1 or not isinstance(bindings.get('rows'), list):
        raise ValueError('Invalid release-plan-sources.json schema')
    rows = plan_rows(body)
    identities = {(row['release'], row['feature']) for row in rows}
    selected = {}
    for entry in bindings['rows']:
        if not isinstance(entry, dict) or any(not isinstance(entry.get(key), str) or not entry[key].strip() for key in ('release', 'feature')):
            raise ValueError('Release-plan source row requires release and feature strings')
        identity = (entry.get('release'), entry.get('feature'))
        if identity not in identities or identity in selected:
            raise ValueError('Release-plan sources must match every row exactly once')
        selected[identity] = entry
    if set(selected) != identities:
        raise ValueError('Release-plan sources must match every row exactly once')
    overlay = import_module('sync-actual-progress-overlay')
    results = []
    for row in rows:
        entry = selected[(row['release'], row['feature'])]
        keys = entry.get('deliveries', [])
        unresolved = entry.get('unresolved_reason')
        if unresolved:
            if keys or not isinstance(unresolved, str) or not unresolved.strip():
                raise ValueError('Unresolved release scope requires a reason and no guessed delivery mapping')
            results.append({**row, 'expected_forecast': UNKNOWN, 'reasons': [unresolved], 'tasks': []})
            continue
        if (not isinstance(keys, list) or not keys or any(not isinstance(key, str) for key in keys)
                or len(keys) != len(set(keys))):
            raise ValueError('Release-plan row requires unique delivery keys or an unresolved_reason')
        evidence = entry.get('evidence', [])
        if not isinstance(evidence, list) or not evidence:
            raise ValueError('Release-plan mapping requires existing scope evidence')
        for proof in evidence:
            if not isinstance(proof, dict):
                raise ValueError('Release-plan evidence requires path and quote')
            source = regular_file(project, proof.get('path'))
            quote = proof.get('quote')
            if not isinstance(quote, str) or not quote.strip() or quote not in source.read_text(encoding='utf-8'):
                raise ValueError('Release-plan mapping quote is absent from its source')
        if layout(project):
            delivery_selection(project, keys, quarter.name)
        reasons, endings, used = [], [], []
        for key in keys:
            root = feature_root(project, key)
            if root.resolve() != root.absolute() or not root.resolve().is_relative_to(project):
                raise ValueError('Delivery source outside project or linked')
            tasks = overlay.load_tasks(root, project)
            active = {task_id: task for task_id, task in tasks.items()
                      if task.kind == 'real' and task.suit != 'bug'
                      and task.status.lower() not in overlay.EXCLUDED_STATUSES}
            members = {task_id: task for task_id, task in active.items()
                       if (task.release_id or task.release_label.split(' · ')[0]) == row['release']}
            if not members:
                reasons.append(key + ': no registered work for this release')
            if any(not task.release_id and task.release_label.split(' · ')[0] in {'', 'релиз не уточнён'}
                   for task in active.values() if overlay.role_for_task(task) in {'BE', 'FE', 'QA'}):
                reasons.append(key + ': release membership is incomplete')
            developers = {task_id for task_id, task in members.items() if overlay.role_for_task(task) in {'BE', 'FE'}}
            qa = [task for task in members.values() if overlay.role_for_task(task) == 'QA']
            covered = {task_id for task in qa for task_id in task.qa_members}
            if not developers or not qa or covered != developers:
                reasons.append(key + ': release development and QA coverage are incomplete')
            for task_id, task in members.items():
                finish, bound = end_from_gantt(task, expanded, overlay)
                used.append({'delivery': key, 'task_id': task_id,
                             'finish': finish.isoformat() if finish else None, 'completion_bound': bound})
                if finish is None:
                    reasons.append(key + '/' + task_id + ': no proven Gantt finish')
                else:
                    endings.append((finish, bound))
        forecast = UNKNOWN
        if endings and not reasons:
            latest = max(finish for finish, _ in endings)
            bounded = any(bound for finish, bound in endings if finish == latest)
            forecast = ('Не позднее ' if bounded else '') + latest.strftime('%d.%m.%Y')
        results.append({**row, 'expected_forecast': forecast, 'reasons': reasons, 'tasks': used})
    return bindings, results
