#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from workspace_paths import ensure_local_state, retired_repositories_path, source_mirror_path
from workspace_entrypoint import embedded_harness_paths, write_local_entrypoint


DEFAULT_REPOSITORIES = {
    "documents": "ssh://git@stash.delta.sbrf.ru:7999/rscon/documents.git",
    "coda": "ssh://git@stash.delta.sbrf.ru:7999/rscon/coda.git",
    "changeswork-copy": "https://github.com/shuhart75/changeswork-copy.git",
}
ENVIRONMENT_URLS = {
    "documents": "CODA_ANALYST_DOCUMENTS_URL",
    "coda": "CODA_ANALYST_CODA_URL",
    "changeswork-copy": "CODA_ANALYST_SOURCE_URL",
}
REPOSITORY_IDS = tuple(DEFAULT_REPOSITORIES)
DEFAULT_ROLES = {
    "analytics": "documents",
    "code": "coda",
    "source": "changeswork-copy",
}
ROLE_CONFIG_NAME = "repository-roles.json"
PROJECT_PATHS = ("baseline", "context", "features", "planning", "releases")
CODE_PUSH_DISABLED = "DISABLED_BY_CODA_ANALYST_HARNESS"
WORKSPACE_STATE_NAME = "workspace.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@contextmanager
def workspace_operation_lock(root: Path):
    path = root / ".workspace-state/workspace-operation.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise ValueError("Другая операция уже изменяет рабочую область") from exc
    try:
        yield
    finally:
        handle.close()


def root_path(explicit: str | None) -> Path:
    return Path(explicit).expanduser().resolve() if explicit else Path(__file__).resolve().parents[1]


def repository_urls() -> dict[str, str]:
    return {
        name: os.environ.get(ENVIRONMENT_URLS[name], default)
        for name, default in DEFAULT_REPOSITORIES.items()
    }


def role_config_path(root: Path) -> Path:
    return root / ".workspace-state" / ROLE_CONFIG_NAME


def workspace_state_path(root: Path) -> Path:
    return root / ".workspace-state" / WORKSPACE_STATE_NAME


def load_previous_workspace_state(root: Path) -> dict | None:
    path = workspace_state_path(root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать состояние рабочей области {path}: {exc}") from exc
    if not isinstance(payload.get("roles"), dict):
        raise ValueError(f"Повреждено состояние рабочей области: {path}")
    return payload


def role_was_prepared(previous: dict | None, role: str, repository_id: str | None) -> bool:
    if previous is None or repository_id is None:
        return False
    item = previous.get("roles", {}).get(role, {})
    return isinstance(item, dict) and item.get("repository") == repository_id


def validate_roles(roles: dict[str, str | None]) -> dict[str, str | None]:
    if set(roles) != set(DEFAULT_ROLES):
        raise ValueError("Настройка ролей должна содержать analytics, code и source")
    if roles["analytics"] not in REPOSITORY_IDS:
        raise ValueError("Роль analytics должна ссылаться на известный репозиторий")
    for role in ("code", "source"):
        if roles[role] is not None and roles[role] not in REPOSITORY_IDS:
            raise ValueError(f"Роль {role} должна ссылаться на известный репозиторий или быть отключена")
    assigned = [item for item in roles.values() if item is not None]
    if len(assigned) != len(set(assigned)):
        raise ValueError("Один репозиторий не может одновременно выполнять несколько ролей")
    return roles


def load_roles(root: Path) -> dict[str, str | None]:
    path = role_config_path(root)
    if not path.is_file():
        return dict(DEFAULT_ROLES)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать настройку ролей {path}: {exc}") from exc
    if payload.get("schema_version") != 1 or not isinstance(payload.get("roles"), dict):
        raise ValueError(f"Неподдерживаемая настройка ролей {path}")
    return validate_roles(payload["roles"])


def misplaced_project_paths(root: Path) -> list[str]:
    return [name for name in PROJECT_PATHS if (root / name).exists()]


def require_clean_harness_boundary(root: Path) -> None:
    misplaced = misplaced_project_paths(root)
    if misplaced:
        raise ValueError(
            "Проектные каталоги ошибочно созданы в корне обвязки: "
            + ", ".join(misplaced)
            + ". Перенеси их в репозиторий роли analytics до продолжения"
        )


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)


def git_root(path: Path) -> Path | None:
    result = run("git", "-C", str(path), "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def is_bare_repository(path: Path) -> bool:
    result = run("git", "-C", str(path), "rev-parse", "--is-bare-repository")
    return result.returncode == 0 and result.stdout.strip() == "true"


def validate_origin(path: Path, name: str, url: str) -> None:
    remotes = run("git", "-C", str(path), "remote", "get-url", "--all", "origin")
    current = {line.strip() for line in remotes.stdout.splitlines() if line.strip()}
    if url not in current:
        raise ValueError(f"origin {name} не совпадает с ожидаемым URL: {sorted(current)}")


def disable_push(path: Path, name: str) -> None:
    result = run(
        "git", "-C", str(path), "config", "remote.origin.pushurl",
        CODE_PUSH_DISABLED,
    )
    if result.returncode != 0:
        raise ValueError(f"Не удалось запретить push для {name}: {result.stderr.strip()}")


def require_push_disabled(path: Path, name: str) -> None:
    result = run("git", "-C", str(path), "remote", "get-url", "--push", "origin")
    if result.returncode != 0 or result.stdout.strip() != CODE_PUSH_DISABLED:
        raise ValueError(
            f"Для существующего репозитория роли code ({name}) не закреплён запрет отправки; "
            "аналитическая обвязка не будет менять его настройки"
        )


def enable_push(path: Path, name: str, url: str) -> None:
    result = run("git", "-C", str(path), "config", "remote.origin.pushurl", url)
    if result.returncode != 0:
        raise ValueError(f"Не удалось разрешить push для роли analytics ({name}): {result.stderr.strip()}")


def clone_or_validate(root: Path, name: str, url: str) -> Path:
    path = root / name
    if not path.exists():
        result = run("git", "clone", url, str(path))
        if result.returncode != 0:
            raise ValueError(f"Не удалось клонировать {name}: {result.stderr.strip()}")
    if git_root(path) != path:
        raise ValueError(f"{path} не является корнем Git-репозитория")
    validate_origin(path, name, url)
    return path


def clone_code_once(root: Path, name: str, url: str) -> Path:
    created = not (root / name).exists()
    path = clone_or_validate(root, name, url)
    if created:
        disable_push(path, name)
    else:
        require_push_disabled(path, name)
    return path


def clone_or_validate_source_mirror(root: Path, repository_id: str, url: str) -> Path:
    path = source_mirror_path(root, repository_id)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        result = run("git", "clone", "--bare", url, str(path))
        if result.returncode != 0:
            raise ValueError(f"Не удалось создать служебное зеркало {repository_id}: {result.stderr.strip()}")
    if not is_bare_repository(path):
        raise ValueError(f"{path} не является bare-репозиторием {repository_id}")
    validate_origin(path, repository_id, url)
    main = run("git", "-C", str(path), "show-ref", "--verify", "--quiet", "refs/heads/main")
    if main.returncode != 0:
        raise ValueError(f"В {repository_id} отсутствует ветка main")
    head = run("git", "-C", str(path), "symbolic-ref", "HEAD", "refs/heads/main")
    if head.returncode != 0:
        raise ValueError(f"Не удалось закрепить main в зеркале {repository_id}: {head.stderr.strip()}")
    disable_push(path, repository_id)
    return path


def retire_source_worktree(root: Path, repository_id: str, url: str) -> Path | None:
    legacy = root / repository_id
    if not legacy.exists():
        return None
    if legacy.is_symlink() or git_root(legacy) != legacy:
        raise ValueError(
            f"Старый путь {legacy} занят не ожидаемым Git-репозиторием; "
            "обвязка не будет перемещать его автоматически"
        )
    validate_origin(legacy, repository_id, url)
    retired_root = retired_repositories_path(root)
    retired_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = retired_root / f"{repository_id}-{stamp}"
    counter = 1
    while target.exists():
        target = retired_root / f"{repository_id}-{stamp}-{counter}"
        counter += 1
    legacy.rename(target)
    return target


def write_workspace(
    root: Path,
    analytics: Path,
    code: Path | None,
) -> Path:
    folders = [
        {"name": "analyst-harness", "path": "."},
        {"name": "analytics", "path": os.path.relpath(analytics, root)},
    ]
    if code:
        folders.append({"name": "code-read-only", "path": os.path.relpath(code, root)})
    payload = {
        "folders": folders,
        "settings": {
            "files.exclude": {
                "**/.git": True,
                ".workspace-state/repositories": True,
                ".workspace-state/retired-repositories": True,
            },
            "search.exclude": {
                "**/.git": True,
                "**/node_modules": True,
                "**/build": True,
                ".workspace-state/repositories": True,
                ".workspace-state/retired-repositories": True,
            },
            "files.watcherExclude": {
                "**/.workspace-state/repositories/**": True,
                "**/.workspace-state/retired-repositories/**": True,
            },
        },
    }
    path = root / "coda-analyst.code-workspace"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def detect_contours(code: Path) -> dict:
    contours = {name: {"path": name} for name in ("backend", "frontend") if (code / name).is_dir()}
    return contours or {"root": {"path": "."}}


def write_code_registry(
    root: Path,
    analytics: Path,
    analytics_id: str,
    code: Path | None,
    code_id: str | None,
    urls: dict[str, str],
) -> Path:
    repositories = []
    if code and code_id:
        repositories.append({
            "id": "code",
            "repository_id": code_id,
            "purpose": "Кодовый репозиторий для проверки требований",
            "access": "read-only",
            "write_policy": {
                "mode": "operations-only",
                "allowed_paths": ["requirements-exchange/**"],
                "allowed_operations": [
                    "initial-clone",
                    "git-pull-ff-only-via-workspace",
                    "requirements-exchange-publish-via-isolated-clone",
                ],
                "user_prompt_can_override": False,
            },
            "location": {
                "environment": "CODA_REPO" if code_id == "coda" else None,
                "relative_to_analytical": os.path.relpath(code, analytics),
            },
            "accepted_remote_urls": [urls[code_id]],
            "expected_branch": None,
            "contours": detect_contours(code),
            "instruction_patterns": [
                "AGENTS.md", "**/AGENTS.md", "CLAUDE.md", "**/CLAUDE.md",
                "openspec/README.md", "**/openspec/README.md", ".sdd/README.md", "**/.sdd/README.md",
            ],
        })
    payload = {
        "schema_version": 3,
        "workspace": {
            "layout": "role-based-sibling-clones",
            "analytical_repository": {
                "id": "analytics",
                "repository_id": analytics_id,
                "accepted_remote_urls": [urls[analytics_id]],
            },
        },
        "repositories": repositories,
    }
    path = root / ".workspace-state" / "code-repos.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def bootstrap_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    require_clean_harness_boundary(root)
    urls = repository_urls()
    roles = load_roles(root)
    previous = load_previous_workspace_state(root)
    ensure_local_state()
    repositories: dict[str, Path] = {}
    availability: dict[str, str] = {}
    analytics_id = str(roles["analytics"])
    analytics_path = root / analytics_id
    if role_was_prepared(previous, "analytics", analytics_id) and not analytics_path.exists():
        raise ValueError(
            f"Репозиторий роли analytics ({analytics_id}) удалён; "
            "автоматическое повторное клонирование запрещено во избежание потери локальных данных"
        )
    repositories[analytics_id] = clone_or_validate(root, analytics_id, urls[analytics_id])
    availability["analytics"] = "ready"
    enable_push(repositories[analytics_id], analytics_id, urls[analytics_id])
    code_id = roles["code"]
    if code_id:
        code_path = root / code_id
        if role_was_prepared(previous, "code", code_id) and not code_path.exists():
            availability["code"] = "absent"
        else:
            repositories[code_id] = clone_code_once(root, code_id, urls[code_id])
            availability["code"] = "ready"
    else:
        availability["code"] = "disabled"
    source_id = roles["source"]
    retired_source = None
    if source_id:
        source_path = source_mirror_path(root, source_id)
        if role_was_prepared(previous, "source", source_id) and not source_path.exists():
            availability["source"] = "absent"
        else:
            repositories[source_id] = clone_or_validate_source_mirror(root, source_id, urls[source_id])
            availability["source"] = "ready"
            retired_source = retire_source_worktree(root, source_id, urls[source_id])
    else:
        availability["source"] = "disabled"
    analytics = repositories[analytics_id]
    code = repositories.get(code_id) if code_id else None
    legacy_harness = embedded_harness_paths(analytics)
    entrypoint = (
        None
        if legacy_harness
        else write_local_entrypoint(analytics, root, code, availability["code"])
    )
    code_registry = write_code_registry(
        root,
        analytics,
        analytics_id,
        code,
        code_id if availability["code"] == "ready" else None,
        urls,
    )
    workspace = write_workspace(root, analytics, code)
    state_dir = root / ".workspace-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    degraded = any(availability[role] == "absent" for role in ("code", "source"))
    expected_paths = {
        "analytics": root / analytics_id,
        "code": root / code_id if code_id else None,
        "source": source_mirror_path(root, source_id) if source_id else None,
    }
    state = {
        "schema_version": 4,
        "status": "degraded" if degraded else "ready",
        "prepared_at": utc_now(),
        "workspace_root": str(root),
        "repositories": {
            repository_id: {
                "path": str(expected_paths[role]),
                "remote_url": urls[repository_id],
                "storage": "bare-mirror" if role == "source" else "worktree",
                "availability": availability[role],
            }
            for role, repository_id in roles.items()
            if repository_id is not None
        },
        "roles": {
            role: {
                "repository": repository_id,
                "path": str(expected_paths[role]) if repository_id else None,
                "availability": availability[role],
            }
            for role, repository_id in roles.items()
        },
        "project_root": str(analytics),
        "local_entrypoint": str(entrypoint) if entrypoint else None,
        "code_registry": str(code_registry),
        "migration": {
            "required": bool(legacy_harness),
            "embedded_harness_paths": legacy_harness,
            "next_command": (
                "workspace.py sync"
                if legacy_harness and availability["source"] == "ready"
                else None
            ),
            "blocked_reason": (
                "source-role-absent"
                if legacy_harness and availability["source"] != "ready"
                else None
            ),
        },
        "write_policy": {
            "analytics": "read-write-push-allowed",
            "code": {
                "access": "read-only",
                "allowed_paths": ["requirements-exchange/**"],
                "allowed_operations": [
                    "initial-clone",
                    "git-pull-ff-only-via-workspace",
                    "requirements-exchange-publish-via-isolated-clone",
                ],
            } if availability["code"] == "ready" else {"access": "disabled", "allowed_paths": []},
            "source": "bare-mirror-fetch-only" if availability["source"] == "ready" else "disabled",
        },
        "retired_legacy_source": str(retired_source) if retired_source else None,
    }
    state_path = workspace_state_path(root)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"workspace": str(workspace), **state}, ensure_ascii=False, indent=2))
    return 0


def status_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    roles = load_roles(root)
    previous = load_previous_workspace_state(root)
    urls = repository_urls()
    misplaced = misplaced_project_paths(root)
    status = "invalid" if misplaced else "ready"
    items = []
    for role, name in roles.items():
        if name is None:
            items.append({"role": role, "repository": None, "state": "disabled"})
            continue
        path = source_mirror_path(root, name) if role == "source" else root / name
        valid = is_bare_repository(path) if role == "source" else git_root(path) == path
        if valid:
            state = "ready"
        elif path.exists():
            state = "invalid"
            status = "invalid"
        elif role != "analytics" and role_was_prepared(previous, role, name):
            state = "absent"
            if status == "ready":
                status = "degraded"
        else:
            state = "missing"
            if status != "invalid":
                status = "incomplete"
        items.append({
            "role": role,
            "repository": name,
            "path": str(path),
            "state": state,
            "storage": "bare-mirror" if role == "source" else "worktree",
            "expected_origin": urls[name],
        })
    print(json.dumps({
        "status": status,
        "project_root": str(root / str(roles["analytics"])),
        "misplaced_project_paths": misplaced,
        "repositories": items,
    }, ensure_ascii=False, indent=2))
    return 0 if status in {"ready", "degraded"} else 1


def update_code(root: Path) -> dict:
    code_id = load_roles(root)["code"]
    if not code_id:
        return {
            "status": "skipped",
            "role": "code",
            "repository": None,
            "reason": "role-disabled",
            "operation": "none",
        }
    code = root / code_id
    if not code.exists():
        return {
            "status": "skipped",
            "role": "code",
            "repository": code_id,
            "reason": "repository-absent",
            "operation": "none",
        }
    if git_root(code) != code:
        raise ValueError(
            f"Путь роли code ({code_id}) существует, но не является отдельным Git-репозиторием: {code}"
        )
    require_push_disabled(code, code_id)
    validate_origin(code, code_id, repository_urls()[code_id])
    dirty = run("git", "-C", str(code), "status", "--porcelain=v1")
    if dirty.returncode != 0 or dirty.stdout:
        raise ValueError(f"Репозиторий роли code ({code_id}) содержит локальные изменения; git pull запрещён")
    branch = run("git", "-C", str(code), "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch.returncode != 0:
        raise ValueError(f"Репозиторий роли code ({code_id}) находится в detached HEAD; git pull запрещён")
    name = branch.stdout.strip()
    before = run("git", "-C", str(code), "rev-parse", "HEAD").stdout.strip()
    pulled = run(
        "git", "-C", str(code), "-c", "core.hooksPath=/dev/null",
        "pull", "--ff-only", "--no-rebase", "origin", name,
    )
    if pulled.returncode != 0:
        raise ValueError(f"Не удалось выполнить защищённый git pull роли code ({code_id}): {pulled.stderr.strip()}")
    after_status = run("git", "-C", str(code), "status", "--porcelain=v1")
    if after_status.returncode != 0 or after_status.stdout:
        raise ValueError("Защищённый git pull оставил изменённое рабочее дерево роли code; требуется владелец кода")
    require_push_disabled(code, code_id)
    after = run("git", "-C", str(code), "rev-parse", "HEAD").stdout.strip()
    return {
        "status": "updated" if before != after else "current",
        "role": "code",
        "repository": code_id,
        "branch": name,
        "before": before,
        "after": after,
        "operation": "git-pull-ff-only-via-workspace",
    }


def update_code_command(args: argparse.Namespace) -> int:
    print(json.dumps(update_code(root_path(args.root)), ensure_ascii=False, indent=2))
    return 0


def _sync_command(args: argparse.Namespace, collaboration_finish: dict | None = None) -> int:
    root = root_path(args.root)
    analytics_id = str(load_roles(root)["analytics"])
    analytics = root / analytics_id
    branch = run("git", "-C", str(analytics), "symbolic-ref", "--quiet", "--short", "HEAD")
    current = branch.stdout.strip() if branch.returncode == 0 else None
    if current != "main":
        print(json.dumps({
            "status": "blocked",
            "reason": "analytics-main-required-for-repository-sync",
            "current_branch": current,
            "collaboration_finish": collaboration_finish,
            "code_update": {"status": "not-started"},
            "allowed_next_action": (
                "python3 scripts/collaboration.py update"
                if current and current.startswith("feature/")
                else "перейти в осознанно выбранную ветку без потери текущей работы"
            ),
            "message": (
                "Полный обмен репозиториев выполняется только из analytics/main. "
                "Код, source и analytics не изменены."
            ),
        }, ensure_ascii=False, indent=2))
        return 2
    code_result = update_code(root)
    source_id = load_roles(root)["source"]
    source_path = source_mirror_path(root, source_id) if source_id else None
    if source_path and source_path.exists() and not is_bare_repository(source_path):
        raise ValueError(
            f"Путь роли source ({source_id}) существует, но не является bare-репозиторием: {source_path}"
        )
    source_ready = bool(source_path and source_path.exists())
    sync_mode = "source-analytics" if source_ready else "analytics-only"
    command = [
        sys.executable,
        str(Path(__file__).with_name("repository-exchange.py")),
        "--root",
        str(root),
        "sync" if source_ready else "sync-analytics-only",
    ]
    if args.no_push:
        command.append("--no-push")
    exchange = run(*command)
    if exchange.returncode != 0:
        exchange_error = exchange.stdout.strip() or exchange.stderr.strip()
        source_conflict = "source-analytics-merge-conflict" in exchange_error
        analytics_origin_conflict = any(
            reason in exchange_error
            for reason in (
                "analytics-origin-merge-conflict",
                "analytics-origin-merge-in-progress",
            )
        )
        if analytics_origin_conflict:
            allowed_next_action = "inspect-analytics-origin-conflict"
            next_command = "python3 scripts/workspace.py inspect-analytics-origin-conflict"
            forbidden_alternatives = [
                "git-reset",
                "git-rebase",
                "force-push",
                "discard-local-history",
                "discard-remote-history",
                "git-add-all",
            ]
        elif source_conflict:
            allowed_next_action = "inspect-source-analytics-conflict"
            next_command = "python3 scripts/workspace.py inspect-source-analytics-conflict"
            forbidden_alternatives = [
                "repeat-code-update-as-fallback",
                "skip-source-merge",
                "overwrite-analytics-from-source",
            ]
        else:
            allowed_next_action = "review-reported-error"
            next_command = None
            forbidden_alternatives = (
                ["repeat-code-update-as-fallback", "skip-source-merge", "overwrite-analytics-from-source"]
                if source_ready
                else ["recreate-source-without-user-request", "claim-reverse-diff-verification"]
            )
        print(json.dumps({
            "status": "blocked",
            "sync_mode": sync_mode,
            "collaboration_finish": collaboration_finish,
            "code_update": code_result,
            "analytics_exchange": exchange_error,
            "allowed_next_action": allowed_next_action,
            "next_command": next_command,
            "forbidden_alternatives": forbidden_alternatives,
        }, ensure_ascii=False, indent=2))
        return exchange.returncode
    try:
        exchange_result = json.loads(exchange.stdout)
    except json.JSONDecodeError:
        exchange_result = {"output": exchange.stdout.strip()}
    source_analytics_state = exchange_result.get("source_analytics_state", "unknown")
    status = {
        "identical": "fully-synchronized",
        "reverse-diff-pending": "analytics-synchronized-reverse-diff-pending",
        "source-unavailable": "analytics-synchronized-source-unavailable",
    }.get(source_analytics_state, "synchronized-state-unknown")
    print(json.dumps({
        "status": status,
        "sync_mode": sync_mode,
        "collaboration_finish": collaboration_finish,
        "code_update": code_result,
        "analytics_exchange": exchange_result,
        "analytics_origin_update": exchange_result.get("analytics_origin_update"),
        "source_analytics_state": source_analytics_state,
        "repositories_identical": (
            exchange_result.get("reverse_diff", {}).get("repositories_identical")
            if isinstance(exchange_result.get("reverse_diff"), dict)
            else None
        ),
        "all_repositories_synchronized": exchange_result.get(
            "all_repositories_synchronized",
            False,
        ),
        "report_message": exchange_result.get(
            "report_message",
            "Состояние обмена не определено; полную синхронизацию подтверждать запрещено.",
        ),
        "next_action": exchange_result.get("next_action"),
        "forbidden_claims": exchange_result.get(
            "forbidden_claims",
            ["all-repositories-synchronized"],
        ),
    }, ensure_ascii=False, indent=2))
    return 0


def finish_accepted_feature_before_sync(root: Path) -> tuple[dict | None, dict | None]:
    analytics_id = str(load_roles(root)["analytics"])
    analytics = root / analytics_id
    branch = run("git", "-C", str(analytics), "symbolic-ref", "--quiet", "--short", "HEAD")
    current = branch.stdout.strip() if branch.returncode == 0 else None
    if not current or not current.startswith("feature/"):
        return None, None
    collaboration_path = root / ".workspace-state/collaboration.json"
    if not collaboration_path.is_file():
        return None, None
    try:
        state = json.loads(collaboration_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать состояние совместной работы: {exc}") from exc
    work = state.get("active_work")
    if (
        not isinstance(work, dict)
        or work.get("branch") != current
        or work.get("status") != "awaiting-merge"
    ):
        return None, None
    finished = run(
        sys.executable,
        str(Path(__file__).with_name("collaboration.py")),
        "--root",
        str(root),
        "finish",
    )
    output = finished.stdout.strip() or finished.stderr.strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        payload = {"status": "error", "output": output}
    if finished.returncode == 0:
        return payload, None
    if finished.returncode == 2:
        return None, {
            "status": "blocked",
            "reason": "submitted-feature-not-contained-in-origin-main",
            "current_branch": current,
            "collaboration_finish": payload,
            "code_update": {"status": "not-started"},
            "source_update": {"status": "not-started"},
            "message": (
                "origin/main ещё не содержит отправленный коммит рабочей ветки. "
                "Полный обмен не начат; code и source не обновлялись."
            ),
            "allowed_next_action": "создать или принять запрос на слияние, затем повторить синкани репы",
        }
    raise ValueError(f"Не удалось завершить принятую рабочую ветку перед обменом: {output}")


def sync_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    collaboration_finish, blocked = finish_accepted_feature_before_sync(root)
    if blocked is not None:
        print(json.dumps(blocked, ensure_ascii=False, indent=2))
        return 2
    with workspace_operation_lock(root):
        return _sync_command(args, collaboration_finish)


def inspect_conflict_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    inspected = run(
        sys.executable,
        str(Path(__file__).with_name("repository-exchange.py")),
        "--root",
        str(root),
        "inspect-source-analytics-conflict",
    )
    output = inspected.stdout.strip() or inspected.stderr.strip()
    if output:
        print(output)
    return inspected.returncode


def inspect_analytics_origin_conflict_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    inspected = run(
        sys.executable,
        str(Path(__file__).with_name("repository-exchange.py")),
        "--root",
        str(root),
        "inspect-analytics-origin-conflict",
    )
    output = inspected.stdout.strip() or inspected.stderr.strip()
    if output:
        print(output)
    return inspected.returncode


def repository_exchange_passthrough(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    command = [
        sys.executable,
        str(Path(__file__).with_name("repository-exchange.py")),
        "--root",
        str(root),
        args.exchange_command,
    ]
    for option in ("snapshot", "side", "path"):
        value = getattr(args, option, None)
        if value is not None:
            command.extend((f"--{option}", value))
    result = run(*command)
    output = result.stdout.strip() or result.stderr.strip()
    if output:
        print(output)
    return result.returncode


def project_root_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    require_clean_harness_boundary(root)
    analytics_id = str(load_roles(root)["analytics"])
    project = root / analytics_id
    if git_root(project) != project:
        raise ValueError("Репозиторий роли analytics не развёрнут; сначала выполни bootstrap")
    print(project)
    return 0


def configure_roles_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    roles = validate_roles({
        "analytics": args.analytics,
        "code": None if args.code == "none" else args.code,
        "source": None if args.source == "none" else args.source,
    })
    path = role_config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "configured_at": utc_now(),
            "roles": roles,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "configured", "roles": roles}, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Рабочая область аналитика АС КОДА")
    result.add_argument("--root", help="Корень coda-analyst-harness; обычно определяется автоматически")
    commands = result.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.set_defaults(handler=bootstrap_command)
    configure_roles = commands.add_parser("configure-roles")
    configure_roles.add_argument("--analytics", choices=REPOSITORY_IDS, required=True)
    configure_roles.add_argument("--code", choices=(*REPOSITORY_IDS, "none"), required=True)
    configure_roles.add_argument("--source", choices=(*REPOSITORY_IDS, "none"), required=True)
    configure_roles.set_defaults(handler=configure_roles_command)
    project_root_parser = commands.add_parser("project-root")
    project_root_parser.set_defaults(handler=project_root_command)
    status = commands.add_parser("status")
    status.set_defaults(handler=status_command)
    update_code_parser = commands.add_parser("update-code")
    update_code_parser.set_defaults(handler=update_code_command)
    sync = commands.add_parser("sync")
    sync.add_argument("--no-push", action="store_true")
    sync.set_defaults(handler=sync_command)
    inspect_conflict = commands.add_parser("inspect-source-analytics-conflict")
    inspect_conflict.set_defaults(handler=inspect_conflict_command)
    inspect_analytics_origin = commands.add_parser("inspect-analytics-origin-conflict")
    inspect_analytics_origin.set_defaults(handler=inspect_analytics_origin_conflict_command)
    snapshots = commands.add_parser("list-analytics-snapshots")
    snapshots.set_defaults(
        handler=repository_exchange_passthrough,
        exchange_command="list-analytics-snapshots",
    )
    inspect_snapshot = commands.add_parser("inspect-analytics-snapshot")
    inspect_snapshot.add_argument("--snapshot", required=True)
    inspect_snapshot.set_defaults(
        handler=repository_exchange_passthrough,
        exchange_command="inspect-analytics-snapshot",
    )
    restore_snapshot = commands.add_parser("restore-analytics-snapshot-file")
    restore_snapshot.add_argument("--snapshot", required=True)
    restore_snapshot.add_argument("--side", choices=("base", "local", "incoming"), required=True)
    restore_snapshot.add_argument("--path", required=True)
    restore_snapshot.set_defaults(
        handler=repository_exchange_passthrough,
        exchange_command="restore-analytics-snapshot-file",
    )
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
