#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
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


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def root_path(explicit: str | None) -> Path:
    return Path(explicit).expanduser().resolve() if explicit else Path(__file__).resolve().parents[1]


def repository_urls() -> dict[str, str]:
    return {
        name: os.environ.get(ENVIRONMENT_URLS[name], default)
        for name, default in DEFAULT_REPOSITORIES.items()
    }


def role_config_path(root: Path) -> Path:
    return root / ".workspace-state" / ROLE_CONFIG_NAME


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
        "DISABLED_BY_CODA_ANALYST_HARNESS",
    )
    if result.returncode != 0:
        raise ValueError(f"Не удалось запретить push для {name}: {result.stderr.strip()}")


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
        "schema_version": 2,
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
    ensure_local_state()
    repositories: dict[str, Path] = {}
    analytics_id = str(roles["analytics"])
    repositories[analytics_id] = clone_or_validate(root, analytics_id, urls[analytics_id])
    enable_push(repositories[analytics_id], analytics_id, urls[analytics_id])
    code_id = roles["code"]
    if code_id:
        repositories[code_id] = clone_or_validate(root, code_id, urls[code_id])
        disable_push(repositories[code_id], code_id)
    source_id = roles["source"]
    retired_source = None
    if source_id:
        repositories[source_id] = clone_or_validate_source_mirror(root, source_id, urls[source_id])
        retired_source = retire_source_worktree(root, source_id, urls[source_id])
    analytics = repositories[analytics_id]
    code = repositories[code_id] if code_id else None
    legacy_harness = embedded_harness_paths(analytics)
    entrypoint = None if legacy_harness else write_local_entrypoint(analytics, root, code)
    code_registry = write_code_registry(root, analytics, analytics_id, code, code_id, urls)
    workspace = write_workspace(root, analytics, code)
    state_dir = root / ".workspace-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": 3,
        "prepared_at": utc_now(),
        "workspace_root": str(root),
        "repositories": {
            name: {
                "path": str(repositories[name]),
                "remote_url": urls[name],
                "storage": "bare-mirror" if name == source_id else "worktree",
            }
            for name in repositories
        },
        "roles": {
            role: {
                "repository": repository_id,
                "path": str(repositories[repository_id]) if repository_id else None,
            }
            for role, repository_id in roles.items()
        },
        "project_root": str(analytics),
        "local_entrypoint": str(entrypoint) if entrypoint else None,
        "code_registry": str(code_registry),
        "migration": {
            "required": bool(legacy_harness),
            "embedded_harness_paths": legacy_harness,
            "next_command": "repository-exchange.py sync" if legacy_harness else None,
        },
        "write_policy": {
            "analytics": "read-write-push-allowed",
            "code": "read-only" if code_id else "disabled",
            "source": "bare-mirror-fetch-only" if source_id else "disabled",
        },
        "retired_legacy_source": str(retired_source) if retired_source else None,
    }
    state_path = state_dir / "workspace.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "ready", "workspace": str(workspace), **state}, ensure_ascii=False, indent=2))
    return 0


def status_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    roles = load_roles(root)
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
        state = "ready" if valid else "missing"
        if state != "ready":
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
    return 0 if status == "ready" else 1


def update_code_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    code_id = load_roles(root)["code"]
    if not code_id:
        raise ValueError("Роль code отключена")
    code = root / code_id
    if git_root(code) != code:
        raise ValueError(f"Репозиторий роли code ({code_id}) не развёрнут; сначала выполни bootstrap")
    dirty = run("git", "-C", str(code), "status", "--porcelain=v1")
    if dirty.stdout:
        raise ValueError(f"Репозиторий роли code ({code_id}) содержит локальные изменения; автоматическое обновление остановлено")
    branch = run("git", "-C", str(code), "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch.returncode != 0:
        raise ValueError(f"Репозиторий роли code ({code_id}) находится в detached HEAD; автоматическое обновление остановлено")
    name = branch.stdout.strip()
    fetch = run("git", "-C", str(code), "fetch", "origin", name)
    if fetch.returncode != 0:
        raise ValueError(f"Не удалось получить изменения роли code ({code_id}): {fetch.stderr.strip()}")
    merge = run("git", "-C", str(code), "merge", "--ff-only", f"origin/{name}")
    if merge.returncode != 0:
        raise ValueError(f"Репозиторий роли code ({code_id}) нельзя обновить fast-forward: {merge.stderr.strip()}")
    print(json.dumps({"status": "updated", "role": "code", "repository": code_id, "branch": name}, ensure_ascii=False, indent=2))
    return 0


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
    update_code = commands.add_parser("update-code")
    update_code.set_defaults(handler=update_code_command)
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
