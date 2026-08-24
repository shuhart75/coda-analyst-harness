#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXCHANGE_DIR = "requirements-exchange"
STATE_NAME = "development-results-state.json"
FORMAT_MARKER = "Формат: **последовательный человекочитаемый**"
REQ_RE = re.compile(r"\bREQ-[A-Z0-9-]+\b")
REQ_DEFINITION_RE = re.compile(r"^\*\*(REQ-[A-Z0-9-]+)\.\s+.+?\*\*\s*$", re.MULTILINE)
ALLOWED_REVISION_STATES = {"sent", "in-progress", "paused", "superseded", "completed", "cancelled"}
FEATURE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


class CodeDestinationUnavailable(ValueError):
    """The code exchange cannot be reached or published, so analytics fallback is allowed."""


def harness_root() -> Path:
    return Path(__file__).resolve().parents[1]


def state_root() -> Path:
    configured = os.environ.get("CODA_ANALYST_STATE_ROOT", "").strip()
    return Path(configured).expanduser().resolve() if configured else harness_root() / ".workspace-state"


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256(path: Path) -> str:
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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def git_value(repository: Path, *args: str) -> str | None:
    result = subprocess.run(
        ("git", "-C", str(repository), *args),
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def git(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", "-C", str(repository), *args),
        text=True,
        capture_output=True,
        check=False,
    )


def git_paths(repository: Path, *args: str) -> list[str]:
    result = subprocess.run(
        ("git", "-C", str(repository), *args),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def validate_feature(feature: str) -> None:
    if not FEATURE_RE.fullmatch(feature):
        raise ValueError(
            "Идентификатор функциональности должен состоять из строчных латинских букв, "
            "цифр и дефисов, начинаться с буквы или цифры и быть не длиннее 80 знаков"
        )


def current_analyst(explicit: str | None, project: Path | None = None) -> str | None:
    if explicit:
        return explicit
    configured = os.environ.get("CODA_ANALYST_ID", "").strip()
    if configured:
        return configured
    path = state_root() / "collaboration.json"
    if path.is_file():
        value = load_json(path).get("analyst_id")
        if isinstance(value, str) and value:
            return value
    if project is not None:
        for key in ("user.email", "user.name"):
            value = git_value(project, "config", key)
            if value:
                return value
    return None


def resolve_code_root(project: Path, explicit: str | None) -> Path | None:
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        return candidate if candidate.is_dir() else None
    registry_path = state_root() / "code-repos.json"
    if not registry_path.is_file():
        registry_path = harness_root() / "templates" / "workflow" / "code-repos.template.json"
    if not registry_path.is_file():
        return None
    registry = load_json(registry_path)
    repositories = registry.get("repositories", [])
    if not isinstance(repositories, list):
        return None
    code = next(
        (item for item in repositories if isinstance(item, dict) and item.get("id") == "code"),
        None,
    )
    if not code:
        return None
    location = code.get("location", {})
    if not isinstance(location, dict):
        return None
    environment = location.get("environment")
    configured = os.environ.get(environment, "").strip() if isinstance(environment, str) else ""
    if configured:
        candidate = Path(configured).expanduser().resolve()
    else:
        relative = location.get("relative_to_analytical")
        if not isinstance(relative, str) or not relative:
            return None
        candidate = (project / relative).resolve()
    return candidate if candidate.is_dir() else None


def install_root_contract(exchange: Path) -> None:
    templates = harness_root() / "templates" / "exchange"
    for source_name, target_name in (("README.template.md", "README.md"), ("AGENTS.template.md", "AGENTS.md")):
        target = exchange / target_name
        if not target.exists():
            target.write_bytes((templates / source_name).read_bytes())


def title_from_requirements(text: str, feature: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return feature


def validate_requirements_text(text: str) -> list[str]:
    errors: list[str] = []
    identifiers = REQ_DEFINITION_RE.findall(text)
    duplicates = sorted({item for item in identifiers if identifiers.count(item) > 1})
    if FORMAT_MARKER not in text:
        errors.append("requirements.md не использует последовательный человекочитаемый формат")
    if not identifiers:
        errors.append("requirements.md не содержит оформленных определений REQ-*")
    if duplicates:
        errors.append("REQ-* встречаются более одного раза: " + ", ".join(duplicates))
    if "ISO/IEC/IEEE 29148" in text:
        errors.append("новая редакция не должна использовать ISO-подобный профиль")
    if re.search(r"(^|/)slices(/|$)|Карточка среза|Порядок срезов", text, flags=re.IGNORECASE | re.MULTILINE):
        errors.append("новая редакция не должна содержать ссылки на производные срезы")
    return errors


def run_requirement_guards(project: Path, feature: str) -> None:
    checks = (
        ("профильная проверка", "validate-requirements-profile.py", ("--feature", feature)),
        ("языковая проверка", "validate-language.py", ("--feature", feature, "--all")),
    )
    for label, script_name, extra in checks:
        result = subprocess.run(
            (sys.executable, str(harness_root() / "scripts" / script_name), str(project), *extra),
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            details = (result.stdout + result.stderr).strip()
            raise ValueError(f"{label} требований не пройдена: {details}")


def requested_returns_contract() -> dict[str, Any]:
    return {
        "tasks": {
            "path": "revisions/<NNN>/returns/tasks.md",
            "meaning": "Уже согласованная разработчиками разбивка по задачам",
            "estimate": "optional",
            "jira_key": "optional",
        },
        "task_results": {
            "path": "revisions/<NNN>/returns/tasks/<task-id>.md",
            "frequency": "После выполнения или существенного изменения каждой задачи",
        },
        "summary": {
            "path": "revisions/<NNN>/returns/summary.md",
            "meaning": "Итоговое покрытие всех требований активной редакции",
        },
    }


def validate_manifest(manifest: dict[str, Any], root: Path) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema_version") != 1 or manifest.get("exchange_kind") != "feature-requirements":
        errors.append("неподдерживаемая схема manifest.json")
    feature = manifest.get("feature")
    if not isinstance(feature, str) or not FEATURE_RE.fullmatch(feature) or feature != root.name:
        errors.append("feature в manifest.json не совпадает с каталогом функциональности")
    owner = manifest.get("owner")
    if not isinstance(owner, dict) or not isinstance(owner.get("analyst_id"), str) or not owner["analyst_id"]:
        errors.append("в manifest.json отсутствует владелец-аналитик")
    if manifest.get("requested_returns") != requested_returns_contract():
        errors.append("manifest.json содержит неподдерживаемый договор возвратов")
    if manifest.get("traceability") != {
        "requirement_pattern": "REQ-*",
        "chain": "REQ-* -> tasks.md -> task result -> summary.md",
    }:
        errors.append("manifest.json содержит неподдерживаемый договор трассировки")
    if manifest.get("sdd_contract") != "../AGENTS.md" or not (root.parent / "AGENTS.md").is_file():
        errors.append("manifest.json не связан с корневым договором SDD")
    active = manifest.get("active_revision")
    revisions = manifest.get("revisions")
    if not isinstance(active, int) or not isinstance(revisions, list):
        errors.append("в manifest.json некорректны active_revision или revisions")
        return errors
    matches = [item for item in revisions if isinstance(item, dict) and item.get("revision") == active]
    if len(matches) != 1:
        errors.append("активная редакция должна быть зарегистрирована ровно один раз")
        return errors
    revision_numbers = [
        item.get("revision") for item in revisions if isinstance(item, dict)
    ]
    if any(not isinstance(value, int) or value < 1 for value in revision_numbers):
        errors.append("номера редакций должны быть положительными целыми числами")
    elif len(revision_numbers) != len(set(revision_numbers)):
        errors.append("номера редакций должны быть уникальными")
    if matches[0].get("state") == "superseded":
        errors.append("активная редакция не может быть вытеснена новой")
    for item in revisions:
        if not isinstance(item, dict) or not isinstance(item.get("revision"), int):
            errors.append("некорректная запись редакции")
            continue
        if item.get("state") not in ALLOWED_REVISION_STATES:
            errors.append(f"редакция {item.get('revision')} имеет неизвестное состояние")
        relative = item.get("requirements_path")
        expected_requirements = f"revisions/{item.get('revision'):03d}/requirements.md"
        if relative != expected_requirements:
            errors.append(f"редакция {item.get('revision')} имеет некорректный путь требований")
            continue
        expected_returns = f"revisions/{item.get('revision'):03d}/returns"
        if item.get("returns_path") != expected_returns:
            errors.append(f"редакция {item.get('revision')} имеет некорректный путь возвратов")
        path = root / relative
        if not path.is_file():
            errors.append(f"отсутствует файл редакции: {relative}")
        elif item.get("sha256") != sha256(path):
            errors.append(f"нарушена неизменяемость редакции: {relative}")
    return errors


def prepare_in_exchange(
    exchange: Path,
    project: Path,
    feature: str,
    requirements: Path,
    requirements_text: str,
    analyst: str,
) -> dict[str, Any]:
    install_root_contract(exchange)
    feature_exchange = exchange / feature
    manifest_path = feature_exchange / "manifest.json"
    manifest = load_json(manifest_path) if manifest_path.is_file() else {
        "schema_version": 1,
        "exchange_kind": "feature-requirements",
        "feature": feature,
        "title": title_from_requirements(requirements_text, feature),
        "owner": {"analyst_id": analyst},
        "active_revision": None,
        "revisions": [],
        "requested_returns": requested_returns_contract(),
        "traceability": {
            "requirement_pattern": "REQ-*",
            "chain": "REQ-* -> tasks.md -> task result -> summary.md",
        },
        "sdd_contract": "../AGENTS.md",
    }
    if manifest.get("feature") != feature:
        raise ValueError("Существующий manifest.json относится к другой функциональности")
    revisions = manifest.get("revisions")
    if not isinstance(revisions, list):
        raise ValueError("В существующем manifest.json отсутствует список редакций")
    existing_errors = validate_manifest(manifest, feature_exchange) if revisions else []
    if existing_errors:
        raise ValueError("; ".join(existing_errors))
    current_hash = sha256(requirements)
    active = manifest.get("active_revision")
    active_entry = next(
        (item for item in revisions if isinstance(item, dict) and item.get("revision") == active),
        None,
    )
    if active_entry and active_entry.get("sha256") == current_hash:
        return {
            "status": "already-current",
            "feature": feature,
            "revision": active,
            "manifest_path": manifest_path,
            "requirements_path": feature_exchange / active_entry["requirements_path"],
        }
    revision = max(
        (item["revision"] for item in revisions if isinstance(item, dict) and isinstance(item.get("revision"), int)),
        default=0,
    ) + 1
    revision_root = feature_exchange / "revisions" / f"{revision:03d}"
    if revision_root.exists():
        raise ValueError(f"Каталог редакции уже существует: {revision_root}")
    revision_root.mkdir(parents=True)
    target_requirements = revision_root / "requirements.md"
    target_requirements.write_bytes(requirements.read_bytes())
    for item in revisions:
        if isinstance(item, dict) and item.get("state") in {"sent", "in-progress", "paused"}:
            item["state"] = "superseded"
    manifest["title"] = title_from_requirements(requirements_text, feature)
    manifest["owner"] = {"analyst_id": analyst}
    manifest["active_revision"] = revision
    revisions.append({
        "revision": revision,
        "state": "sent",
        "created_at": now(),
        "requirements_path": f"revisions/{revision:03d}/requirements.md",
        "returns_path": f"revisions/{revision:03d}/returns",
        "sha256": current_hash,
        "source": {
            "repository_role": "analytics",
            "feature_path": f"features/{feature}/requirements.md",
            "commit": git_value(project, "rev-parse", "HEAD"),
        },
    })
    save_json(manifest_path, manifest)
    validation = validate_manifest(manifest, feature_exchange)
    if validation:
        raise ValueError("; ".join(validation))
    return {
        "status": "sent",
        "feature": feature,
        "revision": revision,
        "manifest_path": manifest_path,
        "requirements_path": target_requirements,
    }


def code_snapshot(code_root: Path) -> dict[str, str] | None:
    if git(code_root, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        return None
    branch = git_value(code_root, "symbolic-ref", "--quiet", "--short", "HEAD")
    head = git_value(code_root, "rev-parse", "HEAD")
    remote = git_value(code_root, "remote", "get-url", "origin")
    if not branch or not head or not remote:
        return None
    status = subprocess.run(
        ("git", "-C", str(code_root), "status", "--porcelain=v1", "-z", "--untracked-files=all"),
        capture_output=True,
        check=False,
    )
    local_config = subprocess.run(
        ("git", "-C", str(code_root), "config", "--local", "--list", "--null"),
        capture_output=True,
        check=False,
    )
    if status.returncode != 0 or local_config.returncode != 0:
        return None
    return {
        "branch": branch,
        "head": head,
        "remote": remote,
        "status_sha256": hashlib.sha256(status.stdout).hexdigest(),
        "config_sha256": hashlib.sha256(local_config.stdout).hexdigest(),
    }


def cache_manifest(feature: str, revision: int, manifest: Path) -> Path:
    target = state_root() / "exchange-publications" / feature / f"{revision:03d}" / "manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(manifest.read_bytes())
    return target


def assert_exchange_only(repository: Path) -> list[str]:
    paths = sorted(set(
        git_paths(repository, "diff", "--name-only", "-z")
        + git_paths(repository, "ls-files", "--others", "--exclude-standard", "-z")
    ))
    prefix = f"{EXCHANGE_DIR}/"
    invalid = [path for path in paths if not path.startswith(prefix)]
    if invalid:
        raise ValueError("За пределами requirements-exchange обнаружены изменения: " + ", ".join(invalid))
    if not paths:
        return []
    staged = git(repository, "add", "--", *paths)
    if staged.returncode != 0:
        raise ValueError(f"Не удалось подготовить точные пути обмена: {staged.stderr.strip()}")
    cached = git_paths(repository, "diff", "--cached", "--name-only", "-z")
    invalid_cached = [path for path in cached if not path.startswith(prefix)]
    if invalid_cached:
        raise ValueError("В индекс попали пути вне requirements-exchange: " + ", ".join(invalid_cached))
    diff_check = git(repository, "diff", "--cached", "--check")
    if diff_check.returncode != 0:
        raise ValueError(f"Проверка содержимого передачи не пройдена: {diff_check.stdout.strip()}")
    return cached


def publish_to_code(
    project: Path,
    code_root: Path,
    feature: str,
    requirements: Path,
    requirements_text: str,
    analyst: str,
) -> dict[str, Any]:
    before = code_snapshot(code_root)
    if before is None:
        raise CodeDestinationUnavailable("роль code не является доступным клоном Git с веткой и origin")
    last_error = "не удалось опубликовать редакцию"
    for attempt in range(2):
        with tempfile.TemporaryDirectory(prefix="requirements-exchange-") as temporary:
            clone = Path(temporary) / "code"
            cloned = subprocess.run(
                ("git", "clone", "--quiet", "--single-branch", "--branch", before["branch"], before["remote"], str(clone)),
                text=True,
                capture_output=True,
                check=False,
            )
            if cloned.returncode != 0:
                raise CodeDestinationUnavailable("нет доступа на чтение удалённой роли code")
            exchange = clone / EXCHANGE_DIR
            if not exchange.is_dir():
                raise CodeDestinationUnavailable(
                    "в актуальной ветке роли code отсутствует корневой requirements-exchange"
                )
            prepared = prepare_in_exchange(
                exchange, project, feature, requirements, requirements_text, analyst
            )
            changed = assert_exchange_only(clone)
            if not changed:
                manifest_cache = cache_manifest(feature, prepared["revision"], prepared["manifest_path"])
                unchanged = code_snapshot(code_root) == before
                result = {
                    **prepared,
                    "destination_role": "code",
                    "manifest": str(manifest_cache),
                    "repository_url": before["remote"],
                    "repository_branch": before["branch"],
                    "repository_path": f"{EXCHANGE_DIR}/{feature}",
                    "requirements_repository_path": (
                        f"{EXCHANGE_DIR}/{feature}/revisions/{prepared['revision']:03d}/requirements.md"
                    ),
                    "published_commit": git_value(clone, "rev-parse", "HEAD"),
                    "local_code_worktree_unchanged": unchanged,
                    "selection_reason": "Редакция уже находится в удалённой роли code",
                    "message": (
                        f"Редакция {prepared['revision']:03d} уже находится в роли code: "
                        f"ветка {before['branch']}, путь {EXCHANGE_DIR}/{feature}"
                    ),
                }
                result.pop("manifest_path", None)
                result.pop("requirements_path", None)
                return result
            committed = git(
                clone,
                "-c", "user.name=Analyst Requirements Exchange",
                "-c", "user.email=analyst-harness@local.invalid",
                "commit", "--quiet", "-m", f"Передать требования {feature}, редакция {prepared['revision']:03d}",
            )
            if committed.returncode != 0:
                raise ValueError(f"не удалось создать защищённый коммит передачи: {committed.stderr.strip()}")
            published_commit = git_value(clone, "rev-parse", "HEAD")
            pushed = git(clone, "push", "--quiet", "origin", f"HEAD:refs/heads/{before['branch']}")
            if pushed.returncode == 0:
                unchanged = code_snapshot(code_root) == before
                manifest_cache = cache_manifest(feature, prepared["revision"], prepared["manifest_path"])
                result = {
                    **prepared,
                    "destination_role": "code",
                    "manifest": str(manifest_cache),
                    "repository_url": before["remote"],
                    "repository_branch": before["branch"],
                    "repository_path": f"{EXCHANGE_DIR}/{feature}",
                    "requirements_repository_path": (
                        f"{EXCHANGE_DIR}/{feature}/revisions/{prepared['revision']:03d}/requirements.md"
                    ),
                    "published_commit": published_commit,
                    "local_code_worktree_unchanged": unchanged,
                    "selection_reason": "Защищённая публикация в существующий каталог роли code выполнена",
                    "message": (
                        f"Редакция {prepared['revision']:03d} опубликована в роли code: "
                        f"ветка {before['branch']}, путь {EXCHANGE_DIR}/{feature}, коммит {published_commit}"
                    ),
                }
                result.pop("manifest_path", None)
                result.pop("requirements_path", None)
                return result
            last_error = pushed.stderr.strip() or "удалённый репозиторий отклонил отправку"
            if attempt == 0 and any(marker in last_error.lower() for marker in ("fetch first", "non-fast-forward", "rejected")):
                continue
            break
    raise CodeDestinationUnavailable(
        f"удалённая роль code отклонила отправку: {last_error.splitlines()[-1]}"
    )


def prepare_command(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    validate_feature(args.feature)
    feature_root = project / "features" / args.feature
    requirements = feature_root / "requirements.md"
    if not requirements.is_file():
        raise ValueError(f"Требования не найдены: {requirements}")
    requirements_text = requirements.read_text(encoding="utf-8")
    errors = validate_requirements_text(requirements_text)
    if errors:
        raise ValueError("; ".join(errors))
    run_requirement_guards(project, args.feature)
    analyst = current_analyst(args.analyst, project)
    if not analyst:
        raise ValueError("Не задан идентификатор аналитика: используй --analyst или CODA_ANALYST_ID")
    code_root = resolve_code_root(project, args.code_root)
    reason = "Репозиторий роли code отсутствует или не настроен"
    if code_root is not None:
        try:
            result = publish_to_code(
                project, code_root, args.feature, requirements, requirements_text, analyst
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        except CodeDestinationUnavailable as exc:
            reason = str(exc)
    exchange = project / EXCHANGE_DIR
    exchange.mkdir(parents=True, exist_ok=True)
    prepared = prepare_in_exchange(
        exchange, project, args.feature, requirements, requirements_text, analyst
    )
    result = {
        **prepared,
        "destination_role": "analytics",
        "exchange_root": str(exchange),
        "manifest": str(prepared["manifest_path"]),
        "requirements": str(prepared["requirements_path"]),
        "selection_reason": reason,
        "message": (
            f"Редакция {prepared['revision']:03d} размещена в резервном каталоге роли analytics: "
            f"{exchange}. Причина: {reason}"
        ),
    }
    result.pop("manifest_path", None)
    result.pop("requirements_path", None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def exchange_roots(project: Path, code_root: Path | None) -> list[tuple[str, Path]]:
    result: list[tuple[str, Path]] = []
    analytics = project / EXCHANGE_DIR
    if analytics.is_dir():
        result.append(("analytics", analytics))
    if code_root and (code_root / EXCHANGE_DIR).is_dir():
        result.append(("code", code_root / EXCHANGE_DIR))
    return result


def processed_ids(feature_root: Path) -> set[str]:
    state_path = feature_root / STATE_NAME
    if not state_path.is_file():
        return set()
    state = load_json(state_path)
    return {
        item["return_id"]
        for item in state.get("processed", [])
        if isinstance(item, dict) and isinstance(item.get("return_id"), str)
    }


def scan_command(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    analyst = current_analyst(args.analyst, project)
    if not args.all and not analyst:
        raise ValueError(
            "Не удалось определить текущего аналитика; укажи --analyst либо настрой автора Git"
        )
    code_root = resolve_code_root(project, args.code_root)
    items: list[dict[str, Any]] = []
    for role, exchange in exchange_roots(project, code_root):
        for manifest_path in sorted(exchange.glob("*/manifest.json")):
            feature = manifest_path.parent.name
            manifest = load_json(manifest_path)
            errors = validate_manifest(manifest, manifest_path.parent)
            if errors:
                items.append({
                    "source_role": role,
                    "feature": feature,
                    "status": "invalid",
                    "errors": errors
                })
                continue
            owner = manifest.get("owner", {}).get("analyst_id")
            if not args.all and analyst and owner != analyst:
                continue
            revision = manifest["active_revision"]
            revision_entry = next(item for item in manifest["revisions"] if item["revision"] == revision)
            returns = manifest_path.parent / revision_entry["returns_path"]
            processed = processed_ids(project / "features" / feature)
            new_returns = []
            for path in sorted(returns.rglob("*")) if returns.is_dir() else []:
                if not path.is_file():
                    continue
                relative = path.relative_to(manifest_path.parent).as_posix()
                digest = sha256(path)
                return_id = f"{feature}:{revision:03d}:{relative}:{digest}"
                if return_id not in processed:
                    new_returns.append({
                        "return_id": return_id,
                        "path": str(path),
                        "relative_path": relative,
                        "sha256": digest
                    })
            items.append({
                "source_role": role,
                "feature": feature,
                "owner": owner,
                "revision": revision,
                "status": "new-results" if new_returns else "no-new-results",
                "new_returns": new_returns
            })
    print(json.dumps({
        "status": "ok",
        "analyst_id": analyst,
        "scope": "all" if args.all else "owned",
        "items": items,
        "new_result_count": sum(len(item.get("new_returns", [])) for item in items)
    }, ensure_ascii=False, indent=2))
    return 0


def record_processed_command(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    feature_root = project / "features" / args.feature
    if not feature_root.is_dir():
        raise ValueError(f"Функциональность не найдена: {feature_root}")
    analyst = current_analyst(args.analyst, project)
    if not analyst:
        raise ValueError("Не задан идентификатор аналитика")
    state_path = feature_root / STATE_NAME
    state = load_json(state_path) if state_path.is_file() else {
        "schema_version": 1,
        "feature": args.feature,
        "processed": []
    }
    if state.get("schema_version") != 1 or state.get("feature") != args.feature:
        raise ValueError("Некорректный реестр обработанных результатов")
    if any(item.get("return_id") == args.return_id for item in state["processed"] if isinstance(item, dict)):
        raise ValueError("Результат уже зарегистрирован как обработанный")
    state["processed"].append({
        "return_id": args.return_id,
        "processed_at": now(),
        "processed_by": analyst,
        "decision": args.decision,
        "note": args.note
    })
    save_json(state_path, state)
    print(json.dumps({
        "status": "recorded",
        "feature": args.feature,
        "return_id": args.return_id,
        "processed_by": analyst,
        "state_path": str(state_path)
    }, ensure_ascii=False, indent=2))
    return 0


def validate_command(args: argparse.Namespace) -> int:
    root = Path(args.exchange).expanduser().resolve()
    errors: list[str] = []
    manifests = sorted(root.glob("*/manifest.json"))
    if not manifests:
        errors.append("В каталоге обмена нет manifest.json функциональностей")
    for manifest_path in manifests:
        errors.extend(
            f"{manifest_path.parent.name}: {error}"
            for error in validate_manifest(load_json(manifest_path), manifest_path.parent)
        )
    print(json.dumps({
        "status": "valid" if not errors else "invalid",
        "exchange_root": str(root),
        "features": len(manifests),
        "errors": errors
    }, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Обмен требованиями и результатами разработки")
    commands = result.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("project")
    prepare.add_argument("feature")
    prepare.add_argument("--analyst")
    prepare.add_argument("--code-root")
    prepare.set_defaults(handler=prepare_command)
    scan = commands.add_parser("scan")
    scan.add_argument("project")
    scan.add_argument("--analyst")
    scan.add_argument("--code-root")
    scan.add_argument("--all", action="store_true")
    scan.set_defaults(handler=scan_command)
    record = commands.add_parser("record-processed")
    record.add_argument("project")
    record.add_argument("feature")
    record.add_argument("--return-id", required=True)
    record.add_argument("--decision", choices=("requirements-updated", "baseline-updated", "deferred", "cancelled", "no-change"), required=True)
    record.add_argument("--note")
    record.add_argument("--analyst")
    record.set_defaults(handler=record_processed_command)
    validate = commands.add_parser("validate")
    validate.add_argument("exchange")
    validate.set_defaults(handler=validate_command)
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
