"""Compact operator console; all technical messages remain in the run log."""
from contextlib import contextmanager
import os
from pathlib import Path
import time

from progress import Progress, clipped
from progress_io import write_projection


class ConsoleProgress(Progress):
    def __init__(self, log_path, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.log_path = Path(log_path)
        self.runtime_path = self.log_path.parent.parent / 'console-status.json'
        self.value = None
        self.last_plain = None
        self.last_notice = float('-inf')
        self.last_save = float('-inf')
        self.runtime_error = False

    def interactive(self):
        # A pending answer command is copyable text, not a clipped live row.
        # Plain updates retain it on screen without redrawing wrapped commands.
        return super().interactive() and not (getattr(self, 'value', None) or {}).get('questions')

    def message(self, message):
        with self.lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open('a', encoding='utf-8') as stream:
                os.chmod(self.log_path, 0o600)
                stream.write(time.strftime('[%Y-%m-%d %H:%M:%S] ') + str(message) + '\n')

    def notice(self, message):
        # Commands/questions must remain copyable, even on narrow terminals.
        Progress.message(self, message)

    def operations(self):
        with self.lock:
            now = self.clock()
            return [{'title': entry['label'], 'elapsed_seconds': max(0, now-entry['start']),
                     'timeout_seconds': entry.get('timeout'),
                     'idle_seconds': (entry['idle'] + now-entry['observed']) if 'idle' in entry else None}
                    for entry in self.active.values()]

    def telemetry(self, token, data):
        with self.lock:
            if token in self.active:
                self.active[token].update(idle=data['idle_seconds'], observed=self.clock())

    def _value(self):
        return dict(self.value or {}, operations=self.operations(), telemetry_stale=False,
                    telemetry_incomplete=self.runtime_error or (self.value or {}).get('telemetry_incomplete', False))

    def _save_runtime(self):
        if self.clock()-self.last_save < 30:
            return
        self.runtime_error = not write_projection(self.runtime_path,
            {'observed_at': time.time(), 'operations': self.operations()})
        self.last_save = self.clock()

    def draw(self):
        if not self.interactive():
            return
        from run_status import dashboard
        self.clear()
        columns, height = self.dimensions()
        lines = dashboard(self._value(), include_command=False).splitlines()
        for line in lines[:max(1, height-2)]:
            self.output.write(clipped(line, max(1, columns-1)) + '\n')
            self.rows += 1
        self.output.flush()

    def _emit(self, force=False):
        from run_status import dashboard
        self._save_runtime()
        if self.interactive():
            self.draw()
            return
        self.clear()
        value = self._value()
        signature = dashboard(value, include_time=False)
        if force or signature != self.last_plain or self.clock()-self.last_notice >= 60:
            self.output.write(dashboard(value) + '\n\n')
            self.output.flush()
            self.last_notice = self.clock()
            self.last_plain = signature

    def summary(self, value):
        with self.lock:
            self.value = value
            self._emit()

    def tick(self):
        with self.lock:
            self._emit()

    def start(self, label, interval=30, title=None, timeout=None):
        token = super().start(title or label, interval)
        with self.lock:
            self.active[token]['timeout'] = timeout
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
    def activity(self, label, interval=30, title=None, timeout=None):
        token = self.start(label, interval, title, timeout)
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
            self.value = dict(self.value or {}, status=status or (self.value or {}).get('status'))
            if failed:
                self.value.update(ready=False, console_error=True)
            self.last_save = float('-inf')
            self._save_runtime()
            if self.interactive():
                from run_status import dashboard
                self.clear()
                self.output.write(dashboard(self._value()) + '\n')
                self.output.flush()
            else:
                self._emit(force=True)
