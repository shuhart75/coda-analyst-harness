#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_NAME = "requirements-state.json"
STATE_SCHEMA_VERSION = 4
AUDIT_METHOD = "three-level-cross-requirement-v1"
AUDIT_LEVELS = ("individual", "system", "delivery")
OFFER_STATES = {
    "not-needed",
    "pending-offer",
    "awaiting-decision",
    "declined-until-explicit-command",
    "audit-required",
    "awaiting-audit-confirmation",
    "preparation-authorized",
}
CHANGE_ORIGINS = {"not-recorded", "analyst", "developer-result"}
AUDIT_STATES = {"not-requested", "required", "blocked", "awaiting-confirmation", "confirmed"}


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Ожидался объект JSON: {path}")
    return value


def save_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def feature_paths(project_value: str, feature: str) -> tuple[Path, Path, Path]:
    project = Path(project_value).expanduser().resolve()
    feature_root = project / "features" / feature
    if not feature_root.is_dir():
        raise ValueError(f"Функциональность не найдена: {feature_root}")
    return project, feature_root, feature_root / STATE_NAME


def requirements_hash(feature_root: Path, required: bool = True) -> str | None:
    path = feature_root / "requirements.md"
    if not path.is_file():
        if required:
            raise ValueError(f"Корневые требования не найдены: {path}")
        return None
    return hash_file(path)


def initial_state(feature_root: Path, feature: str) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "feature": feature,
        "updated_at": now(),
        "requirements_sha256": requirements_hash(feature_root, required=False),
        "last_change": {
            "origin": "not-recorded",
            "recorded_at": None,
            "return_id": None,
        },
        "revision_offer": {
            "state": "not-needed",
            "offered_at": None,
            "reason": "Новая редакция создаётся только по явной команде аналитика",
        },
        "delivery_audit": empty_audit(),
        "last_published": None,
    }


def empty_audit(state: str = "not-requested") -> dict[str, Any]:
    return {
        "state": state,
        "method": AUDIT_METHOD if state == "required" else None,
        "levels": {level: "pending" for level in AUDIT_LEVELS},
        "requirements_sha256": None,
        "audited_at": None,
        "confirmed_at": None,
        "finding_count": 0,
        "resolved_finding_count": 0,
        "accepted_risk_count": 0,
        "blocking_finding_count": 0,
        "summary": None,
    }


def migrate_state(payload: dict[str, Any], feature: str) -> dict[str, Any]:
    version = payload.get("schema_version")
    if version == STATE_SCHEMA_VERSION:
        return payload
    if version not in {1, 2, 3}:
        return payload
    last_change = payload.get("last_change", {})
    origin = last_change.get("origin")
    if origin == "developer-receipt":
        origin = "developer-result"
    published = payload.get("last_published")
    if isinstance(published, dict) and not isinstance(published.get("manifest_path"), str):
        package_id = published.get("package_id")
        if isinstance(package_id, str) and package_id:
            published = {
                **published,
                "manifest_path": f"features/{feature}/handoffs/{package_id}/handoff.json",
                "destination_role": "analytics",
                "legacy_format": "feature-handoff",
            }
    migrated = {
        "schema_version": STATE_SCHEMA_VERSION,
        "feature": feature,
        "updated_at": payload.get("updated_at") or now(),
        "requirements_sha256": payload.get("requirements_sha256"),
        "last_change": {
            "origin": origin if origin in CHANGE_ORIGINS else "not-recorded",
            "recorded_at": last_change.get("recorded_at"),
            "return_id": last_change.get("return_id") or last_change.get("receipt_path"),
        },
        "revision_offer": payload.get("revision_offer") or {
            "state": "not-needed",
            "offered_at": None,
            "reason": "Состояние перенесено на новый формат обмена",
        },
        "last_published": published,
        "delivery_audit": empty_audit(),
    }
    if migrated["revision_offer"].get("state") in {
        "audit-required",
        "awaiting-audit-confirmation",
        "preparation-authorized",
    }:
        migrated["revision_offer"] = {
            "state": "audit-required",
            "offered_at": None,
            "reason": "После обновления метода перед публикацией требуется новый трёхуровневый аудит",
        }
        migrated["delivery_audit"] = empty_audit("required")
    return migrated


def validate_state(payload: dict[str, Any], feature: str) -> None:
    if payload.get("schema_version") != STATE_SCHEMA_VERSION or payload.get("feature") != feature:
        raise ValueError("Некорректная схема или функциональность в состоянии требований")
    if not isinstance(payload.get("updated_at"), str):
        raise ValueError("В состоянии требований отсутствует дата обновления")
    checksum = payload.get("requirements_sha256")
    if checksum is not None and not isinstance(checksum, str):
        raise ValueError("Некорректная контрольная сумма требований")
    change = payload.get("last_change")
    if not isinstance(change, dict) or change.get("origin") not in CHANGE_ORIGINS:
        raise ValueError("Некорректный источник последнего изменения требований")
    for key in ("recorded_at", "return_id"):
        if change.get(key) is not None and not isinstance(change.get(key), str):
            raise ValueError("Некорректные сведения о последнем изменении требований")
    offer = payload.get("revision_offer")
    if not isinstance(offer, dict) or offer.get("state") not in OFFER_STATES:
        raise ValueError("Некорректное состояние предложения редакции")
    if offer.get("offered_at") is not None and not isinstance(offer.get("offered_at"), str):
        raise ValueError("Некорректная дата предложения редакции")
    if not isinstance(offer.get("reason"), str):
        raise ValueError("В состоянии требований отсутствует причина решения по редакции")
    audit = payload.get("delivery_audit")
    if not isinstance(audit, dict) or audit.get("state") not in AUDIT_STATES:
        raise ValueError("Некорректное состояние аудита требований")
    if audit.get("method") not in {None, AUDIT_METHOD}:
        raise ValueError("Некорректный метод аудита требований")
    levels = audit.get("levels")
    if not isinstance(levels, dict) or set(levels) != set(AUDIT_LEVELS):
        raise ValueError("В состоянии аудита отсутствуют три обязательных уровня")
    if any(value not in {"pending", "complete"} for value in levels.values()):
        raise ValueError("Некорректное состояние уровня аудита")
    for key in ("requirements_sha256", "audited_at", "confirmed_at", "summary"):
        if audit.get(key) is not None and not isinstance(audit.get(key), str):
            raise ValueError("Некорректные сведения об аудите требований")
    for key in (
        "finding_count",
        "resolved_finding_count",
        "accepted_risk_count",
        "blocking_finding_count",
    ):
        if not isinstance(audit.get(key), int) or audit.get(key) < 0:
            raise ValueError("Некорректное количество замечаний аудита")
    classified = (
        audit["resolved_finding_count"]
        + audit["accepted_risk_count"]
        + audit["blocking_finding_count"]
    )
    if classified != audit["finding_count"]:
        raise ValueError("Результаты аудита не распределены по решениям полностью")
    if audit["state"] in {"blocked", "awaiting-confirmation", "confirmed"}:
        if not audit.get("requirements_sha256") or not audit.get("audited_at") or not audit.get("summary"):
            raise ValueError("Завершённый аудит не содержит обязательных сведений")
        if audit.get("method") != AUDIT_METHOD or any(
            levels[level] != "complete" for level in AUDIT_LEVELS
        ):
            raise ValueError("Завершённый аудит не прошёл все три уровня")
    if audit["state"] == "confirmed" and not audit.get("confirmed_at"):
        raise ValueError("Подтверждённый аудит не содержит даты подтверждения")
    published = payload.get("last_published")
    if published is not None:
        if not isinstance(published, dict):
            raise ValueError("Некорректные сведения о последней публикации")
        if not isinstance(published.get("revision"), int):
            raise ValueError("Некорректный номер последней публикации")
        if not isinstance(published.get("requirements_sha256"), str):
            raise ValueError("Некорректная контрольная сумма последней публикации")
        if not isinstance(published.get("manifest_path"), str):
            raise ValueError("Некорректный путь манифеста последней публикации")


def load_or_create(feature_root: Path, state_path: Path, feature: str) -> dict[str, Any]:
    if not state_path.exists():
        payload = initial_state(feature_root, feature)
        save_json(state_path, payload)
        return payload
    payload = migrate_state(load_json(state_path), feature)
    validate_state(payload, feature)
    if load_json(state_path).get("schema_version") != STATE_SCHEMA_VERSION:
        save_json(state_path, payload)
    return payload


def output(payload: dict[str, Any], next_action: str) -> None:
    print(json.dumps({"state": payload, "next_action": next_action}, ensure_ascii=False, indent=2))


def action_for(payload: dict[str, Any]) -> str:
    return {
        "pending-offer": "offer-new-revision-once",
        "awaiting-decision": "await-analyst-decision-without-repeating-offer",
        "declined-until-explicit-command": "wait-explicit-preparation-command",
        "audit-required": "audit-requirements-before-publication",
        "awaiting-audit-confirmation": "show-audit-and-request-analyst-confirmation",
        "preparation-authorized": "validate-and-publish-requirements",
        "not-needed": "continue-root-requirements",
    }[payload["revision_offer"]["state"]]


def init_command(args: argparse.Namespace) -> int:
    _, feature_root, state_path = feature_paths(args.project, args.feature)
    payload = load_or_create(feature_root, state_path, args.feature)
    output(payload, "continue-root-requirements")
    return 0


def record_change_command(args: argparse.Namespace) -> int:
    _, feature_root, state_path = feature_paths(args.project, args.feature)
    payload = load_or_create(feature_root, state_path, args.feature)
    if args.origin == "developer-result" and not args.return_id:
        raise ValueError("Для изменения по результату разработки требуется --return-id")
    if args.origin == "analyst" and args.return_id:
        raise ValueError("--return-id допустим только для origin=developer-result")
    offer = payload["revision_offer"]
    if args.origin == "analyst" and payload.get("last_published"):
        if offer["state"] not in {
            "awaiting-decision",
            "declined-until-explicit-command",
        }:
            offer.update({
                "state": "pending-offer",
                "offered_at": None,
                "reason": "После аналитического изменения доступна новая редакция требований",
            })
    elif args.origin == "analyst":
        offer.update({
            "state": "not-needed",
            "offered_at": None,
            "reason": "Первая редакция создаётся только по явной команде аналитика",
        })
    else:
        offer.update({
            "state": "not-needed",
            "offered_at": None,
            "reason": "Изменение по результату разработки не создаёт новую редакцию",
        })
    payload.update({
        "updated_at": now(),
        "requirements_sha256": requirements_hash(feature_root),
        "last_change": {
            "origin": args.origin,
            "recorded_at": now(),
            "return_id": args.return_id,
        },
        "delivery_audit": empty_audit(),
    })
    save_json(state_path, payload)
    output(payload, action_for(payload))
    return 0


def mark_offered_command(args: argparse.Namespace) -> int:
    _, feature_root, state_path = feature_paths(args.project, args.feature)
    payload = load_or_create(feature_root, state_path, args.feature)
    if payload["revision_offer"]["state"] != "pending-offer":
        raise ValueError("Предложение новой редакции сейчас не требуется")
    payload["revision_offer"].update({"state": "awaiting-decision", "offered_at": now()})
    payload["updated_at"] = now()
    save_json(state_path, payload)
    output(payload, "await-analyst-decision-without-repeating-offer")
    return 0


def decline_command(args: argparse.Namespace) -> int:
    _, feature_root, state_path = feature_paths(args.project, args.feature)
    payload = load_or_create(feature_root, state_path, args.feature)
    if payload["revision_offer"]["state"] not in {"pending-offer", "awaiting-decision"}:
        raise ValueError("Нет предложения новой редакции, которое можно отклонить")
    payload["revision_offer"].update({
        "state": "declined-until-explicit-command",
        "reason": "Аналитик отказался от новой редакции до отдельной явной команды",
    })
    payload["updated_at"] = now()
    save_json(state_path, payload)
    output(payload, "wait-explicit-preparation-command")
    return 0


def begin_preparation_command(args: argparse.Namespace) -> int:
    _, feature_root, state_path = feature_paths(args.project, args.feature)
    payload = load_or_create(feature_root, state_path, args.feature)
    payload["requirements_sha256"] = requirements_hash(feature_root)
    payload["revision_offer"].update({
        "state": "audit-required",
        "reason": "Аналитик поручил передачу; до публикации требуется аудит и его подтверждение",
    })
    payload["delivery_audit"] = empty_audit("required")
    payload["updated_at"] = now()
    save_json(state_path, payload)
    output(payload, "audit-requirements-before-publication")
    return 0


def record_audit_command(args: argparse.Namespace) -> int:
    _, feature_root, state_path = feature_paths(args.project, args.feature)
    payload = load_or_create(feature_root, state_path, args.feature)
    if payload["revision_offer"]["state"] != "audit-required":
        raise ValueError("Аудит не был начат явной командой передачи требований")
    if args.finding_count < 0 or args.blocking_finding_count < 0:
        raise ValueError("Количество замечаний не может быть отрицательным")
    if args.blocking_finding_count > args.finding_count:
        raise ValueError("Количество блокирующих замечаний превышает общее количество")
    if args.accepted_risk_count < 0:
        raise ValueError("Количество принятых рисков не может быть отрицательным")
    if args.accepted_risk_count + args.blocking_finding_count > args.finding_count:
        raise ValueError("Принятые риски и блокировки превышают общее количество замечаний")
    if not args.summary.strip():
        raise ValueError("Итог аудита не может быть пустым")
    checksum = requirements_hash(feature_root)
    blocked = args.blocking_finding_count > 0
    resolved = args.finding_count - args.accepted_risk_count - args.blocking_finding_count
    payload["delivery_audit"] = {
        "state": "blocked" if blocked else "awaiting-confirmation",
        "method": AUDIT_METHOD,
        "levels": {level: "complete" for level in AUDIT_LEVELS},
        "requirements_sha256": checksum,
        "audited_at": now(),
        "confirmed_at": None,
        "finding_count": args.finding_count,
        "resolved_finding_count": resolved,
        "accepted_risk_count": args.accepted_risk_count,
        "blocking_finding_count": args.blocking_finding_count,
        "summary": args.summary.strip(),
    }
    if blocked:
        payload["revision_offer"].update({
            "state": "audit-required",
            "reason": "Аудит выявил блокирующие замечания; публикация запрещена",
        })
        next_action = "resolve-blocking-audit-findings"
    else:
        payload["revision_offer"].update({
            "state": "awaiting-audit-confirmation",
            "reason": "Аудит завершён; требуется явное подтверждение аналитика",
        })
        next_action = "show-audit-and-request-analyst-confirmation"
    payload["updated_at"] = now()
    save_json(state_path, payload)
    output(payload, next_action)
    return 0


def confirm_audit_command(args: argparse.Namespace) -> int:
    _, feature_root, state_path = feature_paths(args.project, args.feature)
    payload = load_or_create(feature_root, state_path, args.feature)
    audit = payload["delivery_audit"]
    if payload["revision_offer"]["state"] != "awaiting-audit-confirmation":
        raise ValueError("Нет завершённого аудита, ожидающего подтверждения аналитика")
    if audit["state"] != "awaiting-confirmation" or audit["blocking_finding_count"]:
        raise ValueError("Аудит с блокирующими замечаниями нельзя подтвердить")
    current_hash = requirements_hash(feature_root)
    if audit["requirements_sha256"] != current_hash:
        raise ValueError("Требования изменились после аудита; выполните аудит заново")
    audit.update({"state": "confirmed", "confirmed_at": now()})
    payload["requirements_sha256"] = current_hash
    payload["revision_offer"].update({
        "state": "preparation-authorized",
        "reason": "Аналитик подтвердил аудит и отправку неизменившихся требований",
    })
    payload["updated_at"] = now()
    save_json(state_path, payload)
    output(payload, "validate-and-publish-requirements")
    return 0


def mark_published_command(args: argparse.Namespace) -> int:
    _, feature_root, state_path = feature_paths(args.project, args.feature)
    payload = load_or_create(feature_root, state_path, args.feature)
    if payload["revision_offer"]["state"] != "preparation-authorized":
        raise ValueError("Передача требований не подтверждена аналитиком после аудита")
    audit = payload["delivery_audit"]
    current_hash = requirements_hash(feature_root)
    if audit.get("state") != "confirmed" or audit.get("requirements_sha256") != current_hash:
        raise ValueError("Нет подтверждённого аудита текущей редакции требований")
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = load_json(manifest_path)
    if manifest.get("feature") != args.feature or manifest.get("active_revision") != args.revision:
        raise ValueError("Манифест не соответствует функциональности или редакции")
    entries = [
        item for item in manifest.get("revisions", [])
        if isinstance(item, dict) and item.get("revision") == args.revision
    ]
    if len(entries) != 1 or entries[0].get("state") != "sent":
        raise ValueError("Опубликованная редакция не найдена в манифесте")
    if entries[0].get("sha256") != current_hash:
        raise ValueError("Передана не текущая редакция корневых требований")
    payload.update({
        "updated_at": now(),
        "requirements_sha256": current_hash,
        "revision_offer": {
            "state": "not-needed",
            "offered_at": None,
            "reason": "Текущие требования переданы в новой редакции",
        },
        "last_published": {
            "revision": args.revision,
            "requirements_sha256": current_hash,
            "published_at": now(),
            "manifest_path": str(manifest_path),
            "destination_role": args.destination_role,
        },
    })
    save_json(state_path, payload)
    output(payload, "continue-root-requirements")
    return 0


def status_command(args: argparse.Namespace) -> int:
    _, feature_root, state_path = feature_paths(args.project, args.feature)
    payload = load_or_create(feature_root, state_path, args.feature)
    current_hash = requirements_hash(feature_root, required=False)
    action = "record-requirements-change" if current_hash != payload.get("requirements_sha256") else action_for(payload)
    output(payload, action)
    return 1 if action == "record-requirements-change" else 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Состояние требований и их передачи")
    commands = result.add_subparsers(dest="command", required=True)
    for name, handler in (("init", init_command), ("status", status_command)):
        command = commands.add_parser(name)
        command.add_argument("project")
        command.add_argument("feature")
        command.set_defaults(handler=handler)
    record = commands.add_parser("record-change")
    record.add_argument("project")
    record.add_argument("feature")
    record.add_argument("--origin", choices=("analyst", "developer-result"), required=True)
    record.add_argument("--return-id")
    record.set_defaults(handler=record_change_command)
    offered = commands.add_parser("mark-offered")
    offered.add_argument("project")
    offered.add_argument("feature")
    offered.set_defaults(handler=mark_offered_command)
    decline = commands.add_parser("decline-revision")
    decline.add_argument("project")
    decline.add_argument("feature")
    decline.set_defaults(handler=decline_command)
    begin = commands.add_parser("begin-preparation")
    begin.add_argument("project")
    begin.add_argument("feature")
    begin.set_defaults(handler=begin_preparation_command)
    audit = commands.add_parser("record-audit")
    audit.add_argument("project")
    audit.add_argument("feature")
    audit.add_argument("--finding-count", type=int, required=True)
    audit.add_argument("--accepted-risk-count", type=int, default=0)
    audit.add_argument("--blocking-finding-count", type=int, required=True)
    audit.add_argument("--summary", required=True)
    audit.set_defaults(handler=record_audit_command)
    confirm = commands.add_parser("confirm-audit")
    confirm.add_argument("project")
    confirm.add_argument("feature")
    confirm.set_defaults(handler=confirm_audit_command)
    published = commands.add_parser("mark-published")
    published.add_argument("project")
    published.add_argument("feature")
    published.add_argument("--manifest", required=True)
    published.add_argument("--revision", type=int, required=True)
    published.add_argument("--destination-role", choices=("analytics", "code"), required=True)
    published.set_defaults(handler=mark_published_command)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        return args.handler(args)
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
