"""Formatting recovery must preserve one complete, unambiguous agent object."""
import unittest

from controller import Blocked, parse_json


class AgentJsonTests(unittest.TestCase):
    def test_raw_and_single_fenced_object(self):
        for text in ('{"verdict":"pass"}', '```json\n{"verdict":"pass"}\n```',
                     'Все проверки завершены. Итог ревью:\n\n```json\n{"verdict":"pass"}\n```',
                     'Result:\n```\n{"verdict":"pass"}\n```\nReview complete.',
                     'Ревью завершено.\n\n{"verdict":"pass"}',
                     'Result: {"verdict":"pass","evidence":"literal {brace}"} Done.'):
            with self.subTest(text=text):
                self.assertEqual(parse_json(text)['verdict'], 'pass')

    def test_ambiguous_truncated_or_nonobject_response_is_rejected(self):
        for text in ('{} {}', '[]', '```json\n[]\n```', 'prefix [{"verdict":"pass"}]',
                     'prefix {broken {"verdict":"pass"}',
                     '```json\n{"verdict":\n```', '```json\n{}',
                     '```json\n{}\n```\n```json\n{}\n```',
                     '{}\n```json\n{}\n```', '```json\n{}\n```\n{}',
                     '```python\n{}\n```'):
            with self.subTest(text=text), self.assertRaises((Blocked, ValueError)):
                parse_json(text)
