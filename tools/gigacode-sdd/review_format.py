"""Lossless presentation normalization; never infer a verdict or an owner."""
import re


def read_response(result, model=None):
    """Transport failures are classified before any attempt to decode a verdict."""
    from controller import RetryableSessionError, parse_json, Blocked
    from session_outcome import classify
    from planning_cycle import verify_model
    outcome = classify(result, 'review')
    if outcome['action'] != 'validate':
        raise RetryableSessionError('Reviewer session failed: ' + outcome['cause'], result, cause=outcome['cause'])
    if model:
        verify_model(result, model)
    try:
        report = parse_json(result['text'])
        if not isinstance(report, dict):
            raise ValueError('Review response must be an object')
        return normalize(report)
    except (ValueError, TypeError, Blocked) as exc:
        raise RetryableSessionError('Malformed reviewer JSON: ' + str(exc), result) from exc


def label(value):
    if not isinstance(value, str):
        return value
    return value.strip().strip('`*_').strip().lower()


def deferred(items):
    if items is None:
        return []
    if isinstance(items, dict) and 'requirement' in items:
        items = [items]
    if not isinstance(items, list):
        return items
    result = []
    for item in items:
        if not isinstance(item, dict):
            result.append(item)
            continue
        item = dict(item)
        for target, aliases in {'owner': ('assignedTo',), 'evidence': ('gap',), 'reason': ('basis',)}.items():
            supplied = [item[k] for k in (target, *aliases) if k in item]
            if supplied and any(v != supplied[0] for v in supplied):
                raise ValueError('Conflicting deferred evidence fields: ' + target)
            if supplied:
                item[target] = supplied[0]
        result.append(item)
    return result


def normalize(report):
    report = dict(report)
    report['verdict'] = label(report.get('verdict'))
    if 'deferred_evidence' in report:
        report['deferred_evidence'] = deferred(report['deferred_evidence'])
    return report


def failure_signature(stage, kind, detail):
    """Ignore incidental run paths, not the field or requirement being repaired."""
    text = str(detail).split('; inspect ', 1)[0]
    text = re.sub(r'[0-9a-f]{12,64}', '<id>', text)
    return stage + ':' + kind + ':' + text
