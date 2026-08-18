#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys


TEXT_SUFFIXES = {
    ".md",
    ".puml",
    ".txt",
    ".html",
    ".yaml",
    ".yml",
    ".json",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".css",
    ".scss",
    ".py",
    ".sh",
}

SKIP_DIRS = {
    ".git",
    ".idea",
    ".vscode",
    "node_modules",
    "__pycache__",
}


def usage() -> None:
    print("Usage: find-stale-terms.py <path> <term> [<term> ...]")


def iter_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]

    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        files.append(path)
    return files


def find_hits(path: Path, terms: list[str]) -> list[tuple[int, str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    hits: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for term in terms:
            if term in line:
                hits.append((lineno, term, line.strip()))
    return hits


def main() -> int:
    if len(sys.argv) < 3:
        usage()
        return 1

    root = Path(sys.argv[1])
    terms = sys.argv[2:]

    if not root.exists():
        print(f"Path not found: {root}")
        return 1

    total_hits = 0
    for path in iter_files(root):
        hits = find_hits(path, terms)
        if not hits:
            continue
        print(f"== {path} ==")
        for lineno, term, line in hits:
            print(f"{lineno}: [{term}] {line}")
            total_hits += 1

    if total_hits == 0:
        print("No matches found.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
