"""Recover an OS failure only from independently revalidated, completed work."""
from pathlib import Path
import traceback

from workspace import export_patch, snapshot


def eligible(fatal):
    # Legacy records have no traceback/errno. Do not turn arbitrary safety
    # verdicts into an OS retry merely because their text mentions permissions.
    return (fatal.get('type') in ('PermissionError', 'OSError')
            and (fatal.get('category') == 'operational'
                 or (fatal.get('type') == 'PermissionError'
                     and fatal.get('error') in ('[Errno 1] Operation not permitted',
                                               '[Errno 13] Permission denied'))))


def failure_record(exc, scope):
    from controller import OperationalBlocked
    # Keep frames, not locals or source lines which may contain sensitive input.
    return {'error': str(exc), 'type': type(exc).__name__, 'scope': scope,
            'category': 'operational' if isinstance(exc, (OSError, OperationalBlocked)) else 'safety',
            'errno': getattr(exc, 'errno', None),
            'frames': [{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
                       for f in traceback.extract_tb(exc.__traceback__)]}


def recover(runner, task, candidates, folder, scope):
    from controller import Blocked, digest, git_controls, protected_context, read, save
    import review_receipt
    from execution_cycle import models_for, _receipt_valid
    fatal_path = folder/'fatal.json'
    fatal = read(fatal_path)
    if not eligible(fatal) or fatal.get('scope') != scope:
        raise Blocked('Unclassified failure requires inspection: ' + str(fatal_path))
    runner.guard()
    matches = []
    review_roots = []
    rejected = []
    models = models_for(runner)
    for path, value in candidates:
        if value.get('stage') not in ('review', 'adjudicate'):
            continue
        _receipt_valid(value)
        model = models[1-models.index(value['model'])] if value['stage'] == 'review' else value['model']
        name = task['id'] + ('-review' if value['stage'] == 'review' else '-adjudicate')
        # A newer interrupted attempt does not erase older completed evidence.
        for receipt in sorted((runner.root/'logs').glob(name+'-*/session-receipt.json'),
                              key=lambda p: p.stat().st_mtime_ns, reverse=True):
            if receipt.is_symlink() or read(receipt).get('model') != model:
                continue
            events = receipt.parent/'stdout.jsonl'
            try:
                root, report = review_receipt.load(runner, task, value, str(events), model)
            except (Blocked, ValueError, OSError, KeyError, TypeError) as exc:
                rejected.append({'session': receipt.parent.name, 'type': type(exc).__name__, 'error': str(exc)})
                continue  # Stale/unusable receipts never authorize recovery.
            matches.append((path, value, events, model))
            review_roots.append(root)
            break
    save(folder/'recovery-attempts.json', {'rejected_count':len(rejected), 'rejected':rejected[:12],
                                        'usable_reviews':[str(m[2]) for m in matches]})
    if not matches:
        raise Blocked('Operational failure has no unchanged candidate with usable audited review; inspect ' + str(fatal_path))
    # The lock entry already checked the whole run. At this point unrelated
    # tasks may be executing concurrently; inspect only this task's copies.
    runner.check_preserved_processes(roots=[Path(value['workspace']) for _, value in candidates] + review_roots)
    anchor = matches[0][1]
    context = protected_context(snapshot(anchor['workspace'], normalize_settings=True))
    pristine_context = protected_context(snapshot(runner.main, normalize_settings=True))
    def controls(root):
        root = Path(root).resolve()
        return {Path(p).resolve().relative_to(root).as_posix(): v for p, v in git_controls(root).items()}
    expected_controls = controls(runner.main)
    audits = []
    for path, value in candidates:
        root = Path(value['workspace'])
        if root.is_symlink() or not root.resolve().is_relative_to((runner.root/'workers').resolve()):
            raise Blocked('Operational recovery candidate is outside managed workers')
        if controls(root) != expected_controls:
            raise Blocked('Operational recovery found changed Git controls')
        # A serialized peer may never have reached permission preparation.
        # Its pristine pinned context is as valid as the audited executor's.
        if protected_context(snapshot(root, normalize_settings=True)) not in (context, pristine_context):
            raise Blocked('Operational recovery found changed instructions or permissions')
        runner.audit(root)
        if value.get('stage') != 'execute':
            _receipt_valid(value)
        patch = export_patch(root, value['base'], task['writes'], path.parent/'operational-recovery.patch')
        audits.append({'model': value['model'], 'patch': patch})
    # Persist proof before removing the gate. Incomplete peers are retained but
    # never accepted or relaunched on the strength of another model's receipt.
    restored = {str(path) for path, *_ in matches}
    for path, value in candidates:
        if str(path) not in restored:
            value.update(status='quarantined', recovery_reason='Unaudited peer retained after operational failure')
            save(path, value)
    for path, value, events, model in matches:
        value['recovered_review'] = {'events_path': str(events), 'model': model, 'stage': value['stage']}
        save(path, value)
    archived = folder/('recovered-fatal-' + digest(fatal_path) + '.json')
    save(folder/'operational-recovery.json', {'source': fatal_path.name, 'archived': archived.name,
         'audits': audits, 'reviews': [str(m[2]) for m in matches],
         'rule': 'unchanged candidate, audited review, ownership and process checks; no assumed cleanup success'})
    fatal_path.replace(archived)
    runner.progress(task['id'] + ': системный сбой перепроверен; продолжаю по сохранённому ревью, '
                    'незавершённые варианты сохранены отдельно')
