#!/usr/bin/env python3
"""analyst-sdd-receiver:v1
Receiver-side OpenSpec input import; inspection is the default operation.

This tool belongs to the developer's workflow. Analyst publication only places
it in requirements-exchange and must never invoke its --apply operation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
from typing import Any

# The deployed helper lives in the code checkout: dry-run must not create
# __pycache__ there, which would also fail the clean-source guard below.
sys.dont_write_bytecode = True
import sdd_bundle


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Ожидался объект JSON: {path}")
    return value


def relative_path(value: Any) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Ожидался безопасный относительный путь")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError(f"Небезопасный относительный путь: {value}")
    if any(ord(char) < 32 for char in value):
        raise ValueError("Управляющие символы запрещены в пути")
    return Path(*path.parts)


def plain_path(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Путь вышел за пределы выбранного каталога") from exc
    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current = current / part
        if current.is_symlink():
            raise ValueError(f"Символические ссылки запрещены: {current}")
        if current != path and current.exists() and not current.is_dir():
            raise ValueError(f"Родитель пути не является каталогом: {current}")


def plain_tree(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Ожидался обычный каталог: {root}")
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"Недопустимый тип файла: {path}")


def ensure_parents(root: Path, path: Path, created: list[Path]) -> None:
    """Create missing parents individually so failed imports can remove only ours."""
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        plain_path(root, current)
        if not current.exists():
            current.mkdir()
            created.append(current)
        elif not current.is_dir():
            raise ValueError(f"Ожидался каталог: {current}")


def receive(packet: Path, revision: int, code_root: Path, *, apply: bool = False) -> dict[str, Any]:
    packet, code_root = Path(packet).absolute(), Path(code_root).absolute()
    for root in (packet, code_root):
        for ancestor in (root, *root.parents):
            if ancestor.is_symlink():
                raise ValueError(f"Символические ссылки запрещены: {ancestor}")
    if type(revision) is not int or revision < 1:
        raise ValueError("Номер редакции должен быть положительным целым")
    manifest_path = packet / "manifest.json"
    plain_path(packet, manifest_path)
    manifest = read_object(manifest_path)
    feature = manifest.get("feature")
    if manifest.get("schema_version") != 5 or manifest.get("exchange_kind") != "feature-requirements":
        raise ValueError("Приём proposal/spec требует manifest schema 5")
    if not isinstance(feature, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", feature) or feature != packet.name:
        raise ValueError("Идентификатор передачи не совпадает с каталогом")
    if manifest.get("active_revision") != revision:
        raise ValueError("Принимать можно только активную редакцию")
    entries = manifest.get("revisions")
    if not isinstance(entries, list):
        raise ValueError("Не найден список редакций")
    matches = [entry for entry in entries if isinstance(entry, dict) and entry.get("revision") == revision]
    if len(matches) != 1:
        raise ValueError("Редакция должна быть зарегистрирована ровно один раз")
    entry = matches[0]
    floor = manifest.get("sdd_revision_floor")
    if type(floor) is not int or floor < 1 or revision < floor or entry.get("returns_contract_version") != 2:
        raise ValueError("Редакция не принадлежит новому договору SDD")
    if entry.get("state") not in {"sent", "in-progress"}:
        raise ValueError("Состояние редакции не допускает приём")
    binding = manifest.get("delivery_binding")
    if entry.get("delivery_binding") != binding:
        raise ValueError("Привязка редакции к поставке не совпадает с манифестом")
    if binding is not None:
        if not isinstance(binding, dict) or set(binding) != {"feature_id", "delivery_id", "quarter", "delivery_key"} or binding.get("delivery_key") != feature:
            raise ValueError("Некорректная идентичность поставки")
        if any(not isinstance(binding.get(field), str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", binding[field]) for field in ("feature_id", "delivery_id")) or not isinstance(binding.get("quarter"), str) or not re.fullmatch(r"\d{4}-Q[1-4]", binding["quarter"]):
            raise ValueError("Некорректная идентичность поставки")
    stage = entry.get("stage")
    if not isinstance(stage, dict) or not stage.get("stage_id") or entry.get("stage_id") != stage["stage_id"]:
        raise ValueError("Не найдена точная привязка редакции к этапу")
    stage_hash = digest(json.dumps(stage, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    if entry.get("stage_sha256") != stage_hash or type(entry.get("stage_revision")) is not int or entry["stage_revision"] < 1:
        raise ValueError("Нарушена привязка редакции к этапу")
    prefix = f"revisions/{revision:03d}"
    if entry.get("requirements_path") != prefix + "/requirements.md":
        raise ValueError("Неверный путь требований редакции")
    requirements_path = packet / prefix / "requirements.md"
    plain_path(packet, requirements_path)
    requirements_bytes = requirements_path.read_bytes()
    if digest(requirements_bytes) != entry.get("sha256"):
        raise ValueError("Контрольная сумма требований не совпадает")
    requirements_text = requirements_bytes.decode("utf-8")
    bundle_root = packet / prefix / "sdd"
    descriptor = entry.get("sdd_input")
    if not isinstance(descriptor, dict):
        raise ValueError("Редакция не содержит SDD-вход")
    sdd_bundle.verify_descriptor(bundle_root, requirements_text, descriptor)
    files = {name: (bundle_root / relative_path(name)).read_bytes() for name in descriptor["files"]}
    if {name: digest(data) for name, data in files.items()} != descriptor["files"]:
        raise ValueError("SDD-вход изменился во время чтения")
    package = json.loads(files["package.json"])
    input_binding = {
        "schema_version": 1, "feature": feature, "revision": revision,
        "requirements_sha256": entry["sha256"], "sdd_input_sha256": descriptor["sha256"],
        "stage_id": entry["stage_id"], "stage_revision": entry["stage_revision"],
        "stage_sha256": stage_hash, "delivery_binding": binding,
    }
    prepared = []
    paths = set()
    for change in package["changes"]:
        root = code_root / relative_path(change["target_root"])
        config = root / "config.yaml"
        plain_path(code_root, config)
        if not config.is_file():
            raise ValueError(f"Отсутствует OpenSpec config: {config}")
        target = root / "changes" / change["change_id"]
        plain_path(code_root, target)
        target_key = target.relative_to(code_root).as_posix().casefold()
        if any(target_key == previous or target_key.startswith(previous + "/") or previous.startswith(target_key + "/") for previous in paths):
            raise ValueError("Пути принимаемых изменений пересекаются")
        paths.add(target_key)
        # Refuse a sibling differing only by case even on case-sensitive systems.
        current = code_root
        for part in target.relative_to(code_root).parts:
            if current.is_dir() and any(child.name != part and child.name.casefold() == part.casefold() for child in current.iterdir()):
                raise ValueError(f"Коллизия регистра в целевом пути: {target}")
            current = current / part
        source_prefix = change["contour"] + "/" + change["change_id"] + "/"
        contents = {name[len(source_prefix):]: data for name, data in files.items() if name.startswith(source_prefix)}
        local_binding = {**input_binding, "target_root": change["target_root"], "change_id": change["change_id"], "contour": change["contour"]}
        metadata = (json.dumps(local_binding, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        contents[".analyst-input.json"] = metadata
        existing = target.exists()
        if existing:
            plain_tree(target)
            extra_specs = [path for path in (target / "specs").rglob("*") if path.is_file() and path.relative_to(target).as_posix() not in contents]
            if extra_specs:
                raise ValueError("Существующий change содержит дополнительные входные спецификации")
            for name, data in contents.items():
                path = target / relative_path(name)
                plain_path(code_root, path)
                if not path.is_file() or path.read_bytes() != data:
                    raise ValueError(f"Существующий change не совпадает с точным входом: {path}")
        prepared.append({"target": target, "root": root, "change": change, "contents": contents, "existing": existing})
    allowed_paths = [item["target"].relative_to(code_root).as_posix() for item in prepared if item["existing"]]
    source_check = sdd_bundle.verify_sources(bundle_root, descriptor, code_root, allowed_receiver_paths=allowed_paths)
    created_targets: list[Path] = []
    created_parents: list[Path] = []
    if apply:
        try:
            for item in prepared:
                if item["existing"]:
                    continue
                target = item["target"]
                ensure_parents(code_root, target.parent, created_parents)
                target.mkdir()  # Atomic refusal if another actor created the change.
                created_targets.append(target)
                for name, data in item["contents"].items():
                    path = target / relative_path(name)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("xb") as output:
                        output.write(data)
            for item in prepared:
                plain_tree(item["target"])
                if any((item["target"] / name).read_bytes() != data for name, data in item["contents"].items()):
                    raise ValueError("Принятые файлы изменились во время размещения")
        except Exception:
            for path in reversed(created_targets):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
            for path in reversed(created_parents):
                try:
                    path.rmdir()
                except OSError:
                    pass
            raise
    return {
        "status": "already-current" if all(item["existing"] for item in prepared) else ("imported" if apply else "ready"),
        "applied": apply, "feature": feature, "revision": revision,
        "sdd_input_sha256": descriptor["sha256"], "sdd_sha256": descriptor["sha256"], "source": source_check,
        "changes": [{"path": str(item["target"]), "existing": item["existing"],
                     "working_directory": str(item["root"].parent),
                     "commands": [["openspec", "status", "--change", item["change"]["change_id"], "--json"],
                                  ["openspec", "validate", item["change"]["change_id"], "--strict", "--no-interactive", "--json"]]}
                    for item in prepared],
        "runtime_validation": "not-performed", "receipt_created": False,
        "next_action": "receiver-validate-openspec-then-record-receipt" if apply else "review-then-receiver-apply",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", required=True, type=Path)
    parser.add_argument("--revision", required=True, type=int)
    parser.add_argument("--code-root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="Разместить вход; только в сессии разработчика")
    args = parser.parse_args()
    try:
        result = receive(args.packet, args.revision, args.code_root, apply=args.apply)
    except (ValueError, OSError, UnicodeError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
