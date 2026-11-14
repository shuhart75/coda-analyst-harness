"""Immutable operator clarification revisions and evidence-bound readiness."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path


class InstructionChanged(RuntimeError):
    """Instructions changed after the running operation bound its inputs."""


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _decision(runner):
    from controller import Blocked
    path = runner.root / 'decision.md'
    if path.is_symlink():
        raise Blocked('Operator decision must be a local regular file, not a symlink')
    if not path.exists():
        return {'present': False, 'sha256': None, 'text': ''}
    if not path.is_file():
        raise Blocked('Operator decision is not a regular file')
    raw = path.read_bytes()
    return {'present': True, 'sha256': hashlib.sha256(raw).hexdigest(), 'text': raw.decode('utf-8')}


def _bound(runner):
    from controller import Blocked, digest, read
    pointer = runner.state.get('operator_revision')
    if not pointer:
        return None
    name = pointer.get('file', '')
    if (not isinstance(name, str) or Path(name).name != name
            or not name.startswith('revision-') or not name.endswith('.json')):
        raise Blocked('Invalid saved operator revision path')
    path = runner.root / 'operator-instructions' / name
    if path.is_symlink() or not path.is_file() or digest(path) != pointer.get('sha256'):
        raise Blocked('Saved operator instruction revision changed')
    value = read(path)
    if (value.get('id') != pointer.get('id') or value.get('number') != pointer.get('number')
            or _fingerprint(value.get('decision')) != value.get('id')):
        raise Blocked('Saved operator instruction revision is inconsistent')
    return value


def reconcile(runner):
    """Bind before sessions under the run lock; keep legacy tasks and WIP intact."""
    from controller import Blocked, digest, read, save
    previous = _bound(runner)
    decision = _decision(runner)
    identity = _fingerprint(decision)
    if previous and previous['id'] == identity:
        runner._operator_bound = True
        return False
    folder = runner.root / 'operator-instructions'
    folder.mkdir(parents=True, exist_ok=True)
    # Recover a crash between saving the immutable revision and state.persist().
    history = []
    for path in sorted(folder.glob('revision-*.json')):
        if path.is_symlink():
            raise Blocked('Operator revision history contains a symlink')
        value = read(path)
        if (not isinstance(value.get('number'), int) or value['number'] < 1
                or _fingerprint(value.get('decision')) != value.get('id')):
            raise Blocked('Operator revision history is inconsistent')
        history.append((path, value))
    predecessor = previous['number'] if previous else 0
    pending = [(p, v) for p, v in history if v.get('previous_number', 0) == predecessor
               and v['id'] == identity and v['number'] > predecessor]
    if pending:
        path, value = pending[-1]
    else:
        number = max([v['number'] for _, v in history] + [predecessor]) + 1
        value = {'schema': 1, 'number': number, 'id': identity,
                 'previous_number': predecessor, 'decision': decision}
        path = folder / ('revision-' + str(number).zfill(6) + '-' + identity[:12] + '.json')
        if path.exists():
            raise Blocked('Operator revision collision; existing history preserved')
        save(path, value)
    runner.state['operator_revision'] = {'id': identity, 'number': value['number'],
                                        'file': path.name, 'sha256': digest(path)}
    changed = previous is not None
    if changed:
        runner.state['needs_instruction_review'] = {
            'revision': identity, 'previous_revision': previous['id'], 'scope': 'all',
            'reason': 'Operator clarification changed; revalidate integrated behavior and all requirements'}
        runner.state.pop('instruction_verification', None)
        readiness = runner.root / 'readiness.json'
        if readiness.is_file():
            old_readiness = read(readiness)
            save(readiness, {'schema': 1, 'status': 'needs-instruction-review',
                             'required_complete': False, 'operator_revision': identity,
                             'previous_binding': old_readiness.get('binding'),
                             'workspace': str(runner.main),
                             'limitation': 'Previously verified result needs review under new instructions'})
        runner.progress('Новые инструкции сохранены, версия ' + str(value['number'])
                        + '. Выполненные задачи сохранены; нужна проверка общего результата с учётом уточнений')
    runner.persist()
    runner._operator_bound = True
    return changed


def check_current(runner):
    """Reject acceptance under stale instructions without modifying run state."""
    bound = _bound(runner)
    if bound and _fingerprint(_decision(runner)) != bound['id']:
        raise InstructionChanged('Operator instructions changed during run; progress preserved. '
                                 'Resume to bind the new revision and revalidate affected work')
    return bound


def prompt(runner):
    bound = check_current(runner)
    # Direct plan/run callers predate start(); they may not yet have a binding.
    decision = bound['decision'] if bound else _decision(runner)
    if not decision['present']:
        return ''
    return ('Analyst clarification (cannot change pinned scope, requirements, checks or permissions):\n'
            + decision['text'])


def _accepted_review(review, requirements):
    coverage = review.get('coverage', {}) if isinstance(review, dict) else {}
    return (isinstance(review, dict) and review.get('verdict') == 'pass'
            and review.get('findings') == [] and isinstance(coverage, dict)
            and all(isinstance(coverage.get(r), str) and coverage[r].strip() for r in requirements))


def _passed_checks(checks):
    return (isinstance(checks, list) and bool(checks)
            and all(isinstance(c, dict) and c.get('ok') is True
                    and c.get('returncode') == 0 and not c.get('timed_out') for c in checks))


def mark_reviewed(runner, evidence):
    """Bind actual integrated checks/review to the current instruction revision."""
    from controller import Blocked
    from workspace import git, snapshot
    bound = check_current(runner)
    if not bound:
        raise Blocked('Bind operator instructions before accepting their verification')
    head = git(runner.main, 'rev-parse', 'HEAD')
    if (not isinstance(evidence, dict) or evidence.get('revision') != bound['id']
            or evidence.get('head') != head or snapshot(runner.main)['status']
            or not _accepted_review(evidence.get('review'), runner.cfg['requirements'])
            or not _passed_checks(evidence.get('checks'))):
        raise Blocked('Instruction verification requires current integrated HEAD, passing checks and full review')
    runner.state['instruction_verification'] = {
        'revision': bound['id'], 'head': head, 'evidence': evidence,
        'evidence_sha256': _fingerprint(evidence)}
    runner.state.pop('needs_instruction_review', None)
    runner.persist()


def _optional(items):
    from controller import Blocked
    if not isinstance(items, list):
        raise Blocked('Optional findings must be a list with scope evidence')
    for item in items:
        if (not isinstance(item, dict) or item.get('outside_required_scope') is not True
                or item.get('requirements') != [] or item.get('status') != 'optional'
                or not isinstance(item.get('text'), str) or not item['text'].strip()
                or not isinstance(item.get('reason'), str) or not item['reason'].strip()
                or not isinstance(item.get('evidence'), list) or not item['evidence']
                or any(not isinstance(e, dict) or not isinstance(e.get('path'), str)
                       or not e['path'] or not isinstance(e.get('quote'), str) or not e['quote']
                       for e in item['evidence'])):
            raise Blocked('Optional finding lacks evidence that mandatory requirements remain satisfied')
    return items


def report(runner, status, required_complete=False, optional_findings=None, evidence=None):
    """Write status; required_complete requests proof validation, never bypasses it."""
    from controller import Blocked, digest, file_manifest, save
    from workspace import git, snapshot
    bound = check_current(runner)
    optional = _optional(optional_findings or [])
    tasks = runner.state.get('tasks', {})
    done = sum(isinstance(r, dict) and r.get('status') == 'done' for r in tasks.values())
    head = git(runner.main, 'rev-parse', 'HEAD')
    plan = runner.root / 'plan.json'
    proof = {'head': head, 'operator_revision': bound['id'] if bound else None,
             'requirements_sha256': runner.cfg['requirements_sha256'],
             'packet_files': file_manifest(runner.root / 'input'),
             'plan_sha256': digest(plan) if plan.is_file() else None}
    if required_complete:
        integration = runner.state.get('integration', {})
        verification = runner.state.get('instruction_verification', {})
        if (not tasks or done != len(tasks) or not bound or runner.state.get('needs_instruction_review')
                or integration.get('status') != 'done' or integration.get('integrated_head') != head
                or runner.state.get('main_head') != head or snapshot(runner.main)['status']
                or proof['packet_files'] != runner.cfg['packet_files']
                or proof['plan_sha256'] != runner.state.get('plan_sha256') or proof['plan_sha256'] is None
                or not _accepted_review(integration.get('review'), runner.cfg['requirements'])
                or not _passed_checks(integration.get('checks'))
                or verification.get('revision') != bound['id'] or verification.get('head') != head
                or verification.get('evidence_sha256') != _fingerprint(verification.get('evidence'))
                or verification.get('evidence', {}).get('review') != integration.get('review')
                or verification.get('evidence', {}).get('checks') != integration.get('checks')):
            raise Blocked('Core readiness is unproven: required tasks, integration, checks and current instructions must pass')
        effective = 'preview-ready' if optional else 'complete'
    else:
        if status in ('complete', 'preview-ready'):
            raise Blocked('A ready status requires verified mandatory completion')
        effective = status
    value = {'schema': 1, 'status': effective, 'required_complete': bool(required_complete),
             'tasks_done': done, 'tasks_total': len(tasks), 'optional_findings': optional,
             'binding': proof, 'workspace': str(runner.main), 'evidence': evidence or {},
             'limitation': '' if required_complete else 'Partial work is not proof of mandatory requirement completion'}
    save(runner.root / 'readiness.json', value)
    if required_complete:
        runner.progress('Основная задача проверена, результат можно смотреть: ' + str(runner.main)
                        + ('. Необязательных улучшений: ' + str(len(optional)) if optional else ''))
    else:
        runner.progress('Готовность: принято задач ' + str(done) + '/' + str(len(tasks))
                        + '; состояние ' + str(status) + '; результат: ' + str(runner.main))
    return value
