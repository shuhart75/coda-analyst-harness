#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from workspace_paths import run_state_path


ID_RE = re.compile(r"\b(?:REQ|AC|DEC|STORY|TASK|CAND|TEST|IMPL)-[A-Z0-9][A-Z0-9-]*\b")
HEADING_ID_RE = re.compile(r"^#{1,6}\s+((?:REQ|AC|DEC|STORY|TASK|CAND|TEST|IMPL)-[A-Z0-9][A-Z0-9-]*)\b", re.MULTILINE)
REFERENCE_COLUMNS = {
    "Source Requirements",
    "Requirement / Criterion",
    "Related Stories",
    "Исходные требования",
    "Исходное требование",
    "Требование / критерий",
    "Связанные плановые истории",
}
DEFINITION_COLUMNS = {
    "Requirement ID",
    "Acceptance ID",
    "Decision ID",
    "Story ID",
    "Candidate ID",
    "Test ID",
    "ID",
    "Идентификатор требования",
    "Идентификатор критерия",
    "Идентификатор решения",
    "Идентификатор влияния",
    "Идентификатор",
}


def markdown_rows(path: Path) -> list[list[str]]:
    result: list[list[str]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.lstrip().startswith("|"):
            result.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return result


def main() -> int:
    args = sys.argv[1:]
    strict = "--strict" in args
    args = [arg for arg in args if arg != "--strict"]
    root = Path(args[0]).resolve() if args else Path.cwd()
    feature = args[args.index("--feature") + 1] if "--feature" in args else None
    base = root / "features" / feature if feature else root
    definitions: dict[str, list[str]] = {}
    references: dict[str, list[str]] = {}
    errors: list[str] = []

    for path in sorted(base.rglob("*.md")):
        if ".git" in path.parts:
            continue
        rel = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8", errors="ignore")
        for identifier in sorted(set(HEADING_ID_RE.findall(text))):
            definitions.setdefault(identifier, []).append(rel)
        rows = markdown_rows(path)
        if not rows:
            continue
        header = rows[0]
        for column in DEFINITION_COLUMNS.intersection(header):
            idx = header.index(column)
            for row in rows[2:]:
                if idx >= len(row):
                    continue
                for identifier in ID_RE.findall(row[idx]):
                    definitions.setdefault(identifier, []).append(rel)
        for column in REFERENCE_COLUMNS.intersection(header):
            idx = header.index(column)
            for row in rows[2:]:
                if idx >= len(row):
                    continue
                for identifier in ID_RE.findall(row[idx]):
                    references.setdefault(identifier, []).append(rel)

    for identifier, paths in references.items():
        if identifier not in definitions:
            errors.append(f"unresolved trace id {identifier}: referenced by {', '.join(paths)}")

    output = run_state_path() / "trace-index.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "feature": feature,
                "definitions": definitions,
                "references": references,
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if errors:
        print("Trace errors:" if strict else "Trace warnings:")
        for item in errors:
            print(f"- {item}")
        return 1 if strict else 0
    print(f"Trace OK: {len(definitions)} ids, index={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
