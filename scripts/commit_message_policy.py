#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


TRACKER_IDENTIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_]*-[0-9]+(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
HOOK_MARKER = "analyst-harness-commit-message-policy:v1"
ERROR_MESSAGE = (
    "Сообщение коммита отклонено: тема и тело не должны содержать номера задач "
    "или другие идентификаторы трекеров. Оставь только смысловое описание изменения."
)


def has_tracker_identifier(message: str) -> bool:
    return TRACKER_IDENTIFIER_RE.search(message) is not None


def require_valid_commit_message(message: str) -> None:
    if has_tracker_identifier(message):
        raise ValueError(ERROR_MESSAGE)


def validate_file(path: Path) -> None:
    try:
        message = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Не удалось прочитать сообщение коммита: {exc}") from exc
    if has_tracker_identifier(message):
        from receiver_tasks import git, read_blob, validate_request, REQUEST_FILE, KEY
        import json
        repository = Path.cwd()
        paths = git(repository, 'diff', '--cached', '--name-only').splitlines()
        if paths and all(path.endswith('/analysis-task.json') for path in paths):
            keys = set()
            for path in paths:
                binding = json.loads(read_blob(repository, '', path))
                request = validate_request(json.loads(read_blob(repository, '', path.rsplit('/', 1)[0] + '/' + REQUEST_FILE)))
                if (binding.get('request_id') != request['request_id'] or binding.get('role') != 'AN'
                        or binding.get('provider') != 'sbertrek' or not KEY.fullmatch(str(binding.get('key', '')))):
                    raise ValueError('Invalid receiver task binding')
                keys.add(binding['key'])
            if keys <= set(TRACKER_IDENTIFIER_RE.findall(message.split('\n', 1)[0])):
                return
    require_valid_commit_message(message)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Запрещает идентификаторы трекеров в сообщениях Git-коммитов"
    )
    parser.add_argument("message_file", type=Path)
    args = parser.parse_args()
    try:
        validate_file(args.message_file)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
