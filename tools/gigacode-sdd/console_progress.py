"""Compact operator console; all technical messages remain in the run log."""
from contextlib import contextmanager
import os
from pathlib import Path
import time
import tempfile
import unicodedata

from progress import Progress, clipped
from progress_io import write_projection


class ConsoleProgress(Progress):
    def __init__(self, log_path, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.log_path = Path(log_path)
        self.runtime_path = self.log_path.parent.parent / 'console-status.json'
        self.value = None
        self.controller_events = []
        self.last_plain = None
        self.last_notice = float('-inf')
        self.last_save = float('-inf')
        self.runtime_error = False
        self.overview_key = None
        self.include_overview = True
        self.screen_open = False
        self.status_path = self.log_path.parent.parent / 'operator-status.txt'

    def interactive(self):
        return super().interactive()

    def clear(self):
        if self.screen_open:
            self.output.write('\x1b[H')
        self.rows = 0

    def _leave_screen(self):
        if self.screen_open:
            self.output.write('\x1b[?25h\x1b[?1049l')
            self.screen_open = False
        self.rows = 0

    def message(self, message):
        with self.lock:
            from prior_context import redact
            self.controller_events = (self.controller_events + [{'at': time.time(), 'message': clipped(redact(str(message))[0], 600)}])[-8:]
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open('a', encoding='utf-8') as stream:
                os.chmod(self.log_path, 0o600)
                stream.write(time.strftime('[%Y-%m-%d %H:%M:%S] ') + str(message) + '\n')

    def notice(self, message):
        with self.lock:
            self.message(message)
            self._leave_screen()
            Progress.message(self, message)

    def operations(self):
        with self.lock:
            now = self.clock()
            return [{**entry.get('metadata', {}), 'activity': entry.get('activity', {}), 'title': entry['label'], 'elapsed_seconds': max(0, now-entry['start']),
                     'timeout_seconds': entry.get('timeout'),
                     'idle_seconds': (entry['idle'] + now-entry['observed']) if 'idle' in entry else None}
                    for entry in self.active.values()]

    def telemetry(self, token, data):
        with self.lock:
            if token in self.active:
                self.active[token].update(idle=data['idle_seconds'], observed=self.clock(), activity={
                    key: data[key] for key in ('events', 'tool_calls', 'tool_results', 'pending_calls',
                    'oldest_pending_seconds', 'recent_events', 'pending_actions', 'terminal_result') if key in data})

    def _value(self):
        from run_status import read_projection
        current = dict(self.value or {})
        for key, filename in (('observation', 'latest.json'), ('observer_health', 'health.json')):
            path = self.log_path.parent.parent/'observer'/filename
            if path.is_file():
                data = read_projection(path, [])
                if data:
                    current[key] = data
        return dict(current, controller_events=list(self.controller_events), operations=self.operations(), telemetry_stale=False,
                    telemetry_incomplete=self.runtime_error or (self.value or {}).get('telemetry_incomplete', False))

    def _save_runtime(self):
        if self.clock()-self.last_save < 30:
            return
        self.runtime_error = not write_projection(self.runtime_path,
            {'observed_at': time.time(), 'operations': self.operations(), 'controller_events': list(self.controller_events)})
        from run_status import dashboard
        temporary = None
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                    dir=self.status_path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(dashboard(self._value()) + '\n')
            os.replace(temporary, self.status_path)
        except OSError:
            self.runtime_error = True
        finally:
            if temporary and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    self.runtime_error = True
        self.last_save = self.clock()

    def draw(self):
        if not self.interactive():
            return
        from run_status import dashboard
        columns, height = self.dimensions()
        width = max(1, columns - 1)
        lines = dashboard(self._value(), compact=True).splitlines()
        def cells(line):
            return sum(0 if unicodedata.combining(c) else
                       2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in line)
        # Sanitize controls without shortening commands. Let the terminal wrap
        # naturally, so copied shell commands do not acquire hard newlines.
        lines = [clipped(line, max(width, cells(line))) for line in lines]
        occupied = sum(max(1, (cells(line) + columns - 1)//max(1, columns)) for line in lines)
        if occupied > max(1, height - 1):
            lines = [lines[0], f"Принято: {(self.value or {}).get('done', 0)} из {(self.value or {}).get('total', 0)}.",
                     'Увеличьте окно для полного маршрута.',
                     'Полный статус и команда ответа: ' + str(self.status_path)]
            occupied = sum(max(1, (cells(line) + columns - 1)//max(1, columns)) for line in lines)
            if occupied > max(1, height - 1):
                lines = ['Увеличьте окно.' if width >= 15 else '...' if width >= 3 else '']
                occupied = 1
        if not self.screen_open:
            self.output.write('\x1b[?1049h\x1b[?25l')
            self.screen_open = True
        # Clear the previous frame too: shorter lines must not retain old tails.
        self.output.write('\x1b[H\x1b[J' + '\n'.join(lines) + '\x1b[J')
        self.rows = occupied
        self.output.flush()

    def _emit(self, force=False):
        from run_status import dashboard
        self._save_runtime()
        if self.interactive() and not force:
            self.draw()
            return
        value = self._value()
        overview_key = (tuple(t['id'] for t in value.get('tasks', [])),
                        tuple(sorted(t['id'] for t in value.get('tasks', []) if t['status'] == 'done')))
        self.include_overview = force or self.overview_key != overview_key
        signature = dashboard(value, include_time=False, include_overview=False)
        if self.include_overview or signature != self.last_plain or self.clock()-self.last_notice >= 60:
            self._leave_screen()
            self.output.write(dashboard(value, include_overview=self.include_overview) + '\n\n')
            self.output.flush()
            self.overview_key = overview_key
            self.last_notice = self.clock()
            self.last_plain = signature

    def summary(self, value):
        with self.lock:
            self.value = value
            self.last_save = float('-inf')
            self._emit()

    def tick(self):
        with self.lock:
            self._emit()

    def start(self, label, interval=30, title=None, timeout=None, metadata=None):
        token = super().start(title or label, interval)
        with self.lock:
            self.active[token]['timeout'] = timeout
            self.active[token]['metadata'] = dict(metadata or {'kind': 'check'})
            self.message('Начало операции: ' + label)
            self.last_save = float('-inf')
            self._emit()
        return token

    def finish(self, token, outcome='завершено'):
        super().finish(token, outcome)
        with self.lock:
            self.last_save = float('-inf')
            self._emit()

    @contextmanager
    def activity(self, label, interval=30, title=None, timeout=None, metadata=None):
        token = self.start(label, interval, title, timeout, metadata)
        outcome = 'завершено'
        try:
            yield token
        except BaseException:
            outcome = 'остановка с ошибкой'
            raise
        finally:
            self.finish(token, outcome)

    def close(self, status=None, failed=False):
        with self.lock:
            # A startup guard may fail before the first progress publication.
            # Preserve saved progress rather than displaying an empty new run.
            if self.value is None and (self.log_path.parent.parent/'state.json').is_file():
                try:
                    from run_status import snapshot
                    self.value = snapshot(self.log_path.parent.parent)
                except (OSError, ValueError, KeyError, TypeError):
                    self.runtime_error = True
            self.value = dict(self.value or {}, status=status or (self.value or {}).get('status'))
            if failed:
                self.value.update(ready=False, console_error=True)
            self.last_save = float('-inf')
            self._save_runtime()
            self._leave_screen()
            self._emit(force=True)
