from pathlib import Path
import tempfile
import unittest
from evidence import bind_quote


class ReferenceTests(unittest.TestCase):
    def test_wording_is_not_a_literal_matching_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'requirements.md'
            source = '**Then** apply the page model to the current revision.\n'
            path.write_text(source)
            for quote in ('Then apply the page model to the current revision.',
                          'Applyy the page model to the current revision.',
                          'Only the response for the current revision is applied.'):
                with self.subTest(quote=quote):
                    citation = {'quote': quote}
                    self.assertTrue(bind_quote(path, citation))
                    self.assertEqual(citation['quote'], quote)
                    self.assertEqual(citation['evidence_kind'], 'semantic-reference')
            self.assertEqual(path.read_text(), source)

    def test_missing_files_and_empty_references_are_not_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'missing.md'
            self.assertFalse(bind_quote(path, {'quote': 'supporting reference'}))
            path.write_text('source')
            for citation in ({}, {'quote': ''}, {'quote': '  '}, {'quote': 3}):
                self.assertFalse(bind_quote(path, citation))
