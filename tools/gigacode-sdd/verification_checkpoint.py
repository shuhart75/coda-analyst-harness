"""Detach verification inputs from live writers, without omitting evidence files."""
from pathlib import Path
import time
import uuid


def prepare(runner, value, candidate_path, *, force=False):
    from controller import Blocked, digest, read, save
    from workspace import clone_snapshot, snapshot, snapshot_difference, _forbidden
    from snapshot_recovery import repair_instruction
    import operator_questions as questions

    if value.get('status') in ('accepted', 'quarantined', 'external-blocker'):
        return False
    # Old controller snapshot-repair advice could produce an unnecessary
    # operator question. Probe the already implemented candidate instead; never
    # execute any of the proposed service actions or fabricate an operator answer.
    old_question = (value.get('status') == 'waiting-user' and value.get('snapshot_recovery')
                    and value.get('feedback') == repair_instruction(value['snapshot_recovery'].get('previous_candidate', ''))
                    and not value.get('service_pending') and not value.get('blocked_on_evidence')
                    and value.get('checks') and all(c.get('ok') for c in value['checks']))
    if old_question:
        old_question = any(q['id'] == value.get('question_id')
                           for q in questions.questions(runner.root, pending_only=True))
    if value.get('status') == 'waiting-user' and not old_question:
        return False
    if not force and not old_question and value.get('stage') not in ('checks', 'review'):
        return False
    before = value.get('workspace_snapshot')
    if not before:
        return False
    source = Path(value['workspace'])
    current = snapshot(source, normalize_settings=True)
    if not force and not old_question and current == before:
        return False
    delta = snapshot_difference(before, current)
    if delta['head_changed'] or any(_forbidden(name) for name in delta['paths']):
        raise Blocked('Saved execution candidate changed: protected inputs before verification checkpoint')
    for name, expected in value.get('evidence_files', {}).items():
        path = Path(name)
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise Blocked('Saved controller evidence changed: ' + name)
    if source.is_symlink() or not source.resolve().is_relative_to((runner.root/'workers').resolve()):
        raise Blocked('Verification source is outside managed workers')
    root = runner.root/'workers'/('verification-' + uuid.uuid4().hex)
    copied = clone_snapshot(source, root, include_dirty=True, attempts=3, capture_appends=True)
    checkpoint = snapshot(root, normalize_settings=True)
    archive = candidate_path.with_name('before-verification-' + uuid.uuid4().hex + '.json')
    save(archive, value)
    report = candidate_path.with_name('verification-capture-' + uuid.uuid4().hex + '.json')
    save(report, {'source': str(source), 'workspace': str(root), 'captured_at': time.time(),
                  'clone': copied, 'snapshot': checkpoint, 'previous_candidate': str(archive),
                  'source_changes_since_checks': delta,
                  'rule': 'New finite verification input. No evidence files excluded; fresh checks and review required.'})
    value.setdefault('verification_sources', []).append(str(source))
    value.update(workspace=str(root), workspace_snapshot=checkpoint, stage='checks', status='pending',
                 checks=[], verification_checkpoint=str(report))
    # Preserve old results in the archive, never recover them against a new copy.
    for key in ('recovered_review', 'review_resolution', 'review', 'review_draft', 'pending_session'):
        value.pop(key, None)
    if old_question:
        value['verification_pending_question'] = value.pop('question_id', None)
    save(candidate_path, value)
    runner.progress('Подготовлена отдельная копия для проверок; исходные службы продолжают работу.')
    return True


def resolve_snapshot_question(runner, value):
    """Fresh checks prove the copy needs no proposed service/log-placement action.

    This is not task acceptance or an operator answer. Independent review remains
    mandatory. Ordinary permission/service questions never enter this path.
    """
    key = value.get('verification_pending_question')
    if (not key or value.get('stage') not in ('review', 'accepted')
            or not value.get('checks') or not all(c.get('ok') for c in value['checks'])):
        return
    import operator_questions as questions
    from controller import save
    for question in questions.questions(runner.root, pending_only=True):
        if question['id'] == key:
            with questions.locked(runner.root) as folder:
                save(folder/(key + '-resolved.json'), {'reason': 'Fresh checks on immutable checkpoint made snapshot repair unnecessary; review still required',
                    'verification_checkpoint': value['verification_checkpoint'], 'resolved_at': time.time(),
                    'operator_action_performed': False})
