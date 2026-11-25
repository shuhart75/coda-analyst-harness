"""Best-effort desktop hints. Questions and answers remain in the mailbox."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


APPLE_SCRIPT = '''on run argv
    display notification (item 2 of argv) with title (item 1 of argv)
end run'''


def deliver():
    title = 'GigaCode SDD'
    body = 'Новый вопрос. Откройте мастер: «Прогресс и ответы на вопросы».'
    enabled = os.environ.get('SDD_DESKTOP_NOTIFICATIONS')
    if enabled == '0' or (enabled is None and os.environ.get('CI') == 'true'):
        return {'status': 'disabled'}
    if sys.platform == 'darwin':
        executable = shutil.which('osascript')
        argv = [executable, '-e', APPLE_SCRIPT, title, body]
    elif sys.platform.startswith('linux'):
        executable = shutil.which('notify-send')
        argv = [executable, '--app-name=GigaCode SDD', title, body]
    else:
        return {'status': 'unsupported'}
    if not executable:
        return {'status': 'unavailable'}
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=3, check=False)
        # OS acceptance does not establish that focus/notification settings showed a banner.
        return {'status': 'submitted' if result.returncode == 0 else 'failed',
                'returncode': result.returncode}
    except (OSError, subprocess.SubprocessError) as exc:
        return {'status': 'failed', 'error_type': type(exc).__name__}


def notify_question(root, question_id):
    """One bounded delivery attempt per durable question, also across restarts."""
    try:
        folder = Path(root) / 'operator-questions' / 'notifications'
        folder.mkdir(exist_ok=True)
        if folder.is_symlink():
            return
        path = folder / (question_id + '.json')
        # Exclusive creation arbitrates concurrent candidate requests. A crash after
        # reservation loses a hint, never duplicates questions or prevents answering.
        with path.open('x', encoding='utf-8') as stream:
            json.dump({'status': 'attempting', 'at': time.time()}, stream)
        outcome = deliver()
        path.write_text(json.dumps({**outcome, 'at': time.time()}), encoding='utf-8')
    except OSError:
        # Notification failure must never turn a persisted question into task failure.
        return
