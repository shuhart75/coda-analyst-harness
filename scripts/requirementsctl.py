#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_NAME = "requirements-state.json"
PUBLISHED_STATES = {"sent", "in-progress", "paused", "superseded", "archived"}
OFFER_STATES = {
    "not-needed",
    "pending-offer",
    "awaiting-decision",
    "declined-until-explicit-command",
    "preparation-authorized",
}
DERIVATION_STATES = {"not-created", "current", "stale"}
CHANGE_ORIGINS = {"not-recorded", "analyst", "developer-receipt"}


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Ожидался объект JSON: {path}")
    return payload


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


def slice_files(feature_root: Path) -> list[Path]:
    slices = feature_root / "slices"
    return sorted(path for path in slices.glob("*/slice.md") if path.is_file()) if slices.is_dir() else []


def published_revisions(feature_root: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    handoffs = feature_root / "handoffs"
    if not handoffs.is_dir():
        return result
    for manifest_path in sorted(handoffs.glob("*/handoff.json")):
        manifest = load_json(manifest_path)
        if manifest.get("package_kind") != "feature-delivery":
            continue
        package_id = manifest.get("package_id")
        for entry in manifest.get("revisions", []):
            if not isinstance(entry, dict) or (
                entry.get("state") not in PUBLISHED_STATES
                and not (entry.get("state") == "cancelled" and entry.get("sent_at"))
            ):
                continue
            package_path = entry.get("package_path")
            revision = entry.get("revision")
            if not isinstance(package_path, str) or not isinstance(revision, int):
                continue
            package = manifest_path.parent / package_path
            requirements = package / "requirements.md"
            package_manifest = package / "manifest.json"
            if not requirements.is_file() or not package_manifest.is_file():
                continue
            result.append({
                "package_id": package_id,
                "revision": revision,
                "state": entry["state"],
                "published_at": entry.get("sent_at") or entry.get("created_at"),
                "requirements_sha256": hash_file(requirements),
                "package": package,
                "manifest": load_json(package_manifest),
            })
    return sorted(result, key=lambda item: (str(item.get("published_at") or ""), item["revision"]))


def latest_published(feature_root: Path) -> dict[str, Any] | None:
    revisions = published_revisions(feature_root)
    return revisions[-1] if revisions else None


def receipt_is_registered(feature_root: Path, receipt: Path) -> bool:
    handoffs = feature_root / "handoffs"
    for manifest_path in sorted(handoffs.glob("*/handoff.json")) if handoffs.is_dir() else []:
        manifest = load_json(manifest_path)
        root = manifest_path.parent
        try:
            relative = receipt.relative_to(root).as_posix()
        except ValueError:
            continue
        for entry in manifest.get("revisions", []):
            if not isinstance(entry, dict):
                continue
            if (
                entry.get("receipt_path") == relative
                and isinstance(entry.get("receipt"), dict)
                and entry["receipt"].get("expectation") == "received"
            ):
                return True
            for key in ("implementation_results", "test_results"):
                if any(
                    isinstance(item, dict) and item.get("path") == relative
                    for item in entry.get(key, [])
                ):
                    return True
    return False


def source_slices_match(feature_root: Path, published: dict[str, Any]) -> bool:
    payload = published["manifest"].get("payload", [])
    recorded = {
        item.get("path"): item.get("sha256")
        for item in payload
        if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"].startswith("slices/")
    }
    if not recorded or not published["manifest"].get("slices"):
        return False
    actual_files: list[Path] = []
    for slice_path in slice_files(feature_root):
        actual_files.append(slice_path)
        detailed = slice_path.parent / "requirements"
        if detailed.is_dir():
            actual_files.extend(path for path in detailed.rglob("*.md") if path.is_file())
    actual_paths = {path.relative_to(feature_root).as_posix() for path in actual_files}
    if actual_paths != set(recorded):
        return False
    return all(hash_file(feature_root / relative) == checksum for relative, checksum in recorded.items())


def initial_state(feature_root: Path, feature: str) -> dict[str, Any]:
    current_hash = requirements_hash(feature_root, required=False)
    published = latest_published(feature_root)
    recorded_hash = (
        published["requirements_sha256"]
        if published and current_hash != published["requirements_sha256"]
        else current_hash
    )
    slices = slice_files(feature_root)
    derivation_state = "not-created"
    derivation_hash = None
    if slices:
        derivation_hash = published["requirements_sha256"] if published else None
        derivation_state = (
            "current"
            if published and current_hash == derivation_hash and source_slices_match(feature_root, published)
            else "stale"
        )
    return {
        "schema_version": 1,
        "feature": feature,
        "updated_at": now(),
        "requirements_sha256": recorded_hash,
        "last_change": {
            "origin": "not-recorded",
            "recorded_at": None,
            "receipt_path": None,
        },
        "slice_derivation": {
            "state": derivation_state,
            "requirements_sha256": derivation_hash,
        },
        "revision_offer": {
            "state": "not-needed",
            "offered_at": None,
            "reason": "Новая редакция создаётся только по явной команде аналитика",
        },
        "last_published": (
            {
                "package_id": published["package_id"],
                "revision": published["revision"],
                "requirements_sha256": published["requirements_sha256"],
                "published_at": published["published_at"],
            }
            if published
            else None
        ),
    }


def validate_state(payload: dict[str, Any], feature: str) -> None:
    if payload.get("schema_version") != 1 or payload.get("feature") != feature:
        raise ValueError("Некорректная схема или функциональность в состоянии требований")
    if not isinstance(payload.get("updated_at"), str):
        raise ValueError("В состоянии требований отсутствует дата обновления")
    requirements_sha256 = payload.get("requirements_sha256")
    if requirements_sha256 is not None and not isinstance(requirements_sha256, str):
        raise ValueError("Некорректная контрольная сумма корневых требований")
    last_change = payload.get("last_change")
    if not isinstance(last_change, dict) or last_change.get("origin") not in CHANGE_ORIGINS:
        raise ValueError("Некорректный источник последнего изменения требований")
    for key in ("recorded_at", "receipt_path"):
        if last_change.get(key) is not None and not isinstance(last_change.get(key), str):
            raise ValueError("Некорректные сведения о последнем изменении требований")
    offer = payload.get("revision_offer")
    if not isinstance(offer, dict) or offer.get("state") not in OFFER_STATES:
        raise ValueError("Некорректное состояние предложения редакции")
    if offer.get("offered_at") is not None and not isinstance(offer.get("offered_at"), str):
        raise ValueError("Некорректная дата предложения редакции")
    if not isinstance(offer.get("reason"), str):
        raise ValueError("В состоянии требований отсутствует причина решения по редакции")
    derivation = payload.get("slice_derivation")
    if not isinstance(derivation, dict) or derivation.get("state") not in DERIVATION_STATES:
        raise ValueError("Некорректное состояние производных срезов")
    derivation_sha256 = derivation.get("requirements_sha256")
    if derivation_sha256 is not None and not isinstance(derivation_sha256, str):
        raise ValueError("Некорректная контрольная сумма производных срезов")
    last_published = payload.get("last_published")
    if last_published is not None:
        if not isinstance(last_published, dict):
            raise ValueError("Некорректные сведения о последней публикации")
        if not isinstance(last_published.get("package_id"), str) or not isinstance(last_published.get("revision"), int):
            raise ValueError("Некорректный пакет или номер последней публикации")
        if not isinstance(last_published.get("requirements_sha256"), str):
            raise ValueError("Некорректная контрольная сумма последней публикации")
        if last_published.get("published_at") is not None and not isinstance(last_published.get("published_at"), str):
            raise ValueError("Некорректная дата последней публикации")


def load_or_create(feature_root: Path, state_path: Path, feature: str) -> dict[str, Any]:
    if not state_path.exists():
        payload = initial_state(feature_root, feature)
        save_json(state_path, payload)
        return payload
    payload = load_json(state_path)
    validate_state(payload, feature)
    return payload


def output(payload: dict[str, Any], next_action: str) -> None:
    print(json.dumps({"state": payload, "next_action": next_action}, ensure_ascii=False, indent=2))


def init_command(args: argparse.Namespace) -> int:
    _, feature_root, state_path = feature_paths(args.project, args.feature)
    if state_path.exists():
        payload = load_json(state_path)
        validate_state(payload, args.feature)
    else:
        payload = initial_state(feature_root, args.feature)
        save_json(state_path, payload)
    output(payload, "continue-root-requirements")
    return 0


def record_change_command(args: argparse.Namespace) -> int:
    project, feature_root, state_path = feature_paths(args.project, args.feature)
    payload = load_or_create(feature_root, state_path, args.feature)
    current_hash = requirements_hash(feature_root)
    receipt_path = None
    if args.origin == "developer-receipt":
        if not args.receipt:
            raise ValueError("Для изменения по квитанции требуется --receipt")
        receipt = (project / args.receipt).resolve() if not Path(args.receipt).is_absolute() else Path(args.receipt).resolve()
        handoffs = (feature_root / "handoffs").resolve()
        if not receipt.is_file() or handoffs not in receipt.parents:
            raise ValueError("Квитанция должна существовать внутри handoffs текущей функциональности")
        if not receipt_is_registered(feature_root, receipt):
            raise ValueError("Квитанция не зарегистрирована в handoff.json функциональности")
        receipt_path = receipt.relative_to(project).as_posix()
    elif args.receipt:
        raise ValueError("--receipt допустим только для origin=developer-receipt")

    published = latest_published(feature_root)
    derivation = payload["slice_derivation"]
    if not slice_files(feature_root):
        derivation.update({"state": "not-created", "requirements_sha256": None})
    elif derivation.get("requirements_sha256") != current_hash:
        derivation["state"] = "stale"

    offer = payload["revision_offer"]
    if args.origin == "analyst" and published:
        if offer["state"] not in {
            "awaiting-decision",
            "declined-until-explicit-command",
            "preparation-authorized",
        }:
            offer.update({
                "state": "pending-offer",
                "offered_at": None,
                "reason": "После аналитического изменения доступна новая редакция пакета",
            })
    elif args.origin == "analyst":
        offer.update({
            "state": "not-needed",
            "offered_at": None,
            "reason": "Первая редакция создаётся только по явной команде аналитика",
        })
    elif offer["state"] == "not-needed":
        offer.update({
            "offered_at": None,
            "reason": "Изменение по квитанции не создаёт новую редакцию и не пересобирает срезы",
        })

    payload.update({
        "updated_at": now(),
        "requirements_sha256": current_hash,
        "last_change": {
            "origin": args.origin,
            "recorded_at": now(),
            "receipt_path": receipt_path,
        },
    })
    save_json(state_path, payload)
    actions = {
        "pending-offer": "offer-new-revision-once",
        "awaiting-decision": "await-analyst-decision-without-repeating-offer",
        "declined-until-explicit-command": "wait-explicit-preparation-command",
        "preparation-authorized": "derive-slices-and-publish",
        "not-needed": "continue-root-requirements",
    }
    output(payload, actions[offer["state"]])
    return 0


def mark_offered_command(args: argparse.Namespace) -> int:
    _, feature_root, state_path = feature_paths(args.project, args.feature)
    payload = load_or_create(feature_root, state_path, args.feature)
    offer = payload["revision_offer"]
    if offer["state"] != "pending-offer":
        raise ValueError("Предложение новой редакции сейчас не требуется")
    offer.update({"state": "awaiting-decision", "offered_at": now()})
    payload["updated_at"] = now()
    save_json(state_path, payload)
    output(payload, "await-analyst-decision-without-repeating-offer")
    return 0


def decline_command(args: argparse.Namespace) -> int:
    _, feature_root, state_path = feature_paths(args.project, args.feature)
    payload = load_or_create(feature_root, state_path, args.feature)
    offer = payload["revision_offer"]
    if offer["state"] not in {"pending-offer", "awaiting-decision"}:
        raise ValueError("Нет предложения новой редакции, которое можно отклонить")
    offer.update({
        "state": "declined-until-explicit-command",
        "reason": "Аналитик отказался от пересборки до отдельной явной команды",
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
        "state": "preparation-authorized",
        "reason": "Аналитик явно поручил подготовить редакцию для разработки",
    })
    if slice_files(feature_root) and payload["slice_derivation"].get("requirements_sha256") != payload["requirements_sha256"]:
        payload["slice_derivation"]["state"] = "stale"
    payload["updated_at"] = now()
    save_json(state_path, payload)
    output(payload, "derive-slices-and-publish")
    return 0


def mark_published_command(args: argparse.Namespace) -> int:
    _, feature_root, state_path = feature_paths(args.project, args.feature)
    payload = load_or_create(feature_root, state_path, args.feature)
    if payload["revision_offer"]["state"] != "preparation-authorized":
        raise ValueError("Подготовка редакции не была явно разрешена аналитиком")
    matches = [
        item for item in published_revisions(feature_root)
        if item["package_id"] == args.package_id and item["revision"] == args.revision
    ]
    if len(matches) != 1:
        raise ValueError("Опубликованная редакция пакета не найдена")
    published = matches[0]
    current_hash = requirements_hash(feature_root)
    if published["requirements_sha256"] != current_hash:
        raise ValueError("Пакет содержит не текущую редакцию корневых требований")
    if not source_slices_match(feature_root, published):
        raise ValueError("Срезы пакета отсутствуют, неполны или отличаются от производных материалов функциональности")
    payload.update({
        "updated_at": now(),
        "requirements_sha256": current_hash,
        "slice_derivation": {"state": "current", "requirements_sha256": current_hash},
        "revision_offer": {
            "state": "not-needed",
            "offered_at": None,
            "reason": "Текущие требования опубликованы в новой редакции",
        },
        "last_published": {
            "package_id": args.package_id,
            "revision": args.revision,
            "requirements_sha256": current_hash,
            "published_at": published["published_at"],
        },
    })
    save_json(state_path, payload)
    output(payload, "continue-root-requirements")
    return 0


def status_command(args: argparse.Namespace) -> int:
    _, feature_root, state_path = feature_paths(args.project, args.feature)
    payload = load_or_create(feature_root, state_path, args.feature)
    current_hash = requirements_hash(feature_root, required=False)
    action = "record-requirements-change" if current_hash != payload.get("requirements_sha256") else {
        "pending-offer": "offer-new-revision-once",
        "awaiting-decision": "await-analyst-decision-without-repeating-offer",
        "declined-until-explicit-command": "wait-explicit-preparation-command",
        "preparation-authorized": "derive-slices-and-publish",
        "not-needed": "continue-root-requirements",
    }[payload["revision_offer"]["state"]]
    output(payload, action)
    return 1 if action == "record-requirements-change" else 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Состояние подготовки требований к передаче")
    commands = result.add_subparsers(dest="command", required=True)
    for name, handler in (("init", init_command), ("status", status_command)):
        command = commands.add_parser(name)
        command.add_argument("project")
        command.add_argument("feature")
        command.set_defaults(handler=handler)
    record = commands.add_parser("record-change")
    record.add_argument("project")
    record.add_argument("feature")
    record.add_argument("--origin", choices=("analyst", "developer-receipt"), required=True)
    record.add_argument("--receipt")
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
    published = commands.add_parser("mark-published")
    published.add_argument("project")
    published.add_argument("feature")
    published.add_argument("--package-id", required=True)
    published.add_argument("--revision", type=int, required=True)
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
