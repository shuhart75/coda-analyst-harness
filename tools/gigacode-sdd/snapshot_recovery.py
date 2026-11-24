"""Recover a failed review copy without accepting stale checks or evidence."""
from pathlib import Path
import uuid

LEGACY_ERROR = 'Source changed during snapshot; clone retained but not usable'


def repair_instruction(report):
    return ('Review snapshot could not be made consistent. Diagnostic: ' + str(report)
            + '. Inspect the reported changing paths and their writers. Preserve existing evidence '
              'and live services. Put future runtime output outside reviewed source/evidence and '
              'capture a finite, provenance-bound evidence artifact when needed. Do not discard '
              'logs by extension, rewrite historical observations, kill or restart an unowned '
              'service, or rerun a no-rebuild experiment merely to obtain a snapshot. '
              'Use the service/operator request protocol if changing the writer needs authority. '
              'All required checks and independent review will run again.')


def recover(runner, folder, scope):
    from controller import Blocked, read, save, digest
    from workspace import snapshot, snapshot_difference, _forbidden
    path = folder / 'fatal.json'
    if not path.is_file() or path.is_symlink():
        return False
    fatal = read(path)
    failures = [fatal, *fatal.get('related_failures', [])]
    for item in failures:
        if (item.get('scope') != scope or item.get('type') != 'WorkspaceError'
                or item.get('error') != LEGACY_ERROR):
            return False
    # Only the review-copy race is migrated, never an initialization/clone failure.
    for item in failures:
        frames = item.get('frames', [])
        if not (any(f.get('file') == 'execution_cycle.py' and f.get('function') == '_review_copy' for f in frames)
                and any(f.get('file') == 'workspace.py' and f.get('function') == 'clone_snapshot' for f in frames)):
            return False
    candidates = list(folder.glob('*/candidate.json'))
    if not candidates:
        raise Blocked('Snapshot recovery has no saved candidates')
    updates = []
    already_recovered = False
    fatal_sha = digest(path)
    roots = []
    for candidate in candidates:
        if candidate.is_symlink() or candidate.parent.is_symlink():
            raise Blocked('Unsafe snapshot recovery candidate')
        value = read(candidate)
        work = Path(value['workspace'])
        if (value.get('scope') != scope or work.is_symlink()
                or not work.resolve().is_relative_to((runner.root / 'workers').resolve())):
            raise Blocked('Snapshot recovery workspace is outside managed workers')
        roots.append(work)
        resumed = (value.get('snapshot_recovery') or {}).get('fatal_sha256') == fatal_sha
        if resumed:
            saved = Path(value['snapshot_recovery']['previous_candidate'])
            if (saved.parent != candidate.parent or saved.is_symlink() or not saved.is_file()
                    or digest(saved) != value['snapshot_recovery'].get('previous_candidate_sha256')):
                raise Blocked('Snapshot recovery archive changed')
            value = read(saved)
            already_recovered = True
        if value.get('stage') != 'review' or value.get('status') == 'accepted':
            continue
        before = value.get('workspace_snapshot')
        if not isinstance(before, dict) or not isinstance(before.get('files'), dict):
            raise Blocked('Snapshot recovery requires the saved check snapshot')
        after = snapshot(work, normalize_settings=True)
        delta = snapshot_difference(before, after)
        if delta['head_changed'] or any(_forbidden(name) for name in delta['paths']):
            raise Blocked('Protected candidate inputs changed after checks')
        for name, expected in value.get('evidence_files', {}).items():
            evidence = Path(name)
            if evidence.is_symlink() or not evidence.is_file() or digest(evidence) != expected:
                raise Blocked('Saved controller evidence changed: ' + name)
        if not resumed:
            updates.append((candidate, value, delta))
    if not updates and not already_recovered:
        return False
    runner.guard()
    runner.check_preserved_processes(roots=roots)
    for candidate, value, delta in updates:
        # Preserve the entire old candidate before invalidating its result.
        saved = candidate.with_name('candidate-before-snapshot-recovery-' + uuid.uuid4().hex + '.json')
        save(saved, value)
        value.update(stage='checks', status='pending', feedback=repair_instruction(saved))
        value['snapshot_recovery'] = {'previous_candidate': str(saved), 'previous_candidate_sha256': digest(saved),
                                      'difference': delta, 'fatal_sha256': fatal_sha}
        for key in ('recovered_review', 'review_resolution', 'review', 'pending_session'):
            value.pop(key, None)
        save(candidate, value)
    path.rename(folder / ('fatal-snapshot-recovered-' + uuid.uuid4().hex + '.json'))
    return True
