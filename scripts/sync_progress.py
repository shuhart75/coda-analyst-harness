"""Small live status file for sync; never used as acceptance evidence."""
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


def progress(root, phase, *, status='running', restart=False, **details):
    path = Path(root) / '.workspace-state/sync-progress.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    try:
        previous = json.loads(path.read_text()) if not restart else {}
    except (OSError, ValueError):
        previous = {}
    started = previous.get('started_epoch', now)
    timings = previous.get('phase_seconds', {})
    if previous.get('status') == 'running' and previous.get('phase'):
        old = previous['phase']
        timings[old] = round(timings.get(old, 0) + now - previous.get('updated_epoch', now), 3)
    payload = dict(status=status, phase=phase, pid=os.getpid(), started_epoch=started,
                   updated_epoch=now, updated_at=datetime.now(timezone.utc).isoformat(),
                   elapsed_seconds=round(now - started, 3), phase_seconds=timings, **details)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.sync-progress-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
