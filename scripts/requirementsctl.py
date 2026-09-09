#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import delivery_stages as stages


STATE_NAME = "requirements-state.json"
STATE_SCHEMA_VERSION = 5
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
    if version == 4:
        migrated = {**payload, "schema_version": STATE_SCHEMA_VERSION}
        audit = payload.get("delivery_audit", {})
        if not audit.get("stage") or not audit.get("stage_sha256"):
            migrated["delivery_audit"] = empty_audit()
            if payload.get("revision_offer", {}).get("state") in {
                "audit-required", "awaiting-audit-confirmation", "preparation-authorized",
            }:
                migrated["delivery_audit"] = empty_audit("required")
                migrated["revision_offer"] = {
                    "state": "audit-required", "offered_at": None,
                    "reason": "После миграции требуется аудит с привязкой к этапу",
                }
        return migrated
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
    if "delivery_stages" in payload:
        migrated["delivery_stages"] = payload["delivery_stages"]
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
    stages.registry(payload)
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
    stages.active_stage(payload, (feature_root / "requirements.md").read_text(encoding="utf-8"))
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
    stage = stages.active_stage(payload, (feature_root / "requirements.md").read_text(encoding="utf-8"))
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
        "stage": stage,
        "stage_sha256": stages.checksum(stage),
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
    stages.require_audit_stage(payload, (feature_root / "requirements.md").read_text(encoding="utf-8"))
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
    current_hash = requirements_hash(feature_root)
    stage = stages.active_stage(payload, (feature_root / "requirements.md").read_text(encoding="utf-8"))
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = load_json(manifest_path)
    publication = manifest.get("publication")
    if isinstance(publication, dict) and publication.get("state") != "merged":
        raise ValueError("Передача ожидает принятия PR/MR; повтори prepare после слияния")
    if args.destination_role == "code":
        publication = manifest.get("publication")
        if not isinstance(publication, dict) or publication.get("state") != "merged" or not publication.get("target_commit"):
            raise ValueError("Передача в code ожидает принятия PR/MR; повтори prepare после слияния")
    if manifest.get("feature") != args.feature or manifest.get("active_revision") != args.revision:
        raise ValueError("Манифест не соответствует функциональности или редакции")
    entries = [
        item for item in manifest.get("revisions", [])
        if isinstance(item, dict) and item.get("revision") == args.revision
    ]
    if len(entries) != 1 or entries[0].get("state") not in {"sent", "in-progress"}:
        raise ValueError("Опубликованная редакция не найдена в манифесте")
    if entries[0].get("sha256") != current_hash:
        raise ValueError("Передана не текущая редакция корневых требований")
    stages.require_manifest_history(payload, manifest)
    stages.require_entry_stage(entries[0], stage)
    records = stages.registry(payload)["revisions"]
    record = next((item for item in records if item["entry"]["revision"] == args.revision), None)
    if (
        not record or args.revision != max(item["entry"]["revision"] for item in records)
        or not record["publication_confirmed"]
        or record["destination_role"] != args.destination_role
        or Path(record["manifest_path"]).resolve() != manifest_path
    ):
        raise ValueError("Сначала повтори prepare: требуется подтверждённое размещение последней редакции")
    published = payload.get("last_published")
    if payload["revision_offer"]["state"] == "not-needed" and published and all((
        published["revision"] == args.revision,
        published["requirements_sha256"] == current_hash,
        published["manifest_path"] == str(manifest_path),
        published["destination_role"] == args.destination_role,
        published.get("stage_id") == stage["stage_id"],
        published.get("stage_revision") == entries[0]["stage_revision"],
        published.get("stage_sha256") == entries[0]["stage_sha256"],
    )):
        output(payload, "continue-root-requirements")
        return 0
    if payload["revision_offer"]["state"] != "preparation-authorized":
        raise ValueError("Передача требований не подтверждена аналитиком после аудита")
    audit = payload["delivery_audit"]
    if audit.get("state") != "confirmed" or audit.get("requirements_sha256") != current_hash:
        raise ValueError("Нет подтверждённого аудита текущей редакции требований")
    stages.require_audit_stage(payload, (feature_root / "requirements.md").read_text(encoding="utf-8"))
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
            "stage_id": stage["stage_id"],
            "stage_revision": entries[0]["stage_revision"],
            "stage_sha256": entries[0]["stage_sha256"],
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


def exchange_module():
    spec = importlib.util.spec_from_file_location("requirements_exchange", Path(__file__).with_name("requirements-exchange.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require_no_legacy_history(project: Path, feature: str, payload: dict[str, Any]) -> None:
    published = payload.get("last_published")
    if published and not published.get("stage_id"):
        raise ValueError("legacy-stage-migration-required: требуется отдельное решение о прежних передачах")
    exchange = exchange_module()
    for _, root in exchange.exchange_roots(project, exchange.resolve_code_root(project, None)):
        manifest_path = root / feature / "manifest.json"
        if manifest_path.is_file():
            stages.require_manifest_history(payload, load_json(manifest_path))


def start_stage_command(args: argparse.Namespace) -> int:
    project, feature_root, state_path = feature_paths(args.project, args.feature)
    payload = load_or_create(feature_root, state_path, args.feature)
    value = stages.registry(payload)
    if value["stages"] and value["stages"][-1]["state"] != "closed":
        raise ValueError("Предыдущий этап должен быть явно закрыт")
    if value["stages"]:
        validate_stage_closure(project, args.feature, payload, value["stages"][-1])
    require_no_legacy_history(project, args.feature, payload)
    stage = stages.snapshot({"stage_id": args.stage_id, "number": len(value["stages"]) + 1, "title": args.title, "goal": args.goal})
    if any(item["stage_id"] == stage["stage_id"] for item in value["stages"]):
        raise ValueError("stage_id уже использован; история этапов неизменяема")
    value["stages"].append({**stage, "state": "open", "started_at": now(), "closure": None})
    value["active_stage_id"] = stage["stage_id"]
    payload["delivery_stages"] = value
    payload["delivery_audit"] = empty_audit()
    payload["revision_offer"] = {"state": "not-needed", "offered_at": None, "reason": "Новый этап требует собственного аудита и подтверждения передачи"}
    payload["updated_at"] = now()
    save_json(state_path, payload)
    output(payload, "continue-delivery-stage")
    return 0


def stage_status_command(args: argparse.Namespace) -> int:
    project, feature_root, state_path = feature_paths(args.project, args.feature)
    payload = load_or_create(feature_root, state_path, args.feature)
    action = stages.stage_action(payload)
    if action == "register-delivery-stage":
        try:
            require_no_legacy_history(project, args.feature, payload)
        except ValueError as exc:
            if not str(exc).startswith("legacy-stage-migration-required"):
                raise
            action = "legacy-stage-migration-required"
    output(payload, action)
    return 0


def adopt_legacy_stage_command(args: argparse.Namespace) -> int:
    project, feature_root, state_path = feature_paths(args.project, args.feature)
    payload = load_or_create(feature_root, state_path, args.feature)
    value = stages.registry(payload)
    if value["stages"] or value["revisions"]:
        raise ValueError("Принятие прежней истории допустимо только до регистрации этапов")
    published = payload.get("last_published")
    manifest_path = Path(args.manifest).expanduser().resolve()
    if not published or published.get("stage_id") or Path(published["manifest_path"]).resolve() != manifest_path:
        raise ValueError("Требуется точный манифест последней зарегистрированной прежней публикации")
    manifest = load_json(manifest_path)
    revisions = manifest.get("revisions", [])
    selected = sorted(args.revisions)
    if (
        manifest.get("feature") != args.feature or manifest.get("active_revision") != published["revision"]
        or not selected or len(selected) != len(set(selected))
        or selected != sorted(item.get("revision") for item in revisions)
        or selected[-1] != published["revision"]
    ):
        raise ValueError("Явно перечисли все редакции прежней истории до последней публикации включительно")
    if any(any(field in item for field in ("stage_id", "stage_revision", "stage", "stage_sha256")) for item in revisions):
        raise ValueError("Редакции с этапами нельзя принимать как прежнюю историю")
    entry = next(item for item in revisions if item["revision"] == published["revision"])
    if entry.get("sha256") != published["requirements_sha256"] or entry.get("state") not in {"sent", "in-progress", "completed"}:
        raise ValueError("Прежняя публикация не совпадает с текущим переданным входом")
    exchange = exchange_module()
    location = {
        "entry": entry, "destination_role": published["destination_role"],
        "manifest_path": str(manifest_path), "publication_confirmed": True,
    }
    root = exchange.publication_root(project, args.feature, location)
    errors = exchange.validate_manifest(manifest, root)
    actual = load_json(root / "manifest.json")
    if errors or actual.get("active_revision") != manifest["active_revision"] or actual.get("revisions") != revisions:
        raise ValueError("Прежний манифест не совпадает с подтверждённым местом передачи: " + "; ".join(errors))
    stage = stages.snapshot({"stage_id": args.stage_id, "number": 1, "title": args.title, "goal": args.goal})
    value["stages"] = [{**stage, "state": "open", "started_at": now(), "closure": None}]
    value["active_stage_id"] = stage["stage_id"]
    for stage_revision, revision in enumerate(selected, 1):
        historical = next(item for item in revisions if item["revision"] == revision)
        value["revisions"].append({
            **location, "entry": historical,
            "legacy_overlay": {
                "stage_id": stage["stage_id"], "stage_revision": stage_revision,
                "stage": stage, "stage_sha256": stages.checksum(stage),
            },
        })
    payload["delivery_stages"] = value
    published.update({"stage_id": stage["stage_id"], "stage_revision": len(selected), "stage_sha256": stages.checksum(stage)})
    payload["delivery_audit"] = empty_audit()
    if requirements_hash(feature_root) == published["requirements_sha256"]:
        payload["revision_offer"] = {"state": "not-needed", "offered_at": None, "reason": "Аналитик явно сопоставил прежнюю передачу этапу"}
    elif payload["revision_offer"]["state"] in {"awaiting-audit-confirmation", "preparation-authorized"}:
        payload["revision_offer"] = {"state": "audit-required", "offered_at": None, "reason": "Сопоставление прежнего этапа требует нового аудита текущего входа"}
    require_no_legacy_history(project, args.feature, payload)
    stages.registry(payload)
    payload["updated_at"] = now()
    save_json(state_path, payload)
    output(payload, "continue-delivery-stage")
    return 0


def current_review(feature_root: Path, feature: str, return_id: str) -> dict[str, Any]:
    results = load_json(feature_root / "development-results-state.json")
    if results.get("schema_version") != 1 or results.get("feature") != feature:
        raise ValueError("Некорректный реестр результатов")
    reviews = [item for item in results.get("processed", []) if isinstance(item, dict) and item.get("return_id") == return_id]
    if not reviews or reviews[-1].get("decision") != "reviewed" or not isinstance(reviews[-1].get("review"), dict):
        raise ValueError("Для закрытия требуется текущее подробное решение reviewed")
    return reviews[-1]["review"]


def validate_stage_closure(project: Path, feature: str, payload: dict[str, Any], stage: dict[str, Any]) -> None:
    closure = stage["closure"]
    records = stages.registry(payload)["revisions"]
    record = next((item for item in records if item["entry"]["revision"] == closure["revision"]), None)
    if record is None or stages.record_entry(record)["stage_id"] != stage["stage_id"] or record["entry"]["sha256"] != closure["requirements_sha256"]:
        raise ValueError("Закрытие не совпадает с переданным этапом")
    review = current_review(project / "features" / feature, feature, closure["return_id"])
    if stages.checksum(review) != closure["review_sha256"]:
        raise ValueError("После закрытия изменилось подробное решение; требуется повторное решение аналитика")
    exchange_module().validate_result_review(project, feature, closure["return_id"], review, publication=record)


def close_stage_command(args: argparse.Namespace) -> int:
    project, feature_root, state_path = feature_paths(args.project, args.feature)
    payload = load_or_create(feature_root, state_path, args.feature)
    value = stages.registry(payload)
    stage = stages.snapshot(value["stages"][-1]) if value["stages"] else stages.active_stage(payload)
    if not args.note.strip():
        raise ValueError("Закрытие требует явного решения --note")
    published = payload.get("last_published")
    records = stages.registry(payload)["revisions"]
    if not published or published.get("stage_id") != stage["stage_id"] or not records:
        raise ValueError("Нельзя закрыть непереданный этап")
    current_hash = requirements_hash(feature_root)
    if payload["revision_offer"]["state"] != "not-needed":
        raise ValueError("Нельзя закрыть этап: изменение объёма или подготовка новой редакции не завершены")
    if current_hash != published["requirements_sha256"] and (
        payload["last_change"]["origin"] != "developer-result"
        or payload.get("requirements_sha256") != current_hash
    ):
        raise ValueError("Нельзя закрыть этап по старому summary после изменения аналитического объёма")
    latest = max(records, key=lambda item: item["entry"]["revision"])
    entry = stages.record_entry(latest)
    if (
        entry["revision"] != published["revision"] or entry["sha256"] != published["requirements_sha256"]
        or not latest["publication_confirmed"]
    ):
        raise ValueError("Последняя редакция не передана или ожидает merge и mark-published")
    manifest = load_json(Path(latest["manifest_path"]))
    stages.require_manifest_history(payload, manifest)
    if manifest.get("active_revision") != entry["revision"]:
        raise ValueError("После публикации появилась другая активная редакция")
    if latest["destination_role"] == "code" and manifest.get("publication", {}).get("state") != "merged":
        raise ValueError("Нельзя закрыть этап, ожидающий merge")
    expected_prefix = f"{args.feature}:{entry['revision']:03d}:revisions/{entry['revision']:03d}/returns/summary.md:"
    if not args.return_id.startswith(expected_prefix):
        raise ValueError("Закрытие требует точный return_id summary последней переданной редакции")
    review = current_review(feature_root, args.feature, args.return_id)
    if review.get("requirements_sha256") != entry["sha256"]:
        raise ValueError("Решение не соответствует checksum последней редакции")
    exchange_module().validate_result_review(project, args.feature, args.return_id, review, publication=latest)
    for item in review["items"]:
        if (
            item["implementation"] == "unknown" or item["conformity"] == "unknown"
            or item["verification"] in {"unknown", "not-run"}
            or item["follow_up"]["action"] == "investigate"
        ):
            raise ValueError("Неизвестный результат или investigate не разрешает закрытие этапа")
    current = payload["delivery_stages"]["stages"][-1]
    if current["state"] == "closed" and current["closure"]["return_id"] == args.return_id and current["closure"]["review_sha256"] == stages.checksum(review):
        raise ValueError("Этап закрыт этим решением; повторное закрытие не требуется")
    current["state"] = "closed"
    current["closure"] = {
        "revision": entry["revision"], "requirements_sha256": entry["sha256"],
        "return_id": args.return_id, "review_sha256": stages.checksum(review),
        "closed_at": now(), "note": args.note.strip(),
    }
    payload["delivery_audit"] = empty_audit()
    payload["revision_offer"] = {"state": "not-needed", "offered_at": None, "reason": "Этап явно закрыт аналитиком"}
    payload["updated_at"] = now()
    save_json(state_path, payload)
    output(payload, "start-next-delivery-stage")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Состояние требований и их передачи")
    commands = result.add_subparsers(dest="command", required=True)
    for name, handler in (("start-stage", start_stage_command), ("stage-status", stage_status_command), ("close-stage", close_stage_command), ("adopt-legacy-stage", adopt_legacy_stage_command)):
        command = commands.add_parser(name)
        command.add_argument("project")
        command.add_argument("feature")
        if name in {"start-stage", "adopt-legacy-stage"}:
            command.add_argument("--analyst-confirmed", action="store_true", required=True)
            command.add_argument("--stage-id", required=True)
            command.add_argument("--title", required=True)
            command.add_argument("--goal", required=True)
        if name == "adopt-legacy-stage":
            command.add_argument("--manifest", required=True)
            command.add_argument("--revisions", type=int, nargs="+", required=True)
        if name == "close-stage":
            command.add_argument("--analyst-confirmed", action="store_true", required=True)
            command.add_argument("--return-id", required=True)
            command.add_argument("--note", required=True)
        command.set_defaults(handler=handler)
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
