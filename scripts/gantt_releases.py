"""Release labels from persistent, reviewed analytics; never query tracker runtime."""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path


def label(value):
    if not isinstance(value, str) or not value.strip() or any(c in value for c in '\n\r[]|'):
        raise ValueError('Release identity must be nonempty plain single-line text')
    return value.strip()


def apply_task_releases(tasks, groups):
    """A validated QA partition supplies membership, including an unknown remainder."""
    for task in tasks.values():
        task.production_confirmed = False
        if task.kind == 'real':
            task.release_label = 'релиз не уточнён'
    for group in groups:
        release = group.get('release')
        text = label(release) if release is not None else 'релиз не уточнён'
        for task_id in [group['task_id'], *group['members']]:
            tasks[task_id].release_label = text


def milestone_overrides(gantt_dir):
    """Explicit alias mappings rename existing milestones; dates stay authoritative here."""
    path = Path(gantt_dir) / 'releases.json'
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if data.get('schema_version') != 1 or not isinstance(data.get('releases'), list):
        raise ValueError(f'{path}: invalid release milestone registry')
    result = {}
    for entry in data['releases']:
        alias, key = entry['alias'], label(entry['release'])
        if not re.fullmatch(r'[A-Za-z0-9_]+', alias) or alias in result:
            raise ValueError(f'{path}: duplicate or invalid release alias')
        if entry.get('analyst_confirmed') is not True or not str(entry.get('evidence', '')).strip():
            raise ValueError(f'{path}: release milestone requires analyst confirmation and evidence')
        day = date.fromisoformat(entry['date']).strftime('%Y/%m/%d')
        state = entry['state']
        if state == 'planned':
            text = f'Релиз {key} · план'
        elif state == 'deployed':
            text = f"Релиз {key} · {label(entry['environment'])} · факт"
        else:
            raise ValueError(f'{path}: state must be planned or deployed')
        result[alias] = (text, day)
    return result


def rename_milestones(text, overrides, seen):
    pattern = re.compile(r'^\[[^\]\n]+\] as \[([^\]\n]+)\] happens at (\d{4}/\d{2}/\d{2})\s*$', re.M)
    def replace(match):
        alias = match[1]
        if alias not in overrides:
            return match[0]
        if alias in seen:
            raise ValueError('Duplicate release milestone: ' + alias)
        seen.add(alias)
        name, day = overrides[alias]
        if day != match[2]:
            raise ValueError('Release milestone date differs from confirmed registry: ' + alias)
        return f'[{name}] as [{alias}] happens at {day}'
    return pattern.sub(replace, text)


MARKER_LEGEND = ('QA ✓ — тестирование задачи подтверждено; ПРОМ ✓ — внедрение подтверждено.\n'
                 'Нет отметки — нет подтверждения, а не отрицательный результат.')


def apply_task_markers(tasks, feature_dir):
    """Read reviewed task facts, bound to the exact execution sources."""
    import hashlib
    root = Path(feature_dir)
    path = root / 'execution/task-markers.json'
    if not path.exists():
        return
    data = json.loads(path.read_text())
    sources = [p for p in [root / 'execution/tasks.md', root / 'execution/qa-groups.json'] if p.exists()]
    sources += sorted(root.glob('slices/*/execution/tasks.md'))
    expected = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    if data.get('schema_version') != 1 or not expected or data.get('sources') != expected:
        raise ValueError(f'{path}: task marker sources changed; review evidence before generation')
    entries = data.get('tasks')
    if not isinstance(entries, list):
        raise ValueError(f'{path}: tasks must be a list')
    seen = set()
    for entry in entries:
        task_id = entry.get('task_id')
        if task_id not in tasks or task_id in seen or tasks[task_id].kind != 'real':
            raise ValueError(f'{path}: unknown, duplicate or non-real task')
        seen.add(task_id)
        task = tasks[task_id]
        release = entry.get('release')
        if release is not None:
            release = label(release)
            if task.release_label not in ('релиз не уточнён', release):
                raise ValueError(f'{path}: release conflicts with QA membership')
            proof = entry.get('membership')
            validate_marker_evidence(proof, path)
            task.release_label = release
        for key, caption in [('qa', 'QA ✓'), ('production', 'ПРОМ ✓')]:
            proof = entry.get(key)
            if proof is None:
                continue
            validate_marker_evidence(proof, path)
            if not isinstance(proof.get('date'), str):
                raise ValueError(f'{path}: marker requires a completion date')
            date.fromisoformat(proof['date'])
            if key == 'production':
                if not release or proof.get('analyst_confirmed') is not True:
                    raise ValueError(f'{path}: production requires exact release membership and analyst confirmation')
            task.release_label += ' · ' + caption
            if key == 'production':
                task.production_confirmed = True


def validate_marker_evidence(proof, path):
    if (not isinstance(proof, dict) or proof.get('confirmed') is not True
            or not isinstance(proof.get('source'), str) or not proof['source'].strip()
            or not isinstance(proof.get('evidence'), str) or not proof['evidence'].strip()):
        raise ValueError(f'{path}: marker requires confirmed source and evidence')


def apply_registry_markers(tasks, groups, project_root=None):
    """Render facts already accepted in execution and deployment registries."""
    def completed(task):
        return (task.status.lower() in {'completed', 'done', 'closed'}
                and task.progress == 100 and bool(task.actual_finish))

    def mark(task_id, caption):
        task = tasks[task_id]
        if caption == 'ПРОМ ✓':
            task.production_confirmed = True
        if caption not in task.release_label.split(' · '):
            task.release_label += ' · ' + caption

    for group in groups:
        ids = [group['task_id'], *group['members']]
        if all(completed(tasks[task_id]) for task_id in ids):
            for task_id in ids:
                mark(task_id, 'QA ✓')
    if project_root is None:
        return
    from baseline_releases import _load, _eligible, digest
    releases = _load(Path(project_root))['releases']
    for release_id, entry in releases.items():
        deployment = entry.get('deployment', {})
        observation = entry.get('observation', {})
        if (entry.get('status') != 'promoted' or deployment.get('environment', '').strip().upper() != 'ПРОМ'
                or not deployment.get('evidence') or not deployment.get('version')):
            continue
        if (observation.get('release_id') != release_id or not _eligible(observation)
                or digest(observation) != entry.get('scope_hash')):
            raise ValueError('Promoted release membership is inconsistent: ' + release_id)
        keys = {task['id'] for task in observation['tasks']}
        for group in groups:
            if group.get('release') != release_id:
                continue
            for task_id in group['members']:
                if tasks[task_id].tracker_key in keys:
                    mark(task_id, 'ПРОМ ✓')
            if all(tasks[task_id].tracker_key in keys for task_id in group['members']):
                mark(group['task_id'], 'ПРОМ ✓')
        for task in tasks.values():
            if task.kind == 'real' and task.tracker_key in keys:
                # Exact tracker membership, including AN outside a QA partition.
                current = task.release_label.split(' · ')[0]
                if current == release_id:
                    mark(task.task_id, 'ПРОМ ✓')
