"""References are assessed semantically by reviewers, not by string equality."""
from pathlib import Path


def bind_quote(path, citation):
    """Keep model wording; require a real, readable referenced file.

    The caller enforces the pinned workspace boundary. This does not certify
    a literal quotation or semantic correctness; the independent reviewer
    assesses whether the reference supports the conclusion.
    """
    quote = citation.get('quote')
    if not isinstance(quote, str) or not quote.strip():
        return False
    path = Path(path)
    if not path.is_file():
        return False
    path.read_text(encoding='utf-8')
    citation['evidence_kind'] = 'semantic-reference'
    return True
