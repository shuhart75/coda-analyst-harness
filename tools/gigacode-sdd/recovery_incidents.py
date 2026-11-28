"""Durable controller recovery receipts, never an authority to accept work.

One file per scope/candidate/cause avoids shared read-modify-write between lanes.
Existing step budgets own retries. A receipt records an actual recovery decision
and its postcondition; observer prose cannot create executable actions.
"""
from datetime import datetime, timezone
import hashlib
import json
import functools
import logging
import os
from pathlib import Path
import re

ACTIVE = ('pending', 'running')
TERMINAL = ('verified', 'failed', 'exhausted', 'interrupted')
MAX_RECEIPT_BYTES = 131072
MAX_SCAN_ENTRIES = 64
MAX_METADATA_ENTRIES = 1024


def _best_effort(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            # The candidate/receipt guards and original retry budget, not this
            # diagnostic projection, authorize recovery and acceptance.
            logging.getLogger(__name__).warning('Recovery telemetry unavailable: %s', exc)
            return None
    return wrapped


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _path(root, identity):
    if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{24}', identity):
        raise ValueError('Invalid recovery incident identity')
    folder = Path(root)/'recovery'/'incidents'
    if folder.is_symlink() or folder.parent.is_symlink():
        raise ValueError('Recovery ledger must remain inside the managed run')
    path = folder/(identity+'.json')
    if path.is_symlink():
        raise ValueError('Recovery incident cannot be a symbolic link')
    return path


def _timestamp(value):
    if not isinstance(value, str) or not value or len(value) > 40:
        raise ValueError('Invalid recovery timestamp')
    if datetime.fromisoformat(value).tzinfo is None:
        raise ValueError('Recovery timestamp requires a timezone')


def _validate_receipt(value, identity):
    if (not isinstance(value, dict) or type(value.get('schema')) is not int
            or value['schema'] != 1 or value.get('id') != identity):
        raise ValueError('Invalid recovery incident receipt')
    for field in ('scope', 'task_id', 'stage', 'cause', 'action', 'reason'):
        if not isinstance(value.get(field), str):
            raise ValueError('Invalid recovery field: ' + field)
    if value.get('model') is not None and not isinstance(value['model'], str):
        raise ValueError('Invalid recovery model')
    for field in ('occurrence', 'evidence_sha256'):
        if not isinstance(value.get(field), str) or not re.fullmatch('[0-9a-f]{64}', value[field]):
            raise ValueError('Invalid recovery digest: ' + field)
    for field in ('created_at', 'updated_at'):
        _timestamp(value.get(field))
    history = value.get('history')
    if not isinstance(history, list) or len(history) > 8:
        raise ValueError('Invalid recovery history')
    for entry in [value, *history]:
        if not isinstance(entry, dict):
            raise ValueError('Invalid recovery history entry')
        if entry.get('status') not in (*ACTIVE, *TERMINAL):
            raise ValueError('Invalid recovery status')
        attempt = entry.get('attempt')
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValueError('Invalid recovery attempt')
        if entry.get('verification') is not None and not isinstance(entry['verification'], dict):
            raise ValueError('Invalid recovery verification')
        _timestamp(entry.get('updated_at'))
    return value


def _read(root, identity):
    path = _path(root, identity)
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_RECEIPT_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError('Oversized recovery incident')
    return _validate_receipt(json.loads(raw), identity)


def _identities(candidate):
    if not isinstance(candidate, dict):
        raise ValueError('Invalid recovery candidate projection')
    references = candidate.get('recovery_incidents', {})
    if not isinstance(references, dict) or len(references) > 64:
        raise ValueError('Invalid recovery incident references')
    return references.values()


def _save(root, value):
    from controller import save
    save(_path(root, value['id']), value)


@_best_effort
def begin(root, *, scope, task_id, model, stage, cause, action, occurrence,
          attempt=None, evidence=None, exhausted=False):
    """Record before action. Re-entering one persisted occurrence is idempotent."""
    identity = _hash([scope, task_id, model, stage, cause])[:24]
    previous = _read(root, identity)
    token = _hash(occurrence)
    if previous and previous.get('occurrence') == token:
        return identity
    if attempt is None:
        attempt = (previous or {}).get('attempt', 0) + 1
    history = list((previous or {}).get('history', []))
    if previous:
        history.append({k: previous.get(k) for k in
                        ('attempt', 'status', 'updated_at', 'verification', 'evidence_sha256')})
        if history[-1]['status'] in ACTIVE:
            history[-1]['status'] = 'failed'
            history[-1]['verification'] = {'kind': 'failure-recurred', 'task_accepted': False}
    raw = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    # Full evidence lives in original candidate history/receipts, never duplicated here.
    value = {'schema': 1, 'id': identity, 'scope': scope, 'task_id': task_id,
             'model': model, 'stage': stage, 'cause': cause, 'action': action,
             'occurrence': token, 'attempt': attempt,
             'status': 'exhausted' if exhausted else 'pending',
             'reason': raw[:1000], 'evidence': evidence if len(raw) <= 2000 else {'excerpt': raw[:2000]},
             'evidence_sha256': _hash(evidence), 'verification': None,
             'created_at': (previous or {}).get('created_at', _now()), 'updated_at': _now(),
             'history': history[-8:]}
    _save(root, value)
    return identity


@_best_effort
def transition(root, identity, status, verification=None):
    if identity is None:
        return
    if status not in (*ACTIVE, *TERMINAL):
        raise ValueError('Invalid recovery state')
    value = _read(root, identity)
    if value is None:
        return
    if status == 'verified' and not verification:
        raise ValueError('Recovery success requires a checked postcondition')
    if value['status'] == status and value.get('verification') == verification:
        return
    if verification is not None:
        if not isinstance(verification, dict):
            raise ValueError('Invalid recovery verification')
        encoded = json.dumps(verification, ensure_ascii=False, sort_keys=True)
        if len(encoded) > 2000:
            verification = {'kind': verification.get('kind'),
                'task_accepted': verification.get('task_accepted', False),
                'excerpt': encoded[:1800], 'sha256': _hash(verification)}
    value.update(status=status, verification=verification, updated_at=_now())
    _save(root, value)


@_best_effort
def pending(root, candidate, *, running=False):
    """Restart resumes receipts without spending or resetting any retry budget."""
    for identity in _identities(candidate):
        if identity is None:
            continue
        value = _read(root, identity)
        if value and value['status'] in (*ACTIVE, 'interrupted'):
            transition(root, identity, 'running' if running else 'pending')


@_best_effort
def finish(root, candidate, milestone, evidence=None):
    """Called only after the controller's existing mandatory validation succeeds."""
    for identity in _identities(candidate):
        if identity is None:
            continue
        value = _read(root, identity)
        if not value or value['status'] not in (*ACTIVE, 'interrupted'):
            continue
        cause, stage = value['cause'], value['stage']
        eligible = milestone == 'accepted'
        eligible |= milestone == 'checks-passed' and cause in ('checks', 'snapshot')
        eligible |= milestone == 'execution-validated' and stage == 'execute' and cause in ('transport', 'timeout', 'model', 'request-protocol')
        eligible |= milestone == 'review-validated' and stage in ('review', 'adjudicate') and cause in ('transport', 'timeout', 'protocol', 'context-limit')
        if eligible:
            transition(root, identity, 'verified', {'kind': milestone, 'evidence': evidence,
                        'task_accepted': milestone == 'accepted'})


@_best_effort
def suspend(root, candidate, status, reason):
    for identity in _identities(candidate):
        if identity is None:
            continue
        value = _read(root, identity)
        if value and value['status'] in ACTIVE:
            transition(root, identity, status, {'kind': status, 'reason': str(reason)[:1000],
                                               'task_accepted': False})


@_best_effort
def supersede(root, identity, next_identity):
    """A different cause closes unfinished recovery, never erases proven success."""
    if not identity or identity == next_identity:
        return
    value = _read(root, identity)
    if value and value['status'] in (*ACTIVE, 'interrupted'):
        transition(root, identity, 'failed',
            {'kind': 'failure-changed', 'next_incident': next_identity, 'task_accepted': False})


def run_verified(root, *, scope, task_id, model, stage, cause, action, occurrence,
                 evidence, operation, verify):
    """Run an existing guarded handler and independently check its postcondition.

    Previous success never skips current guards. A crash leaves running state;
    the idempotent handler is revalidated on restart with the same occurrence.
    """
    identity = begin(root, scope=scope, task_id=task_id, model=model, stage=stage,
                     cause=cause, action=action, occurrence=occurrence, evidence=evidence)
    transition(root, identity, 'running')
    try:
        result = operation()
        proof = verify(result)
        if not proof:
            raise ValueError('Recovery action did not establish its postcondition')
    except Exception as exc:
        transition(root, identity, 'failed', {'kind': 'action-failed', 'reason': str(exc)[:1000],
                                             'task_accepted': False})
        raise
    transition(root, identity, 'verified', {**proof, 'task_accepted': False})
    return result


def context(root):
    """Read newest 64 receipts from at most 1024 metadata entries per poll.

    Both limits are explicit partial observations, never a complete absence of
    incidents. Metadata selection prevents old directory entries hiding new work.
    """
    result = {'schema': 1, 'incidents': [], 'active': [], 'unavailable': False}
    folder = Path(root)/'recovery'/'incidents'
    def unavailable(exc):
        result.update(unavailable=True, error=str(exc)[:400])
    try:
        if folder.is_symlink() or folder.parent.is_symlink():
            raise ValueError('Unsafe recovery ledger')
        if not folder.exists():
            return result
        newest = []
        with os.scandir(folder) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_METADATA_ENTRIES:
                    result['truncated'] = True
                    unavailable('Recovery metadata scan limit reached; latest observations may be incomplete')
                    break
                if not entry.name.endswith('.json'):
                    continue
                try:
                    newest.append((entry.stat(follow_symlinks=False).st_mtime_ns, entry.name))
                except OSError as exc:
                    unavailable(exc)
        newest.sort(reverse=True)
        if len(newest) > MAX_SCAN_ENTRIES:
            result['truncated'] = True
            unavailable('Recovery receipt limit reached; showing newest observations only')
        for _, name in newest[:MAX_SCAN_ENTRIES]:
            try:
                value = _read(root, Path(name).stem)
                if value is not None:
                    result['incidents'].append(value)
            except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
                unavailable(exc)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        unavailable(exc)
    result['incidents'].sort(key=lambda v: (v['status'] in ACTIVE, v['updated_at'], v['id']), reverse=True)
    result['incidents'] = result['incidents'][:24]
    result['active'] = [v for v in result['incidents'] if v['status'] in ACTIVE]
    return result
