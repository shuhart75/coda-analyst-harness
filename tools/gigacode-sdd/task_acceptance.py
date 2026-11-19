"""Task contribution contracts; whole-product evidence remains a final gate."""
import json

PROTOCOL = 1


def with_incoming(runner, task):
    """Carry validated predecessor proof obligations into their named owner."""
    incoming = []
    for source, record in runner.state.get('tasks', {}).items():
        if record.get('status') != 'done':
            continue
        for item in record.get('review', {}).get('deferred_evidence', []):
            if item['owner'] == task['id']:
                incoming.append({'source': source, **item})
    if not incoming:
        return task
    return {**task, 'requirements': sorted(set(task['requirements']) | {i['requirement'] for i in incoming}),
            'incoming_evidence': incoming,
            'prompt': task['prompt'] + '\nMandatory evidence assigned here by accepted predecessor reviews '
                      '(part of the original pinned requirements, never optional):\n'
                      + json.dumps(incoming, ensure_ascii=False)}


def descendants(plan, task_id):
    found = {task_id}
    while True:
        after = found | {t['id'] for t in plan['tasks'] if set(t['depends_on']) & found}
        if after == found:
            return found - {task_id}
        found = after


def validate_contracts(plan):
    for task in plan['tasks']:
        contract = task.get('acceptance')
        if contract is None:  # Existing immutable plans use their explicit task instructions.
            continue
        if (not isinstance(contract, dict) or not isinstance(contract.get('deliverables'), list)
                or not contract['deliverables'] or any(not isinstance(x, str) or not x.strip()
                    for x in contract['deliverables'])):
            raise ValueError('Task acceptance needs concrete deliverables')
        validate_deferred(plan, task, contract.get('deferred_evidence', []))


def validate_deferred(plan, task, items):
    if not isinstance(items, list):
        raise ValueError('Deferred evidence must be a list')
    later = descendants(plan, task['id']) if task['id'] != 'integration' else set()
    for item in items:
        if (not isinstance(item, dict) or item.get('requirement') not in task['requirements']
                or item.get('owner') not in later
                or not all(isinstance(item.get(k), str) and item[k].strip() for k in ('evidence', 'reason'))):
            raise ValueError('Deferred proof needs an assigned requirement, downstream owner and explanation')
    return items


def instructions(runner, task):
    from controller import read
    if task['id'] == 'integration':
        return '\nFINAL_ACCEPTANCE: every pinned requirement and all deferred evidence must be satisfied. No deferrals allowed.'
    path = runner.root / 'plan.json'
    if not path.is_file():
        return ''
    plan = read(path)
    later = descendants(plan, task['id'])
    owners = [{'id': t['id'], 'requirements': t['requirements'], 'prompt': t['prompt'],
               'acceptance': t.get('acceptance')} for t in plan['tasks'] if t['id'] in later]
    return ('\nTASK_CONTRIBUTION_ACCEPTANCE: Assess the concrete work assigned by this task prompt and '
            'acceptance contract. Requirement IDs are traceability, not a demand that this intermediate '
            'task alone complete the entire product. All current task checks and deliverables remain mandatory. '
            'A live proof explicitly assigned to a downstream task is not a defect of this contribution. '
            'For each such proof return deferred_evidence:[{"requirement":"REQ-id","owner":"downstream-id",'
            '"evidence":"what remains to demonstrate","reason":"why the pinned plan assigns it downstream"}]. '
            'Keep these obligations mandatory. Never label them optional or claim they already passed. '
            'Only defer when the pinned task instructions actually assign that proof downstream; uncertainty '
            'about an implementation defect is not grounds for deferral. Coverage/coverage_checks describe '
            'this task contribution; covered does not mean downstream live proof is complete. '
            'In adjudication refute a premature whole-product gate only with the plan assignment plus actual '
            'task implementation evidence, and retain deferred_evidence. '
            'A downstream task prompt may explicitly own a scenario even if its legacy requirement list '
            'omitted the identifier; record the identifier in deferred_evidence for mandatory final verification. '
            '\nDownstream assignments from the pinned plan:\n' + json.dumps(owners, ensure_ascii=False))


def check_report(runner, task, report):
    from controller import read, RetryableSessionError
    path = runner.root / 'plan.json'
    items = report.get('deferred_evidence', [])
    if not path.exists() and not items:
        return
    try:
        validate_deferred(read(path), task, items)
    except ValueError as exc:
        raise RetryableSessionError(str(exc)) from exc
