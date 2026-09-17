from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path


def confirmed_qa_updates(comparison: dict, confirmations: list[dict]) -> list[dict]:
    updates, seen = [], set()
    for confirmation in confirmations:
        identity = (confirmation['feature'], confirmation['task_id'])
        if identity in seen:
            raise ValueError('Duplicate QA confirmation')
        seen.add(identity)
        targets = [row for row in comparison['rows'] if row['role'] == 'QA'
                   and (row['feature'], row['task_id']) == identity]
        if len(targets) != 1 or not targets[0]['registry']:
            raise ValueError('Confirmation requires one existing QA registry row')
        if confirmation.get('analyst_confirmed') is not True:
            raise ValueError('An explicit analyst confirmation is required')
        source = confirmation['source']
        raw = Path(source['file']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source['sha256'] or not source.get('quote', '').strip():
            raise ValueError('Invalid analyst confirmation evidence')
        if source['quote'] not in raw.decode('utf-8'):
            raise ValueError('Analyst quote not found in confirmation evidence')
        kind = confirmation['kind']
        field_names = {'exact-interval': ('Actual Start', 'Actual Finish'),
                       'exact-start': ('Actual Start',), 'exact-finish': ('Actual Finish',),
                       'completed-by': ('Completed By',)}
        if kind not in field_names or set(confirmation['fields']) != set(field_names[kind]):
            raise ValueError('Confirmation kind and date fields do not match')
        fields = confirmation['fields']
        for value in fields.values():
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError('QA dates require YYYY-MM-DD')
        target = targets[0]
        expected = {**target['current'], **fields}
        start, finish, bound = (expected.get(name) for name in ('Actual Start', 'Actual Finish', 'Completed By'))
        if start not in (None, '', '-', '—') and finish not in (None, '', '-', '—') and date.fromisoformat(start) > date.fromisoformat(finish):
            raise ValueError('QA finish precedes start')
        if bound not in (None, '', '-', '—') and finish not in (None, '', '-', '—') and date.fromisoformat(finish) > date.fromisoformat(bound):
            raise ValueError('Exact finish conflicts with the saved completion bound; review the bound')
        updates.append({'feature': identity[0], 'task_id': identity[1], 'registry': target['registry'],
                        'fields_to_write': fields,
                        'expected_registry_fields': {name: expected.get(name, '') for name in
                                                     ('Actual Start', 'Actual Finish', 'Completed By')},
                        'previous': target['current'],
                        'confirmation': confirmation, 'verification_required': True,
                        'expected_rendering': 'actual-interval' if start not in (None, '', '-', '—') and finish not in (None, '', '-', '—') else 'no-exact-interval'})
    return updates


def check_qa_application(args) -> int:
    from tracker_workflow import load_json, digest_object, run_root
    from tracker_registry import read_registry
    from tracker_execution import git

    path = Path(args.review_file).resolve()
    review = load_json(path)
    expected = (run_root(review['run_id']) / 'history' / (digest_object(review) + '.json')).resolve()
    if path != expected:
        raise ValueError('Expected an unchanged saved history review')
    project = Path(args.project_root).resolve()
    if str(project) != review.get('project_root') or git(project, 'rev-parse', 'HEAD').strip() != review['head']:
        raise ValueError('Project or HEAD differs from the reviewed application')
    updates = review.get('qa_application', [])
    if not updates:
        raise ValueError('No explicit QA confirmations in this review')
    errors = []
    for update in updates:
        registry = (project / update['registry']).resolve()
        if not registry.is_relative_to(project):
            raise ValueError('Registry is outside project')
        tables, _ = read_registry(registry)
        matches = [row for table in tables for row in table if row.get('Role', '').upper() == 'QA'
                   and (row.get('Task ID') or row.get('Jira') or '') == update['task_id']]
        if len(matches) != 1:
            errors.append({'task_id': update['task_id'], 'reason': 'QA row missing or ambiguous'})
            continue
        for field, value in update['expected_registry_fields'].items():
            if matches[0].get(field, '') != value:
                errors.append({'task_id': update['task_id'], 'field': field,
                               'expected': value, 'actual': matches[0].get(field)})
    print(json.dumps({'status': 'qa-application-mismatch' if errors else 'qa-application-verified',
                      'errors': errors, 'writes_performed': False,
                      'gantt_verified': False, 'next_action': 'fix-registry' if errors else 'generate-actual-only-and-check-gantt'}, ensure_ascii=False, indent=2))
    return 2 if errors else 0
