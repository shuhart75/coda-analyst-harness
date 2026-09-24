from __future__ import annotations

import hashlib
from decimal import Decimal
from importlib import import_module
import os
from pathlib import Path
import re
import subprocess

from tracker_scope import inside_project, select_features
from tracker_registry import read_registry


REGISTRY = re.compile(r"features/[^/]+/(?:slices/[^/]+/)?execution/tasks\.md")
KEY = re.compile(r"([A-Z][A-Z0-9_]*-[1-9][0-9]*)(?:/(AN|BE|FE|QA))?")
ROLES = {"AN", "BE", "FE", "QA"}


def feature_qa_estimate(issues: list[dict]) -> dict:
    total = Decimal("0")
    sources, missing, seen = [], [], set()
    for issue in issues:
        identity = (issue.get("jira_key"), issue.get("sbertrek_key"))
        if identity in seen:
            continue
        seen.add(identity)
        key = issue.get("jira_key") or issue.get("sbertrek_key")
        estimate = issue.get("role_estimates", {}).get("QA")
        if estimate is None:
            missing.append(key)
            continue
        if estimate.get("unit") not in {"story-points", "person-days"}:
            raise ValueError("QA estimate requires the agreed person-day unit")
        value = Decimal(str(estimate["value"]))
        if not value.is_finite() or value < 0:
            raise ValueError("Invalid QA estimate")
        total += value
        sources.append({"key": key, "value": float(value)})
    return {"value": float(total) if sources else None, "unit": "person-days",
            "sources": sources, "unestimated_keys": missing,
            "basis": "sum-of-populated-card-qa-estimates", "target_count": 1}


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
    def selection():
        if result.get("scope", {}).get("kind") == "release" and not quarter and not feature:
            from tracker_release import release_owners
            return dict.fromkeys(release_owners(project, result)["selected_features"])
        return select_features(project, quarter, feature)
    selected = selection()
    overlay = import_module("sync-actual-progress-overlay")
    paths = registry_paths(project)
    sources, rows, warnings, blockers = {}, [], [], []
    for path in paths:
        relative = path.relative_to(project).as_posix()
        if not path.is_file():
            raise ValueError(f"Реестр недоступен: {relative}")
        sources[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        try:
            tables, note = read_registry(path, identity_only=path.relative_to(project).parts[1] not in selected)
        except ValueError:
            blockers.append({"reason": "unreadable-registry", "registry": relative})
            continue
        if note:
            warnings.append({"reason": note, "registry": relative})
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
                    "saved_facts": {name: row.get(name, "") for name in
                                    ("Actual Start", "Actual Finish", "Completed By", "Status", "Progress %", "Estimate", "Estimate (дн)", "Details", "Notes")},
                    "registry_fields": dict(row),
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

    proposed_rows = []
    if result.get("scope", {}).get("kind") == "release":
        for issue in result["issues"]:
            owner, role = issue.get("confirmed_feature"), issue.get("task_role")
            if not owner or role not in {"BE", "FE"}:
                continue
            if any(issue.get(field) and row.get(field) == issue[field]
                   for row in rows for field in ("jira_key", "sbertrek_key")):
                continue
            identity = issue.get("jira_key") or issue.get("sbertrek_key")
            reference = {
                "feature": owner, "registry": f"features/{owner}/execution/tasks.md",
                "table": None, "row": None, "task_id": f"{identity}/{role}",
                "summary": issue.get("summary"),
                "role": role, "kind": "real", "jira_key": issue.get("jira_key"),
                "sbertrek_key": issue.get("sbertrek_key"), "invalid_key": False,
                "saved_facts": {}, "uncommitted": False, "registration_required": True,
            }
            proposed_rows.append(reference)
        rows.extend(proposed_rows)

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
        if issue.get("confirmed_feature") and owners != [issue["confirmed_feature"]]:
            reasons.append("confirmed-owner-conflicts-with-registry")
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
        if issue.get("task_role") in {"FE", "BE"} and any(
            row["role"] in {"FE", "BE"} and row["role"] != issue["task_role"] for row in matched
        ):
            reasons.append("registry-role-conflicts-with-task-prefix")
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
            if item["role"] == "QA" and result.get("scope", {}).get("tracker_mode") == "single":
                continue
            if item["role"] == "AN" and any(row.get("registration_required") for row in matched):
                continue
            if not any(row["role"] == item["role"] for row in matched):
                reasons.append(f"new-role-needs-confirmation:{item['role']}")
        identities = {item["work_item_id"] for item in roles} | {key for key in (jira_key, sbertrek_key) if key}
        identities.update(row["task_id"] for row in matched if row.get("registration_required"))
        local_collisions = [row for row in rows if row not in matched and row["task_id"] in identities]
        if local_collisions:
            reasons.append("unconfirmed-internal-id-collision")
        if issue.get("reason") == "jira-counterpart-absent":
            reasons.append("excluded-counterpart-needs-disposition")
        entry = {
            "tracker_key": identity, "jira_key": jira_key, "sbertrek_key": sbertrek_key,
            "summary": issue.get("summary"),
            "owners": owners, "targets": matched, "work_item_ids": [item["work_item_id"] for item in roles],
            "internal_id_collisions": local_collisions, "blockers": sorted(set(reasons)),
            "proposed_action": "delete-current-execution" if issue.get("reason") == "confirmed-source-deletion" else "update",
            "deletion_evidence": issue.get("evidence") if issue.get("reason") == "confirmed-source-deletion" else None,
            "role_estimates": issue.get("role_estimates", {}),
        }
        items.append(entry)
        blockers.extend({"tracker_key": identity, "reason": reason} for reason in entry["blockers"])

    if not items and not result.get("skipped"):
        blockers.append({"reason": "no-reconciled-tasks"})
    qa_proposals = []
    for selected_feature in selected:
        owned = [issue for issue, item in zip([*result["issues"], *result["excluded"]], items)
                 if item["owners"] == [selected_feature] and issue in result["issues"]]
        targets = [row for row in rows if row["feature"] == selected_feature and row["role"] == "QA"]
        provider = result.get("scope", {}).get("provider")
        provider_field = str(provider) + "_key"
        expected_keys = {row[provider_field] for row in rows
                         if row["feature"] == selected_feature and row.get(provider_field)}
        observed_keys = {issue.get(provider_field) for issue in owned}
        observed_keys.update(issue.get(provider_field) for issue in result.get("skipped", []))
        deleted_keys = {issue.get(provider_field) for issue in result["excluded"]
                        if issue.get("reason") == "confirmed-source-deletion"}
        missing_keys = sorted(expected_keys - observed_keys - deleted_keys)
        qa_proposals.append({"feature": selected_feature, "estimate": feature_qa_estimate(owned),
                             "missing_card_keys": missing_keys,
                             "collection_limitations": result.get("limitations", []),
                             "full_feature_estimate_proven": False,
                             "existing_targets": targets,
                             "action": "consolidate" if len(targets) > 1 else "update" if targets else "create",
                             "requires_analyst_review": True})
        from tracker_release import qa_groups
        skipped_keys = {issue.get(provider_field) for issue in result.get("skipped", [])}
        group_rows = [row for row in rows if not row.get(provider_field) or row.get(provider_field) not in skipped_keys]
        groups = qa_groups(project, selected_feature, group_rows, owned, result.get("scope", {}),
                           set(result.get("release_member_keys", [issue.get(provider_field) for issue in result["issues"]])))
        if groups is not None:
            if result.get("scope", {}).get("kind") == "release" and any(
                "result-limit" in limit or "result-incomplete" in limit for limit in result.get("limitations", [])
            ):
                groups = {"ready": False, "reason": "release-collection-incomplete", "path": groups["path"]}
            qa_proposals[-1]["partition"] = groups
            qa_proposals[-1]["action"] = "review-partition" if groups.get("changed") else "update-groups"
    scope = result.get("scope", {})
    provider = scope.get("provider")
    application_scope = None
    if scope.get("kind") == "release":
        member_keys = set(result.get("release_member_keys", [
            issue.get(str(provider) + "_key") for issue in [*result["issues"], *result.get("skipped", [])]
            if issue.get(str(provider) + "_key")]))
        for item in items:
            if item.get(str(provider) + "_key") not in member_keys:
                item["proposed_action"] = "reference-only"
        application_scope = {
            "kind": "release-members-only", "provider": provider, "release": scope["ids"][0],
            "member_keys": sorted(member_keys),
            "protected_rows": [row for row in rows if row["feature"] in selected
                               and row["role"] != "QA" and row.get(str(provider) + "_key") not in member_keys
                               and not row.get("registration_required")],
        }
        for proposal in qa_proposals:
            partition = proposal.get("partition", {})
            remainder_ids = {group["task_id"] for group in partition.get("groups", [])
                             if group["release"] != scope["ids"][0]}
            application_scope["protected_rows"].extend(
                {**row, "allowed_fields": ["Estimate", "Estimate (дн)"]}
                for row in proposal["existing_targets"] if row["task_id"] in remainder_ids)
    returned = {issue.get(str(provider) + "_key") for issue in result["issues"]}
    returned.update(issue.get(str(provider) + "_key") for issue in result.get("skipped", []))
    returned.update(issue.get(str(provider) + "_key") for issue in result["excluded"]
                    if issue.get("reason") == "confirmed-source-deletion")
    missing_candidates = [
        {"key": key, "provider": provider, "absence_proven": False,
         "targets": [row for row in rows if row["feature"] in selected and row.get(str(provider) + "_key") == key],
         "next_action": "verify-absence-or-access", "deletion_allowed": False}
        for key in scope.get("ids", []) if scope.get("kind") == "tasks" and key not in returned
    ]
    if selection() != selected:
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
        "selected_features": list(selected), "items": items,
        "application_scope": application_scope,
        "feature_qa_proposals": qa_proposals, "missing_task_candidates": missing_candidates,
        "registry_sha256": sources, "reviewed_registry_sha256": reviewed,
        "proposed_registrations": proposed_rows,
        "blockers": blockers, "warnings": warnings,
        "ownership_ready": not blockers, "writes_performed": False,
        "creation_allowed": False, "actualization_complete": False,
        "before_any_registry_write": {
            "type": "application-preflight", "required": True,
            "features": list(selected), "main_allowed": False,
            "includes": ["new-work-items", "identity-columns", "epic-associations", "facts"],
        },
        "next_action": {"type": "resolve-ownership" if blockers else "review-execution-facts"},
    }
