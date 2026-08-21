#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REVISION_STATES = {
    "draft",
    "sent",
    "in-progress",
    "paused",
    "receipt-received",
    "reviewed",
    "superseded",
    "archived",
    "cancelled",
}
SDD_ACTIONS = {"wait", "process", "continue", "stop-and-report", "no-action"}
RECEIPT_EXPECTATIONS = {"required", "optional", "not-expected", "received"}
TERMINAL_STATES = {"reviewed", "superseded", "archived", "cancelled"}
FEATURE_REVISION_STATES = {
    "draft",
    "sent",
    "in-progress",
    "paused",
    "superseded",
    "archived",
    "cancelled",
}
DECOMPOSITION_STATUSES = {
    "proposed",
    "confirmed-by-development",
    "replaced",
    "split",
    "merged",
    "cancelled",
}
IMPLEMENTATION_STATUSES = {
    "delivered",
    "delivered-with-deviations",
    "partially-delivered",
    "no-change-required",
    "not-delivered",
}
ITEM_RESULT_STATUSES = {
    "already-implemented",
    "implemented-as-required",
    "implemented-with-deviation",
    "implemented-with-scope-change",
    "partially-implemented",
    "not-implemented",
    "deferred",
    "blocked-dependency",
    "blocked-input-ambiguity",
    "not-applicable",
}
TEST_STATUSES = {"passed", "passed-with-findings", "failed", "blocked", "partial"}
TASK_ID_RE = re.compile(r"DEV-(BE|FE)-[0-9]+")
REQ_ID_RE = re.compile(r"\bREQ-[A-Z0-9-]+\b")
SCN_ID_RE = re.compile(r"\bSCN-[A-Z0-9-]+\b")
IMP_ID_RE = re.compile(r"\bIMP-[A-Z0-9-]+\b")
TRACE_ID_RANGE_RE = re.compile(
    r"\b((?:REQ|SCN|IMP)-[A-Z0-9-]*?)([0-9]+)\s*[—–]\s*"
    r"((?:REQ|SCN|IMP)-[A-Z0-9-]*?)([0-9]+)\b"
)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
STABLE_SECTION_RE = re.compile(r"^([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\s+(?:—|-)\s+(.+)$")
SLICE_CARD_RE = re.compile(r"Карточка среза:\s*`([^`]+)`", re.IGNORECASE)
PACKAGE_PLACEHOLDERS = (
    "<package-id>",
    "<revision>",
    "<feature-slug>",
    "<requirements-path>",
    "<Название функциональности>",
    "<Законченный пользовательский",
    "<Обязательное ограничение",
    "<Текущее состояние",
    "<Характерный положительный",
    "<Наблюдаемый критерий",
    "<slices-path-or-list>",
)
TASK_CARD_SECTIONS = (
    "Карточка задачи",
    "Пользовательский или системный результат",
    "Требования",
    "Сценарии и влияния",
    "Состояние кода",
    "Состав работы",
    "Не входит",
    "Условия приёмки",
    "Проверки",
    "Открытые вопросы",
    "Короткие команды разработчика",
)
INDEX_COMMANDS = (
    "Подготовь декомпозицию серверной части.",
    "Подготовь декомпозицию клиентской части.",
    "Проверь декомпозицию.",
    "Покажи непокрытые требования.",
    "Подготовь список для Jira.",
    "Декомпозиция подтверждена разработкой.",
)


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def revision_name(revision: int) -> str:
    return f"{revision:03d}"


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_hashes(package: Path) -> dict[str, str]:
    return {
        path.relative_to(package).as_posix(): hash_file(path)
        for path in sorted(package.rglob("*"))
        if path.is_file()
    }


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def save(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def root_manifest(root: Path) -> Path:
    return root / "handoff.json"


def revision_entry(manifest: dict[str, Any], revision: int) -> dict[str, Any]:
    matches = [item for item in manifest.get("revisions", []) if item.get("revision") == revision]
    if len(matches) != 1:
        raise ValueError(f"Revision {revision_name(revision)} is not registered exactly once")
    return matches[0]


def is_feature_manifest(manifest: dict[str, Any]) -> bool:
    return manifest.get("schema_version") in {2, 3} and manifest.get("package_kind") == "feature-delivery"


def relative_path(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and not Path(value).is_absolute() and ".." not in Path(value).parts


def markdown_sections(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    headings: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2).strip()))
    result: list[dict[str, Any]] = []
    for position, (start, level, title) in enumerate(headings):
        end = len(lines)
        for next_start, next_level, _ in headings[position + 1:]:
            if next_level <= level:
                end = next_start
                break
        result.append({
            "level": level,
            "title": title,
            "body": "\n".join(lines[start + 1:end]).strip(),
        })
    return result


def feature_contract(requirements_text: str) -> dict[str, Any]:
    requirements = sorted(set(REQ_ID_RE.findall(requirements_text)))
    legacy_sections: list[dict[str, str]] = []
    if not requirements:
        for section in markdown_sections(requirements_text):
            match = STABLE_SECTION_RE.match(section["title"])
            slice_match = SLICE_CARD_RE.search(section["body"])
            if not match or not slice_match:
                continue
            section_id = match.group(1)
            if section_id.startswith(("REQ-", "SCN-", "IMP-", "AC-", "DEV-", "CAND-", "STORY-")):
                continue
            legacy_sections.append({
                "id": section_id,
                "title": match.group(2).strip(),
                "slice_path": Path(slice_match.group(1)).as_posix(),
                "body": section["body"],
            })
        requirements = sorted({item["id"] for item in legacy_sections})
    mode = "atomic-identifiers" if REQ_ID_RE.search(requirements_text) else "legacy-sections"
    return {
        "requirements": requirements,
        "scenarios": sorted(set(SCN_ID_RE.findall(requirements_text))),
        "impacts": sorted(set(IMP_ID_RE.findall(requirements_text))),
        "traceability": {
            "mode": mode,
            "requirement_unit": "REQ-*" if mode == "atomic-identifiers" else "section-with-slice-card",
            "note": (
                "Атомарная трассировка по устойчивым идентификаторам требований."
                if mode == "atomic-identifiers"
                else "Совместимость с прежним форматом: единицей трассировки является раздел с устойчивым идентификатором и ссылкой на карточку среза."
            ),
        },
        "legacy_sections": legacy_sections,
    }


def referenced_identifiers(text: str, pattern: re.Pattern[str], known: set[str]) -> set[str]:
    identifiers = set(pattern.findall(text)) & known
    for match in TRACE_ID_RANGE_RE.finditer(text):
        left_prefix, left_number, right_prefix, right_number = match.groups()
        if left_prefix != right_prefix or len(left_number) != len(right_number):
            continue
        start = int(left_number)
        end = int(right_number)
        if end < start or end - start > 1000:
            continue
        width = len(left_number)
        identifiers.update(
            identifier
            for number in range(start, end + 1)
            if (identifier := f"{left_prefix}{number:0{width}d}") in known
        )
    return identifiers


def slice_contract_from_files(
    contract: dict[str, Any],
    slice_path: str,
    texts: list[str],
) -> dict[str, list[str]]:
    combined = "\n".join(texts)
    known_requirements = set(contract["requirements"])
    requirements = referenced_identifiers(combined, REQ_ID_RE, known_requirements)
    if contract["traceability"]["mode"] == "legacy-sections":
        requirements.update(
            item["id"]
            for item in contract["legacy_sections"]
            if Path(item["slice_path"]).as_posix() == Path(slice_path).as_posix()
        )
    return {
        "requirements": sorted(requirements),
        "scenarios": sorted(referenced_identifiers(combined, SCN_ID_RE, set(contract["scenarios"]))),
    }


def validate_task_card(card_text: str, task: dict[str, Any]) -> list[str]:
    task_id = task["id"]
    errors: list[str] = []
    if task_id not in card_text:
        errors.append(f"{task_id}: card must contain its id")
    if not re.search(
        r"^\|\s*Состояние декомпозиции\s*\|\s*подтверждена разработкой\s*\|\s*$",
        card_text,
        flags=re.MULTILINE | re.IGNORECASE,
    ):
        errors.append(f"{task_id}: card decomposition state must be confirmed-by-development")
    expected_contour = task.get("contour")
    if isinstance(expected_contour, str) and not re.search(
        rf"^\|\s*Контур\s*\|\s*`?{re.escape(expected_contour)}`?\s*\|\s*$",
        card_text,
        flags=re.MULTILINE | re.IGNORECASE,
    ):
        errors.append(f"{task_id}: card contour must be {expected_contour}")
    sections = {item["title"]: item["body"] for item in markdown_sections(card_text)}
    for name in TASK_CARD_SECTIONS:
        if name not in sections or not sections[name].strip():
            errors.append(f"{task_id}: card section is missing or empty: {name}")
    content = card_text.split("## Короткие команды разработчика", 1)[0]
    placeholders = re.findall(r"<[^>\n]+>", content)
    if placeholders:
        errors.append(f"{task_id}: card contains unfilled placeholders: {', '.join(sorted(set(placeholders)))}")
    for key in ("requirements", "scenarios", "impacts"):
        for identifier in task.get(key, []):
            if identifier not in card_text:
                errors.append(f"{task_id}: card does not contain assigned {key} id {identifier}")
    command_fragments = (
        f"Уточни {task_id} по коду.",
        f"Измени состав {task_id}:",
        f"Переименуй {task_id}:",
        "Перенеси <REQ/SCN/IMP> в <задача>.",
        f"Объедини {task_id} и <задача>.",
        f"Раздели {task_id}:",
        f"Добавь зависимость {task_id} от <задача>.",
        f"Убери зависимость {task_id} от <задача>.",
        f"{task_id} уже реализована. Проверь.",
        f"Отмени {task_id}:",
        f"{task_id} подтверждена разработкой.",
        f"Свяжи {task_id} с <ключ Jira>.",
        f"Возьми {task_id} в разработку.",
    )
    for fragment in command_fragments:
        if fragment not in card_text:
            errors.append(f"{task_id}: developer command is missing: {fragment}")
    return errors


def copy_control(root: Path, template_names: tuple[str, ...]) -> None:
    control = root / ".control"
    control.mkdir()
    shutil.copy2(Path(__file__).resolve(), control / "handoffctl.py")
    shutil.copy2(Path(__file__).resolve().with_name("validate-handoff.py"), control / "validate-handoff.py")
    control_templates = control / "templates"
    control_templates.mkdir()
    template_root = Path(__file__).resolve().parents[1] / "templates" / "handoff"
    for name in template_names:
        shutil.copy2(template_root / name, control_templates / name)


def update_next_action(manifest: dict[str, Any]) -> None:
    feature = is_feature_manifest(manifest)
    active = manifest.get("active_revision")
    if active is None:
        manifest["status"] = "inactive"
        action = {
            "action": "wait",
            "reason": "Нет редакции, разрешённой к обработке",
            "revision": None,
            "revision_state": None,
            "claimed_by": None,
            "package_path": None,
            "transport_path": None,
        }
        action["returns_path" if feature else "receipt_path"] = None
        manifest["next_sdd_action"] = action
        return
    entry = revision_entry(manifest, active)
    state = entry["state"]
    action = entry["sdd_action"]
    manifest["status"] = "active" if action in {"process", "continue", "stop-and-report"} else "waiting"
    next_action = {
        "action": action,
        "reason": entry.get("reason"),
        "revision": active,
        "revision_state": state,
        "claimed_by": entry.get("claimed_by"),
        "package_path": entry["package_path"],
        "transport_path": entry.get("transport_path"),
    }
    next_action["returns_path" if feature else "receipt_path"] = entry.get("returns_path") if feature else entry["receipt_path"]
    manifest["next_sdd_action"] = next_action


def validate_feature_package(package: Path) -> list[str]:
    errors: list[str] = []
    for name in ("README.md", "request.md", "manifest.json"):
        if not (package / name).is_file():
            errors.append(f"feature package file is missing: {name}")
    if errors:
        return errors
    try:
        manifest = load(package / "manifest.json")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [str(exc)]
    if manifest.get("schema_version") != 1 or manifest.get("package_kind") != "feature-requirements-to-delivery":
        errors.append("feature manifest has unsupported schema or package_kind")
    for key in ("requirements", "scenarios", "impacts", "slices", "payload"):
        if not isinstance(manifest.get(key), list):
            errors.append(f"feature manifest {key} must be an array")
    payload = manifest.get("payload", [])
    paths: list[str] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict) or not relative_path(item.get("path")):
            errors.append("feature manifest payload contains invalid path")
            continue
        path = package / item["path"]
        paths.append(item["path"])
        if not path.is_file():
            errors.append(f"feature payload is missing: {item['path']}")
        elif item.get("sha256") != hash_file(path):
            errors.append(f"feature payload checksum mismatch: {item['path']}")
    if len(paths) != len(set(paths)):
        errors.append("feature manifest payload contains duplicate paths")
    actual_payload_paths = {
        path.relative_to(package).as_posix()
        for path in package.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if set(paths) != actual_payload_paths:
        missing = sorted(actual_payload_paths - set(paths))
        extra = sorted(set(paths) - actual_payload_paths)
        if missing:
            errors.append(f"feature manifest payload omits files: {', '.join(missing)}")
        if extra:
            errors.append(f"feature manifest payload lists unexpected files: {', '.join(extra)}")
    for key in ("requirements", "scenarios", "impacts"):
        values = manifest.get(key, [])
        if isinstance(values, list) and (any(not isinstance(value, str) or not value for value in values) or len(values) != len(set(values))):
            errors.append(f"feature manifest {key} must contain unique identifiers")
    requirements_text = (package / "requirements.md").read_text(encoding="utf-8", errors="ignore") if (package / "requirements.md").is_file() else ""
    actual_contract = feature_contract(requirements_text)
    if not actual_contract["requirements"]:
        errors.append("feature package has no traceable requirements: add REQ-* ids or stable sections with Карточка среза")
    for key in ("requirements", "scenarios", "impacts"):
        if manifest.get(key) != actual_contract[key]:
            errors.append(f"feature manifest {key} does not match requirements.md")
    if manifest.get("traceability") != actual_contract["traceability"]:
        errors.append("feature manifest traceability does not match requirements.md")
    request_text = (package / "request.md").read_text(encoding="utf-8", errors="ignore")
    for placeholder in PACKAGE_PLACEHOLDERS:
        if placeholder in request_text or placeholder in json.dumps(manifest, ensure_ascii=False):
            errors.append(f"feature package contains unfilled placeholder: {placeholder}")

    slices = manifest.get("slices", [])
    slice_ids: list[str] = []
    for item in slices if isinstance(slices, list) else []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            slice_ids.append(item["id"])
            expected_path = f"slices/{item['id']}/slice.md"
            if item.get("path") != expected_path:
                errors.append(f"slice {item['id']} path must be {expected_path}")
            slice_root = package / "slices" / item["id"]
            slice_texts = [
                path.read_text(encoding="utf-8", errors="ignore")
                for path in sorted(slice_root.rglob("*.md"))
            ] if slice_root.is_dir() else []
            actual_slice = slice_contract_from_files(actual_contract, expected_path, slice_texts)
            for key in ("requirements", "scenarios"):
                if not isinstance(item.get(key, []), list):
                    errors.append(f"slice {item['id']} {key} must be an array")
                elif item.get(key) != actual_slice[key]:
                    errors.append(f"slice {item['id']} {key} does not match packaged files")
        else:
            errors.append("feature manifest slices contains invalid item")
    if len(slice_ids) != len(set(slice_ids)):
        errors.append("feature manifest slices contains duplicate ids")
    actual_slice_ids = {
        path.parent.name
        for path in (package / "slices").glob("*/slice.md")
    } if (package / "slices").is_dir() else set()
    if set(slice_ids) != actual_slice_ids:
        errors.append("feature manifest slices do not match packaged slice directories")
    return errors


def validate_feature_root(root: Path, manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema_version") not in {2, 3}:
        errors.append("feature handoff has unsupported schema_version")
    if not isinstance(manifest.get("package_id"), str) or not manifest["package_id"]:
        errors.append("package_id is required")
    if manifest.get("schema_version") == 3:
        expected_transport_policy = {
            "creation": "on-request",
            "repository_archives": "forbidden",
            "destination": "~/Downloads",
        }
        if manifest.get("transport_policy") != expected_transport_policy:
            errors.append("feature handoff transport_policy is invalid")
    agent_contract = manifest.get("agent_contract")
    if manifest.get("schema_version") == 3 or agent_contract is not None:
        if not isinstance(agent_contract, dict):
            errors.append("agent_contract is required for feature handoff schema 3")
        else:
            expected_contract = {
                "path": "AGENTS.md",
                "schema_version": 1,
                "required": True,
            }
            for key, value in expected_contract.items():
                if agent_contract.get(key) != value:
                    errors.append(f"agent_contract {key} must be {value!r}")
            agent_path_value = agent_contract.get("path")
            if not relative_path(agent_path_value):
                errors.append("agent_contract path must be a relative path")
            else:
                agent_path = root / agent_path_value
                if not agent_path.is_file():
                    errors.append(f"agent contract file is missing: {agent_path_value}")
                else:
                    if agent_contract.get("sha256") != hash_file(agent_path):
                        errors.append("agent contract checksum mismatch")
                    agent_text = agent_path.read_text(encoding="utf-8", errors="ignore")
                    for fragment in (
                        "# Контракт SDD",
                        "## Область действия и приоритет",
                        "## Обязательный порядок начала работы",
                        "## Ограничение контекста",
                        "## Техническая декомпозиция",
                        "## Подтверждение декомпозиции",
                        "## Реализация",
                        "## Тестирование",
                        "## Запреты",
                        "next_sdd_action",
                    ):
                        if fragment not in agent_text:
                            errors.append(f"agent contract is incomplete: {fragment}")
    required = (
        "README.md",
        ".control/handoffctl.py",
        ".control/validate-handoff.py",
        ".control/templates/feature-package-readme.template.md",
        ".control/templates/feature-request.template.md",
        ".control/templates/feature-manifest.template.json",
        ".control/templates/development-tasks-instruction.template.md",
        ".control/templates/development-tasks-index.template.md",
        ".control/templates/development-task-card.template.md",
        ".control/templates/decomposition-receipt.template.json",
        ".control/templates/implementation-receipt.template.json",
        ".control/templates/test-receipt.template.json",
    )
    for path in required:
        if not (root / path).is_file():
            errors.append(f"shared feature handoff file is missing: {path}")
    revisions = manifest.get("revisions")
    if not isinstance(revisions, list):
        return errors + ["revisions must be an array"]
    ids = [item.get("revision") for item in revisions if isinstance(item, dict)]
    if len(ids) != len(revisions) or len(ids) != len(set(ids)) or any(not isinstance(item, int) or item < 1 for item in ids):
        errors.append("revision numbers must be unique positive integers")
    for item in revisions:
        if not isinstance(item, dict):
            continue
        revision = item.get("revision")
        label = revision_name(revision) if isinstance(revision, int) else "?"
        if item.get("state") not in FEATURE_REVISION_STATES:
            errors.append(f"revision {label}: invalid feature revision state")
        if item.get("sdd_action") not in SDD_ACTIONS:
            errors.append(f"revision {label}: invalid sdd_action")
        for key in ("package_path", "returns_path"):
            if not relative_path(item.get(key)):
                errors.append(f"revision {label}: invalid {key}")
        package = root / str(item.get("package_path", ""))
        if not package.is_dir():
            errors.append(f"revision {label}: package directory is missing")
        recorded = item.get("package_files")
        if item.get("state") != "draft" and (not isinstance(recorded, dict) or recorded != package_hashes(package)):
            errors.append(f"revision {label}: immutable package files differ")
        if item.get("transport_path") is not None or item.get("transport_sha256") is not None:
            errors.append(f"revision {label}: repository transport metadata is forbidden")
        decomposition = item.get("decomposition")
        if not isinstance(decomposition, dict):
            errors.append(f"revision {label}: decomposition state is missing")
        else:
            snapshots = decomposition.get("snapshots")
            if not isinstance(snapshots, list):
                errors.append(f"revision {label}: decomposition snapshots must be an array")
            else:
                snapshot_ids = [snapshot.get("revision") for snapshot in snapshots if isinstance(snapshot, dict)]
                if len(snapshot_ids) != len(snapshots) or len(snapshot_ids) != len(set(snapshot_ids)):
                    errors.append(f"revision {label}: decomposition snapshot revisions must be unique")
                for snapshot in snapshots:
                    if not isinstance(snapshot, dict) or not relative_path(snapshot.get("path")):
                        errors.append(f"revision {label}: invalid decomposition snapshot")
                        continue
                    path = root / snapshot["path"]
                    if not path.is_dir() or snapshot.get("files") != package_hashes(path):
                        errors.append(f"revision {label}: decomposition snapshot differs from recorded files")
        for key in ("implementation_results", "test_results"):
            results = item.get(key)
            if not isinstance(results, list):
                errors.append(f"revision {label}: {key} must be an array")
                continue
            for result in results:
                if not isinstance(result, dict) or not relative_path(result.get("path")):
                    errors.append(f"revision {label}: invalid {key} entry")
                    continue
                path = root / result["path"]
                if not path.is_file() or result.get("sha256") != hash_file(path):
                    errors.append(f"revision {label}: {key} receipt mismatch")
    active = manifest.get("active_revision")
    if active is not None and active not in ids:
        errors.append("active_revision is not registered")
    expected = json.loads(json.dumps(manifest, ensure_ascii=False))
    try:
        update_next_action(expected)
    except ValueError as exc:
        errors.append(str(exc))
    if manifest.get("next_sdd_action") != expected.get("next_sdd_action"):
        errors.append("next_sdd_action does not match active feature revision")
    actionable = [item for item in revisions if isinstance(item, dict) and item.get("sdd_action") in {"process", "continue", "stop-and-report"}]
    if len(actionable) > 1:
        errors.append("more than one feature revision is actionable")
    if actionable and (active is None or actionable[0].get("revision") != active):
        errors.append("actionable feature revision does not match active_revision")
    return errors


def validate_root(root: Path) -> list[str]:
    errors: list[str] = []
    path = root_manifest(root)
    try:
        manifest = load(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [str(exc)]
    repository_archives = sorted(path.relative_to(root).as_posix() for path in root.rglob("*.zip"))
    if repository_archives:
        errors.append(f"transport archives inside the handoff repository are forbidden: {', '.join(repository_archives)}")
    if is_feature_manifest(manifest):
        return errors + validate_feature_root(root, manifest)
    if manifest.get("schema_version") != 1:
        errors.append("handoff.json schema_version must be 1")
    if not isinstance(manifest.get("package_id"), str) or not manifest["package_id"]:
        errors.append("package_id is required")
    transport_policy = manifest.get("transport_policy")
    if transport_policy is not None and transport_policy != {
        "creation": "on-request",
        "repository_archives": "forbidden",
        "destination": "~/Downloads",
    }:
        errors.append("handoff transport_policy is invalid")
    for required in (
        "README.md",
        ".control/handoffctl.py",
        ".control/validate-handoff.py",
        ".control/templates/developer-package-readme.template.md",
        ".control/templates/developer-request.template.md",
        ".control/templates/developer-manifest.template.json",
        ".control/templates/developer-receipt.template.json",
        ".control/templates/analyst-receipt-review.template.json",
    ):
        if not (root / required).is_file():
            errors.append(f"shared handoff file is missing: {required}")
    revisions = manifest.get("revisions")
    if not isinstance(revisions, list):
        return errors + ["revisions must be an array"]
    ids = [item.get("revision") for item in revisions if isinstance(item, dict)]
    if len(ids) != len(revisions) or len(ids) != len(set(ids)) or any(not isinstance(item, int) or item < 1 for item in ids):
        errors.append("revision numbers must be unique positive integers")
    for item in revisions:
        if not isinstance(item, dict):
            continue
        revision = item.get("revision")
        label = revision_name(revision) if isinstance(revision, int) else "?"
        if item.get("state") not in REVISION_STATES:
            errors.append(f"revision {label}: invalid state")
        if item.get("sdd_action") not in SDD_ACTIONS:
            errors.append(f"revision {label}: invalid sdd_action")
        receipt = item.get("receipt")
        if not isinstance(receipt, dict) or receipt.get("expectation") not in RECEIPT_EXPECTATIONS:
            errors.append(f"revision {label}: invalid receipt expectation")
        for key in ("package_path", "receipt_path"):
            relative = item.get(key)
            if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
                errors.append(f"revision {label}: invalid {key}")
        package_path = root / str(item.get("package_path", ""))
        if not package_path.is_dir():
            errors.append(f"revision {label}: package directory is missing")
        recorded_files = item.get("package_files")
        if item.get("state") not in {"draft"}:
            if not isinstance(recorded_files, dict) or recorded_files != package_hashes(package_path):
                errors.append(f"revision {label}: package files differ from the recorded immutable revision")
        if item.get("transport_path") is not None or item.get("transport_sha256") is not None:
            errors.append(f"revision {label}: repository transport metadata is forbidden")
        receipt_path = root / str(item.get("receipt_path", ""))
        if isinstance(receipt, dict) and receipt.get("expectation") == "received":
            if not receipt_path.is_file():
                errors.append(f"revision {label}: registered receipt is missing")
            elif receipt.get("sha256") != hash_file(receipt_path):
                errors.append(f"revision {label}: receipt checksum mismatch")
        review_status = item.get("analyst_review_status")
        if review_status not in {"not-started", "pending", "approved"}:
            errors.append(f"revision {label}: invalid analyst_review_status")
        if review_status == "approved":
            review_path = root / str(item.get("analyst_review_path", ""))
            if not review_path.is_file():
                errors.append(f"revision {label}: registered analyst review is missing")
            elif item.get("analyst_review_sha256") != hash_file(review_path):
                errors.append(f"revision {label}: analyst review checksum mismatch")
    active = manifest.get("active_revision")
    if active is not None and active not in ids:
        errors.append("active_revision is not registered")
    expected = json.loads(json.dumps(manifest, ensure_ascii=False))
    try:
        update_next_action(expected)
    except ValueError as exc:
        errors.append(str(exc))
    if manifest.get("next_sdd_action") != expected.get("next_sdd_action"):
        errors.append("next_sdd_action does not match active revision")
    actionable = [item for item in revisions if isinstance(item, dict) and item.get("sdd_action") in {"process", "continue", "stop-and-report"}]
    if len(actionable) > 1:
        errors.append("more than one revision is actionable for SDD")
    if actionable and (active is None or actionable[0].get("revision") != active):
        errors.append("actionable revision does not match active_revision")
    return errors


def init_command(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    root = project / "features" / args.feature / "handoffs" / args.package_id
    if root.exists():
        raise ValueError(f"Handoff root already exists: {root}")
    root.mkdir(parents=True)
    (root / "revisions").mkdir()
    template_root = Path(__file__).resolve().parents[1] / "templates" / "handoff"
    readme = (template_root / "handoff-root-readme.template.md").read_text(encoding="utf-8")
    (root / "README.md").write_text(readme.replace("<package-id>", args.package_id), encoding="utf-8")
    manifest = load(template_root / "handoff-root.template.json")
    manifest.update({"package_id": args.package_id, "feature": args.feature, "role": args.role})
    manifest["source_task"] = {"id": args.source_task_id, "path": args.source_task_path}
    update_next_action(manifest)
    save(root_manifest(root), manifest)
    control = root / ".control"
    control.mkdir()
    shutil.copy2(Path(__file__).resolve(), control / "handoffctl.py")
    shutil.copy2(Path(__file__).resolve().with_name("validate-handoff.py"), control / "validate-handoff.py")
    control_templates = control / "templates"
    control_templates.mkdir()
    for name in (
        "developer-package-readme.template.md",
        "developer-request.template.md",
        "developer-manifest.template.json",
        "developer-receipt.template.json",
        "analyst-receipt-review.template.json",
    ):
        shutil.copy2(template_root / name, control_templates / name)
    print(root)
    return 0


def require_collaboration_main_for_handoff(project: Path, feature: str) -> None:
    harness = Path(__file__).resolve().parents[1]
    state_root = Path(os.environ.get("CODA_ANALYST_STATE_ROOT", harness / ".workspace-state")).resolve()
    state_path = state_root / "collaboration.json"
    if not state_path.is_file():
        workspace_path = state_root / "workspace.json"
        if workspace_path.is_file():
            workspace = load(workspace_path)
            analytics = workspace.get("roles", {}).get("analytics", {})
            analytics_path = analytics.get("path") if isinstance(analytics, dict) else None
            if analytics_path and Path(analytics_path).resolve() == project:
                raise ValueError(
                    "Совместная работа ещё не настроена. Это незавершённая одноразовая миграция; "
                    "до неё пакет и производные материалы создавать нельзя"
                )
        return
    state = load(state_path)
    if state.get("schema_version") != 1 or state.get("mode") != "multi-user-branches":
        raise ValueError(f"Повреждена настройка совместной работы: {state_path}")
    if state.get("active_work"):
        raise ValueError(
            f"Функциональность {feature} находится в незавершённой рабочей ветке; "
            "пакет разрешено создавать только после принятия ветки в main"
        )
    branch = subprocess.run(
        ("git", "-C", str(project), "symbolic-ref", "--quiet", "--short", "HEAD"),
        text=True,
        capture_output=True,
        check=False,
    )
    if branch.returncode != 0 or branch.stdout.strip() != "main":
        raise ValueError("Пакет для разработки разрешено создавать только из ветки analytics/main")
    fetched = subprocess.run(
        ("git", "-C", str(project), "fetch", "origin", "main"),
        text=True,
        capture_output=True,
        check=False,
    )
    if fetched.returncode != 0:
        raise ValueError(
            "Не удалось получить актуальную origin/main перед созданием пакета: "
            f"{fetched.stderr.strip()}"
        )
    local = subprocess.run(
        ("git", "-C", str(project), "rev-parse", "HEAD"),
        text=True,
        capture_output=True,
        check=False,
    )
    remote = subprocess.run(
        ("git", "-C", str(project), "rev-parse", "origin/main"),
        text=True,
        capture_output=True,
        check=False,
    )
    if local.returncode or remote.returncode or local.stdout.strip() != remote.stdout.strip():
        raise ValueError(
            "Локальная analytics/main не совпадает с актуальной origin/main; "
            "перед созданием пакета выполни проверку require-main-for-delivery"
        )


def init_feature_command(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    require_collaboration_main_for_handoff(project, args.feature)
    root = project / "features" / args.feature / "handoffs" / args.package_id
    if root.exists():
        raise ValueError(f"Handoff root already exists: {root}")
    root.mkdir(parents=True)
    (root / "revisions").mkdir()
    template_root = Path(__file__).resolve().parents[1] / "templates" / "handoff"
    readme = (template_root / "handoff-root-feature-readme.template.md").read_text(encoding="utf-8")
    (root / "README.md").write_text(readme.replace("<package-id>", args.package_id), encoding="utf-8")
    agents = (template_root / "handoff-root-feature-agents.template.md").read_text(encoding="utf-8")
    agents_path = root / "AGENTS.md"
    agents_path.write_text(agents.replace("<package-id>", args.package_id), encoding="utf-8")
    manifest = load(template_root / "handoff-root.feature.template.json")
    manifest.update({"package_id": args.package_id, "feature": args.feature})
    manifest["source_requirements"] = {"path": args.requirements_path or f"features/{args.feature}/requirements.md"}
    manifest["agent_contract"]["sha256"] = hash_file(agents_path)
    update_next_action(manifest)
    save(root_manifest(root), manifest)
    copy_control(root, (
        "feature-package-readme.template.md",
        "feature-request.template.md",
        "feature-manifest.template.json",
        "development-tasks-instruction.template.md",
        "development-tasks-index.template.md",
        "development-task-card.template.md",
        "decomposition-receipt.template.json",
        "implementation-receipt.template.json",
        "test-receipt.template.json",
    ))
    print(root)
    return 0


def add_feature_revision(root: Path, manifest: dict[str, Any], args: argparse.Namespace) -> int:
    revision = args.revision
    if any(item.get("revision") == revision for item in manifest.get("revisions", [])):
        raise ValueError(f"Revision already exists: {revision_name(revision)}")
    project = root.parents[3]
    preparation_state = project / "features" / manifest["feature"] / "requirements-state.json"
    if preparation_state.is_file():
        state = load(preparation_state)
        offer = state.get("revision_offer")
        if not isinstance(offer, dict) or offer.get("state") != "preparation-authorized":
            raise ValueError(
                "Подготовка редакции функциональности не разрешена; "
                "после явной команды аналитика выполни requirementsctl.py begin-preparation"
            )
    name = revision_name(revision)
    revision_root = root / "revisions" / name
    package = revision_root / "package"
    returns = revision_root / "returns"
    package.mkdir(parents=True)
    returns.mkdir()
    development_tasks = returns / "development-tasks"
    development_tasks.mkdir()
    (returns / "decomposition-snapshots").mkdir()
    (returns / "implementation-results").mkdir()
    (returns / "test-results").mkdir()
    templates = root / ".control/templates"
    replacements = {
        "<package-id>": manifest["package_id"],
        "<revision>": str(revision),
        "<feature-slug>": manifest["feature"],
        "<requirements-path>": Path(manifest["source_requirements"]["path"]).name,
    }
    for source_name, target in (
        ("feature-package-readme.template.md", package / "README.md"),
        ("feature-request.template.md", package / "request.md"),
        ("feature-manifest.template.json", package / "manifest.json"),
        ("development-tasks-instruction.template.md", development_tasks / "README.md"),
        ("development-tasks-index.template.md", development_tasks / "index.md"),
        ("decomposition-receipt.template.json", returns / "decomposition-receipt.json"),
    ):
        text = (templates / source_name).read_text(encoding="utf-8")
        for old, new in replacements.items():
            text = text.replace(old, new)
        target.write_text(text, encoding="utf-8")
    requirements_source = project / manifest["source_requirements"]["path"]
    if not requirements_source.is_file():
        raise ValueError(f"Feature requirements are missing: {requirements_source}")
    requirements_target = package / "requirements.md"
    shutil.copy2(requirements_source, requirements_target)
    requirements_text = requirements_target.read_text(encoding="utf-8", errors="ignore")
    feature_payload = load(package / "manifest.json")
    contract = feature_contract(requirements_text)
    feature_payload.update({
        "package_id": manifest["package_id"],
        "package_revision": revision,
        "feature": manifest["feature"],
        "requirements": contract["requirements"],
        "scenarios": contract["scenarios"],
        "impacts": contract["impacts"],
        "traceability": contract["traceability"],
    })
    payload = [{
        "path": "requirements.md",
        "sha256": hash_file(requirements_target),
        "purpose": "Полные требования функциональности",
    }]
    slices: list[dict[str, Any]] = []
    slices_root = project / "features" / manifest["feature"] / "slices"
    if slices_root.is_dir():
        for source in sorted(slices_root.glob("*/slice.md")):
            slice_id = source.parent.name
            relative = Path("slices") / slice_id / "slice.md"
            target = package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            slice_texts = [target.read_text(encoding="utf-8", errors="ignore")]
            payload.append({
                "path": relative.as_posix(),
                "sha256": hash_file(target),
                "purpose": f"Срез для проверки: {slice_id}",
            })
            detailed_root = source.parent / "requirements"
            if detailed_root.is_dir():
                for detailed_source in sorted(detailed_root.rglob("*.md")):
                    detailed_relative = Path("slices") / slice_id / detailed_source.relative_to(source.parent)
                    detailed_target = package / detailed_relative
                    detailed_target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(detailed_source, detailed_target)
                    slice_texts.append(detailed_target.read_text(encoding="utf-8", errors="ignore"))
                    payload.append({
                        "path": detailed_relative.as_posix(),
                        "sha256": hash_file(detailed_target),
                        "purpose": f"Подробные требования среза: {slice_id}",
                    })
            ids = slice_contract_from_files(contract, relative.as_posix(), slice_texts)
            slices.append({
                "id": slice_id,
                "path": relative.as_posix(),
                "requirements": ids["requirements"],
                "scenarios": ids["scenarios"],
            })
    request_text, title = render_feature_request(
        templates / "feature-request.template.md",
        replacements,
        requirements_text,
        [item["id"] for item in slices],
        contract,
    )
    (package / "request.md").write_text(request_text, encoding="utf-8")
    index_path = development_tasks / "index.md"
    index_path.write_text(
        index_path.read_text(encoding="utf-8").replace("<Название>", title),
        encoding="utf-8",
    )
    feature_payload["request"] = {"title": title, "requirements_path": "requirements.md"}
    feature_payload["payload"] = [
        {"path": "README.md", "sha256": hash_file(package / "README.md"), "purpose": "Инструкция принимающей SDD"},
        {"path": "request.md", "sha256": hash_file(package / "request.md"), "purpose": "Краткий паспорт функциональности"},
        *payload,
    ]
    feature_payload["slices"] = slices
    save(package / "manifest.json", feature_payload)
    timestamp = now()
    manifest.setdefault("revisions", []).append({
        "revision": revision,
        "state": "draft",
        "sdd_action": "wait",
        "reason": "Редакция требований формируется",
        "created_at": timestamp,
        "sent_at": None,
        "claimed_at": None,
        "claimed_by": None,
        "package_path": f"revisions/{name}/package",
        "returns_path": f"revisions/{name}/returns",
        "transport_path": None,
        "transport_sha256": None,
        "package_files": None,
        "decomposition": {
            "status": "not-started",
            "working_path": f"revisions/{name}/returns/development-tasks",
            "receipt_path": f"revisions/{name}/returns/decomposition-receipt.json",
            "current_revision": None,
            "snapshots": [],
        },
        "implementation_results": [],
        "test_results": [],
        "replaces_revision": args.replaces,
    })
    manifest["revisions"].sort(key=lambda item: item["revision"])
    update_next_action(manifest)
    save(root_manifest(root), manifest)
    print(revision_root)
    return 0


def add_revision_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest = load(root_manifest(root))
    if is_feature_manifest(manifest):
        return add_feature_revision(root, manifest, args)
    revision = args.revision
    if any(item.get("revision") == revision for item in manifest.get("revisions", [])):
        raise ValueError(f"Revision already exists: {revision_name(revision)}")
    name = revision_name(revision)
    revision_root = root / "revisions" / name
    (revision_root / "package").mkdir(parents=True)
    (revision_root / "returns").mkdir()
    templates = root / ".control/templates"
    replacements = {
        "<package-id>": manifest["package_id"],
        "<revision>": name,
        "<BE|FE>": manifest.get("role", "<BE|FE>"),
    }
    for source_name, target_name in (
        ("developer-package-readme.template.md", "README.md"),
        ("developer-request.template.md", "request.md"),
        ("developer-manifest.template.json", "manifest.json"),
        ("developer-receipt.template.json", "receipt.template.json"),
    ):
        text = (templates / source_name).read_text(encoding="utf-8")
        for old, new in replacements.items():
            text = text.replace(old, new)
        (revision_root / "package" / target_name).write_text(text, encoding="utf-8")
    timestamp = now()
    manifest.setdefault("revisions", []).append({
        "revision": revision,
        "state": "draft",
        "sdd_action": "wait",
        "reason": "Редакция формируется",
        "created_at": timestamp,
        "sent_at": None,
        "claimed_at": None,
        "claimed_by": None,
        "receipt_received_at": None,
        "package_path": f"revisions/{name}/package",
        "transport_path": None,
        "transport_sha256": None,
        "receipt_path": f"revisions/{name}/returns/receipt.json",
        "package_files": None,
        "receipt": {"expectation": "not-expected", "sha256": None},
        "analyst_review_path": f"revisions/{name}/returns/analyst-review.json",
        "analyst_review_status": "not-started",
        "replaces_revision": args.replaces,
    })
    manifest["revisions"].sort(key=lambda item: item["revision"])
    update_next_action(manifest)
    save(root_manifest(root), manifest)
    print(revision_root)
    return 0


def transport_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest = load(root_manifest(root))
    entry = revision_entry(manifest, args.revision)
    if entry.get("state") == "draft":
        raise ValueError("Publish the revision before building an on-request transport archive")
    root_errors = validate_root(root)
    if root_errors:
        raise ValueError("\n".join(root_errors))
    package = root / entry["package_path"]
    if is_feature_manifest(manifest):
        errors = validate_feature_package(package)
        if errors:
            raise ValueError("\n".join(errors))
    else:
        validator = Path(__file__).resolve().with_name("validate-handoff.py")
        result = subprocess.run([sys.executable, str(validator), str(package)], text=True, capture_output=True, check=False)
        if result.returncode:
            raise ValueError(result.stdout + result.stderr)
    name = revision_name(args.revision)
    downloads = Path.home() / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / f"{manifest['package_id']}-r{name}.zip"
    if archive.exists() and not args.force:
        raise ValueError(f"Transport already exists: {archive}; use --force to rebuild")
    temp = archive.with_suffix(".zip.tmp")
    if temp.exists():
        temp.unlink()
    prefix = f"{manifest['package_id']}-r{name}"
    with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as target:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                info = zipfile.ZipInfo(f"{prefix}/{path.relative_to(package).as_posix()}")
                info.date_time = (1980, 1, 1, 0, 0, 0)
                info.external_attr = 0o100644 << 16
                target.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    temp.replace(archive)
    print(f"{archive}\nsha256={hash_file(archive)}")
    return 0


def validate_send_preconditions(
    manifest: dict[str, Any],
    entry: dict[str, Any],
    revision: int,
    force: bool,
) -> None:
    if any(other.get("revision", 0) > revision for other in manifest.get("revisions", [])):
        raise ValueError("An older revision cannot be sent after a newer revision has been registered")
    terminal = (
        {"superseded", "archived", "cancelled"}
        if is_feature_manifest(manifest)
        else TERMINAL_STATES | {"receipt-received"}
    )
    for other in manifest.get("revisions", []):
        if other is entry or other.get("revision", revision) >= revision or other.get("state") in terminal:
            continue
        if other.get("sdd_action") in {"continue", "stop-and-report"} and not force:
            raise ValueError("Another revision is in progress or must return a report; finish it or use --force")


def set_state_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest = load(root_manifest(root))
    entry = revision_entry(manifest, args.revision)
    state = args.state
    allowed_states = FEATURE_REVISION_STATES if is_feature_manifest(manifest) else REVISION_STATES
    if state not in allowed_states:
        raise ValueError(f"Unsupported state: {state}")
    if state == "sent":
        validate_send_preconditions(manifest, entry, args.revision, args.force)
        package = root / entry["package_path"]
        if is_feature_manifest(manifest):
            errors = validate_feature_package(package)
        else:
            validator = Path(__file__).resolve().with_name("validate-handoff.py")
            result = subprocess.run(
                [sys.executable, str(validator), str(package)],
                text=True,
                capture_output=True,
                check=False,
            )
            errors = [] if result.returncode == 0 else [result.stdout + result.stderr]
        if errors:
            raise ValueError("\n".join(errors))
        entry["package_files"] = package_hashes(package)
        for other in manifest.get("revisions", []):
            if (
                other is entry
                or other.get("revision", args.revision) >= args.revision
                or other.get("state") in (
                    {"superseded", "archived", "cancelled"}
                    if is_feature_manifest(manifest)
                    else TERMINAL_STATES | {"receipt-received"}
                )
            ):
                continue
            other.update({
                "state": "superseded",
                "sdd_action": "no-action",
                "reason": f"Заменена редакцией {revision_name(args.revision)}",
            })
            if not is_feature_manifest(manifest) and other.get("receipt", {}).get("expectation") != "received":
                other["receipt"] = {"expectation": "not-expected", "sha256": None}
        update = {
            "state": "sent",
            "sdd_action": "process",
            "reason": args.reason or "Редакция отправлена и разрешена к обработке",
            "sent_at": now(),
        }
        if not is_feature_manifest(manifest):
            update["receipt"] = {"expectation": "required", "sha256": None}
        entry.update(update)
        manifest["active_revision"] = args.revision
    elif state == "paused":
        request_report = args.request_report and bool(entry.get("claimed_at"))
        entry.update({
            "state": state,
            "sdd_action": "stop-and-report" if request_report else "wait",
            "reason": args.reason or ("Остановить работу и вернуть промежуточную квитанцию" if request_report else "Обработка приостановлена"),
        })
        if not is_feature_manifest(manifest):
            entry["receipt"]["expectation"] = "required" if request_report else "optional" if entry.get("claimed_at") else "not-expected"
        manifest["active_revision"] = args.revision
    elif state in {"superseded", "cancelled", "archived"}:
        entry.update({"state": state, "sdd_action": "no-action", "reason": args.reason or state})
        if not is_feature_manifest(manifest) and entry["receipt"].get("expectation") != "received":
            entry["receipt"]["expectation"] = "not-expected"
        if manifest.get("active_revision") == args.revision:
            manifest["active_revision"] = None
    else:
        raise ValueError(f"Use a dedicated command for state {state}")
    update_next_action(manifest)
    save(root_manifest(root), manifest)
    return 0


def publish_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest = load(root_manifest(root))
    entry = revision_entry(manifest, args.revision)
    if entry.get("state") != "draft":
        raise ValueError("Only a draft revision can be published")
    validate_send_preconditions(manifest, entry, args.revision, args.force)
    state_args = argparse.Namespace(
        root=str(root),
        revision=args.revision,
        state="sent",
        reason=args.reason,
        force=args.force,
        request_report=False,
    )
    set_state_command(state_args)
    updated = load(root_manifest(root))
    published = revision_entry(updated, args.revision)
    print(json.dumps({
        "package_id": updated["package_id"],
        "revision": args.revision,
        "state": published["state"],
        "next_sdd_action": updated["next_sdd_action"],
    }, ensure_ascii=False, indent=2))
    return 0


def claim_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest = load(root_manifest(root))
    entry = revision_entry(manifest, args.revision)
    if manifest.get("active_revision") != args.revision or entry.get("sdd_action") != "process":
        raise ValueError("This revision is not currently authorized for processing")
    entry.update({
        "state": "in-progress",
        "sdd_action": "continue",
        "claimed_at": now(),
        "claimed_by": args.by,
        "reason": "SDD начала обработку; продолжать в уже начатом рабочем контуре",
    })
    update_next_action(manifest)
    save(root_manifest(root), manifest)
    return 0


def resume_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest = load(root_manifest(root))
    entry = revision_entry(manifest, args.revision)
    if entry.get("state") != "paused":
        raise ValueError("Only a paused revision can be resumed")
    if manifest.get("active_revision") not in {None, args.revision}:
        raise ValueError("Another revision is active")
    was_claimed = bool(entry.get("claimed_at"))
    entry.update({
        "state": "in-progress" if was_claimed else "sent",
        "sdd_action": "continue" if was_claimed else "process",
        "reason": args.reason or (
            "Обработка ранее взятой редакции возобновлена"
            if was_claimed
            else "Редакция снова разрешена к обработке"
        ),
    })
    if not is_feature_manifest(manifest):
        entry["receipt"]["expectation"] = "required"
    manifest["active_revision"] = args.revision
    update_next_action(manifest)
    save(root_manifest(root), manifest)
    return 0


def feature_input_manifest(root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    return load(root / entry["package_path"] / "manifest.json")


def task_ids(values: Any, key: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    if not isinstance(values, list):
        return [], [f"{key} must be an array"]
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            errors.append(f"{key} contains invalid identifier")
        else:
            result.append(value)
    if len(result) != len(set(result)):
        errors.append(f"{key} contains duplicate identifiers")
    return result, errors


def has_cycle(tasks: dict[str, dict[str, Any]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> bool:
        if task_id in visiting:
            return True
        if task_id in visited:
            return False
        visiting.add(task_id)
        for dependency in tasks[task_id].get("dependencies", []):
            if dependency in tasks and visit(dependency):
                return True
        visiting.remove(task_id)
        visited.add(task_id)
        return False

    return any(visit(task_id) for task_id in tasks)


def validate_decomposition(
    root: Path,
    manifest: dict[str, Any],
    entry: dict[str, Any],
    receipt: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    feature_manifest = feature_input_manifest(root, entry)
    if receipt.get("schema_version") != 1 or receipt.get("kind") != "technical-decomposition":
        errors.append("decomposition receipt has unsupported schema or kind")
    if receipt.get("package_id") != manifest.get("package_id") or receipt.get("package_revision") != entry.get("revision"):
        errors.append("decomposition receipt does not match package revision")
    if receipt.get("status") != "confirmed-by-development":
        errors.append("decomposition status must be confirmed-by-development")
    if not isinstance(receipt.get("confirmed_by"), str) or not receipt["confirmed_by"].strip():
        errors.append("confirmed_by is required")
    if not isinstance(receipt.get("confirmed_at"), str) or not receipt["confirmed_at"].strip():
        errors.append("confirmed_at is required")
    tasks = receipt.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return errors + ["decomposition tasks must be a non-empty array"]
    by_id: dict[str, dict[str, Any]] = {}
    working = root / entry["decomposition"]["working_path"]
    for task in tasks:
        if not isinstance(task, dict):
            errors.append("decomposition task must be an object")
            continue
        task_id = task.get("id")
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            errors.append(f"invalid development task id: {task_id!r}")
            continue
        if task_id in by_id:
            errors.append(f"duplicate development task id: {task_id}")
        by_id[task_id] = task
        contour = task.get("contour")
        expected_contour = "backend" if task_id.startswith("DEV-BE-") else "frontend"
        if contour != expected_contour:
            errors.append(f"{task_id}: contour must be {expected_contour}")
        if task.get("decomposition_status") != "confirmed-by-development":
            errors.append(f"{task_id}: decomposition_status must be confirmed-by-development")
        card_path = task.get("card_path")
        if not relative_path(card_path):
            errors.append(f"{task_id}: invalid card_path")
        else:
            card = working / card_path
            if not card.is_file():
                errors.append(f"{task_id}: card is missing: {card_path}")
            else:
                card_text = card.read_text(encoding="utf-8", errors="ignore")
                errors.extend(validate_task_card(card_text, task))
        estimate = task.get("estimate_days")
        if estimate is not None and (not isinstance(estimate, (int, float)) or isinstance(estimate, bool) or estimate <= 0):
            errors.append(f"{task_id}: estimate_days must be null or a positive number")
        estimate_source = task.get("estimate_source")
        if estimate_source not in {None, "developer", "sdd", "other"}:
            errors.append(f"{task_id}: invalid estimate_source")
        jira_key = task.get("jira_key")
        if jira_key is not None and (not isinstance(jira_key, str) or not jira_key.strip()):
            errors.append(f"{task_id}: jira_key must be null or non-empty text")
        maximum = 5 if contour == "backend" else 10
        if isinstance(estimate, (int, float)) and not isinstance(estimate, bool) and estimate > maximum:
            if not isinstance(task.get("size_exception_reason"), str) or not task["size_exception_reason"].strip():
                errors.append(f"{task_id}: estimate above {maximum} days requires size_exception_reason")
        for key in ("requirements", "scenarios", "impacts", "dependencies"):
            _, field_errors = task_ids(task.get(key, []), f"{task_id}.{key}")
            errors.extend(field_errors)
    all_task_ids = set(by_id)
    for task_id, task in by_id.items():
        dependencies = task.get("dependencies", [])
        for dependency in dependencies if isinstance(dependencies, list) else []:
            if dependency == task_id:
                errors.append(f"{task_id}: task cannot depend on itself")
            elif dependency not in all_task_ids:
                errors.append(f"{task_id}: unknown dependency {dependency}")
    if has_cycle(by_id):
        errors.append("development task dependencies contain a cycle")
    coverage = receipt.get("coverage")
    if not isinstance(coverage, dict):
        errors.append("decomposition coverage must be an object")
        return errors
    field_map = {
        "requirements": "unassigned_requirements",
        "scenarios": "unassigned_scenarios",
        "impacts": "unassigned_impacts",
    }
    for task_key, coverage_key in field_map.items():
        expected, field_errors = task_ids(feature_manifest.get(task_key, []), f"manifest.{task_key}")
        errors.extend(field_errors)
        assigned = {
            value
            for task in by_id.values()
            for value in task.get(task_key, [])
            if isinstance(value, str)
        }
        unknown = assigned - set(expected)
        if unknown:
            errors.append(f"decomposition assigns unknown {task_key}: {', '.join(sorted(unknown))}")
        unassigned, field_errors = task_ids(coverage.get(coverage_key), f"coverage.{coverage_key}")
        errors.extend(field_errors)
        if set(unassigned) != set(expected) - assigned:
            errors.append(f"coverage.{coverage_key} does not match actual unassigned ids")
    if not (working / "README.md").is_file() or not (working / "index.md").is_file():
        errors.append("development-tasks must contain README.md and index.md")
    else:
        instruction_text = (working / "README.md").read_text(encoding="utf-8", errors="ignore")
        for fragment in (
            "Сначала прочитать `handoff.json`",
            "Не загружать весь репозиторий `coda`",
            "development-task-card.template.md",
            "блок `Короткие команды разработчика`",
        ):
            if fragment not in instruction_text:
                errors.append(f"development instruction is incomplete: {fragment}")
        index_text = (working / "index.md").read_text(encoding="utf-8", errors="ignore")
        for placeholder in ("<Название>", "<revision>"):
            if placeholder in index_text:
                errors.append(f"development task index contains unfilled placeholder: {placeholder}")
        for task_id in by_id:
            if task_id not in index_text:
                errors.append(f"development task index does not list {task_id}")
        if "Состояние декомпозиции: **подтверждена разработкой**" not in index_text:
            errors.append("development task index must have confirmed decomposition state")
        for command in INDEX_COMMANDS:
            if command not in index_text:
                errors.append(f"development task index command is missing: {command}")
    return errors


def decomposition_snapshot(root: Path, entry: dict[str, Any], revision: int | None = None) -> tuple[dict[str, Any], dict[str, Any], Path]:
    decomposition = entry.get("decomposition", {})
    selected = revision if revision is not None else decomposition.get("current_revision")
    matches = [item for item in decomposition.get("snapshots", []) if item.get("revision") == selected]
    if len(matches) != 1:
        raise ValueError("Confirmed decomposition snapshot is not available")
    record = matches[0]
    path = root / record["path"]
    return record, load(path / "receipt.json"), path


def confirm_decomposition_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest = load(root_manifest(root))
    if not is_feature_manifest(manifest):
        raise ValueError("confirm-decomposition is available only for feature packages")
    entry = revision_entry(manifest, args.revision)
    if entry.get("state") != "in-progress" or entry.get("sdd_action") != "continue":
        raise ValueError("Claim the feature revision before confirming decomposition")
    receipt_path = root / entry["decomposition"]["receipt_path"]
    receipt = load(receipt_path)
    expected_revision = len(entry["decomposition"].get("snapshots", [])) + 1
    if receipt.get("decomposition_revision") != expected_revision:
        raise ValueError(f"decomposition_revision must be {expected_revision}")
    errors = validate_decomposition(root, manifest, entry, receipt)
    if errors:
        raise ValueError("\n".join(errors))
    snapshot = root / entry["returns_path"] / "decomposition-snapshots" / revision_name(expected_revision)
    if snapshot.exists():
        raise ValueError(f"Decomposition snapshot already exists: {snapshot}")
    shutil.copytree(root / entry["decomposition"]["working_path"], snapshot)
    shutil.copy2(receipt_path, snapshot / "receipt.json")
    record = {
        "revision": expected_revision,
        "status": "delivered-to-analyst",
        "confirmed_by": receipt["confirmed_by"],
        "confirmed_at": receipt["confirmed_at"],
        "delivered_at": now(),
        "path": snapshot.relative_to(root).as_posix(),
        "files": package_hashes(snapshot),
    }
    entry["decomposition"].update({
        "status": "confirmed-by-development",
        "current_revision": expected_revision,
    })
    entry["decomposition"].setdefault("snapshots", []).append(record)
    update_next_action(manifest)
    save(root_manifest(root), manifest)
    print(snapshot)
    return 0


def replace_template(path: Path, replacements: dict[str, str]) -> str:
    text = path.read_text(encoding="utf-8")
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def markdown_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return fallback


def markdown_section(text: str, names: tuple[str, ...], fallback: str) -> str:
    wanted = {name.casefold() for name in names}
    value = ""
    for section in markdown_sections(text):
        if section["title"].casefold() in wanted:
            value = section["body"]
            break
    if not value:
        return fallback
    value = "\n".join(
        line for line in value.splitlines()
        if not re.search(r"(?:^|[\s`(])/(?:home|Users|mnt)/", line)
    ).strip()
    if not value:
        return fallback
    if len(value) > 2500:
        value = value[:2500].rstrip() + "\n\nПолное содержание приведено в `requirements.md`."
    return value


def legacy_request_parts(contract: dict[str, Any]) -> tuple[str, str, str]:
    goal_parts: list[str] = []
    examples: list[str] = []
    criteria: list[str] = []
    for section in contract.get("legacy_sections", []):
        purpose = ""
        match = re.search(
            r"\*\*Назначение\*\*\s*(.*?)(?=\n\*\*[^\n]+\*\*|\Z)",
            section["body"],
            flags=re.DOTALL | re.IGNORECASE,
        )
        if match:
            purpose = re.sub(r"^\s*[-*]\s+", "", match.group(1).strip())
        goal_parts.append(f"- `{section['id']}` — {section['title']}: {purpose or 'результат определён в одноимённом разделе `requirements.md`.'}")
        acceptance = re.search(
            r"\*\*Критерии при(?:е|ё)мки\*\*\s*(.*?)(?=\n\*\*[^\n]+\*\*|\Z)",
            section["body"],
            flags=re.DOTALL | re.IGNORECASE,
        )
        if acceptance:
            for line in acceptance.group(1).splitlines():
                item = re.match(r"^\s*\d+\.\s+(.+)$", line)
                if item:
                    criteria.append(f"{len(criteria) + 1}. `{section['id']}`: {item.group(1).strip()}")
                    if not any(example.startswith(f"- `{section['id']}`") for example in examples):
                        examples.append(f"- `{section['id']}`: {item.group(1).strip()}")
    return "\n".join(goal_parts), "\n".join(examples), "\n".join(criteria)


def render_feature_request(
    template: Path,
    replacements: dict[str, str],
    requirements_text: str,
    slice_ids: list[str],
    contract: dict[str, Any],
) -> tuple[str, str]:
    title = markdown_title(requirements_text, replacements["<feature-slug>"])
    legacy_goal, legacy_examples, legacy_criteria = legacy_request_parts(contract)
    values = dict(replacements)
    values.update({
        "<Название функциональности>": title,
        "<Законченный пользовательский или системный результат.>": markdown_section(
            requirements_text,
            ("Цель", "Назначение и границы", "Пользовательский и системный результат"),
            legacy_goal or "Требуемый результат и его границы полностью определены в `requirements.md`.",
        ),
        "- <Обязательное ограничение.>": markdown_section(
            requirements_text,
            ("Ограничения", "Зависимости и предположения", "Нефункциональные требования"),
            "Обязательны все ограничения, зависимости и нормативные формулировки из `requirements.md`.",
        ),
        "<Текущее состояние, участники, данные и внешние системы.>": markdown_section(
            requirements_text,
            ("Текущее состояние", "Контекст", "Участники и внешние системы", "Общий контур функциональности"),
            "Текущее состояние, данные и участники описаны в `requirements.md`; перед реализацией их необходимо сверить с кодом.",
        ),
        "- <Характерный положительный, отрицательный или граничный пример.>": markdown_section(
            requirements_text,
            ("Примеры", "Сценарии"),
            legacy_examples or "Характерные и граничные сценарии перечислены как `SCN-*` в `requirements.md` и срезах.",
        ),
        "1. <Наблюдаемый критерий.>": markdown_section(
            requirements_text,
            ("Критерии приёмки", "Критерии завершённости", "Условия приёмки"),
            legacy_criteria or "Каждый входной `REQ-*` и `SCN-*` получил отдельный фактический результат и доказательства проверки.",
        ),
        "<slices-path-or-list>": ", ".join(f"slices/{item}/slice.md" for item in slice_ids) or "срезы отсутствуют",
    })
    return replace_template(template, values), title


def prepare_implementation_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest = load(root_manifest(root))
    if not is_feature_manifest(manifest):
        raise ValueError("prepare-implementation is available only for feature packages")
    entry = revision_entry(manifest, args.revision)
    _, decomposition, _ = decomposition_snapshot(root, entry, args.decomposition_revision)
    tasks = {item.get("id"): item for item in decomposition.get("tasks", []) if isinstance(item, dict)}
    if args.task_id not in tasks:
        raise ValueError(f"Unknown confirmed development task: {args.task_id}")
    target = root / entry["returns_path"] / "implementation-results" / args.task_id / revision_name(args.result_revision) / "receipt.json"
    if target.exists():
        raise ValueError(f"Implementation receipt already exists: {target}")
    target.parent.mkdir(parents=True)
    receipt = load(root / ".control/templates/implementation-receipt.template.json")
    receipt.update({
        "package_id": manifest["package_id"],
        "package_revision": args.revision,
        "decomposition_revision": args.decomposition_revision,
        "task_id": args.task_id,
        "result_revision": args.result_revision,
        "jira_key": tasks[args.task_id].get("jira_key"),
        "requirement_results": [implementation_result_item("requirement", item) for item in tasks[args.task_id].get("requirements", [])],
        "scenario_results": [implementation_result_item("scenario", item) for item in tasks[args.task_id].get("scenarios", [])],
    })
    save(target, receipt)
    print(target)
    return 0


def implementation_result_item(key: str, identifier: str) -> dict[str, Any]:
    return {
        key: identifier,
        "status": "not-implemented",
        "behavior_before": None,
        "delivered_behavior": None,
        "deviation_from_input": None,
        "remaining_work": None,
        "evidence": [],
    }


def validate_result_items(values: Any, key: str, expected: list[str], status_key: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(values, list):
        return [f"{key} must be an array"]
    actual: list[str] = []
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get(status_key), str):
            errors.append(f"{key} contains invalid identifier")
            continue
        actual.append(item[status_key])
        if item.get("status") not in ITEM_RESULT_STATUSES:
            errors.append(f"{key} {item[status_key]} has invalid status")
        if not isinstance(item.get("evidence", []), list):
            errors.append(f"{key} {item[status_key]} evidence must be an array")
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        errors.append(f"{key} must exactly cover the confirmed task ids")
    return errors


def register_implementation_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest = load(root_manifest(root))
    if not is_feature_manifest(manifest):
        raise ValueError("register-implementation is available only for feature packages")
    entry = revision_entry(manifest, args.revision)
    _, decomposition, _ = decomposition_snapshot(root, entry, args.decomposition_revision)
    tasks = {item.get("id"): item for item in decomposition.get("tasks", []) if isinstance(item, dict)}
    task = tasks.get(args.task_id)
    if task is None:
        raise ValueError(f"Unknown confirmed development task: {args.task_id}")
    path = root / entry["returns_path"] / "implementation-results" / args.task_id / revision_name(args.result_revision) / "receipt.json"
    receipt = load(path)
    errors: list[str] = []
    expected = {
        "schema_version": 1,
        "kind": "implementation-result",
        "package_id": manifest["package_id"],
        "package_revision": args.revision,
        "decomposition_revision": args.decomposition_revision,
        "task_id": args.task_id,
        "result_revision": args.result_revision,
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            errors.append(f"implementation receipt {key} does not match")
    if receipt.get("status") not in IMPLEMENTATION_STATUSES:
        errors.append("implementation receipt has invalid final status")
    errors.extend(validate_result_items(receipt.get("requirement_results"), "requirement_results", task.get("requirements", []), "requirement"))
    errors.extend(validate_result_items(receipt.get("scenario_results"), "scenario_results", task.get("scenarios", []), "scenario"))
    for key in ("commits", "additional_deliveries", "remaining_work", "verification"):
        if not isinstance(receipt.get(key), list):
            errors.append(f"implementation receipt {key} must be an array")
    if errors:
        raise ValueError("\n".join(errors))
    if any(item.get("task_id") == args.task_id and item.get("result_revision") == args.result_revision for item in entry["implementation_results"]):
        raise ValueError("Implementation result is already registered")
    entry["implementation_results"].append({
        "task_id": args.task_id,
        "decomposition_revision": args.decomposition_revision,
        "result_revision": args.result_revision,
        "status": receipt["status"],
        "registered_at": now(),
        "path": path.relative_to(root).as_posix(),
        "sha256": hash_file(path),
    })
    update_next_action(manifest)
    save(root_manifest(root), manifest)
    print(path)
    return 0


def slice_contract(feature_manifest: dict[str, Any], slice_id: str) -> dict[str, Any]:
    for item in feature_manifest.get("slices", []):
        if item == slice_id:
            return {"id": item, "requirements": [], "scenarios": []}
        if isinstance(item, dict) and item.get("id") == slice_id:
            return item
    raise ValueError(f"Unknown slice: {slice_id}")


def prepare_test_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest = load(root_manifest(root))
    if not is_feature_manifest(manifest):
        raise ValueError("prepare-test is available only for feature packages")
    entry = revision_entry(manifest, args.revision)
    contract = slice_contract(feature_input_manifest(root, entry), args.slice_id)
    snapshot_record, decomposition, _ = decomposition_snapshot(root, entry, args.decomposition_revision)
    decomposition_revision = snapshot_record["revision"]
    target = root / entry["returns_path"] / "test-results" / args.slice_id / revision_name(args.result_revision) / "receipt.json"
    if target.exists():
        raise ValueError(f"Test receipt already exists: {target}")
    target.parent.mkdir(parents=True)
    requirement_ids = contract.get("requirements", [])
    scenario_ids = contract.get("scenarios", [])
    related_tasks = sorted({
        task["id"]
        for task in decomposition.get("tasks", [])
        if isinstance(task, dict)
        and (
            set(task.get("requirements", [])) & set(requirement_ids)
            or set(task.get("scenarios", [])) & set(scenario_ids)
        )
    })
    registered_receipts = sorted({
        item["path"]
        for item in entry.get("implementation_results", [])
        if isinstance(item, dict)
        and item.get("task_id") in related_tasks
        and item.get("decomposition_revision") == decomposition_revision
        and isinstance(item.get("path"), str)
    })
    receipt = load(root / ".control/templates/test-receipt.template.json")
    receipt.update({
        "package_id": manifest["package_id"],
        "package_revision": args.revision,
        "decomposition_revision": decomposition_revision,
        "slice_id": args.slice_id,
        "result_revision": args.result_revision,
        "related_tasks": related_tasks,
        "implementation_receipts": registered_receipts,
        "requirement_results": [test_result_item("requirement", item) for item in requirement_ids],
        "scenario_results": [test_result_item("scenario", item) for item in scenario_ids],
    })
    save(target, receipt)
    print(target)
    return 0


def test_result_item(key: str, identifier: str) -> dict[str, Any]:
    return {key: identifier, "status": "not-tested", "evidence": []}


def register_test_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest = load(root_manifest(root))
    if not is_feature_manifest(manifest):
        raise ValueError("register-test is available only for feature packages")
    entry = revision_entry(manifest, args.revision)
    contract = slice_contract(feature_input_manifest(root, entry), args.slice_id)
    _, decomposition, _ = decomposition_snapshot(root, entry, args.decomposition_revision)
    known_tasks = {item.get("id") for item in decomposition.get("tasks", []) if isinstance(item, dict)}
    path = root / entry["returns_path"] / "test-results" / args.slice_id / revision_name(args.result_revision) / "receipt.json"
    receipt = load(path)
    errors: list[str] = []
    expected = {
        "schema_version": 1,
        "kind": "slice-test-result",
        "package_id": manifest["package_id"],
        "package_revision": args.revision,
        "decomposition_revision": args.decomposition_revision,
        "slice_id": args.slice_id,
        "result_revision": args.result_revision,
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            errors.append(f"test receipt {key} does not match")
    if receipt.get("status") not in TEST_STATUSES:
        errors.append("test receipt has invalid final status")
    related = receipt.get("related_tasks")
    if not isinstance(related, list) or any(task not in known_tasks for task in related):
        errors.append("test receipt related_tasks contains unknown task")
    requirement_results = receipt.get("requirement_results")
    scenario_results = receipt.get("scenario_results")
    for values, key, id_key, allowed in (
        (requirement_results, "requirement_results", "requirement", set(feature_input_manifest(root, entry).get("requirements", []))),
        (scenario_results, "scenario_results", "scenario", set(feature_input_manifest(root, entry).get("scenarios", []))),
    ):
        if not isinstance(values, list):
            errors.append(f"test receipt {key} must be an array")
            continue
        actual = [item.get(id_key) for item in values if isinstance(item, dict)]
        if len(actual) != len(values) or len(actual) != len(set(actual)) or any(value not in allowed for value in actual):
            errors.append(f"test receipt {key} contains invalid ids")
        for item in values:
            if not isinstance(item, dict) or item.get("status") not in {"passed", "failed", "blocked", "not-tested"}:
                errors.append(f"test receipt {key} contains invalid status")
    for key, id_key in (("requirements", "requirement"), ("scenarios", "scenario")):
        expected_ids = contract.get(key, [])
        if expected_ids:
            values = requirement_results if key == "requirements" else scenario_results
            actual_ids = {item.get(id_key) for item in values if isinstance(item, dict)} if isinstance(values, list) else set()
            if actual_ids != set(expected_ids):
                errors.append(f"test receipt must exactly cover slice {key}")
    for key in ("implementation_receipts", "findings", "blocked_scope", "verification"):
        if not isinstance(receipt.get(key), list):
            errors.append(f"test receipt {key} must be an array")
    implementation_receipts = receipt.get("implementation_receipts")
    if isinstance(implementation_receipts, list):
        registered_receipts = {
            item.get("path")
            for item in entry.get("implementation_results", [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        }
        for receipt_path in implementation_receipts:
            if not relative_path(receipt_path) or receipt_path not in registered_receipts:
                errors.append(f"test receipt references unregistered implementation receipt: {receipt_path}")
    if errors:
        raise ValueError("\n".join(errors))
    if any(item.get("slice_id") == args.slice_id and item.get("result_revision") == args.result_revision for item in entry["test_results"]):
        raise ValueError("Test result is already registered")
    entry["test_results"].append({
        "slice_id": args.slice_id,
        "decomposition_revision": args.decomposition_revision,
        "result_revision": args.result_revision,
        "status": receipt["status"],
        "registered_at": now(),
        "path": path.relative_to(root).as_posix(),
        "sha256": hash_file(path),
    })
    update_next_action(manifest)
    save(root_manifest(root), manifest)
    print(path)
    return 0


def register_receipt_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest = load(root_manifest(root))
    if is_feature_manifest(manifest):
        raise ValueError("Feature package uses confirm-decomposition, register-implementation and register-test")
    entry = revision_entry(manifest, args.revision)
    receipt = root / entry["receipt_path"]
    if not receipt.is_file():
        raise ValueError(f"Receipt is missing: {receipt}")
    package = root / entry["package_path"]
    validator = Path(__file__).resolve().with_name("validate-handoff.py")
    result = subprocess.run(
        [sys.executable, str(validator), str(package), "--receipt", str(receipt)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(result.stdout + result.stderr)
    entry.update({
        "state": "receipt-received",
        "sdd_action": "no-action",
        "reason": "Квитанция получена и прошла структурную проверку",
        "receipt_received_at": now(),
        "receipt": {"expectation": "received", "sha256": hash_file(receipt)},
        "analyst_review_status": "pending",
    })
    update_next_action(manifest)
    save(root_manifest(root), manifest)
    print(receipt)
    return 0


def register_review_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest = load(root_manifest(root))
    if is_feature_manifest(manifest):
        raise ValueError("Feature package results are reviewed independently and do not block development")
    entry = revision_entry(manifest, args.revision)
    if entry.get("receipt", {}).get("expectation") != "received":
        raise ValueError("Register the developer receipt before the analyst review")
    review = root / entry["analyst_review_path"]
    if not review.is_file():
        raise ValueError(f"Analyst review is missing: {review}")
    payload = load(review)
    if payload.get("schema_version") != 1 or payload.get("status") != "approved":
        raise ValueError("Analyst review must use schema_version 1 and status approved")
    if payload.get("package_id") != manifest.get("package_id") or payload.get("package_revision") != args.revision:
        raise ValueError("Analyst review does not match the package revision")
    receipt = root / entry["receipt_path"]
    if payload.get("receipt_sha256") != hash_file(receipt):
        raise ValueError("Analyst review receipt_sha256 does not match receipt.json")
    receipt_payload = load(receipt)
    allowed = {
        "promote-to-baseline",
        "update-requirement",
        "keep-open",
        "defer",
        "move-to-other-change",
        "cancel",
        "investigate",
        "no-action",
    }
    requirement_ids = [item.get("requirement") for item in receipt_payload.get("requirement_coverage", [])]
    requirement_reviews = payload.get("requirement_dispositions")
    if not isinstance(requirement_reviews, list):
        raise ValueError("requirement_dispositions must be an array")
    reviewed_requirements = [item.get("requirement") for item in requirement_reviews if isinstance(item, dict)]
    if len(reviewed_requirements) != len(requirement_reviews) or len(reviewed_requirements) != len(set(reviewed_requirements)) or set(reviewed_requirements) != set(requirement_ids):
        raise ValueError("requirement_dispositions must exactly cover receipt requirements")
    additional_ids = [item.get("id") for item in receipt_payload.get("additional_deliveries", [])]
    additional_reviews = payload.get("additional_delivery_dispositions")
    if not isinstance(additional_reviews, list):
        raise ValueError("additional_delivery_dispositions must be an array")
    reviewed_additional = [item.get("additional_delivery") for item in additional_reviews if isinstance(item, dict)]
    if len(reviewed_additional) != len(additional_reviews) or len(reviewed_additional) != len(set(reviewed_additional)) or set(reviewed_additional) != set(additional_ids):
        raise ValueError("additional_delivery_dispositions must exactly cover additional deliveries")
    for item in requirement_reviews + additional_reviews:
        disposition = item.get("disposition")
        if disposition not in allowed:
            raise ValueError(f"Invalid analyst disposition: {disposition!r}")
        if disposition == "move-to-other-change" and not isinstance(item.get("destination"), str):
            raise ValueError("move-to-other-change requires destination")
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise ValueError("Every approved analyst disposition requires a reason")
        if not isinstance(item.get("applied_changes"), list):
            raise ValueError("applied_changes must be an array")
    for key in (
        "baseline_updates",
        "requirement_updates",
        "deferred_or_moved_work",
        "cancelled_work",
        "consistency_updates",
    ):
        if not isinstance(payload.get(key), list):
            raise ValueError(f"{key} must be an array")
    if not isinstance(payload.get("approved_by"), str) or not payload["approved_by"].strip():
        raise ValueError("approved_by is required")
    if not isinstance(payload.get("approved_at"), str) or not payload["approved_at"].strip():
        raise ValueError("approved_at is required")
    entry.update({
        "state": "reviewed",
        "sdd_action": "no-action",
        "reason": "Квитанция разобрана аналитической стороной",
        "analyst_review_status": "approved",
        "analyst_review_sha256": hash_file(review),
        "analyst_reviewed_at": now(),
    })
    if manifest.get("active_revision") == args.revision:
        manifest["active_revision"] = None
    update_next_action(manifest)
    save(root_manifest(root), manifest)
    print(review)
    return 0


def validate_command(args: argparse.Namespace) -> int:
    errors = validate_root(Path(args.root).resolve())
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Handoff root OK")
    return 0


def status_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest = load(root_manifest(root))
    action = manifest.get("next_sdd_action", {})
    payload = {
        "package_id": manifest.get("package_id"),
        "package_kind": manifest.get("package_kind", "task-delivery"),
        "transport_policy": manifest.get("transport_policy"),
        "status": manifest.get("status"),
        "active_revision": manifest.get("active_revision"),
        "next_sdd_action": action,
    }
    if is_feature_manifest(manifest):
        payload["agent_contract"] = manifest.get("agent_contract")
    if is_feature_manifest(manifest) and manifest.get("active_revision") is not None:
        entry = revision_entry(manifest, manifest["active_revision"])
        payload["decomposition"] = entry.get("decomposition")
        payload["implementation_results"] = entry.get("implementation_results")
        payload["test_results"] = entry.get("test_results")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Управление общим каталогом передачи в разработку")
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("project")
    init.add_argument("feature")
    init.add_argument("package_id")
    init.add_argument("--role", required=True, choices=("BE", "FE"))
    init.add_argument("--source-task-id", required=True)
    init.add_argument("--source-task-path", required=True)
    init.set_defaults(handler=init_command)
    init_feature = commands.add_parser("init-feature")
    init_feature.add_argument("project")
    init_feature.add_argument("feature")
    init_feature.add_argument("package_id")
    init_feature.add_argument("--requirements-path")
    init_feature.set_defaults(handler=init_feature_command)
    revision = commands.add_parser("add-revision")
    revision.add_argument("root")
    revision.add_argument("revision", type=int)
    revision.add_argument("--replaces", type=int)
    revision.set_defaults(handler=add_revision_command)
    transport = commands.add_parser("transport")
    transport.add_argument("root")
    transport.add_argument("revision", type=int)
    transport.add_argument("--force", action="store_true", help="перезаписать существующий архив в ~/Downloads")
    transport.set_defaults(handler=transport_command)
    publish = commands.add_parser("publish")
    publish.add_argument("root")
    publish.add_argument("revision", type=int)
    publish.add_argument("--reason")
    publish.add_argument("--force", action="store_true")
    publish.set_defaults(handler=publish_command)
    state = commands.add_parser("set-state")
    state.add_argument("root")
    state.add_argument("revision", type=int)
    state.add_argument("state", choices=sorted(REVISION_STATES))
    state.add_argument("--reason")
    state.add_argument("--force", action="store_true")
    state.add_argument("--request-report", action="store_true")
    state.set_defaults(handler=set_state_command)
    claim = commands.add_parser("claim")
    claim.add_argument("root")
    claim.add_argument("revision", type=int)
    claim.add_argument("--by", default="SDD")
    claim.set_defaults(handler=claim_command)
    resume = commands.add_parser("resume")
    resume.add_argument("root")
    resume.add_argument("revision", type=int)
    resume.add_argument("--reason")
    resume.set_defaults(handler=resume_command)
    confirm_decomposition = commands.add_parser("confirm-decomposition")
    confirm_decomposition.add_argument("root")
    confirm_decomposition.add_argument("revision", type=int)
    confirm_decomposition.set_defaults(handler=confirm_decomposition_command)
    prepare_implementation = commands.add_parser("prepare-implementation")
    prepare_implementation.add_argument("root")
    prepare_implementation.add_argument("revision", type=int)
    prepare_implementation.add_argument("task_id")
    prepare_implementation.add_argument("--decomposition-revision", type=int, required=True)
    prepare_implementation.add_argument("--result-revision", type=int, default=1)
    prepare_implementation.set_defaults(handler=prepare_implementation_command)
    register_implementation = commands.add_parser("register-implementation")
    register_implementation.add_argument("root")
    register_implementation.add_argument("revision", type=int)
    register_implementation.add_argument("task_id")
    register_implementation.add_argument("--decomposition-revision", type=int, required=True)
    register_implementation.add_argument("--result-revision", type=int, default=1)
    register_implementation.set_defaults(handler=register_implementation_command)
    prepare_test = commands.add_parser("prepare-test")
    prepare_test.add_argument("root")
    prepare_test.add_argument("revision", type=int)
    prepare_test.add_argument("slice_id")
    prepare_test.add_argument("--decomposition-revision", type=int)
    prepare_test.add_argument("--result-revision", type=int, default=1)
    prepare_test.set_defaults(handler=prepare_test_command)
    register_test = commands.add_parser("register-test")
    register_test.add_argument("root")
    register_test.add_argument("revision", type=int)
    register_test.add_argument("slice_id")
    register_test.add_argument("--decomposition-revision", type=int, required=True)
    register_test.add_argument("--result-revision", type=int, default=1)
    register_test.set_defaults(handler=register_test_command)
    receipt = commands.add_parser("register-receipt")
    receipt.add_argument("root")
    receipt.add_argument("revision", type=int)
    receipt.set_defaults(handler=register_receipt_command)
    review = commands.add_parser("register-review")
    review.add_argument("root")
    review.add_argument("revision", type=int)
    review.set_defaults(handler=register_review_command)
    validate = commands.add_parser("validate")
    validate.add_argument("root")
    validate.set_defaults(handler=validate_command)
    status = commands.add_parser("status")
    status.add_argument("root")
    status.set_defaults(handler=status_command)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return args.handler(args)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
