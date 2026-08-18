#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


REQUIREMENT_PATTERNS = (
    "features/*/requirements.md",
    "features/*/slices/*/slice.md",
    "features/*/slices/*/requirements/*.md",
    "baseline/current/requirements/*.md",
)
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`]*`")
URL_RE = re.compile(r"https?://\S+")
MACHINE_ID_RE = re.compile(r"\b(?:REQ|AC|DEC|STORY|TASK|CAND|TEST|IMPL|IMP)-[A-Z0-9<>-]+\b")
PLACEHOLDER_RE = re.compile(r"<[^>]+>")


def changed_files(root: Path) -> set[Path]:
    commands = (
        ["git", "-C", str(root), "diff", "--name-only", "--diff-filter=ACMRT", "HEAD"],
        ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"],
    )
    result: set[Path] = set()
    for command in commands:
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            continue
        for line in completed.stdout.splitlines():
            path = root / line.strip()
            if path.is_file():
                result.add(path)
    return result


def requirement_files(root: Path, feature: str | None, all_files: bool) -> list[Path]:
    if feature:
        base = root / "features" / feature
        candidates = set(base.glob("requirements.md"))
        candidates.update(base.glob("slices/*/slice.md"))
        candidates.update(base.glob("slices/*/requirements/*.md"))
    else:
        candidates: set[Path] = set()
        for pattern in REQUIREMENT_PATTERNS:
            candidates.update(root.glob(pattern))
    if not all_files:
        candidates.intersection_update(changed_files(root))
    return sorted(path for path in candidates if path.is_file())


def prose_lines(path: Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    text = FENCE_RE.sub(lambda match: "\n" * match.group(0).count("\n"), text)
    result: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = INLINE_CODE_RE.sub("", line)
        line = URL_RE.sub("", line)
        line = MACHINE_ID_RE.sub("", line)
        line = PLACEHOLDER_RE.sub("", line)
        result.append((number, line))
    return result


def compile_term(term: str) -> re.Pattern[str]:
    escaped = re.escape(term)
    if re.fullmatch(r"[A-Za-zА-Яа-яЁё -]+", term):
        return re.compile(rf"(?<![A-Za-zА-Яа-яЁё]){escaped}(?![A-Za-zА-Яа-яЁё])", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check changed requirements for avoidable anglicisms")
    parser.add_argument("project")
    parser.add_argument("--feature")
    parser.add_argument("--all", action="store_true", help="scan all requirement files, not only changed files")
    args = parser.parse_args()
    root = Path(args.project).resolve()
    policy_path = root / ".workflow/language-policy.json"
    if not policy_path.exists():
        print(f"Missing language policy: {policy_path}")
        return 1
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    terms = [(item["term"], item["replacement"], compile_term(item["term"])) for item in policy.get("avoid", [])]
    findings: list[tuple[Path, int, str, str]] = []
    files = requirement_files(root, args.feature, args.all)
    for path in files:
        for line_number, line in prose_lines(path):
            for term, replacement, pattern in terms:
                if pattern.search(line):
                    findings.append((path.relative_to(root), line_number, term, replacement))
    if findings:
        output_limit = 300
        print(f"Language findings: {len(findings)}")
        for path, line, term, replacement in findings[:output_limit]:
            print(f"- {path}:{line}: {term!r} -> {replacement}")
        if len(findings) > output_limit:
            print(f"... {len(findings) - output_limit} more findings omitted")
        print("Use backticks only when the English form is an exact code identifier, path, API value, or fixed external-system term.")
        return 1
    if not files:
        print("Language OK: no changed requirement files")
    else:
        print(f"Language OK: {len(files)} requirement files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
