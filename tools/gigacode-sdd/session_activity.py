"""Bounded, incremental journal telemetry. Activity is not completion evidence."""
import json
import os
import time
from pathlib import Path


class SessionActivity:
    def __init__(self, path, clock=time.monotonic):
        self.path = Path(path)
        self.clock = clock
        self.started = self.last_event = clock()
        self.offset = 0
        self.events = self.calls = self.results = 0
        self.pending = {}
        self.pending_details = {}
        self.recent = []
        self.terminal = False
        self.buffer = b''
        self.discarding = False

    def poll(self):
        # Never reread the whole growing log or retain tool arguments/results.
        with self.path.open('rb') as stream:
            stream.seek(self.offset)
            raw = stream.read(1024 * 1024)
            self.offset += len(raw)
        if raw:
            self.last_event = self.clock()
        for part in raw.splitlines(keepends=True):
            if not self.discarding:
                self.buffer += part
                if len(self.buffer) > 1024 * 1024:
                    self.buffer = b''
                    self.discarding = True
            if not part.endswith(b'\n'):
                continue
            if not self.discarding:
                try:
                    event = json.loads(self.buffer)
                except (ValueError, UnicodeError):
                    event = None
                if isinstance(event, dict):
                    self.observe(event)
            self.buffer = b''
            self.discarding = False
        return self.summary()

    def record(self, action, outcome='', tool=None):
        from prior_context import redact
        import unicodedata
        def clean(text, limit):
            safe = redact(str(text))[0]
            return ' '.join(''.join(c if not unicodedata.category(c).startswith('C') else ' ' for c in safe).split())[:limit]
        item = {'at': time.time(), 'action': clean(action, 500), 'outcome': outcome}
        if tool:
            item['tool'] = clean(tool, 100)
        self.recent = (self.recent + [item])[-8:]

    def observe(self, event):
        self.events += 1
        self.terminal |= event.get('type') == 'result' and not event.get('parent_tool_use_id')
        if event.get('type') == 'result' and not event.get('parent_tool_use_id'):
            self.record('Сессия вернула результат', 'ошибка' if event.get('is_error') else 'ожидает проверки контроллером')
        message = event.get('message')
        content = message.get('content', []) if isinstance(message, dict) else []
        if not isinstance(content, list):
            return
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'tool_use':
                self.calls += 1
                key = item.get('id')
                name = item.get('name') if isinstance(item.get('name'), str) else 'инструмент'
                inputs = item.get('input') if isinstance(item.get('input'), dict) else {}
                # Only selected descriptive fields; never serialize an input/result object.
                detail = next((inputs[k] for k in ('description', 'file_path', 'path', 'command', 'url')
                               if isinstance(inputs.get(k), str)), '')
                action = name + (': ' + detail if detail else '')
                self.record(action, 'вызван; результат ещё не получен', name)
                if isinstance(key, str) and len(self.pending) < 256:
                    self.pending[key] = self.clock()
                    self.pending_details[key] = self.recent[-1]['action']
            elif item.get('type') == 'tool_result':
                self.results += 1
                key = item.get('tool_use_id')
                if isinstance(key, str):
                    self.pending.pop(key, None)
                    action = self.pending_details.pop(key, 'Инструмент')
                    self.record(action, 'ошибка инструмента' if item.get('is_error') else 'ответ инструмента получен')
            elif item.get('type') == 'text' and isinstance(item.get('text'), str):
                self.record(item['text'], 'сообщение модели; не подтверждённый результат')

    def summary(self):
        now = self.clock()
        return {'elapsed_seconds': round(now - self.started, 1),
                'idle_seconds': round(now - self.last_event, 1),
                'events': self.events, 'tool_calls': self.calls, 'tool_results': self.results,
                'pending_calls': len(self.pending), 'recent_events': list(self.recent),
                'pending_actions': list(self.pending_details.values())[:8], 'terminal_result': self.terminal,
                'oldest_pending_seconds': round(now - min(self.pending.values()), 1) if self.pending else 0,
                'rule': 'Journal activity only; not verified task progress'}

    def save(self, summary):
        target = self.path.parent / 'activity.json'
        temporary = target.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(summary, stream)
        temporary.replace(target)
