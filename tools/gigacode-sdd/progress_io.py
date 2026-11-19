"""Best-effort projections only. Never use for checkpoints or acceptance evidence."""
import json
import os
from pathlib import Path
import tempfile
import time


def read_projection(path, problems):
    for attempt in range(3):
        try:
            value = json.loads(Path(path).read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                raise ValueError('Projection must be an object')
            return value
        except (OSError, ValueError):
            if attempt < 2:
                time.sleep(.02)
    problems.append(str(path))
    return {}


def write_projection(path, value):
    path = Path(path)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
        for attempt in range(3):
            try:
                os.replace(temporary, path)
                return True
            except OSError:
                if attempt < 2:
                    time.sleep(.02)
        return False
    except OSError:
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
