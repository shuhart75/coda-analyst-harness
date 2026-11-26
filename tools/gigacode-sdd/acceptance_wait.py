"""Route independent external findings without spending implementation retries.

An operator decision can defer work, never prove that a requirement passed.
Existing answers are referenced, not guessed from prose or asked again.
"""
import copy
import operator_questions as questions

INSTRUCTIONS = '''
EXTERNAL_ACCEPTANCE_GAPS: changes means actionable implementation defects; blocked means
only external dependencies, analyst decisions or manual verification remain. Never send an
already deferred analyst decision back as a code repair or silently count it as satisfied.
For blocked return acceptance_blockers, one per finding, with:
{"finding_index":0,"kind":"analyst-decision|manual-check|external-dependency",
"requirements":["REQ-id"],"next_step":"specific missing input or verification, in Russian",
"answer_refs":[{"id":"existing operator answer id","quote":"exact answer excerpt"}]}.
Use answer_refs only when a recorded answer already explains why this obligation remains open.
Do not ask that same decision again. A previous decision to do a manual check is not its
result: supply manual_check and question to request the actual result when that check is due,
even with answer_refs. Respect explicit deferrals; do not re-request a deferred decision.
When no answer applies use answer_refs:[] and include
question (a concrete Russian question). For manual-check also include manual_check with
url, artifact, preconditions, requirements and steps:[{action,expected}]; use actual known
stand URL/build and observable steps, never invent them. If those are unknown ask for the
missing external information as external-dependency. Missing machine tests are not manual
checks. Do not classify implementation defects as external merely to avoid fixing them.
If both code defects and external gaps remain, return changes with only actionable code
defects in findings, retain external gaps in coverage, and re-evaluate them after repairs.
A pass still requires every mandatory proof; deferred/unknown never becomes pass.
'''


def answers(root, task):
    return questions.replies(root, None if task['id'] == 'integration' else task['id'])


def validate(root, task, report):
    from controller import RetryableSessionError
    if report.get('verdict') != 'blocked':
        if report.get('acceptance_blockers'):
            raise RetryableSessionError('External acceptance blockers require verdict=blocked')
        return []
    items = report.get('acceptance_blockers')
    saved = {a['id']: a for a in answers(root, task)}
    try:
        if not isinstance(items, list) or len(items) != len(report['findings']) or not items:
            raise ValueError('blocked needs acceptance_blockers for every finding; preserve all gaps')
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('Acceptance blocker must be an object')
            index = item.get('finding_index')
            if (type(index) is not int or index not in range(len(report['findings'])) or index in seen):
                raise ValueError('Acceptance blocker needs a unique finding_index')
            seen.add(index)
            if item.get('kind') not in ('analyst-decision', 'manual-check', 'external-dependency'):
                raise ValueError('Unknown acceptance blocker kind')
            reqs = item.get('requirements')
            if (not isinstance(reqs, list) or not reqs or any(not isinstance(r, str)
                    or r not in task['requirements'] for r in reqs)):
                raise ValueError('Acceptance blocker must name assigned requirements')
            if (not isinstance(item.get('next_step'), str) or not item['next_step'].strip()
                    or len(item['next_step']) > 4000):
                raise ValueError('Acceptance blocker needs a concrete next_step')
            refs = item.get('answer_refs')
            if not isinstance(refs, list):
                raise ValueError('Acceptance blocker needs answer_refs, possibly empty')
            for ref in refs:
                if not isinstance(ref, dict) or not isinstance(ref.get('id'), str) or ref['id'] not in saved:
                    raise ValueError('Acceptance blocker references an unknown operator answer')
                quote = ref.get('quote')
                answer = saved[ref['id']]['answer']
                if (not isinstance(quote, str) or not quote.strip() or not any(
                        quote in answer.get(k, '') for k in ('text', 'explanation'))):
                    raise ValueError('Acceptance blocker quote does not match the recorded answer')
            if not refs or (item['kind'] == 'manual-check' and item.get('manual_check')):
                if (not isinstance(item.get('question'), str) or not item['question'].strip()
                        or len(item['question']) > 16000):
                    raise ValueError('Unanswered acceptance blocker needs a concrete question')
                if item['kind'] == 'manual-check':
                    check = questions.validate_manual_check(item.get('manual_check'))
                    if not check or set(check['requirements']) != set(reqs):
                        raise ValueError('Manual acceptance blocker needs matching reproducible manual_check')
    except (ValueError, TypeError, KeyError) as exc:
        raise RetryableSessionError(str(exc)) from exc
    return copy.deepcopy(items)


def park(root, task, value, report, items):
    keys = []
    for item in items:
        if item['answer_refs'] and not (item['kind'] == 'manual-check' and item.get('manual_check')):
            continue
        # Same question is shared; distinct inputs for one requirement stay distinct.
        identity = {'kind': item['kind'], 'requirements': sorted(item['requirements']),
                    'question': ' '.join(item['question'].casefold().split())}
        keys.append(questions.request(root, task['id'], 'acceptance-' + questions.fingerprint(identity)[:16],
            item['question'], details=report['findings'][item['finding_index']] + '\n' + item['next_step'],
            model=value['model'], manual_check=item.get('manual_check')))
    value['acceptance_wait'] = {'items': items, 'question_ids': keys,
        'answers_sha256': questions.fingerprint(answers(root, task)),
        'findings': copy.deepcopy(report['findings'])}
    value.update(status='external-blocker', stage='review')
    return summary(value)


def summary(value):
    wait = value.get('acceptance_wait', {})
    return 'Ожидается итоговая проверка или внешнее решение: ' + '; '.join(
        ', '.join(item['requirements']) + ': ' + item['next_step'] for item in wait.get('items', []))


def refresh(root, task, value):
    wait = value.get('acceptance_wait')
    if not wait:
        return False
    changed = wait['answers_sha256'] != questions.fingerprint(answers(root, task))
    if changed or value.get('stage') not in ('review', 'accepted'):
        value.setdefault('acceptance_wait_history', []).append(value.pop('acceptance_wait'))
        if value.get('status') == 'external-blocker':
            value['status'] = 'pending'
        value.pop('recovered_review', None)
        return True
    return False


def pending(root, candidates):
    keys = {key for _, v in candidates for key in v.get('acceptance_wait', {}).get('question_ids', [])}
    return [q['id'] for q in questions.questions(root, pending_only=True) if q['id'] in keys]
