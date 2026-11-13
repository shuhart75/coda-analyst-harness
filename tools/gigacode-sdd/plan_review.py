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


def audit(root, events, expected):
    root = Path(root).resolve()
    candidate = root / '.review-input/plan.json'
    if hashlib.sha256(candidate.read_bytes()).hexdigest() != expected:
        raise ValueError('Pinned review candidate changed')
    read_candidate = False
    keys = {'read_file': ('file_path',), 'list_directory': ('path', 'directory_path'),
            'glob': ('path', 'pattern'), 'grep_search': ('path', 'include_pattern', 'glob')}
    for line in Path(events).read_text().splitlines():
        event = json.loads(line)
        for item in event.get('message', {}).get('content', []):
            if not isinstance(item, dict) or item.get('type') != 'tool_use':
                continue
            name = item.get('name')
            if harmless_shell_call(item):
                continue
            if name not in keys:
                raise ValueError('Plan review used a disallowed tool: ' + str(name))
            args = item.get('input')
            if not isinstance(args, dict):
                raise ValueError('Plan review tool arguments unavailable')
            for key in keys[name]:
                value = args.get(key)
                if value is None:
                    continue
                if not isinstance(value, str):
                    raise ValueError('Invalid review path')
                path = (root / value).resolve()
                if not path.is_relative_to(root):
                    raise ValueError('Plan review read outside its pinned workspace')
                if name == 'read_file' and key == 'file_path' and path == candidate:
                    read_candidate = True
            if name == 'read_file' and 'file_path' not in args:
                raise ValueError('Review read has no explicit path')
    if not read_candidate:
        raise ValueError('Reviewer did not read the pinned candidate')
