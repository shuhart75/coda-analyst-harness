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
