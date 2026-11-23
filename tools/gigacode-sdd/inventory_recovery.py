"""Revalidate service inventory/handoff failures without discarding evidence."""
from pathlib import Path
import re
import uuid


def preserve_changed_receipts(runner, roots, pending_prompts=None):
    """Live servers can append workspace logs after a session completes.

    An altered ordinary file requires fresh execution/checks, not a fabricated
    old snapshot. Protected inputs and evidence remain hard failures.
    """
    from controller import Blocked, digest, git_controls, read, snapshot
    allowed = {str(root.resolve()) for root in roots}
    for path in (runner.root/'logs').glob('*/session-receipt.json'):
        if path.is_symlink() or path.parent.is_symlink():
            raise Blocked('Unsafe session receipt during service handoff')
        value = read(path)
        if value.get('workspace') not in allowed or not value.get('workspace_snapshot'):
            continue
        if pending_prompts is not None:
            prompt = pending_prompts.get(value['workspace'])
            prompt_file = path.parent/'prompt.txt'
            if not prompt or not prompt_file.is_file() or prompt_file.is_symlink():
                continue
            if not prompt_file.read_text(encoding='utf-8').endswith('\n' + prompt):
                continue
        work = Path(value['workspace'])
        before = value['workspace_snapshot']
        after = snapshot(work, normalize_settings=True)
        if after == before:
            continue
        events = path.parent/'stdout.jsonl'
        if (value.get('events_sha256') != digest(events) or events.is_symlink()
                or (path.parent/'prompt.txt').is_symlink()
                or value.get('prompt_sha256') != digest(path.parent/'prompt.txt')
                or value.get('git_controls') != git_controls(work) or before['head'] != after['head']):
            raise Blocked('Saved session evidence or Git controls changed during service handoff')
        for name in before['files'].keys() | after['files'].keys():
            if (any(part in ('.gigacode', 'AGENTS.md', 'GIGACODE.md') for part in Path(name).parts)
                    and before['files'].get(name) != after['files'].get(name)):
                raise Blocked('Protected execution context changed during service handoff')
        path.rename(path.with_name('session-receipt-before-handoff-' + uuid.uuid4().hex + '.json'))


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
        new = value.get('new_workspace_services')
        incomplete = new == [] and inventory.get('complete') is False and inventory.get('limitations')
        handoff = (isinstance(new, list) and bool(new) and inventory.get('complete') is True
                   and all(isinstance(item, dict) and item.get('started') and item.get('cwd')
                           and type(item.get('pid')) is int for item in new))
        if not (incomplete or handoff):
            return False
        reports.append(report)
    if not reports:
        return False
    runner.guard()
    inventory = inspect_services()
    if not inventory.get('complete'):
        raise Blocked('Service inventory remains incomplete; preserved failure retained: ' + str(path))
    roots = [Path(record['workspace'])]
    pending_prompts = {}
    for candidate in folder.glob('*/candidate.json'):
        if candidate.is_symlink() or candidate.parent.is_symlink():
            raise Blocked('Unsafe candidate path during inventory recovery')
        value = read(candidate)
        if value.get('scope') != scope:
            raise Blocked('Candidate scope mismatch during inventory recovery')
        roots.append(Path(value['workspace']))
        prompt = (value.get('pending_session') or {}).get('prompt')
        if isinstance(prompt, str) and prompt:
            pending_prompts[str(Path(value['workspace']).resolve())] = prompt
    # No signals: every surviving service must have a current reuse decision;
    # live agent groups and incomplete inventories remain blockers.
    runner.check_preserved_processes(roots=roots)
    preserve_changed_receipts(runner, roots, pending_prompts)
    path.rename(folder/('fatal-inventory-recovered-' + uuid.uuid4().hex + '.json'))
    stop = runner.root/'service-stop.json'
    if stop.is_file() and not stop.is_symlink() and read(stop).get('report') in [str(p) for p in reports]:
        stop.rename(folder/('service-stop-recovered-' + uuid.uuid4().hex + '.json'))
    return True
