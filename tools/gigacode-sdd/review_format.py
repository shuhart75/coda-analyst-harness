"""Lossless presentation normalization; never infer a verdict or an owner."""
import json
import re


def response_object(text):
    """One complete object, allowing Markdown checkboxes in surrounding prose.

    Never skip a broken object, choose between objects or extract nested fragments.
    The generic executor/control parser deliberately retains its stricter contract.
    """
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate review field: ' + key)
            value[key] = item
        return value
    decoder = json.JSONDecoder(object_pairs_hook=unique)
    text = text.strip()
    if '```' in text:
        match = re.fullmatch(r'(?P<before>.*?)^```(?:json)?[^\S\n]*\n'
                             r'(?P<body>.*?)^```[^\S\n]*(?:\n(?P<after>.*))?',
                             text, re.MULTILINE | re.DOTALL)
        if not match or '```' in match['body']:
            raise ValueError('Review must contain one unambiguous JSON object')
        value = decoder.decode(match['body'].strip())
        outside = [match['before'], match['after'] or '']
    else:
        start = text.find('{')
        if start < 0:
            raise ValueError('Review must contain a JSON object')
        value, end = decoder.raw_decode(text[start:])
        outside = [text[:start], text[start + end:]]
    # Only checklist markers are presentation; arrays, extra objects and fences
    # outside the selected object remain ambiguous and must be repaired.
    surrounding = ''.join(re.sub(r'\[(?:[xX ]|)\]', '', part) for part in outside)
    if any(c in surrounding for c in ('{', '}', '[', ']', '```')) or not isinstance(value, dict):
        raise ValueError('Review must contain one unambiguous JSON object')
    return value


def read_response(result, model=None):
    """Transport failures are classified before any attempt to decode a verdict."""
    from controller import RetryableSessionError, Blocked
    from session_outcome import classify
    from planning_cycle import verify_model
    outcome = classify(result, 'review')
    if outcome['action'] not in ('validate', 'validate-feedback'):
        raise RetryableSessionError('Reviewer session failed: ' + outcome['cause'], result, cause=outcome['cause'])
    if model:
        verify_model(result, model)
    try:
        report = response_object(result['text'])
        if not isinstance(report, dict):
            raise ValueError('Review response must be an object')
        report = normalize(report)
    except (ValueError, TypeError, Blocked) as exc:
        raise RetryableSessionError('Malformed reviewer JSON: ' + str(exc), result) from exc
    if outcome['action'] == 'validate-feedback' and report.get('verdict') != 'changes':
        raise RetryableSessionError('Unresolved tool errors permit negative feedback only', result, cause='tool-errors')
    return report


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
