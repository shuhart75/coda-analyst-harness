"""Bind plan reviews to one local input and reject cross-workspace tool reads."""
import hashlib
import json
from pathlib import Path
import shutil


def prepare(root, packet, candidate, decision):
    target = Path(root) / '.review-input'
    target.mkdir(mode=0o700)
    shutil.copytree(packet, target / 'packet')
    shutil.copyfile(candidate, target / 'plan.json')
    if Path(decision).is_file():
        shutil.copyfile(decision, target / 'decision.md')
    return hashlib.sha256((target / 'plan.json').read_bytes()).hexdigest()


def harmless_shell_call(item):
    """Exact literals only: never parse, normalize or execute model commands."""
    args = item.get('input')
    return (item.get('name') == 'run_shell_command' and isinstance(args, dict)
            and set(args) <= {'command', 'description'}
            and args.get('command') in ('true', 'echo no'))


class ProtocolError(ValueError):
    """Review evidence cannot be used; a fresh reviewer attempt may repair it."""


def no_data(item):
    """Recognize exact tool outcomes, never infer no disclosure from is_error alone."""
    if not isinstance(item, dict):
        return False
    body = item.get('content')
    if not isinstance(body, str):
        return False
    if item.get('is_error') is False:
        return body.strip() in ('No files found', 'No matches found')
    return (item.get('is_error') is True and
            ((body.startswith('Filesystem Guard denied ') and 'root_not_found' in body
              and body.endswith('check that the search path exists'))
             or body.lower().startswith('file not found:')
             or body.lower().startswith('error listing directory: enoent:')))


def audit(root, events, expected):
    root = Path(root).resolve()
    candidate = root / '.review-input/plan.json'
    if hashlib.sha256(candidate.read_bytes()).hexdigest() != expected:
        raise ProtocolError('Pinned review candidate changed')
    read_candidate = False
    harmless_failures = []
    notices = []
    records = [json.loads(line) for line in Path(events).read_text().splitlines() if line.strip()]
    results = {}
    for event in records:
        if (not isinstance(event, dict) or not isinstance(event.get('message', {}), dict)
                or not isinstance(event.get('message', {}).get('content', []), list)):
            raise ProtocolError('Malformed reviewer stream event')
        if event.get('parent_tool_use_id') is not None:
            raise ProtocolError('Unexpected child reviewer session')
        for item in event.get('message', {}).get('content', []):
            if isinstance(item, dict) and item.get('type') == 'tool_result':
                results.setdefault(item.get('tool_use_id'), []).append(item)
    keys = {'read_file': ('file_path',), 'list_directory': ('path', 'directory_path'),
            'glob': ('path', 'pattern'), 'grep_search': ('path', 'include_pattern', 'glob')}
    for event in records:
        for item in event.get('message', {}).get('content', []):
            if not isinstance(item, dict) or item.get('type') != 'tool_use':
                continue
            name = item.get('name')
            if len(results.get(item.get('id'), [])) != 1:
                raise ProtocolError('Reviewer tool result missing or ambiguous: ' + str(item.get('id')))
            if harmless_shell_call(item):
                continue
            if name not in keys:
                raise ProtocolError('Plan review used a disallowed tool: ' + str(name))
            returned = results.get(item.get('id'), [])
            empty = len(returned) == 1 and no_data(returned[0])
            if empty and returned[0].get('is_error') is True:
                harmless_failures.append(item.get('id'))
            args = item.get('input')
            if not isinstance(args, dict):
                raise ProtocolError('Plan review tool arguments unavailable')
            for key in keys[name]:
                value = args.get(key)
                if value is None:
                    continue
                if not isinstance(value, str):
                    raise ProtocolError('Invalid review path')
                path = (root / value).resolve()
                if not path.is_relative_to(root) and empty:
                    notices.append({'call': item.get('id'), 'tool': name, 'path': value,
                                    'outcome': 'no data returned'})
                    continue
                if not path.is_relative_to(root):
                    raise ProtocolError('Plan review read outside its pinned workspace: '
                                     + str(name) + ' call=' + str(item.get('id', 'unknown'))
                                     + ' ' + key + '=' + value)
                if (name == 'read_file' and key == 'file_path' and path == candidate
                        and not empty and len(returned) == 1 and returned[0].get('is_error') is False
                        and bool(returned[0].get('content'))):
                    read_candidate = True
            if name == 'read_file' and 'file_path' not in args:
                raise ProtocolError('Review read has no explicit path')
    if not read_candidate:
        raise ProtocolError('Reviewer did not read the pinned candidate')

    return {'no_data_calls': harmless_failures, 'notices': notices}
