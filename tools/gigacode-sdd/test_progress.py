import io
import os
import unittest
from unittest.mock import patch
from progress import Progress, elapsed, clipped


class Terminal(io.StringIO):
    def isatty(self):
        return True


class ProgressTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'TERM':'xterm-256color'})
        env.start()
        self.addCleanup(env.stop)
        self.now = 0

    def display(self, stream, width=80):
        return Progress(stream, clock=lambda:self.now, dimensions=lambda:(width,24))

    def test_parallel_timers_keep_separate_rows_and_finish(self):
        stream = Terminal()
        p = self.display(stream)
        a = p.start('backend')
        self.now = 5
        b = p.start('frontend')
        try:
            self.now = 3666
            p.tick()
            self.assertEqual(p.rows, 2)
            self.assertIn('01:01:06  backend', stream.getvalue())
            self.assertIn('01:01:01  frontend', stream.getvalue())
            p.message('Обычное сообщение')
            self.assertEqual(p.rows, 2)
        finally:
            p.finish(a)
            p.finish(b)
        self.assertEqual(p.rows, 0)
        self.assertFalse(p.thread.is_alive())
        self.assertIn('\x1b[1A',stream.getvalue())
        self.assertIn('frontend: завершено за 01:01:01',stream.getvalue())

    def test_plain_output_has_no_cursor_codes_or_per_second_spam(self):
        stream = io.StringIO()
        p = self.display(stream)
        a = p.start('ревью')
        try:
            for self.now in range(1,30):
                p.tick()
            self.assertEqual(len(stream.getvalue().splitlines()),1)
            self.now=30
            p.tick()
            self.assertIn('прошло 00:00:30',stream.getvalue())
        finally:
            p.finish(a)
        self.assertNotIn('\x1b',stream.getvalue())
        self.assertEqual(len(stream.getvalue().splitlines()),3)

    def test_exception_cleans_display_and_dumb_terminal_uses_plain_text(self):
        stream=Terminal()
        with patch.dict(os.environ, {'TERM':'dumb'}):
            p=self.display(stream)
            with self.assertRaises(KeyboardInterrupt):
                with p.activity('операция'):
                    raise KeyboardInterrupt()
        self.assertFalse(p.active)
        self.assertFalse(p.thread.is_alive())
        self.assertNotIn('\x1b',stream.getvalue())
        self.assertIn('остановка с ошибкой',stream.getvalue())

    def test_long_unicode_labels_fit_one_row(self):
        self.assertEqual(elapsed(360000),'100:00:00')
        self.assertEqual(elapsed(3661),'01:01:01')
        self.assertEqual(clipped('界界界',5),'界界')
        self.assertNotIn('\x1b',clipped('abc\x1b[2J\nxyz',30))
        stream=Terminal()
        p=self.display(stream,20)
        a=p.start('очень длинная операция '*10)
        try:
            self.assertEqual(len(stream.getvalue().splitlines()[0]),19)
            self.assertTrue(stream.getvalue().startswith('00:00:00'))
        finally:
            p.finish(a)
