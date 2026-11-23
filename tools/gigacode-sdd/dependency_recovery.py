"""Reclassify only the old inventory-wide Nexus fatal, preserving its evidence."""
from pathlib import Path
import time


def recover_static_gate(runner, folder, scope):
    from controller import read, save, Blocked
    path = folder / 'fatal.json'
    if not path.exists() or path.is_symlink():
        return
    fatal = read(path)
    if (fatal.get('scope') != scope or fatal.get('type') != 'Blocked'
            or not str(fatal.get('error', '')).startswith('Dependency sources are not Nexus-only: ')):
        return
    # Do not erase a compound incident containing unrelated failures.
    for item in fatal.get('related_failures', []):
        if not str(item.get('error', '')).startswith('Dependency sources are not Nexus-only: '):
            return
    candidates = list(folder.glob('*/candidate.json'))
    if not candidates:
        raise Blocked('Static audit recovery has no saved candidates')
    reports = []
    for candidate in candidates:
        if candidate.is_symlink():
            raise Blocked('Static audit recovery candidate is a symlink')
        value = read(candidate)
        work = Path(value['workspace'])
        if value.get('scope') != scope or work.is_symlink() or not work.resolve().is_relative_to((runner.root / 'workers').resolve()):
            raise Blocked('Static audit recovery candidate is outside managed workers')
        report = runner.audit(work, inventory_only=True)
        reports.append({'candidate': str(candidate), 'assessment': report})
    save(folder / 'dependency-reassessment.json', {'scope': scope, 'candidates': reports,
         'meaning': 'Static findings are rechecked before use; no checks or results accepted by migration.'})
    path.rename(folder / ('fatal-static-audit-' + str(time.time_ns()) + '.json'))
