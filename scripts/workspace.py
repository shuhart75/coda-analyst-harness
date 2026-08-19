#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from workspace_paths import ensure_local_state, retired_repositories_path, source_mirror_path


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
READ_ONLY_REPOSITORIES = {"coda"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def root_path(explicit: str | None) -> Path:
    return Path(explicit).expanduser().resolve() if explicit else Path(__file__).resolve().parents[1]


def repository_urls() -> dict[str, str]:
    return {
        name: os.environ.get(ENVIRONMENT_URLS[name], default)
        for name, default in DEFAULT_REPOSITORIES.items()
    }


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


def clone_or_validate(root: Path, name: str, url: str) -> Path:
    path = root / name
    if not path.exists():
        result = run("git", "clone", url, str(path))
        if result.returncode != 0:
            raise ValueError(f"Не удалось клонировать {name}: {result.stderr.strip()}")
    if git_root(path) != path:
        raise ValueError(f"{path} не является корнем Git-репозитория")
    validate_origin(path, name, url)
    if name in READ_ONLY_REPOSITORIES:
        disable_push(path, name)
    return path


def clone_or_validate_source_mirror(root: Path, url: str) -> Path:
    path = source_mirror_path(root)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        result = run("git", "clone", "--bare", url, str(path))
        if result.returncode != 0:
            raise ValueError(f"Не удалось создать служебное зеркало changeswork-copy: {result.stderr.strip()}")
    if not is_bare_repository(path):
        raise ValueError(f"{path} не является bare-репозиторием changeswork-copy")
    validate_origin(path, "changeswork-copy", url)
    main = run("git", "-C", str(path), "show-ref", "--verify", "--quiet", "refs/heads/main")
    if main.returncode != 0:
        raise ValueError("В changeswork-copy отсутствует ветка main")
    head = run("git", "-C", str(path), "symbolic-ref", "HEAD", "refs/heads/main")
    if head.returncode != 0:
        raise ValueError(f"Не удалось закрепить main в зеркале changeswork-copy: {head.stderr.strip()}")
    disable_push(path, "changeswork-copy")
    return path


def retire_legacy_source_checkout(root: Path, url: str) -> Path | None:
    legacy = root / "changeswork-copy"
    if not legacy.exists():
        return None
    if legacy.is_symlink() or git_root(legacy) != legacy:
        raise ValueError(
            f"Старый путь {legacy} занят не ожидаемым Git-репозиторием; "
            "обвязка не будет перемещать его автоматически"
        )
    validate_origin(legacy, "changeswork-copy", url)
    retired_root = retired_repositories_path(root)
    retired_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = retired_root / f"changeswork-copy-{stamp}"
    counter = 1
    while target.exists():
        target = retired_root / f"changeswork-copy-{stamp}-{counter}"
        counter += 1
    legacy.rename(target)
    return target


def write_workspace(root: Path) -> Path:
    payload = {
        "folders": [
            {"name": "analyst-harness", "path": "."},
            {"name": "documents", "path": "documents"},
            {"name": "coda-read-only", "path": "coda"},
        ],
        "settings": {
            "files.exclude": {
                "**/.git": True,
                ".workspace-state/repositories": True,
                ".workspace-state/retired-repositories": True,
                "changeswork-copy": True,
            },
            "search.exclude": {
                "**/.git": True,
                "**/node_modules": True,
                "**/build": True,
                ".workspace-state/repositories": True,
                ".workspace-state/retired-repositories": True,
                "changeswork-copy": True,
            },
            "files.watcherExclude": {
                "**/.workspace-state/repositories/**": True,
                "**/.workspace-state/retired-repositories/**": True,
                "**/changeswork-copy/**": True,
            },
        },
    }
    path = root / "coda-analyst.code-workspace"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def bootstrap_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    urls = repository_urls()
    ensure_local_state()
    repositories = {
        name: clone_or_validate(root, name, urls[name])
        for name in ("documents", "coda")
    }
    repositories["changeswork-copy"] = clone_or_validate_source_mirror(
        root, urls["changeswork-copy"]
    )
    retired_source = retire_legacy_source_checkout(root, urls["changeswork-copy"])
    workspace = write_workspace(root)
    state_dir = root / ".workspace-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": 2,
        "prepared_at": utc_now(),
        "workspace_root": str(root),
        "repositories": {
            name: {
                "path": str(repositories[name]),
                "remote_url": urls[name],
                "storage": "bare-mirror" if name == "changeswork-copy" else "worktree",
            }
            for name in repositories
        },
        "write_policy": {
            "documents": "push-allowed",
            "coda": "read-only",
            "changeswork-copy": "bare-mirror-no-worktree",
        },
        "retired_legacy_source": str(retired_source) if retired_source else None,
    }
    state_path = state_dir / "workspace.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "ready", "workspace": str(workspace), **state}, ensure_ascii=False, indent=2))
    return 0


def status_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    urls = repository_urls()
    status = "ready"
    items = []
    for name, url in urls.items():
        path = source_mirror_path(root) if name == "changeswork-copy" else root / name
        valid = is_bare_repository(path) if name == "changeswork-copy" else git_root(path) == path
        state = "ready" if valid else "missing"
        if state != "ready":
            status = "incomplete"
        items.append({
            "id": name,
            "path": str(path),
            "state": state,
            "storage": "bare-mirror" if name == "changeswork-copy" else "worktree",
            "expected_origin": url,
        })
    print(json.dumps({"status": status, "repositories": items}, ensure_ascii=False, indent=2))
    return 0 if status == "ready" else 1


def update_code_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    code = root / "coda"
    if git_root(code) != code:
        raise ValueError("coda не развёрнут; сначала выполни bootstrap")
    dirty = run("git", "-C", str(code), "status", "--porcelain=v1")
    if dirty.stdout:
        raise ValueError("coda содержит локальные изменения; автоматическое обновление остановлено")
    branch = run("git", "-C", str(code), "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch.returncode != 0:
        raise ValueError("coda находится в detached HEAD; автоматическое обновление остановлено")
    name = branch.stdout.strip()
    fetch = run("git", "-C", str(code), "fetch", "origin", name)
    if fetch.returncode != 0:
        raise ValueError(f"Не удалось получить изменения coda: {fetch.stderr.strip()}")
    merge = run("git", "-C", str(code), "merge", "--ff-only", f"origin/{name}")
    if merge.returncode != 0:
        raise ValueError(f"coda нельзя обновить fast-forward: {merge.stderr.strip()}")
    print(json.dumps({"status": "updated", "repository": "coda", "branch": name}, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Рабочая область аналитика АС КОДА")
    result.add_argument("--root", help="Корень coda-analyst-harness; обычно определяется автоматически")
    commands = result.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.set_defaults(handler=bootstrap_command)
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
