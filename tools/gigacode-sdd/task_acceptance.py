"""Task contribution contracts; whole-product evidence remains a final gate."""
import json

PROTOCOL = 2


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
    owners = {t['id']: t for t in plan['tasks']}
    for index, item in enumerate(items):
        prefix = f'Deferred evidence item {index + 1}: '
        if not isinstance(item, dict):
            raise ValueError(prefix + 'expected an object')
        requirement, owner = item.get('requirement'), item.get('owner')
        if not isinstance(owner, str) or owner not in later:
            raise ValueError(prefix + 'owner must be a downstream task: ' + ', '.join(sorted(later)))
        # A reference to an obligation already assigned to the named downstream
        # owner does not transfer new scope or waive any current deliverable.
        if (requirement not in task['requirements']
                and requirement not in owners[owner].get('requirements', [])):
            raise ValueError(prefix + str(requirement) + ' is assigned to neither this task nor owner ' + owner)
        missing = [k for k in ('evidence', 'reason') if not isinstance(item.get(k), str) or not item[k].strip()]
        if missing:
            raise ValueError(prefix + 'missing nonempty fields: ' + ', '.join(missing))
    return items


def instructions(runner, task, *, focused=False):
    from controller import read
    if task['id'] == 'integration':
        return ('\nFINAL_ACCEPTANCE: every pinned requirement and all deferred evidence must be satisfied for pass. '
                'Recorded deferrals remain open, not implementation defects or satisfied requirements. '
                'Route missing analyst decisions and manual proof through EXTERNAL_ACCEPTANCE_GAPS; '
                'never repeat development merely to reverse an operator decision.')
    path = runner.root / 'plan.json'
    if not path.is_file():
        return ''
    plan = read(path)
    later = descendants(plan, task['id'])
    owners = [{'id': t['id'], 'requirements': t['requirements'], 'prompt': t['prompt'],
               'acceptance': t.get('acceptance')} for t in plan['tasks'] if t['id'] in later]
    if focused:
        return ('\nAny deferred proof must remain mandatory and name its downstream owner. '
                'Only defer proof explicitly assigned downstream in .review-input/plan.json; '
                'current task defects cannot be deferred. Downstream task IDs: '
                + json.dumps(sorted(later), ensure_ascii=False))
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
            'When disputing a premature whole-product gate, use the plan assignment plus actual '
            'task implementation evidence, and retain deferred_evidence. '
            'A downstream task prompt may explicitly own a scenario even if its legacy requirement list '
            'omitted the identifier; record the identifier in deferred_evidence for mandatory final verification. '
            '\nDownstream assignments from the pinned plan:\n' + json.dumps(owners, ensure_ascii=False))


def check_report(runner, task, report):
    from controller import read, RetryableSessionError
    from review_format import deferred
    from acceptance_wait import validate
    validate(runner.root, task, report)
    path = runner.root / 'plan.json'
    try:
        items = deferred(report.get('deferred_evidence', []))
        if not path.exists() and not items:
            return
        validate_deferred(read(path), task, items)
        report['deferred_evidence'] = items
    except ValueError as exc:
        raise RetryableSessionError(str(exc)) from exc
