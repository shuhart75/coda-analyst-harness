"""Exact-file repair grants for predecessor or unassigned files, without rewriting plans."""
from copy import deepcopy
from pathlib import Path, PurePosixPath

import operator_questions as oq
from workspace import git, _allowed, _forbidden, _safe_path, WorkspaceError

APPROVE = 'Разрешить исправление этих файлов'
DENY = 'Оставить область без изменений'


def ancestors(plan, task_id):
    tasks = {t['id']: t for t in plan['tasks']}
    result, pending = set(), list(tasks[task_id]['depends_on'])
    while pending:
        key = pending.pop()
        if key not in result:
            result.add(key)
            pending.extend(tasks[key]['depends_on'])
    return result


def blob(root, ref, path):
    row = git(root, 'ls-tree', ref, '--', path).split()
    if len(row) < 4 or row[0] not in ('100644', '100755') or row[1] != 'blob':
        raise ValueError('Repair requires an existing tracked regular file: ' + path)
    return row[2]


def validate_owners(runner, task, path):
    from controller import read
    plan = read(runner.root/'plan.json')
    prior = ancestors(plan, task['id'])
    owners, downstream = [], []
    for other in plan['tasks']:
        if other['id'] == task['id'] or not _allowed(path, other['writes']):
            continue
        if other['id'] in prior and runner.state['tasks'].get(other['id'], {}).get('status') == 'done':
            owners.append(other['id'])
        elif task['id'] not in ancestors(plan, other['id']):
            raise ValueError('Repair overlaps an unfinished or independent owner: ' + other['id'])
        else:
            downstream.append(other['id'])
    if not owners and downstream:
        raise ValueError('No accepted predecessor owns the requested file: ' + path)
    return sorted(owners)


def is_request(kind, request):
    return kind == 'scope_request' or (kind == 'operator_question' and
        request.get('topic') in ('task-scope-expansion', 'scope-repair', 'write-scope') and isinstance(request.get('patch'), dict))


def prepare(runner, task, value, request):
    paths = request.get('paths')
    if paths is None and isinstance(request.get('patch'), dict):
        paths = [request['patch'].get('file')]
    if not isinstance(paths, list) or not 1 <= len(paths) <= 8:
        raise ValueError('Request one to eight exact existing files, not a directory')
    work = Path(value['workspace'])
    files = []
    for raw in paths:
        if not isinstance(raw, str) or _forbidden(raw) or any(c in raw for c in '*?[]:\x00\n\r') or str(PurePosixPath(raw)) != raw:
            raise ValueError('Invalid or protected repair path')
        matches = set()
        for path in (raw, (PurePosixPath(task['cwd']) / raw).as_posix()):
            if _forbidden(path):
                continue
            try:
                target = _safe_path(work, path)
            except WorkspaceError as exc:
                raise ValueError(str(exc)) from exc
            cursor, linked = target, False
            while cursor != work.resolve():
                linked |= cursor.is_symlink()
                cursor = cursor.parent
            if target.is_file() and not linked:
                matches.add(path)
        if len(matches) != 1:
            raise ValueError('Repair path is missing or ambiguous: ' + raw)
        path = matches.pop()
        if _allowed(path, task['writes']):
            raise ValueError('File already belongs to this task: ' + path)
        expected = blob(work, value['base'], path)
        if blob(runner.main, 'HEAD', path) != expected:
            raise ValueError('Repair file differs from the candidate base: ' + path)
        files.append({'path': path, 'blob': expected, 'owners': validate_owners(runner, task, path)})
    files = sorted({item['path']: item for item in files}.values(), key=lambda item: item['path'])
    action = {'kind': 'scope-repair-v1', 'task': task['id'], 'base': value['base'], 'files': files}
    return oq.request(runner.root, task['id'], 'scope-repair-' + oq.fingerprint(action)[:24],
        'Разрешить исправление перечисленных файлов за пределами текущей задачи?',
        options=[APPROVE, DENY], details={'reason': request.get('diagnosis') or request.get('reason') or request.get('question'),
            'reported_context': request.get('details'),
            'verification': 'После ремонта обязательны все проверки задачи, независимое ревью и итоговая проверка.'},
        model=value['model'], action=action)


def effective_task(runner, task, record, *, integration=False):
    from controller import Blocked
    effective = deepcopy(task)
    grants = []
    known = {q['id']: q for q in oq.questions(runner.root)}
    ids = record.get('scope_grants', []) if integration else [key for key, q in known.items()
        if q['task'] == task['id'] and q.get('action', {}).get('kind') == 'scope-repair-v1'
        and not q.get('resolved') and q.get('answer') and q['answer']['text'] == APPROVE]
    for key in ids:
        q = known.get(key)
        if not q or q.get('resolved') or not q.get('answer') or q['answer']['text'] != APPROVE:
            raise Blocked('Repair approval is missing or stale')
        action = q.get('action', {})
        if (q['answer'].get('action_sha256') != oq.fingerprint(action)
                or action.get('kind') != 'scope-repair-v1' or action.get('task') != task['id']
                or action.get('base') != record['base']):
            raise Blocked('Repair approval does not match the task/base')
        for item in action['files']:
            path = item['path']
            if _forbidden(path) or validate_owners(runner, task, path) != item['owners']:
                raise Blocked('Repair ownership changed')
            if blob(record['workspace'], record['base'], path) != item['blob']:
                raise Blocked('Repair base file changed')
            reference = record.get('integration_base') or 'HEAD'
            if blob(runner.main, reference, path) != item['blob']:
                raise Blocked('Repair target changed before integration')
            if path not in effective['writes']:
                effective['writes'].append(path)
        grants.append({'question_id': key, 'files': action['files']})
    if grants:
        effective['approved_repairs'] = grants
        effective['prompt'] += ('\nController-approved exact-file repair: change only the explicitly added files '
            'as needed for the reported defect. Do not weaken assertions, requirements or check commands. '
            'Re-run all task checks and obtain independent review; prior task acceptance is not proof of this repair.')
    if not integration:
        record['scope_grants'] = [g['question_id'] for g in grants]
    return effective


def refresh_pending(runner, task, value):
    """Replace an obsolete unsupported-scope question, never reinterpret an answer."""
    from controller import save
    import time
    if value.get('status') != 'waiting-user':
        return False
    known = {q['id']: q for q in oq.questions(runner.root)}
    old = known.get(value.get('question_id'))
    replacement = (old or {}).get('resolved') or {}
    if replacement.get('replacement') in known:
        value['question_id'] = replacement['replacement']
        return True
    if old and (old.get('answer') or old.get('resolved')):
        return False
    if not old or not old['topic'].startswith('scope-unavailable-'):
        return False
    for variant in old['requests']:
        details = variant.get('details')
        if not isinstance(details, dict) or not str(details.get('reason', '')).startswith('No accepted predecessor owns'):
            continue
        request = details.get('model_request')
        if not isinstance(request, dict):
            continue
        try:
            key = prepare(runner, task, value, request)
        except ValueError:
            return False
        with oq.locked(runner.root) as folder:
            if (folder/(old['id'] + '-answer.json')).exists():
                save(folder/(key + '-resolved.json'), {'reason': 'Prior question answered during migration', 'resolved_at': time.time()})
                return False
            save(folder/(old['id'] + '-resolved.json'), {'reason': 'Replaced by exact-file approval question',
                 'replacement': key, 'resolved_at': time.time()})
            value['question_id'] = key
        return True
    return False


def needs_refresh(root, question_ids):
    """Wake a saved waiting portfolio once on resume to migrate its question."""
    for q in oq.questions(root):
        if q['id'] not in question_ids or q.get('answer'):
            continue
        if (q.get('resolved') or {}).get('replacement'):
            return True
        if q.get('resolved') or not q['topic'].startswith('scope-unavailable-'):
            continue
        if any(isinstance(r.get('details'), dict) and
               str(r['details'].get('reason', '')).startswith('No accepted predecessor owns')
               for r in q['requests']):
            return True
    return False
