#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


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
READ_ONLY_REPOSITORIES = {"coda", "changeswork-copy"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def root_path(explicit: str | None) -> Path:
    return Path(explicit).expanduser().resolve() if explicit else Path(__file__).resolve().parents[1]


def harness_source() -> Path:
    return Path(__file__).resolve().parents[1]


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


def clone_or_validate(root: Path, name: str, url: str) -> Path:
    path = root / name
    if not path.exists():
        result = run("git", "clone", url, str(path))
        if result.returncode != 0:
            raise ValueError(f"Не удалось клонировать {name}: {result.stderr.strip()}")
    if git_root(path) != path:
        raise ValueError(f"{path} не является корнем Git-репозитория")
    remotes = run("git", "-C", str(path), "remote", "get-url", "--all", "origin")
    current = {line.strip() for line in remotes.stdout.splitlines() if line.strip()}
    if url not in current:
        raise ValueError(f"origin {name} не совпадает с ожидаемым URL: {sorted(current)}")
    if name in READ_ONLY_REPOSITORIES:
        result = run(
            "git", "-C", str(path), "config", "remote.origin.pushurl",
            "DISABLED_BY_CODA_ANALYST_HARNESS",
        )
        if result.returncode != 0:
            raise ValueError(f"Не удалось запретить push для {name}: {result.stderr.strip()}")
    return path


def write_workspace(root: Path) -> Path:
    payload = {
        "folders": [
            {"name": "documents", "path": "documents"},
            {"name": "coda-read-only", "path": "coda"},
            {"name": "changeswork-copy-pull-only", "path": "changeswork-copy"},
        ],
        "settings": {
            "files.exclude": {"**/.git": True},
            "search.exclude": {"**/.git": True, "**/node_modules": True, "**/build": True},
        },
    }
    path = root / "coda-analyst.code-workspace"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def ensure_documents_harness(root: Path, documents: Path) -> None:
    if (documents / ".workflow").is_dir() and (documents / "AGENTS.md").is_file():
        return
    result = run("bash", str(harness_source() / "scripts/scaffold-project.sh"), str(documents), "--merge")
    if result.returncode != 0:
        raise ValueError(f"Не удалось установить аналитическую обвязку в documents: {(result.stdout + result.stderr).strip()}")


def bootstrap_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    urls = repository_urls()
    repositories = {name: clone_or_validate(root, name, url) for name, url in urls.items()}
    ensure_documents_harness(root, repositories["documents"])
    workspace = write_workspace(root)
    state_dir = root / ".workspace-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": 1,
        "prepared_at": utc_now(),
        "workspace_root": str(root),
        "repositories": {
            name: {"path": str(repositories[name]), "remote_url": urls[name]}
            for name in repositories
        },
        "write_policy": {
            "documents": "push-allowed",
            "coda": "read-only",
            "changeswork-copy": "pull-only-reverse-patches",
        },
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
        path = root / name
        state = "ready" if git_root(path) == path else "missing"
        if state != "ready":
            status = "incomplete"
        items.append({"id": name, "path": str(path), "state": state, "expected_origin": url})
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
