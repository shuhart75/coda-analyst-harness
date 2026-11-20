"""Bounded retry policies scoped to the failed step, not the whole candidate.

Architectural references (no upstream code copied): CAO durable step identity,
LangGraph per-task RetryPolicy. Existing receipts still own successful work.
"""
PER_STEP_FAILURES = 6
TOTAL_FAILURES = 24
REVIEW_STRATEGIES = (
    'Repair only the reported protocol or evidence defect. Reuse supported observations '
    'from the previous draft after checking them against the current pinned input.',
    'Before further research, make a checklist of every required output field and requirement id. '
    'Fill only missing or invalid entries using short targeted reads; reserve time for final JSON.',
    'Reconstruct the response from the required schema and a compact requirement-to-evidence matrix. '
    'Use actual paths in this workspace. Stop optional research and return the complete JSON now; '
    'report real gaps honestly rather than inventing evidence.',
)


def step_key(stage, kind):
    if kind in ('review-protocol', 'review-context'):
        return stage + ':protocol'
    if kind in ('checks', 'review'):
        return 'implementation:repair'
    return stage + ':' + kind if kind == 'transport' else 'execute:' + kind


def record_failure(state, stage, kind, count, detail=None):
    key = step_key(stage, kind)
    issue = (state.get('review_resolution') or {}).get('active_id') if stage == 'adjudicate' else None
    if issue:
        key = key.replace('adjudicate:', 'adjudicate:' + issue + ':', 1)
    budget = state.setdefault('retry_steps', {}).setdefault(key, {'failures': 0})
    budget['failures'] += 1
    state['failures'] = state.get('failures', 0) + 1
    budget['last_count'] = count
    budget['strategy'] = min(budget['failures'] // 2, 2)
    # Legacy summary fields remain useful to old diagnostic clients.
    state['strategy'] = budget['strategy']
    state['stagnation'] = budget['failures'] % 2
    repeated = 0
    if kind in ('review-protocol', 'review-context', 'request-protocol') and detail is not None:
        from review_format import failure_signature
        signature = failure_signature(stage, kind, detail)
        guard = state.setdefault('repeat_guard', {})
        repeated = guard.get('count', 0) + 1 if guard.get('signature') == signature else 1
        guard.update(signature=signature, count=repeated)
    return {'step': key, 'step_failures': budget['failures'], 'repeated_failure_count': repeated,
            'strategy': budget['strategy'], 'total_failures': state['failures'],
            'exhausted': repeated >= 3 or budget['failures'] >= PER_STEP_FAILURES or state['failures'] >= TOTAL_FAILURES}
