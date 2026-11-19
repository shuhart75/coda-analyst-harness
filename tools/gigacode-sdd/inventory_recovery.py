"""Revalidate only legacy fatal records caused by incomplete service inventory."""
from pathlib import Path
import re
import uuid


def recover(runner, folder, scope, record):
    from controller import Blocked, read
    from service_inventory import inspect_services
    path = folder/'fatal.json'
    if path.is_symlink() or not path.is_file():
        return False
    fatal = read(path)
    if fatal.get('type') != 'Blocked' or fatal.get('scope') != scope:
        return False
    prefix = 'Service inspection requires attention; no automatic service shutdown. See '
    failures = [fatal, *fatal.get('related_failures', [])]
    reports = []
    for failure in failures:
        if failure.get('type') != 'Blocked' or failure.get('scope') != scope:
            return False
        message = failure.get('error', '')
        if message == 'Cannot inspect services before session; run bootstrap --diagnose':
            continue
        if not message.startswith(prefix):
            return False
        report = Path(message[len(prefix):])
        if (report.parent != runner.root/'logs' or report.parent.is_symlink() or report.is_symlink() or not report.is_file()
                or not re.fullmatch(r'[a-zA-Z0-9_-]+-services-[a-f0-9]{12}\.json', report.name)):
            return False
        value = read(report)
        inventory = value.get('inventory', {})
        if value.get('new_workspace_services') != [] or inventory.get('complete') is not False or not inventory.get('limitations'):
            return False
        reports.append(report)
    if not reports:
        return False
    runner.guard()
    inventory = inspect_services()
    if not inventory.get('complete'):
        raise Blocked('Service inventory remains incomplete; preserved failure retained: ' + str(path))
    roots = [Path(record['workspace'])]
    for candidate in folder.glob('*/candidate.json'):
        if candidate.is_symlink() or candidate.parent.is_symlink():
            raise Blocked('Unsafe candidate path during inventory recovery')
        value = read(candidate)
        if value.get('scope') != scope:
            raise Blocked('Candidate scope mismatch during inventory recovery')
        roots.append(Path(value['workspace']))
    # Does not kill or adopt services; surviving managed processes remain blockers.
    runner.check_preserved_processes(roots=roots)
    path.rename(folder/('fatal-inventory-recovered-' + uuid.uuid4().hex + '.json'))
    return True
