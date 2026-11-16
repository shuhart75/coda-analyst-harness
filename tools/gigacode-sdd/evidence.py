"""Bind quotations to literal sources, tolerating only Markdown emphasis markup."""
import re
from pathlib import Path


def _emphasis_view(text):
    removed = set()
    offset = 0
    fence = None
    # Markdown code spans may cross line boundaries. Match whole delimiter
    # runs so double backticks are not mistaken for two empty single spans.
    code = list(re.finditer(r'(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)', text, re.DOTALL))
    for line in text.splitlines(keepends=True):
        marker = re.match(r' {0,3}(`{3,}|~{3,})', line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            offset += len(line)
            continue
        if fence or line.startswith(('    ', '\t')):
            offset += len(line)
            continue
        # Code spans are literal, including emphasis characters inside them.
        for match in re.finditer(r'(?<![\w\\*])(?P<mark>\*\*|__)(?P<body>[^*_`\n]+)(?P=mark)(?![\w*])', line):
            if match['body'] != match['body'].strip() or any(
                    offset + match.start() < part.end() and offset + match.end() > part.start() for part in code):
                continue
            removed.update(range(offset + match.start(), offset + match.start('body')))
            removed.update(range(offset + match.end('body'), offset + match.end()))
        offset += len(line)
    positions = [i for i in range(len(text)) if i not in removed]
    return ''.join(text[i] for i in positions), positions, removed


def bind_quote(path, citation):
    """Mutate only the parsed citation; the recorded model answer stays immutable."""
    quote = citation.get('quote')
    if not isinstance(quote, str) or not quote.strip():
        return False
    path = Path(path)
    text = path.read_text(encoding='utf-8')
    if quote in text:
        return True
    if path.suffix.lower() != '.md':
        return False
    view, positions, removed = _emphasis_view(text)
    # Do not rewrite the model's words, whitespace, punctuation or code.
    start = view.find(quote)
    if start < 0 or view.find(quote, start + 1) >= 0:
        return False
    first, last = positions[start], positions[start + len(quote) - 1] + 1
    while first > 0 and first - 1 in removed:
        first -= 1
    while last in removed:
        last += 1
    citation.update(reported_quote=quote, quote=text[first:last], quote_match='markdown-emphasis-only')
    return True
