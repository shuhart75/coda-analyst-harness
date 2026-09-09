from __future__ import annotations

import hashlib
import json
import re
from typing import Any


STAGE_FIELDS = ("stage_id", "number", "title", "goal")
REVISION_FIELDS = ("revision", "sha256", "stage_id", "stage_revision", "stage", "stage_sha256")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")


def checksum(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def empty_registry() -> dict[str, Any]:
    return {"schema_version": 1, "active_stage_id": None, "stages": [], "revisions": []}


def snapshot(stage: dict[str, Any]) -> dict[str, Any]:
    result = {field: stage.get(field) for field in STAGE_FIELDS}
    if not isinstance(result["stage_id"], str) or not SLUG.fullmatch(result["stage_id"]):
        raise ValueError("Некорректный stage_id этапа")
    if type(result["number"]) is not int or result["number"] < 1:
        raise ValueError("Некорректный номер этапа")
    for field in ("title", "goal"):
        value = result[field]
        if not isinstance(value, str) or not value.strip() or value != value.strip() or "\n" in value or "\r" in value:
            raise ValueError(f"Этап требует непустое однострочное поле {field}")
    return result


def revision_identity(entry: dict[str, Any]) -> dict[str, Any]:
    return {field: entry.get(field) for field in REVISION_FIELDS}


def record_entry(record: dict[str, Any]) -> dict[str, Any]:
    overlay = record.get("legacy_overlay", {})
    if not isinstance(overlay, dict):
        raise ValueError("Некорректный overlay прежней редакции")
    return {**record["entry"], **overlay}


def immutable_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if key != "state"}


def validate_revision(entry: dict[str, Any]) -> None:
    if not any(field in entry for field in ("stage_id", "stage_revision", "stage", "stage_sha256")):
        return
    stage = entry.get("stage")
    if not isinstance(stage, dict) or stage != snapshot(stage):
        raise ValueError("Редакция требует точный снимок этапа")
    if entry.get("stage_id") != stage["stage_id"] or entry.get("stage_sha256") != checksum(stage):
        raise ValueError("Нарушена привязка редакции к этапу")
    if type(entry.get("stage_revision")) is not int or entry["stage_revision"] < 1:
        raise ValueError("Некорректный stage_revision")


def registry(state: dict[str, Any]) -> dict[str, Any]:
    value = state.get("delivery_stages", empty_registry())
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Некорректный реестр этапов")
    stages, revisions = value.get("stages"), value.get("revisions")
    if not isinstance(stages, list) or not isinstance(revisions, list):
        raise ValueError("Реестр этапов требует stages и revisions")
    seen = set()
    for number, stage in enumerate(stages, 1):
        if not isinstance(stage, dict):
            raise ValueError("Некорректный этап")
        identity = snapshot(stage)
        if identity["number"] != number or identity["stage_id"] in seen:
            raise ValueError("Этапы должны иметь уникальные slug и последовательные номера")
        seen.add(identity["stage_id"])
        if stage.get("state") not in {"open", "closed"} or not stage.get("started_at"):
            raise ValueError("Некорректное состояние этапа")
        if number < len(stages) and stage["state"] != "closed":
            raise ValueError("Предыдущий этап не закрыт")
        closure = stage.get("closure")
        if stage["state"] == "closed":
            if not isinstance(closure, dict) or any(not closure.get(field) for field in (
                "revision", "requirements_sha256", "return_id", "review_sha256", "closed_at", "note",
            )):
                raise ValueError("Закрытый этап требует подробное решение о закрытии")
        elif closure is not None:
            raise ValueError("Открытый этап не может содержать решение о закрытии")
    expected = stages[-1]["stage_id"] if stages else None
    if value.get("active_stage_id") != expected:
        raise ValueError("Активным может быть только последний этап")
    numbers = set()
    stage_numbers: dict[str, list[int]] = {}
    for record in revisions:
        if not isinstance(record, dict) or not isinstance(record.get("entry"), dict):
            raise ValueError("Некорректная запись редакции этапа")
        entry = record_entry(record)
        if "legacy_overlay" in record:
            overlay = record["legacy_overlay"]
            if record["entry"].get("stage_id") or not isinstance(overlay, dict) or set(overlay) != {"stage_id", "stage_revision", "stage", "stage_sha256"}:
                raise ValueError("Некорректный overlay прежней редакции")
        validate_revision(entry)
        stage = next((item for item in stages if item["stage_id"] == entry.get("stage_id")), None)
        if stage is None or snapshot(stage) != entry.get("stage"):
            raise ValueError("Метаданные этапа изменились после подготовки редакции")
        revision = entry.get("revision")
        if type(revision) is not int or revision < 1 or revision in numbers:
            raise ValueError("Некорректная глобальная нумерация редакций")
        numbers.add(revision)
        stage_numbers.setdefault(stage["stage_id"], []).append(entry["stage_revision"])
        if record.get("destination_role") not in {"analytics", "code"} or type(record.get("publication_confirmed")) is not bool:
            raise ValueError("Некорректные сведения о размещении редакции")
    for values in stage_numbers.values():
        if sorted(values) != list(range(1, len(values) + 1)):
            raise ValueError("Редакции этапа должны иметь последовательные номера")
    return value


def active_stage(state: dict[str, Any], text: str | None = None) -> dict[str, Any]:
    value = registry(state)
    if not value["stages"]:
        if state.get("last_published"):
            raise ValueError("legacy-stage-migration-required: прежняя передача не привязана к этапу")
        raise ValueError("register-delivery-stage: сначала зарегистрируй этап поставки")
    stage = value["stages"][-1]
    if stage["state"] != "open":
        raise ValueError("Этап закрыт; публикация запрещена")
    identity = snapshot(stage)
    if text is not None:
        validate_scope(identity, text)
    return identity


def validate_scope(stage: dict[str, Any], text: str) -> None:
    text = re.sub(
        r"```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)|<!--.*?(?:-->|\Z)",
        lambda match: "\n" * match.group(0).count("\n"), text, flags=re.DOTALL,
    )
    sections = re.findall(r"^## Границы\s*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    markers = re.findall(r"^Этап поставки: (.+)$", text, re.MULTILINE)
    if len(sections) != 1 or markers != [stage["stage_id"]]:
        raise ValueError("В разделе Границы требуется единственная строка Этап поставки: <stage_id>")
    scope = sections[0]
    if not re.search(r"^Этап поставки: " + re.escape(stage["stage_id"]) + r"$", scope, re.MULTILINE):
        raise ValueError("Маркер этапа должен находиться в разделе Границы")
    if any(stage[field] not in scope for field in ("title", "goal")):
        raise ValueError("Границы должны дословно содержать название и цель зарегистрированного этапа")


def require_audit_stage(state: dict[str, Any], text: str) -> dict[str, Any]:
    stage = active_stage(state, text)
    audit = state.get("delivery_audit", {})
    if audit.get("stage") != stage or audit.get("stage_sha256") != checksum(stage):
        raise ValueError("Метаданные этапа изменились после аудита; выполни аудит заново")
    return stage


def require_manifest_history(state: dict[str, Any], manifest: dict[str, Any]) -> None:
    value = registry(state)
    known = {record["entry"]["revision"]: record for record in value["revisions"]}
    stages = {stage["stage_id"]: stage for stage in value["stages"]}
    for entry in manifest.get("revisions", []):
        previous = known.get(entry["revision"])
        if not entry.get("stage_id"):
            if not previous or "legacy_overlay" not in previous:
                raise ValueError("legacy-stage-migration-required: прежние редакции нельзя автоматически привязать к этапу")
            if immutable_entry(previous["entry"]) != immutable_entry(entry):
                raise ValueError("Прежняя редакция не совпадает с зарегистрированным overlay")
            continue
        validate_revision(entry)
        stage = stages.get(entry["stage_id"])
        if stage is None or snapshot(stage) != entry["stage"]:
            raise ValueError("Снимок этапа в манифесте не совпадает с реестром")
        if previous and ("legacy_overlay" in previous or revision_identity(previous["entry"]) != revision_identity(entry)):
            raise ValueError("Нельзя перепривязать или изменить зарегистрированную редакцию")


def require_entry_stage(entry: dict[str, Any], stage: dict[str, Any]) -> None:
    validate_revision(entry)
    if entry.get("stage") != stage or entry.get("stage_id") != stage["stage_id"]:
        raise ValueError("Редакция относится к другому этапу; перепривязка запрещена")


def stage_action(state: dict[str, Any]) -> str:
    value = registry(state)
    if not value["stages"]:
        return "legacy-stage-migration-required" if state.get("last_published") else "register-delivery-stage"
    return "start-next-delivery-stage" if value["stages"][-1]["state"] == "closed" else "continue-delivery-stage"
