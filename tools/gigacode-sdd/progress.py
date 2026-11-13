"""Thread-safe terminal status lines; plain periodic messages for redirected output."""
from contextlib import contextmanager
import os
import shutil
import sys
import threading
import time
import unicodedata


def elapsed(seconds):
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}'


def clipped(text, width):
    # One physical row, no injected cursor controls, including wide Unicode text.
    out, used = [], 0
    for char in str(text):
        if unicodedata.category(char).startswith('C') or char in '\r\n\t':
            char = ' '
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in 'WF' else 1
        if used + size > width:
            break
        out.append(char)
        used += size
    return ''.join(out)


class Progress:
    def __init__(self, stream=None, clock=time.monotonic, dimensions=None):
        self.stream = stream
        self.clock = clock
        self.dimensions = dimensions or (lambda: shutil.get_terminal_size((80, 24)))
        self.lock = threading.RLock()
        self.active = {}
        self.rows = 0
        self.stop = None
        self.thread = None

    @property
    def output(self):
        return self.stream if self.stream is not None else sys.stdout

    def interactive(self):
        return bool(self.output.isatty() and os.environ.get('TERM') != 'dumb')

    def clear(self):
        if self.rows:
            self.output.write('\r' + '\x1b[1A\x1b[2K\r' * self.rows)
            self.rows = 0

    def draw(self):
        if not self.interactive():
            return
        self.clear()
        columns, lines = self.dimensions()
        limit = max(1, lines - 2)
        items = list(self.active.values())
        visible = items if len(items) <= limit else items[:max(0, limit-1)]
        for entry in visible:
            # Put time first so it remains visible on narrow terminals.
            line = elapsed(self.clock()-entry['start']) + '  ' + entry['label']
            if entry['detail']:
                line += ' · ' + entry['detail']
            self.output.write(clipped(line, max(1, columns-1)) + '\n')
            self.rows += 1
        if len(visible) != len(items):
            self.output.write(clipped(f'Ещё активных операций: {len(items)-len(visible)}', max(1, columns-1))+'\n')
            self.rows += 1
        self.output.flush()

    def message(self, message):
        with self.lock:
            self.clear()
            self.output.write(time.strftime('[%H:%M:%S] ') + str(message) + '\n')
            self.draw()
            self.output.flush()

    def tick(self):
        with self.lock:
            if self.interactive():
                self.draw()
            else:
                now = self.clock()
                for entry in self.active.values():
                    if now-entry['notice'] >= entry['interval']:
                        entry['notice'] = now
                        detail = ': ' + entry['detail'] if entry['detail'] else ''
                        self.message(entry['label'] + ': прошло ' + elapsed(now-entry['start']) + detail)

    def start(self, label, interval=30):
        with self.lock:
            token = object()
            now = self.clock()
            if not self.interactive():
                self.message(label + ': начало')
            self.active[token] = {'label':label, 'start':now, 'notice':now, 'interval':interval, 'detail':''}
            self.draw()
            if len(self.active) == 1:
                stop = self.stop = threading.Event()
                def watch():
                    while not stop.wait(1):
                        with self.lock:
                            if stop.is_set():
                                return
                            self.tick()
                self.thread = threading.Thread(target=watch, daemon=True)
                self.thread.start()
            return token

    def detail(self, token, text):
        with self.lock:
            if token in self.active:
                self.active[token]['detail'] = text

    def finish(self, token, outcome='завершено'):
        thread = None
        with self.lock:
            entry = self.active.pop(token)
            if not self.active:
                self.stop.set()
                thread = self.thread
            self.message(entry['label'] + ': ' + outcome + ' за ' + elapsed(self.clock()-entry['start']))
        if thread:
            thread.join()

    @contextmanager
    def activity(self, label, interval=30):
        token = self.start(label, interval)
        outcome = 'завершено'
        try:
            yield token
        except BaseException:
            outcome = 'остановка с ошибкой'
            raise
        finally:
            self.finish(token, outcome)
