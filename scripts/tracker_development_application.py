from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
from pathlib import Path


FIELDS = ('Actual Start', 'Actual Finish', 'Completed By', 'Status', 'Progress %')
EMPTY = {'', '-', '—', 'unknown'}
STATUSES = {'planned': 'not-started', 'not_started': 'not-started',
            'in_progress': 'in-progress', 'done': 'completed', 'canceled': 'cancelled'}


def verify_decision(decision: dict) -> None:
    if decision.get('analyst_confirmed') is not True:
        raise ValueError('Development application requires an explicit analyst decision')
    source = decision['source']
    raw = Path(source['file']).read_bytes()
    if (hashlib.sha256(raw).hexdigest() != source['sha256'] or not source.get('quote', '').strip()
            or source['quote'] not in raw.decode('utf-8')):
        raise ValueError('Invalid development decision evidence')


def development_updates(comparison: dict, decision: dict | None) -> list[dict]:
    if decision is None:
        return []
    verify_decision(decision)
    kind = decision['kind']
    if kind not in {'accept-dates', 'accept-dates-and-statuses', 'keep-current', 'custom'}:
        raise ValueError('Unsupported development decision')
    rows = [row for row in comparison['rows'] if row['role'] in {'BE', 'FE'}]
    deletions = set()
    for item in decision.get('deletions', []):
        identity = (item['feature'], item['task_id'])
        if (kind == 'keep-current' or identity in deletions or not any(
                (row['feature'], row['task_id']) == identity
                and row.get('proposed_action') == 'delete-current-execution' for row in rows)):
            raise ValueError('Development deletion requires a unique reviewed deletion target')
        deletions.add(identity)
    overrides = {}
    for item in decision.get('overrides', []):
        identity = (item['feature'], item['task_id'])
        if identity in overrides or identity in deletions or not any((row['feature'], row['task_id']) == identity for row in rows):
            raise ValueError('Development override has a duplicate or out-of-scope task')
        fields = item['fields']
        if not fields or set(fields) - set(FIELDS) or any(not isinstance(value, str) for value in fields.values()):
            raise ValueError('Development override requires factual registry fields')
        for field, value in fields.items():
            if value in EMPTY:
                continue
            if field in {'Actual Start', 'Actual Finish', 'Completed By'}:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError('Development dates require YYYY-MM-DD')
            elif field == 'Status' and STATUSES.get(value, value) not in {'not-started', 'in-progress', 'completed', 'cancelled'}:
                raise ValueError('Unsupported development status')
            elif field == 'Progress %':
                try:
                    number = Decimal(value.replace(',', '.'))
                except InvalidOperation as error:
                    raise ValueError('Invalid development progress') from error
                if not number.is_finite() or not 0 <= number <= 100:
                    raise ValueError('Invalid development progress')
        overrides[identity] = fields
    if kind == 'custom' and set(overrides) | deletions != {(row['feature'], row['task_id']) for row in rows}:
        raise ValueError('Custom development decision must cover every shown BE/FE row')
    updates = []
    for row in rows:
        if kind == 'keep-current' and row.get('registration_required'):
            raise ValueError('Cannot keep a development row that has not been registered')
        if (row['feature'], row['task_id']) in deletions:
            updates.append({'feature': row['feature'], 'task_id': row['task_id'], 'role': row['role'],
                            'registry': row['registry'], 'action': 'delete-current-execution'})
            continue
        expected = {field: row['current'].get(field, '') for field in FIELDS}
        history = row.get('history')
        if kind in {'accept-dates', 'accept-dates-and-statuses'}:
            if not history:
                raise ValueError('Development history unavailable; record keep-current or a custom decision')
            for field, key in (('Actual Start', 'started_at'), ('Actual Finish', 'finished_at')):
                if history.get(key):
                    expected[field] = datetime.fromisoformat(history[key]).date().isoformat()
            if kind == 'accept-dates-and-statuses':
                expected['Status'] = history['state']
                progress = history.get('progress_percent')
                expected['Progress %'] = 'unknown' if progress is None else str(progress)
        expected.update(overrides.get((row['feature'], row['task_id']), {}))
        updates.append({'feature': row['feature'], 'task_id': row['task_id'], 'role': row['role'],
                        'registry': row['registry'], 'expected_registry_fields': expected})
    return updates


def equivalent(field: str, actual: str, expected: str) -> bool:
    actual, expected = actual.strip(), expected.strip()
    if actual in EMPTY or expected in EMPTY:
        return actual in EMPTY and expected in EMPTY
    if field == 'Status':
        return STATUSES.get(actual, actual) == STATUSES.get(expected, expected)
    if field == 'Progress %':
        try:
            return Decimal(actual.replace(',', '.')) == Decimal(expected.replace(',', '.'))
        except InvalidOperation:
            return False
    return actual == expected


def check_development(project: Path, review: dict) -> list[dict]:
    from tracker_registry import read_registry

    comparison = review.get('comparison') or {'rows': []}
    rows = [row for row in comparison['rows'] if row['role'] in {'BE', 'FE'}]
    if not rows:
        return []
    decision = review.get('development_decision')
    if not decision:
        raise ValueError('Record development_decision in a new history-review of the same run before application verification')
    expected = development_updates(comparison, decision)
    if expected != review.get('development_application'):
        raise ValueError('Development application differs from the reviewed decision')
    errors = []
    for update in expected:
        path = (project / update['registry']).resolve()
        if not path.is_relative_to(project):
            raise ValueError('Development registry is outside project')
        tables, _ = read_registry(path)
        matches = [row for table in tables for row in table
                   if (row.get('Task ID') or row.get('Jira') or '') == update['task_id']
                   and row.get('Role') == update['role']]
        if update.get('action') == 'delete-current-execution':
            if matches:
                errors.append({'task_id': update['task_id'], 'reason': 'Confirmed deletion not applied'})
            continue
        if len(matches) != 1:
            errors.append({'task_id': update['task_id'], 'reason': 'Development row missing or ambiguous'})
            continue
        for field, value in update['expected_registry_fields'].items():
            actual = matches[0].get(field, '')
            if not equivalent(field, actual, value):
                errors.append({'task_id': update['task_id'], 'registry': update['registry'],
                               'field': field, 'expected': value, 'actual': actual,
                               'reason': 'Development fact not applied as reviewed'})
    return errors


def check_generation(project: Path, quarter: str, review_files: list[str]) -> None:
    from contextlib import redirect_stdout
    from io import StringIO
    import json
    from types import SimpleNamespace
    import collaboration
    from tracker_workflow import state_root
    from tracker_qa_application import check_qa_application

    root = state_root().parent
    state = collaboration.load_state(root, required=False)
    scope = None
    if state and state.get('active_work'):
        analytics, _ = collaboration.analytics_repository(root)
        if analytics == project:
            work = collaboration.require_active_work(root, analytics, state)
            scope = work.get('execution_scope')
    required = set(scope.get('run_ids', [])) if scope else set()
    if required and quarter not in scope['quarters']:
        raise ValueError('Generation quarter is outside the registered execution scope')
    reviewed = set()
    for filename in review_files:
        review = json.loads(Path(filename).read_text())
        run_id = review['run_id']
        if run_id in reviewed or (scope and run_id not in required):
            raise ValueError('Generation review must match one registered tracker run')
        if scope and not {item['feature'] for item in review['features']} <= set(scope['features']):
            raise ValueError('Generation review is outside the registered delivery scope')
        output = StringIO()
        with redirect_stdout(output):
            result = check_qa_application(SimpleNamespace(project_root=str(project), review_file=filename))
        if result:
            raise ValueError('Execution application check failed before generation: ' + output.getvalue())
        reviewed.add(run_id)
    if required - reviewed:
        raise ValueError('Pass --review-file for every registered tracker run before generation')
