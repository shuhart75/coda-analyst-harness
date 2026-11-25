"""Presentation-only attention rules adapted from Orca (MIT).

Copyright (c) 2026 Lovecast Inc.
See third_party/orca/LICENSE and third_party/orca/README.md.
Never use presentation categories to stop a process or accept a task.
"""

ACTION_CATEGORIES = frozenset({
    'guidance', 'input', 'approval', 'failure', 'interruption', 'unverifiable',
})


def project_attention(facts):
    """Keep task outcome, process liveness and human attention independent.

    Unlike upstream, liveness may be absent: a task is not itself a process.
    Missing liveness is never implicitly turned into a live/exited verdict.
    """
    categories = []
    for field, category in (('pending_guidance', 'guidance'), ('pending_input', 'input'),
                            ('pending_approval', 'approval')):
        if facts.get(field):
            categories.append(category)
    if facts.get('outcome') == 'failed':
        categories.append('failure')
    if facts.get('interrupted'):
        categories.append('interruption')
    liveness = facts.get('liveness')
    if liveness is not None:
        verdict = liveness.get('verdict')
        if verdict not in ('live', 'unverifiable', 'exited'):
            raise ValueError('Unknown liveness verdict')
        if verdict == 'unverifiable' and liveness.get('reason') != 'unsupervised_settled':
            categories.append('stale' if liveness.get('reason') == 'stale_status' else 'unverifiable')
        if verdict != 'exited' and facts.get('outcome') in ('outcome_unknown', 'finished_unverified'):
            if 'unverifiable' not in categories:
                categories.append('unverifiable')
    if facts.get('is_root') and facts.get('outcome') == 'succeeded':
        categories.append('root_completion')
    return {'categories': categories, 'requires_action': any(c in ACTION_CATEGORIES for c in categories)}
