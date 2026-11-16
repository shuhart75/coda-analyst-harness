"""Bind review evidence to pinned input without vetoing tool preferences."""
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


def output_texts(value):
    """Inspect recorded output only. Never execute or interpret shell source."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from output_texts(item)
    elif isinstance(value, dict):
        for key in ('text', 'output', 'stdout', 'content'):
            if key in value:
                yield from output_texts(value[key])


class ProtocolError(ValueError):
    """Review evidence cannot be used; a fresh reviewer attempt may repair it."""


def path_instructions(root):
    return ('Tool paths must stay inside this exact workspace root: ' + json.dumps(str(Path(root).resolve()))
            + '. Use relative paths where supported. If a tool requires an absolute path, '
            'join this exact root with the relative path; never guess another root or search parents. '
            'Paths in the returned JSON evidence remain repository-relative.')


def path_argument_rejected(item, args, name):
    """A known local argument rejection occurs before any filesystem read."""
    key = {'read_file': 'file_path', 'list_directory': 'path'}.get(name)
    if name == 'list_directory' and 'path' not in args:
        key = 'directory_path'
    value = args.get(key)
    return (isinstance(value, str) and bool(value) and not Path(value).is_absolute()
            and item.get('is_error') is True
            and item.get('content') == 'Path must be absolute: ' + value)


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
    candidate_bytes = candidate.read_bytes()
    if hashlib.sha256(candidate_bytes).hexdigest() != expected:
        raise ProtocolError('Pinned review candidate changed')
    candidate_text = candidate_bytes.decode('utf-8').strip()
    read_candidate = False
    harmless_failures = []
    recoverable_errors = []
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
            returned = results.get(item.get('id'), [])
            empty = len(returned) == 1 and no_data(returned[0])
            if empty and returned[0].get('is_error') is True:
                harmless_failures.append(item.get('id'))
            args = item.get('input')
            if not isinstance(args, dict):
                raise ProtocolError('Plan review tool arguments unavailable')
            if not isinstance(name, str) or not name:
                raise ProtocolError('Plan review tool name unavailable')
            if name not in keys:
                # Tool preference is not evidence quality. Source/workspace,
                # Git controls, services and permission denials are checked by
                # the session receipt before any report may be reused.
                for key in ('cwd', 'workdir', 'file_path', 'path'):
                    value = args.get(key)
                    if value is not None and (not isinstance(value, str)
                            or not (root / value).resolve().is_relative_to(root)):
                        raise ProtocolError('Plan review tool has an outside workspace path: ' + key + '=' + str(value))
                if returned[0].get('is_error') is True:
                    body = '\n'.join(output_texts(returned[0].get('content'))).lower()
                    if any(token in body for token in ('denied', 'permission', 'approval', 'confirmation', 'forbidden')):
                        raise ProtocolError('Reviewer tool reported a permission/guard denial')
                    recoverable_errors.append(item.get('id'))
                elif returned[0].get('is_error') is False and candidate_text and any(
                        candidate_text in text for text in output_texts(returned[0].get('content'))):
                    # A full pinned candidate in successful tool output is also
                    # evidence of reading it, e.g. cat through a shell tool.
                    read_candidate = True
                continue
            argument_rejected = path_argument_rejected(returned[0], args, name)
            if argument_rejected:
                harmless_failures.append(item.get('id'))
                notices.append({'call': item.get('id'), 'tool': name,
                                'outcome': 'local path argument rejected before read'})
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
                        and not empty and not argument_rejected and len(returned) == 1 and returned[0].get('is_error') is False
                        and bool(returned[0].get('content'))):
                    read_candidate = True
            if name == 'read_file' and 'file_path' not in args:
                raise ProtocolError('Review read has no explicit path')
    if not read_candidate:
        raise ProtocolError('Reviewer did not read the pinned candidate')

    return {'no_data_calls': harmless_failures, 'recoverable_error_calls': recoverable_errors, 'notices': notices}
