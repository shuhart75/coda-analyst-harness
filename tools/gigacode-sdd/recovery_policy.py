"""Pure recovery decisions; facts come from audited controller events, not prose.

Inspired by Paperclip's separate policy/domain and durable recovery state.
No upstream code or dependencies copied. This policy never accepts a result.
"""
import math


def decide(stage, cause):
    if stage not in ('execute', 'checks', 'review', 'adjudicate'):
        raise ValueError('Unknown recovery stage')
    if cause == 'cancelled':
        return {'action': 'pause', 'kind': None, 'instruction_change': False}
    if cause in ('cleanup', 'integrity', 'external'):
        return {'action': 'block', 'kind': None, 'instruction_change': False}
    if cause in ('transport', 'timeout'):
        return {'action': 'retry-later', 'kind': 'transport', 'instruction_change': False}
    if cause == 'resource-busy':
        return {'action': 'wait-resource', 'kind': None, 'instruction_change': False}
    if cause == 'request-protocol':
        return {'action': 'repair-response', 'kind': 'request-protocol', 'instruction_change': True}
    if cause == 'snapshot':
        return {'action': 'repair-implementation', 'kind': 'snapshot', 'instruction_change': True}
    if stage in ('review', 'adjudicate'):
        kind = 'review-context' if cause == 'context-limit' else 'review-protocol'
        return {'action': 'repair-response', 'kind': kind, 'instruction_change': True}
    kind = {'checks': 'checks', 'finding': 'review', 'model': 'model'}.get(cause)
    if kind:
        return {'action': 'repair-implementation' if kind != 'model' else 'repair-model',
                'kind': kind, 'instruction_change': True}
    raise ValueError('Unsupported recovery cause')


def retry_at(now, failures, base_seconds=5):
    if (not isinstance(base_seconds, (int, float)) or isinstance(base_seconds, bool)
            or not math.isfinite(base_seconds) or not 0 <= base_seconds <= 60):
        raise ValueError('recovery_retry_seconds must be between 0 and 60')
    return now + min(60, base_seconds * 2 ** min(max(failures - 1, 0), 4))


def wait_until(deadline, stop, clock):
    """A restart keeps the recorded deadline; cancellation never spends a retry."""
    while deadline > clock():
        if stop.wait(min(1, max(0, deadline - clock()))):
            return False
    return not stop.is_set()


def transport_cause(result):
    """Only transport metadata, never error-looking text inside tool payloads."""
    if not result:
        return None
    if (result.get('cleanup') or {}).get('preserved'):
        return 'cleanup'
    if result.get('cancelled'):
        return 'cancelled'
    if result.get('timed_out'):
        return 'timeout'
    if result.get('returncode') not in (None, 0) or result.get('terminal_result') is False:
        return 'transport'
    return None
