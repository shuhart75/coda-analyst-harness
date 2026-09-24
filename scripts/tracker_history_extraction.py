from __future__ import annotations

import hashlib


def source_span(text: str, reference: dict, bounds: tuple[int, int]) -> str:
    if not isinstance(reference, dict):
        raise ValueError('Text extraction requires a source span')
    start, end = (reference.get(name) for name in ('start', 'end'))
    if (type(start) is not int or type(end) is not int
            or not bounds[0] <= start < end <= bounds[1]):
        raise ValueError('Text extraction span or quote differs from its source')
    quote = text[start:end]
    if (not quote.strip() or not {'quote', 'sha256'}.intersection(reference)
            or ('quote' in reference and reference['quote'] != quote)
            or ('sha256' in reference and reference['sha256'] != hashlib.sha256(quote.encode('utf-8')).hexdigest())):
        raise ValueError('Text extraction span or quote differs from its source')
    return quote


def decode_text_extraction(text: str, extraction: dict) -> tuple[list[dict], tuple | None, list[str]]:
    if not isinstance(extraction, dict) or extraction.get('schema_version') != 1:
        raise ValueError('Unsupported text extraction schema')
    if extraction.get('text_sha256') != hashlib.sha256(text.encode('utf-8')).hexdigest():
        raise ValueError('Text extraction checksum differs from the selected source text')
    segments = extraction.get('segments')
    if not isinstance(segments, list) or not segments:
        raise ValueError('Text extraction requires coverage of the entire source')
    records, contexts, limits = [], [], []
    previous_end = 0
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError('Invalid text extraction segment')
        reference = segment.get('source')
        raw = source_span(text, reference, (previous_end, len(text)))
        if 'sha256' not in reference or 'quote' in reference:
            raise ValueError('Whole segment references require a checksum, not a copied response body')
        start, end = reference['start'], reference['end']
        if text[previous_end:start].strip():
            raise ValueError('Text extraction leaves source content uncovered')
        previous_end = end
        bounds = (start, end)
        kind = segment.get('kind')
        if kind in {'context', 'metadata', 'unresolved'}:
            if not isinstance(segment.get('reason'), str) or not segment['reason'].strip():
                raise ValueError('Non-lifecycle text requires an explicit classification reason')
            if kind == 'context':
                contexts.append(bounds)
                continue
            if kind == 'unresolved':
                limits = ['history-text-extraction-unresolved']
            records.append({'at': None, 'field': None, 'before': None, 'after': None, 'raw': raw})
            continue
        if kind not in {'assignment', 'status'}:
            raise ValueError('Unknown text extraction segment kind')
        source_span(text, segment.get('field'), bounds)
        operation = segment.get('operation')
        if not isinstance(operation, dict) or operation.get('kind') not in {'add', 'change', 'remove'}:
            raise ValueError('Text extraction requires an evidenced change operation')
        source_span(text, operation.get('source'), bounds)
        if 'at' not in segment or 'before' not in segment or 'after' not in segment:
            raise ValueError('Text extraction must state date and both transition sides explicitly')
        at = source_span(text, segment['at'], bounds) if segment['at'] is not None else None
        before = source_span(text, segment['before'], bounds) if segment['before'] is not None else None
        after = source_span(text, segment['after'], bounds) if segment['after'] is not None else None
        valid_sides = {'add': before is None and after is not None,
                       'remove': before is not None and after is None,
                       'change': before is not None and after is not None}
        if not valid_sides[operation['kind']] or (kind == 'status' and operation['kind'] != 'change'):
            raise ValueError('Text extraction operation contradicts its transition sides')
        records.append({'at': at, 'field': kind, 'before': before, 'after': after, 'raw': raw})
    if text[previous_end:].strip():
        raise ValueError('Text extraction leaves source content uncovered')

    coverage = extraction.get('pagination')
    window = None
    if coverage is not None:
        if not isinstance(coverage, dict) or not set(coverage) <= {'count', 'start', 'has_next', 'total'}:
            raise ValueError('Invalid text extraction pagination evidence')
        values = {}
        for name, reference in coverage.items():
            quote = source_span(text, reference, (0, len(text)))
            if not any(start <= reference['start'] < reference['end'] <= end for start, end in contexts):
                raise ValueError('Pagination evidence must belong to source context, not a task event')
            if name == 'has_next':
                if quote.casefold() not in {'true', 'false'}:
                    raise ValueError('Pagination has_next requires a literal boolean in the source')
                values[name] = quote.casefold() == 'true'
            else:
                if not quote.isascii() or not quote.isdecimal():
                    raise ValueError('Pagination numbers must be literal nonnegative integers')
                values[name] = int(quote)
        if 'count' in values and values['count'] != len(records):
            raise ValueError('Text extraction record count differs from source pagination')
        start = values.get('start')
        total = values.get('total')
        if total is not None and start is not None and start + len(records) > total:
            raise ValueError('Text extraction records exceed source total')
        if (total is not None and start is not None and 'has_next' in values
                and values['has_next'] != (start + len(records) < total)):
            raise ValueError('Text extraction pagination is contradictory')
        if start == 0 and (total == len(records) or values.get('has_next') is False):
            window = (0, len(records), len(records))
    return records, window, limits
