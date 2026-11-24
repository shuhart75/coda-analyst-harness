"""Next steps for an audited session, never proof that a task is accepted.

Call only after controller workspace/source/service audits or receipt integrity
validation. Candidate ownership, model identity, mandatory checks and independent
review remain separate gates. A tool refusal does not authorize retrying that tool.
"""
from transport import execution_result_usable, review_diagnostic_errors


def classify(result, role):
    if role not in ('execute', 'review'):
        raise ValueError('Unknown session role')
    if (result.get('cleanup') or {}).get('preserved'):
        return {'action': 'stop-cleanup', 'cause': 'cleanup', 'incomplete': True}
    if result.get('cancelled'):
        return {'action': 'cancel', 'cause': 'cancelled', 'incomplete': True}
    errors = result.get('errors', [])
    if not isinstance(errors, list) or not all(isinstance(error, str) for error in errors):
        return {'action': 'retry', 'cause': 'malformed-transport', 'incomplete': True}
    context = any('CLI context exhausted' in error for error in errors)
    provider = result.get('provider_failure')
    incomplete = (result.get('timed_out') or context or provider or not result.get('terminal_result')
                  or result.get('returncode') != 0)
    cause = ('context-limit' if context else provider if provider else 'timeout' if result.get('timed_out') else
             'transport' if incomplete else 'tool-errors' if errors or result.get('permission_denials') else 'complete')
    if not incomplete and result.get('ok') and not errors and not result.get('permission_denials'):
        action = 'check' if role == 'execute' else 'validate'
    elif role == 'execute':
        # A timed-out/context-exhausted executor may have left useful code. Only
        # audit + model identity + real checks + independent review can accept it.
        # No terminal prose is promoted to proof of completed implementation.
        salvage = (result.get('returncode') is not None and
                   (result.get('timed_out') or context) and result.get('event_count', 0) > 0
                   and not any(error.startswith('Malformed stream event') for error in errors))
        action = 'check' if salvage or execution_result_usable(result) else 'retry'
    else:
        action = ('validate' if not incomplete and
                  (review_diagnostic_errors(result) is not None) else
                  'validate-feedback' if not incomplete and execution_result_usable(result) else 'retry')
    return {'action': action, 'cause': cause, 'incomplete': bool(incomplete)}
