"""Read immutable Git trees and blobs without a subprocess per path."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


def tree_entries(repository: Path, revision: str) -> dict[str, tuple[str, str]]:
    result = subprocess.run(['git', '-C', str(repository), 'ls-tree', '-rz', revision],
                            capture_output=True, check=False)
    if result.returncode:
        raise ValueError('Cannot read Git tree: ' + result.stderr.decode(errors='replace').strip())
    entries = {}
    for record in result.stdout.split(b'\0'):
        if record:
            header, path = record.split(b'\t', 1)
            mode, kind, oid = header.decode('ascii').split()
            entries[path.decode('utf-8', 'surrogateescape')] = (kind, oid)
    return entries


def read_blobs(repository: Path, oids) -> dict[str, bytes]:
    oids = list(dict.fromkeys(oids))
    if not oids:
        return {}
    if any(not re.fullmatch(r'[0-9a-f]{40,64}', oid) for oid in oids):
        raise ValueError('Expected immutable Git object IDs')
    result = subprocess.run(['git', '-C', str(repository), 'cat-file', '--batch'],
                            input=('\n'.join(oids) + '\n').encode(), capture_output=True, check=False)
    if result.returncode:
        raise ValueError('Cannot read Git blobs: ' + result.stderr.decode(errors='replace').strip())
    data, offset, blobs = result.stdout, 0, {}
    for expected in oids:
        end = data.index(b'\n', offset)
        header = data[offset:end].decode('ascii').split()
        if len(header) != 3 or header[:2] != [expected, 'blob']:
            raise ValueError('Git batch did not return the requested blob')
        size = int(header[2])
        offset = end + 1
        blobs[expected] = data[offset:offset + size]
        if len(blobs[expected]) != size or data[offset + size:offset + size + 1] != b'\n':
            raise ValueError('Truncated Git batch response')
        offset += size + 1
    if offset != len(data):
        raise ValueError('Unexpected Git batch output')
    return blobs
