from __future__ import annotations

import hashlib
from importlib import import_module
import os
from pathlib import Path
import re
import subprocess

from tracker_scope import inside_project, select_features


REGISTRY = re.compile(r"features/[^/]+/(?:slices/[^/]+/)?execution/tasks\.md")
KEY = re.compile(r"([A-Z][A-Z0-9_]*-[1-9][0-9]*)(?:/(AN|BE|FE|QA))?")
ROLES = {"AN", "BE", "FE", "QA"}


def git(project: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(project), *args], capture_output=True, text=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Не удалось проверить Git-состояние проекта")
    return result.stdout


def repository_state(project: Path) -> dict:
    return {
        "head": git(project, "rev-parse", "HEAD").strip(),
        "tracked": set(git(project, "ls-files", "-z").split("\0")) - {""},
        "changed": set(git(project, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--", "features").split("\0")) - {""},
    }


def registry_paths(project: Path) -> list[Path]:
    features = inside_project(project, project / "features")
    paths = set(features.glob("*/execution/tasks.md")) | set(features.glob("*/slices/*/execution/tasks.md"))
    return sorted(inside_project(project, path) for path in paths)


def read_key(value: str, role: str) -> tuple[str | None, bool]:
    value = value.strip()
    if value in {"", "-", "—"}:
        return None, False
    match = KEY.fullmatch(value)
    if match is None:
        return None, True
    return match.group(1), bool(match.group(2) and match.group(2) != role)


def preview_execution(
    project: Path, quarter: str | None, feature: str | None, result: dict,
    reviewed: dict[str, str] | None = None, expected_head: str | None = None,
) -> dict:
    project = project.expanduser().resolve()
    if Path(git(project, "rev-parse", "--show-toplevel").strip()).resolve() != project:
        raise ValueError("PROJECT_ROOT должен быть корнем аналитического Git-репозитория")
    before = repository_state(project)
    reviewed = reviewed or {}
    if reviewed and before["head"] != expected_head:
        raise ValueError("HEAD изменился после проверки реестров")
    selected = select_features(project, quarter, feature)
    overlay = import_module("sync-actual-progress-overlay")
    paths = registry_paths(project)
    sources, rows, warnings, blockers = {}, [], [], []
    for path in paths:
        relative = path.relative_to(project).as_posix()
        if not path.is_file():
            raise ValueError(f"Реестр недоступен: {relative}")
        sources[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        tables = [table for table in overlay.parse_tables(path) if table and ("Task ID" in table[0] or "Jira" in table[0])]
        if not tables:
            blockers.append({"reason": "unreadable-registry", "registry": relative})
        for table_number, table in enumerate(tables, start=1):
            for row_number, row in enumerate(table, start=1):
                role = overlay.normalize_role(row.get("Role", ""))
                jira, jira_invalid = read_key(row.get("Jira", ""), role)
                sbertrek, sber_invalid = read_key(row.get("SberTrek", ""), role)
                reference = {
                    "feature": path.relative_to(project).parts[1], "registry": relative,
                    "table": table_number, "row": row_number,
                    "task_id": row.get("Task ID") or row.get("Jira") or "",
                    "role": role, "kind": row.get("Kind", "").casefold(),
                    "jira_key": jira, "sbertrek_key": sbertrek,
                    "invalid_key": jira_invalid or sber_invalid,
                    "uncommitted": relative not in before["tracked"] or relative in before["changed"],
                }
                rows.append(reference)
                if reference["invalid_key"]:
                    warnings.append({"reason": "invalid-external-key", "reference": reference})
    for relative in sorted(before["changed"]):
        if REGISTRY.fullmatch(relative) and relative not in sources:
            blockers.append({"reason": "deleted-or-renamed-registry", "registry": relative})
    for relative, checksum in reviewed.items():
        if sources.get(relative) != checksum or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError(f"Подтверждённая версия реестра не совпадает: {relative}")

    items = []
    for issue in [*result["issues"], *result["excluded"]]:
        jira_key, sbertrek_key = issue.get("jira_key"), issue.get("sbertrek_key")
        identity = jira_key or sbertrek_key
        matched = [
            row for row in rows
            if (jira_key and row["jira_key"] == jira_key) or (sbertrek_key and row["sbertrek_key"] == sbertrek_key)
        ]
        roles = [item for item in result["work_items"]
                 if item.get("jira_key") == jira_key and item.get("sbertrek_key") == sbertrek_key]
        reasons = []
        owners = sorted({row["feature"] for row in matched})
        if not matched:
            reasons.append("task-owner-not-confirmed")
        if len(owners) > 1:
            reasons.append("multiple-feature-owners")
        if set(owners) - set(selected):
            reasons.append("owner-outside-selected-scope")
        if any(row["uncommitted"] and row["registry"] not in reviewed for row in matched):
            reasons.append("uncommitted-owner-registry")
        if any(row["invalid_key"] for row in matched):
            reasons.append("invalid-owner-key")
        if any(row["kind"] != "real" for row in matched):
            reasons.append("materialization-not-confirmed")
        if any(row["role"] not in ROLES for row in matched):
            reasons.append("registry-role-not-confirmed")
        if any(
            (jira_key and row["jira_key"] and row["jira_key"] != jira_key)
            or (sbertrek_key and row["sbertrek_key"] and row["sbertrek_key"] != sbertrek_key)
            for row in matched
        ):
            reasons.append("registry-pair-conflicts-with-reconciliation")
        for role in sorted({row["role"] for row in matched}):
            if sum(row["role"] == role for row in matched) > 1:
                reasons.append(f"duplicate-registry-role:{role}")
        for item in roles:
            if not any(row["role"] == item["role"] for row in matched):
                reasons.append(f"new-role-needs-confirmation:{item['role']}")
        identities = {item["work_item_id"] for item in roles} | {key for key in (jira_key, sbertrek_key) if key}
        local_collisions = [row for row in rows if row not in matched and row["task_id"] in identities]
        if local_collisions:
            reasons.append("unconfirmed-internal-id-collision")
        if issue.get("reason") == "jira-counterpart-absent":
            reasons.append("excluded-counterpart-needs-disposition")
        entry = {
            "tracker_key": identity, "jira_key": jira_key, "sbertrek_key": sbertrek_key,
            "owners": owners, "targets": matched, "work_item_ids": [item["work_item_id"] for item in roles],
            "internal_id_collisions": local_collisions, "blockers": sorted(set(reasons)),
        }
        items.append(entry)
        blockers.extend({"tracker_key": identity, "reason": reason} for reason in entry["blockers"])

    if not items:
        blockers.append({"reason": "no-reconciled-tasks"})
    if select_features(project, quarter, feature) != selected:
        raise ValueError("Область изменилась во время проверки; повтори execution-preview")
    if repository_state(project) != before or registry_paths(project) != paths or any(
        not (project / relative).is_file()
        or hashlib.sha256((project / relative).read_bytes()).hexdigest() != checksum
        for relative, checksum in sources.items()
    ):
        raise ValueError("Реестры или Git-состояние изменились во время проверки; повтори execution-preview")
    return {
        "status": "tracker-execution-preview",
        "project_root": str(project), "head": before["head"], "quarter": quarter,
        "selected_features": sorted(selected), "items": items,
        "registry_sha256": sources, "reviewed_registry_sha256": reviewed,
        "blockers": blockers, "warnings": warnings,
        "ownership_ready": not blockers, "writes_performed": False,
        "creation_allowed": False, "actualization_complete": False,
        "next_action": {"type": "resolve-ownership" if blockers else "review-execution-facts"},
    }
