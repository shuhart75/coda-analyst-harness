#!/usr/bin/env python3
from __future__ import annotations

import re
import json
import hashlib
import sys
from pathlib import Path

from workspace_paths import approved_plans_path


ROLE_ORDER = ("AN", "BE", "FE", "QA")
ROLE_LIMITS = {"AN": None, "BE": 5, "FE": 10, "QA": 10}
VECTOR_RE = re.compile(r"`?\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*`?")
STORY_RE = re.compile(r"\bSTORY-[A-Z0-9-]+-(AN|BE|FE|QA)\b")


def markdown_rows(path: Path) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and not all(set(cell) <= {"-", ":"} for cell in cells):
            rows.append(cells)
    return rows


def validate_estimates(path: Path, root: Path, errors: list[str], warnings: list[str]) -> None:
    display = path.relative_to(root)
    rows = markdown_rows(path)
    if len(rows) < 2:
        errors.append(f"{display}: estimates table is missing")
        return
    header = rows[0]
    required = {"Story ID", "Role", "Agreed effort, дн", "Max parallelism", "Efficiency"}
    if not required.issubset(set(header)):
        # Legacy tables remain readable but are not valid for a newly approved plan.
        warnings.append(f"{display}: legacy estimate format; migrate before next approval")
        for row in rows[1:]:
            for cell in row:
                match = VECTOR_RE.search(cell)
                if match and len(match.groups()) != 4:
                    errors.append(f"{display}: invalid AN/FE/BE/QA vector")
        return

    indexes = {name: header.index(name) for name in required}
    seen_roles: set[str] = set()
    for row in rows[1:]:
        if len(row) < len(header):
            continue
        role = row[indexes["Role"]].upper()
        story_id = row[indexes["Story ID"]]
        if role not in ROLE_ORDER:
            errors.append(f"{display}: invalid role {role!r}")
            continue
        if not STORY_RE.search(story_id) or not story_id.endswith(f"-{role}"):
            errors.append(f"{display}: story {story_id!r} must end with -{role}")
        if role in seen_roles:
            errors.append(f"{display}: more than one {role} planning story")
        seen_roles.add(role)
        try:
            effort = float(row[indexes["Agreed effort, дн"]])
            parallelism = int(row[indexes["Max parallelism"]])
            efficiency = float(row[indexes["Efficiency"]])
        except ValueError:
            errors.append(f"{display}: non-numeric planning parameters for {story_id}")
            continue
        if effort <= 0 or parallelism <= 0:
            errors.append(f"{display}: effort and max parallelism must be positive")
        if not 0 < efficiency <= 1:
            errors.append(f"{display}: efficiency must be within (0, 1]")


def validate_task_candidates(path: Path, root: Path, errors: list[str], warnings: list[str]) -> None:
    display = path.relative_to(root)
    rows = markdown_rows(path)
    if len(rows) < 2:
        return
    header = rows[0]
    english = {"Candidate ID", "Role", "Estimate (дн)", "Source Requirements", "Verification"}
    russian = {"Идентификатор", "Роль", "Оценка (дн)", "Исходные требования", "Проверка"}
    if english.issubset(set(header)):
        columns = {
            "id": "Candidate ID",
            "role": "Role",
            "estimate": "Estimate (дн)",
            "requirements": "Source Requirements",
            "verification": "Verification",
        }
    elif russian.issubset(set(header)):
        columns = {
            "id": "Идентификатор",
            "role": "Роль",
            "estimate": "Оценка (дн)",
            "requirements": "Исходные требования",
            "verification": "Проверка",
        }
    else:
        errors.append(f"{display}: task candidate table misses required columns")
        return
    indexes = {name: header.index(column) for name, column in columns.items()}
    for row in rows[1:]:
        if len(row) < len(header):
            continue
        role = row[indexes["role"]].upper()
        try:
            estimate = float(row[indexes["estimate"]])
        except ValueError:
            errors.append(f"{display}: invalid task estimate")
            continue
        limit = ROLE_LIMITS.get(role)
        if role not in ROLE_LIMITS:
            errors.append(f"{display}: invalid task role {role!r}")
        elif limit is not None and estimate > limit:
            errors.append(f"{display}: {role} task exceeds {limit} days")
        elif role != "AN" and not 1 <= estimate <= 3:
            warnings.append(f"{display}: {role} task is outside target size 1-3 days")
        if not row[indexes["requirements"]]:
            errors.append(f"{display}: candidate has no requirement reference")
        if not row[indexes["verification"]]:
            errors.append(f"{display}: candidate has no verification")


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    errors: list[str] = []
    warnings: list[str] = []
    for path in sorted(root.glob("features/*/planning/estimates.md")):
        validate_estimates(path, root, errors, warnings)
    for path in sorted(root.glob("features/*/slices/*/execution/task-candidates.md")):
        validate_task_candidates(path, root, errors, warnings)
    approved_snapshots = approved_plans_path(root)
    for state in sorted(root.glob("planning/*/plan-state.md")):
        text = state.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"^Status:\s*`?approved`?\s*$", text, re.MULTILINE | re.IGNORECASE):
            quarter = state.parent.name
            if not (approved_snapshots / f"{quarter}.json").exists():
                errors.append(f"{state.relative_to(root)}: approved plan has no immutable hash snapshot")
    for snapshot_path in sorted(approved_snapshots.glob("*.json")) if approved_snapshots.exists() else []:
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{snapshot_path.relative_to(root)}: invalid approval snapshot: {exc}")
            continue
        for rel, expected in snapshot.get("files", {}).items():
            target = root / rel
            if not target.exists():
                errors.append(f"approved plan file missing: {rel}")
                continue
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != expected:
                errors.append(f"approved plan was modified: {rel}")
        for rel, expected_rows in snapshot.get("actualization_baseline", {}).items():
            target = root / rel
            actual_rows: list[list[str]] = []
            if target.exists():
                for line in target.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if not line.startswith("| STORY-"):
                        continue
                    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
                    if len(cells) >= 4:
                        actual_rows.append([cells[0], cells[2], cells[3]])
            if actual_rows != expected_rows:
                errors.append(f"approved actualization baseline was modified: {rel}")
    if errors:
        print("Planning errors:")
        for item in errors:
            print(f"- {item}")
    if warnings:
        print("Planning warnings:")
        for item in warnings[:200]:
            print(f"- {item}")
    if not errors and not warnings:
        print("Planning OK")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
