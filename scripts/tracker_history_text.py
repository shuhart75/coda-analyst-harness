from __future__ import annotations

import ast
import json
import re


def decode_text_records(text: str, mapping: dict) -> tuple[list[dict], tuple[int, int, int | None] | None]:
    parser = mapping['text_parser']
    if not isinstance(parser, dict) or parser.get('schema_version') != 1:
        raise ValueError('Unsupported text history parser schema')
    try:
        start_pattern = re.compile(parser['record_start_pattern'])
        record_pattern = re.compile(parser['record_pattern'])
        header_pattern = re.compile(parser['header_pattern']) if parser.get('header_pattern') else None
    except (re.error, TypeError, KeyError) as error:
        raise ValueError('Invalid text history patterns') from error
    if not {'at', 'field', 'before', 'after'} <= record_pattern.groupindex.keys():
        raise ValueError('Text history pattern requires at, field, before and after captures')
    if mapping.get('assignment_field') == mapping.get('status_field') or not all(
        isinstance(mapping.get(name), str) and mapping[name] for name in ('assignment_field', 'status_field')
    ):
        raise ValueError('Text history requires distinct assignment and status fields')
    boundaries = list(start_pattern.finditer(text))
    if any(match.start() == match.end() for match in boundaries):
        raise ValueError('Text record boundaries must consume a record prefix')
    header = text[:boundaries[0].start()] if boundaries else text
    header_match = header_pattern.fullmatch(header.strip()) if header_pattern else None
    if header_pattern and header_match is None:
        raise ValueError('Text history header does not match its declared format')
    if not header_pattern and header.strip():
        raise ValueError('Declare a header pattern; unparsed history text cannot be ignored')
    records = []
    for index, boundary in enumerate(boundaries):
        end = boundaries[index + 1].start() if index + 1 < len(boundaries) else len(text)
        raw = text[boundary.start():end].strip()
        matched = record_pattern.fullmatch(raw)
        if matched is None:
            raise ValueError(f'Text history record {index + 1} does not match its declared format')
        values = matched.groupdict()
        record = {'at': values['at'], 'field': values['field'], 'raw': raw,
                  'before': None, 'after': None}
        if record['field'] in {mapping['assignment_field'], mapping['status_field']}:
            for side in ('before', 'after'):
                value = values[side]
                if value is None:
                    continue
                value_format = parser.get('value_format')
                try:
                    if value_format == 'json':
                        record[side] = json.loads(value)
                    elif value_format == 'python-literal':
                        record[side] = ast.literal_eval(value)
                    elif value_format == 'text':
                        record[side] = value
                    else:
                        raise ValueError('Choose json, python-literal or text for history values')
                except (ValueError, SyntaxError, TypeError, RecursionError) as error:
                    raise ValueError(f'Invalid {side} value in text history record {index + 1}') from error
        records.append(record)
    metadata = header_match.groupdict() if header_match else {}
    count = metadata.get('count')
    if count is not None and (not count.isdecimal() or int(count) != len(records)):
        raise ValueError('Text history record count does not match the header')
    window = None
    if count is not None and metadata.get('page') == '0' and metadata.get('has_next', '').casefold() == 'false':
        window = (0, len(records), len(records))
    return records, window


def validate_participants(participants: dict, configured: dict) -> None:
    for identity, role in participants.items():
        if role not in {'AN', 'BE', 'FE', 'QA'}:
            raise ValueError(f'Participant requires a lifecycle role, not a resource ID: {identity}')
        known = configured.get(identity)
        resource_role = None
        if isinstance(known, dict):
            resource = known.get('team_id')
            match = re.fullmatch(r'(QA|BE|FE|AN|Q|B|F|A)[0-9]+', resource or '')
            if match:
                prefix = match.group(1)
                resource_role = {'Q': 'QA', 'B': 'BE', 'F': 'FE', 'A': 'AN'}.get(prefix, prefix)
            known = known.get('role')
        if any(value and value != role for value in (known, resource_role)):
            raise ValueError(f'Participant role contradicts the configured role or team resource: {identity}')
