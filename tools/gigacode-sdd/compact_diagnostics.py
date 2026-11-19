"""Bounded text export of a current failure, not an archive of all run history."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone

MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 192 * 1024
MAX_SESSIONS = 8
MAX_CYCLES = 3
MAX_ATTEMPTS = 24
HEADER = ('GigaCode SDD compact diagnostics (UTF-8)\n'
          'Компактный отчёт: текущий сбой и связанные попытки. Это не полный архив.\n'
          'Проекции, сокращения и пропуски отмечены явно; исходные журналы сохранены локально.\n'
          'Бинарные файлы, патчи, снимки рабочих копий и исходники обвязки не включены.\n'
          'Текст не обезличен; автоматической отправки нет.\n\n')
OMIT_KEYS = {'workspace_snapshot', 'protected_snapshots', 'protected_git_controls',
             'clone_report', 'packet_files', 'snapshot', 'evidence_files'}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def clip(text, limit=4096):
    raw = str(text).encode('utf-8')
    if len(raw) <= limit:
        return str(text)
    return raw[:limit].decode('utf-8', 'ignore') + '\n[truncated]'


def section(name, text):
    from diagnostics import write_section
    stream = io.StringIO()
    write_section(stream, name, text)
    return stream.getvalue()


class Export:
    def __init__(self, root, error):
        self.root = root
        self.parts = []
        self.size = len(HEADER.encode('utf-8'))
        self.contents = {}
        self.sources = {}
        self.sessions = []
        self.error_sessions = []
        self.failure_records = set()
        self.packet_refs = set()
        self.omitted = []
        self.omitted_count = 0
        self.error = clip(error, 12000) if error is not None else None

    def skip(self, name, reason):
        self.omitted_count += 1
        if len(self.omitted) < 40:
            self.omitted.append({'path': clip(name, 500), 'reason': reason})

    def safe(self, path):
        path = Path(path)
        try:
            rel = path.relative_to(self.root)
        except ValueError:
            return False
        if '..' in rel.parts:
            return False
        return (not any((self.root / Path(*rel.parts[:i])).is_symlink()
                        for i in range(1, len(rel.parts) + 1)) and path.is_file())

    def read(self, path):
        path = Path(path)
        name = 'run/' + path.relative_to(self.root).as_posix()
        if not self.safe(path) or path.suffix == '.patch':
            self.skip(name, 'not an allowed regular text source')
            return None
        try:
            if path.stat().st_size > MAX_SOURCE_BYTES:
                self.skip(name, 'source exceeds read limit; retained locally')
                return None
            data = path.read_bytes()
        except OSError as exc:
            self.skip(name, 'read failed: ' + type(exc).__name__)
            return None
        self.sources[name] = {'bytes': len(data), 'sha256': sha(data)}
        try:
            text = data.decode('utf-8')
            if any(ord(c) < 32 and c not in '\t\r\n' for c in text) or '\x7f' in text:
                raise ValueError('control characters')
        except (ValueError, UnicodeError):
            self.skip(name, 'not plain UTF-8 text')
            return None
        return text

    def add(self, name, text):
        data = text.encode('utf-8')
        identity = sha(data)
        if identity in self.contents:
            return {'section': self.contents[identity], 'sha256': identity, 'deduplicated': True}
        if len(data) > MAX_TEXT_BYTES:
            self.skip(name, 'section shortened; original retained locally')
            text = encoded({'truncated': True, 'original_bytes': len(data), 'original_sha256': identity,
                            'head': clip(text, MAX_TEXT_BYTES // 2),
                            'tail': data[-MAX_TEXT_BYTES // 2:].decode('utf-8', 'ignore')})
            name += '.excerpt.json'
        block = section(name, text)
        # Reserve a fixed allowance for the bounded manifest and header.
        if self.size + len(block.encode('utf-8')) > MAX_REPORT_BYTES - 256 * 1024:
            self.skip(name, 'report size limit; original retained locally')
            return {'omitted': True, 'sha256': identity, 'bytes': len(data)}
        self.parts.append(block)
        self.size += len(block.encode('utf-8'))
        self.contents[identity] = name
        return {'section': name, 'sha256': identity}

    def payload(self, text):
        # Report bodies occur in attempts, receipts and terminal events. Store once.
        self.packet_refs.update(re.findall(r'\.review-input/packet/([A-Za-z0-9_./-]+)', text))
        return self.add('payloads/' + sha(text.encode('utf-8')) + '.txt', text)

    def local_session(self, value):
        if not isinstance(value, str):
            return
        # Accept only a log reference owned by this run; never follow another root.
        path = Path(value)
        if not path.is_absolute():
            path = self.root / path
        try:
            rel = path.relative_to(self.root / 'logs')
        except ValueError:
            return
        if len(rel.parts) == 2 and '..' not in rel.parts and self.safe(path):
            folder = path.parent
            if folder not in self.sessions:
                self.sessions.append(folder)

    def failure_sessions(self, value):
        """Follow only exact log references in controller errors, never arbitrary paths."""
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('error', 'last_error') and isinstance(item, str):
                    pattern = re.escape(str(self.root)) + r'[\\/]logs[\\/][^\\/\r\n]+[\\/](?:stdout\.jsonl|recovery-binding\.json)'
                    for match in re.finditer(pattern, item):
                        path = Path(match.group())
                        if self.safe(path) and path.parent not in self.error_sessions:
                            self.error_sessions.append(path.parent)
                    pattern = (re.escape(str(self.root))
                               + r'[\\/]execution-cycles[\\/][a-f0-9]{64}[\\/]fatal\.json')
                    for match in re.finditer(pattern, item):
                        path = Path(match.group())
                        if path in self.failure_records or len(self.failure_records) >= 8 or not self.safe(path):
                            continue
                        self.failure_records.add(path)
                        try:
                            self.failure_sessions(json.loads(self.read(path) or '{}'))
                        except ValueError:
                            pass
                elif isinstance(item, (list, dict)):
                    self.failure_sessions(item)
        elif isinstance(value, list):
            for item in value:
                self.failure_sessions(item)

    def arguments(self, value):
        text = encoded(value)
        if len(text.encode('utf-8')) <= 2048:
            return value
        return {'truncated': True, 'bytes': len(text.encode('utf-8')),
                'sha256': sha(text.encode('utf-8')), 'excerpt': clip(text, 2048)}

    def thin(self, value, depth=0):
        if depth > 12:
            return {'omitted': 'nesting limit'}
        if isinstance(value, dict):
            result = {}
            for key, item in list(value.items())[:160]:
                if key in OMIT_KEYS:
                    result[key] = {'omitted': 'local integrity snapshot or file inventory'}
                elif key == 'events_path':
                    self.local_session(item)
                    result[key] = item
                elif key in ('text', 'result') and isinstance(item, str) and item:
                    result[key] = self.payload(item)
                elif key in ('tool_calls', 'tool_results') and isinstance(item, (list, dict)):
                    result[key] = {'count': len(item) if isinstance(item, (list, dict)) else None,
                                   'details': 'selected session event projections'}
                elif key in ('review', 'original_review', 'report') and isinstance(item, dict):
                    result[key] = self.payload(encoded(item))
                else:
                    result[key] = self.thin(item, depth + 1)
            if len(value) > 160:
                result['_omitted_keys'] = len(value) - 160
            return result
        if isinstance(value, list):
            result = [self.thin(x, depth + 1) for x in value[:80]]
            if len(value) > 80:
                result.append({'omitted_items': len(value) - 80})
            return result
        return clip(value, 12000) if isinstance(value, str) else value

    def json_file(self, path, *, raw=False):
        text = self.read(path)
        if text is None:
            return None
        rel = path.relative_to(self.root).as_posix()
        try:
            value = json.loads(text)
        except ValueError:
            self.add('compact/' + rel + '.malformed.txt', clip(text))
            return None
        self.failure_sessions(value)
        self.add(('run/' if raw else 'compact/') + rel, text if raw else encoded(self.thin(value)))
        return value

    def session(self, folder, include_prompt=False, summary_only=False):
        if (folder/'activity.json').exists():
            self.json_file(folder/'activity.json')
        binding = folder/'recovery-binding.json'
        if binding.exists():
            self.json_file(binding)
        path = folder / 'stdout.jsonl'
        text = self.read(path) if path.exists() else None
        if text is not None:
            rows, counts, messages = [], {}, []
            calls, failures, terminals, denial_events, denied_ids = {}, [], [], [], set()
            for number, line in enumerate(text.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError('non-object event')
                except ValueError:
                    rows.append({'line': number, 'malformed_event': clip(line)})
                    continue
                kind = event.get('type', 'unknown')
                if not isinstance(kind, str):
                    rows.append({'line': number, 'malformed_event': clip(line)})
                    continue
                counts[kind] = counts.get(kind, 0) + 1
                denials = event.get('permission_denials')
                if denials:
                    denial_events.append({'line': number, 'type': kind, 'denials': self.arguments(denials)})
                    if isinstance(denials, list):
                        denied_ids.update(d.get('tool_use_id') for d in denials if isinstance(d, dict)
                                          and isinstance(d.get('tool_use_id'), str))
                if kind == 'system' and event.get('subtype') == 'init':
                    rows.append({'line': number, **{k: event.get(k) for k in
                                 ('type', 'subtype', 'model', 'session_id', 'cwd')}})
                elif kind == 'result':
                    result = {k: event.get(k) for k in ('type', 'subtype', 'is_error', 'duration_ms',
                              'num_turns', 'permission_denials', 'session_id', 'parent_tool_use_id')}
                    terminals.append({k: self.arguments(v) for k, v in result.items()})
                    if summary_only:
                        continue
                    result['result'] = self.payload(event['result']) if isinstance(event.get('result'), str) else None
                    rows.append({'line': number, **result})
                content = event.get('message', {}).get('content', []) if isinstance(event.get('message'), dict) else []
                for item in content if isinstance(content, list) else []:
                    if not isinstance(item, dict):
                        continue
                    if item.get('type') == 'tool_use':
                        call = {'id': item.get('id'), 'name': item.get('name'),
                                'input': self.arguments(item.get('input'))}
                        if isinstance(item.get('id'), str):
                            calls[item['id']] = call
                        rows.append({'line': number, 'parent_tool_use_id': event.get('parent_tool_use_id'),
                                     'type': 'tool_use', **call})
                    elif item.get('type') == 'tool_result':
                        body = item.get('content')
                        body_text = body if isinstance(body, str) else encoded(body)
                        if item.get('is_error'):
                            failures.append({'line': number, 'tool_use_id': item.get('tool_use_id'),
                                             'error': clip(body_text, 2048)})
                        rows.append({'line': number, 'type': 'tool_result',
                                     'tool_use_id': item.get('tool_use_id'), 'is_error': item.get('is_error'),
                                     'content_bytes': len(body_text.encode('utf-8')), 'content_sha256': sha(body_text.encode('utf-8')),
                                     **({'error_content': clip(body_text)} if item.get('is_error') else {})})
                    elif item.get('type') == 'text':
                        messages.append(clip(item.get('text', ''), 1000))
            if summary_only:
                selected = failures if len(failures) <= 12 else failures[:6] + failures[-6:]
                ids = {item['tool_use_id'] for item in selected if isinstance(item.get('tool_use_id'), str)} | denied_ids
                self.add('compact/logs/' + folder.name + '/failure-summary.json', encoded({
                    'source': 'run/logs/' + folder.name + '/stdout.jsonl', 'event_counts': counts,
                    'terminal_results': terminals[-2:], 'failed_tools_total': len(failures),
                    'denial_events': denial_events[-12:], 'denial_events_omitted': max(0, len(denial_events)-12),
                    'failures_omitted': max(0, len(failures)-len(selected)), 'failed_tools': selected,
                    'related_calls': [value for key, value in calls.items() if key in ids][:24],
                    'last_messages': messages[-3:]}))
                return
            if len(rows) > 500:
                self.skip(str(path), 'event projection shortened to first 30 and last 470 relevant events')
                rows = rows[:30] + rows[-470:]
            self.add('compact/logs/' + folder.name + '/events.json', encoded(
                {'projection': True, 'source': 'run/logs/' + folder.name + '/stdout.jsonl',
                 'event_counts': counts, 'last_messages': messages[-3:], 'events': rows,
                 'excluded': 'thinking and successful read contents; full originals remain local'}))
        if summary_only:
            return
        for filename in (('prompt.txt', 'stderr.log', 'output.log') if include_prompt else ('stderr.log', 'output.log')):
            path = folder / filename
            if path.exists():
                content = self.read(path)
                if content:
                    self.add('run/logs/' + folder.name + '/' + filename, content)


def newest(paths):
    return sorted(paths, key=lambda p: (p.stat().st_mtime_ns, p.as_posix()), reverse=True)


def current_candidates(out, tasks):
    """Reserve bounded current-step evidence before optional historical payloads."""
    base = out.root/'execution-cycles'
    if not base.is_dir() or base.is_symlink():
        return []
    owners = {value.get('workspace'): key for key, value in tasks.items()
              if isinstance(value, dict) and value.get('workspace')
              and value.get('status') not in ('done', 'accepted', 'skipped')}
    paths = []
    for folder, dirs, names in os.walk(base, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(folder)/d).is_symlink()]
        if 'candidate.json' in names and out.safe(Path(folder)/'candidate.json'):
            paths.append(Path(folder)/'candidate.json')
    selected, seen, sessions = [], set(), []
    for path in newest(paths):
        try:
            value = json.loads(out.read(path) or '{}')
        except ValueError:
            out.skip(str(path), 'candidate is not valid JSON')
            continue
        if not isinstance(value, dict):
            continue
        task = owners.get(value.get('origin_workspace')) or owners.get(value.get('workspace'))
        if owners and task is None:
            continue
        if value.get('status') == 'accepted':
            continue
        identity = (task or path.parent.parent.name, value.get('model'))
        if identity in seen:
            continue
        seen.add(identity)
        if len(selected) >= 24:
            out.skip(str(path), 'current candidate count limit')
            continue
        selected.append(path)
        def bounded(item, limit=2048):
            # Inline, explicitly clipped diagnostics: no payload expansion here.
            text = encoded(item)
            return item if len(text.encode('utf-8')) <= limit else {'truncated': True, 'excerpt': clip(text, limit)}
        history = value.get('history', [])
        history = history if isinstance(history, list) else []
        history = [h for h in history if isinstance(h, dict)]
        recent = [{key: bounded(h[key], 2048 if key == 'detail' else 256) for key in
                   ('kind', 'detail', 'stage', 'step', 'step_failures', 'total_failures', 'result', 'decision') if key in h}
                  for h in history[-6:]]
        result = value.get('session_result') or {}
        result = result if isinstance(result, dict) else {}
        projection = {'projection': True, 'source': 'run/' + path.relative_to(out.root).as_posix(),
            'task': task, **{key: bounded(value[key], 4096 if key in ('feedback', 'retry_steps') else 512) for key in
                ('model', 'status', 'stage', 'attempts', 'failures', 'runtime_revision', 'retry_steps',
                 'feedback', 'resource_wait', 'recovery_wait') if key in value},
            'recent_failures': recent, 'history_omitted': max(0, len(history)-6),
            'review_draft': bounded(value.get('review_draft'), 12000),
            'session': {key: bounded(result[key]) for key in
                ('returncode', 'timed_out', 'cancelled', 'errors', 'events_path') if key in result},
            'note': 'Support projection only; original checkpoints remain unchanged.'}
        name = 'compact/current-execution/' + path.parent.parent.name + '/' + path.parent.name + '.json'
        out.add(name, encoded(projection))
        before = len(out.error_sessions)
        for h in history[-6:]:
            if isinstance(h.get('detail'), str):
                out.failure_sessions({'error': h['detail']})
        sessions.extend(out.error_sessions[before:])
    return list(dict.fromkeys(sessions))


def collect_compact(config, destination=None, error=None):
    config = Path(config).absolute()
    root = config.parent
    home = root.parent.parent if root.parent.name == 'runs' else root
    destination = Path(destination) if destination else home / 'diagnostics/latest-diagnostics.txt'
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    out = Export(root, error)
    try:
        state = json.loads(out.read(root/'state.json') or '{}') if (root/'state.json').exists() else {}
    except ValueError:
        state = {}
    state = state if isinstance(state, dict) else {}
    out.failure_sessions(state)
    out.failure_sessions({'error': str(error)})
    for name in ('progress.json',):
        if (root/name).is_file():
            out.json_file(root/name)
    for path in sorted((root/'operator-questions').glob('*.json')):
        if out.safe(path):
            out.json_file(path)
    for path in sorted((root/'managed-services').glob('*/service.json')):
        if out.safe(path):
            out.json_file(path)
    tasks = state.get('tasks', {})
    if isinstance(tasks, dict):
        out.add('compact/task-status.json', encoded({key: {
            'status': value.get('status'), 'error': value.get('error')}
            for key, value in tasks.items() if isinstance(value, dict)}))
    current_sessions = current_candidates(out, tasks if isinstance(tasks, dict) else {})
    logs = root/'logs'
    recent = []
    folders = []
    if logs.is_dir() and not logs.is_symlink():
        folders = [p for p in logs.iterdir() if p.is_dir() and not p.is_symlink()]
        recent = newest([p for p in folders if (p/'stdout.jsonl').is_file() or (p/'output.log').is_file()])
    # Current failure evidence gets space before historical reviews and plan bodies.
    active_sessions = []
    if isinstance(tasks, dict):
        for task, value in tasks.items():
            if isinstance(value, dict) and value.get('status') not in ('done', 'accepted', 'skipped', 'pending'):
                active_sessions.extend([p for p in recent if p.name.startswith(task + '-')][:2])
    priority_sessions = list(dict.fromkeys(current_sessions + active_sessions + out.error_sessions + recent[:2]))[:MAX_SESSIONS]
    for folder in priority_sessions:
        out.session(folder, summary_only=True)
    if (root/'state.json').exists():
        out.json_file(root/'state.json', raw=True)
    for dirname in ('preserved-processes', 'preserved-services'):
        for path in newest(list((root/dirname).glob('*.json')))[:8]:
            if out.safe(path):
                out.json_file(path)
    out.json_file(config)
    for name in ('planning-portfolio.json', 'review-ledger.json'):
        if (root/name).exists():
            out.json_file(root/name)
    candidate_record = state.get('plan_candidate')
    candidate = candidate_record.get('file', 'plan-candidate.json') if isinstance(candidate_record, dict) else 'plan-candidate.json'
    selection = state.get('plan_selection') or {}
    selected_candidate = selection.get('candidate') if isinstance(selection, dict) else None
    if isinstance(selected_candidate, str) and out.safe(Path(selected_candidate)):
        out.json_file(Path(selected_candidate), raw=True)
    elif isinstance(candidate, str) and re.fullmatch(r'plan-candidate(?:-[a-f0-9]{32})?\.json', candidate):
        if (root/candidate).exists():
            out.json_file(root/candidate, raw=True)
    for name in ('decision.md', 'input/requirements.md', 'readiness.json'):
        path = root/name
        if path.exists():
            text = out.read(path)
            if text is not None:
                out.add('run/' + name, text)
    inventory = {}
    for dirname in ('execution-cycles', 'review-cycles', 'planning-cycles'):
        base = root/dirname
        if not base.is_dir() or base.is_symlink():
            continue
        records = []
        for folder, dirs, names in os.walk(base, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(folder)/d).is_symlink()]
            records.extend(Path(folder)/n for n in names if n.endswith('.json') and out.safe(Path(folder)/n))
        ordered = newest(records)
        groups = []
        for path in ordered:
            group = path.relative_to(base).parts[0]
            if group not in groups:
                groups.append(group)
        # Errors naming a particular cycle take precedence over unrelated recent work.
        groups.sort(key=lambda group: (0 if group in str(error) else 1))
        selected_groups = groups[:MAX_CYCLES]
        queues = [[p for p in ordered if p.relative_to(base).parts[0] == group
                   and (p.name.startswith('attempt-') or p.name in
                        ('candidate.json', 'fatal.json', 'operational.json', 'operational-recovery.json', 'recovery-attempts.json',
                         'selection.json', 'generation.json', 'plan.json',
                         'failure.json', 'comparison.json'))] for group in selected_groups]
        # Round-robin prevents a busy candidate from hiding its parallel peers.
        selected = []
        while any(queues):
            for queue in queues:
                if queue:
                    selected.append(queue.pop(0))
        inventory[dirname] = {'json_files_total': len(records), 'selected_attempts': min(len(selected), MAX_ATTEMPTS),
                              'cycles_selected': selected_groups, 'old_history': 'retained locally'}
        for path in selected[:MAX_ATTEMPTS]:
            out.json_file(path)
    if folders:
        # Always include the newest interrupted session, even before its receipt is saved.
        sessions = list(dict.fromkeys(priority_sessions + out.error_sessions + out.sessions))
        inventory['logs'] = {'sessions_total': len(folders), 'selected': len(sessions[:MAX_SESSIONS]),
                             'older_sessions': 'retained locally, not exported'}
        for index, folder in enumerate(sessions[:MAX_SESSIONS]):
            out.session(folder, include_prompt=index == 0)
    for rel in sorted(out.packet_refs)[:16]:
        if '..' in Path(rel).parts or Path(rel).is_absolute():
            continue
        path = root/'input'/rel
        if path.exists():
            text = out.read(path)
            if text is not None:
                out.add('run/input/' + rel, text)
    # Code identity suffices for matching the installed runner; never embed all tests/source.
    runner = {p.name: sha(p.read_bytes()) for p in Path(__file__).parent.glob('*.py') if not p.is_symlink()}
    summary = {'format': 'compact-v1', 'run': root.name, 'created_at': datetime.now(timezone.utc).isoformat(),
               'error': out.error, 'max_report_bytes': MAX_REPORT_BYTES, 'inventory': inventory,
               'sources': dict(list(out.sources.items())[:140]), 'runner_sha256': runner,
               'omitted_count': out.omitted_count, 'omitted_examples': out.omitted,
               'note': 'Projection for support, not replayable evidence. Full originals and checkpoints remain local.'}
    report = HEADER + section('summary.json', encoded(summary)) + ''.join(out.parts)
    data = report.encode('utf-8')
    if len(data) > MAX_REPORT_BYTES:
        # Metadata is bounded too; keep the current error and explicit limit rather than fail.
        summary.pop('sources', None)
        summary['metadata_shortened'] = True
        report = HEADER + section('summary.json', encoded(summary)) + ''.join(out.parts)
        data = report.encode('utf-8')
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError('Compact diagnostic size invariant exceeded')
    fd, temp = tempfile.mkstemp(prefix='.latest-', suffix='.txt', dir=destination.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(temp, destination)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return destination
