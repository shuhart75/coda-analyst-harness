from pathlib import Path
import tempfile
import unittest
from evidence import bind_quote


class QuoteTests(unittest.TestCase):
    def test_markdown_emphasis_binds_to_literal_source_without_changing_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'requirements.md'
            original = '**Then** the record is in `draft`.\n'
            path.write_text(original)
            citation = {'quote': 'Then the record is in `draft`.'}
            self.assertTrue(bind_quote(path, citation))
            self.assertEqual(citation['quote'], original.strip())
            self.assertEqual(citation['reported_quote'], 'Then the record is in `draft`.')
            self.assertEqual(path.read_text(), original)
            for changed in ('Then the record is not in `draft`.', 'Then the record is in `ready`.',
                            'Then  the record is in `draft`.'):
                self.assertFalse(bind_quote(path, {'quote': changed}))

    def test_code_nonmarkdown_escaped_and_ambiguous_text_stay_literal(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, source, quote in (
                ('source.py', '**Then** ready', 'Then ready'),
                ('source.md', '```python\n**Then** ready\n```', 'Then ready'),
                ('source.md', '`**Then** ready`', 'Then ready'),
                ('source.md', '    **Then** ready', 'Then ready'),
                ('source.md', r'\**Then** ready', 'Then ready'),
                ('source.md', '**Then** ready\n**Then** ready', 'Then ready'),
            ):
                with self.subTest(source=source):
                    path = Path(tmp)/name; path.write_text(source)
                    self.assertFalse(bind_quote(path, {'quote': quote}))
