from __future__ import annotations

from datetime import date
import hashlib
import json
from decimal import Decimal, InvalidOperation
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
                       'completed-by': ('Completed By',), 'keep-current': ()}
        reviewed_fields = {'Actual Start', 'Actual Finish', 'Completed By', 'Status', 'Progress %', 'Estimate', 'Estimate (дн)'}
        if kind == 'reviewed-fields':
            valid_fields = bool(confirmation['fields']) and set(confirmation['fields']) <= reviewed_fields
        else:
            valid_fields = kind in field_names and set(confirmation['fields']) == set(field_names[kind])
        if not valid_fields:
            raise ValueError('Confirmation kind and date fields do not match')
        fields = confirmation['fields']
        for name, value in fields.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError('QA fields require explicit nonempty registry strings')
            if value == '-' and kind == 'reviewed-fields' and name in {'Actual Start', 'Actual Finish', 'Completed By'}:
                continue
            if value == 'unknown' and kind == 'reviewed-fields' and name == 'Progress %':
                continue
            if name in {'Actual Start', 'Actual Finish', 'Completed By'}:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError('QA dates require YYYY-MM-DD')
            elif name in {'Progress %', 'Estimate', 'Estimate (дн)'}:
                try:
                    number = Decimal(value)
                except InvalidOperation as error:
                    raise ValueError('QA estimate/progress must be numeric') from error
                if not number.is_finite() or number < 0 or (name == 'Progress %' and number > 100):
                    raise ValueError('QA estimate/progress is outside its allowed range')
            elif name == 'Status' and value not in {'not-started', 'in-progress', 'completed', 'done', 'cancelled'}:
                raise ValueError('Unsupported QA status')
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
                                                     ('Actual Start', 'Actual Finish', 'Completed By', 'Status', 'Progress %', 'Estimate', 'Estimate (дн)')},
                        'previous': target['current'],
                        'confirmation': confirmation, 'verification_required': True,
                        'expected_rendering': 'actual-interval' if start not in (None, '', '-', '—') and finish not in (None, '', '-', '—') else 'no-exact-interval'})
    return updates


def check_qa_application(args) -> int:
    from tracker_workflow import load_json, digest_object, run_root, verified_result
    from tracker_registry import read_registry
    from tracker_execution import git

    path = Path(args.review_file).resolve()
    review = load_json(path)
    expected = (run_root(review['run_id']) / 'history' / (digest_object(review) + '.json')).resolve()
    if path != expected:
        raise ValueError('Expected an unchanged saved history review')
    completion, _ = verified_result(review['run_id'])
    if not completion['planning_application_allowed']:
        raise ValueError('Tracker application is paused or was not requested')
    if review.get('qa_application_blockers'):
        raise ValueError('Resolve QA partition blockers before application')
    project = Path(args.project_root).resolve()
    if str(project) != review.get('project_root') or git(project, 'rev-parse', 'HEAD').strip() != review['head']:
        raise ValueError('Project or HEAD differs from the reviewed application')
    updates = review.get('qa_application', [])
    if not updates:
        raise ValueError('No explicit QA confirmations in this review')
    required = {(row['feature'], row['task_id']) for row in review.get('comparison', {}).get('rows', []) if row['role'] == 'QA'}
    if required - {(update['feature'], update['task_id']) for update in updates}:
        raise ValueError('Every feature QA requires a reviewed application or keep-current decision')
    errors = []
    for proposal in review.get('feature_qa_proposals', []):
        partition = proposal.get('partition')
        if partition and partition.get('ready'):
            group_path = (project / partition['path']).resolve()
            if not group_path.is_relative_to(project):
                raise ValueError('QA partition is outside project')
            if not group_path.is_file() or load_json(group_path) != partition['document']:
                errors.append({'feature': proposal['feature'], 'reason': 'QA partition not applied as reviewed'})
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
        for proposal in review.get('feature_qa_proposals', []):
            if proposal['feature'] != update['feature']:
                continue
            for group in proposal.get('partition', {}).get('groups', []):
                if group['task_id'] == update['task_id']:
                    actual = matches[0].get('Estimate (дн)', matches[0].get('Estimate', ''))
                    try:
                        equal = Decimal(actual.replace(',', '.')) == Decimal(group['estimate'])
                    except InvalidOperation:
                        equal = False
                    if not equal:
                        errors.append({'task_id': update['task_id'], 'reason': 'QA partition estimate differs'})
    print(json.dumps({'status': 'qa-application-mismatch' if errors else 'qa-application-verified',
                      'errors': errors, 'writes_performed': False,
                      'gantt_verified': False, 'next_action': 'fix-registry' if errors else 'generate-actual-only-and-check-gantt'}, ensure_ascii=False, indent=2))
    return 2 if errors else 0
