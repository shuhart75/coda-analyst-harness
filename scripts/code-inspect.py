#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from workspace_paths import code_registry_path


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def inspection_state_dir(project: Path) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")).expanduser()
    key = hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:12]
    return base / "coda-analyst-harness/code-inspection" / key


def run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )


def load_registry(project: Path) -> dict:
    path = code_registry_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать {path}: {exc}") from exc
    if payload.get("schema_version") not in {2, 3} or not isinstance(payload.get("repositories"), list):
        raise ValueError("реестр кода обвязки должен соответствовать схеме 2 или 3")
    return payload


def repository_entry(registry: dict, repository_id: str) -> dict:
    for entry in registry["repositories"]:
        if isinstance(entry, dict) and entry.get("id") == repository_id:
            if entry.get("access") != "read-only":
                raise ValueError(f"Для {repository_id} не закреплён режим только для чтения")
            policy = entry.get("write_policy") or {
                "mode": "forbidden",
                "allowed_paths": [],
                "allowed_operations": [],
                "user_prompt_can_override": False,
            }
            if (
                not isinstance(policy, dict)
                or policy.get("allowed_paths") != []
                or policy.get("user_prompt_can_override") is not False
            ):
                raise ValueError(f"Для {repository_id} задана небезопасная политика записи")
            return entry
    raise ValueError(f"Репозиторий не зарегистрирован: {repository_id}")


def resolve_repository(project: Path, entry: dict) -> Path:
    location = entry.get("location")
    if not isinstance(location, dict):
        raise ValueError(f"Для {entry.get('id')} не задан location")
    environment = location.get("environment")
    value = os.environ.get(environment, "").strip() if isinstance(environment, str) else ""
    if value:
        root = Path(value).expanduser()
    else:
        relative = location.get("relative_to_analytical")
        if not isinstance(relative, str) or not relative:
            raise ValueError(f"Для {entry.get('id')} не задан относительный путь")
        root = project / relative
    return root.resolve()


def contour_root(repository: Path, entry: dict, contour: str | None) -> Path:
    if contour is None:
        return repository
    contours = entry.get("contours")
    if not isinstance(contours, dict) or contour not in contours:
        raise ValueError(f"Контур {contour} не зарегистрирован для {entry.get('id')}")
    contour_entry = contours[contour]
    if not isinstance(contour_entry, dict) or not isinstance(contour_entry.get("path"), str):
        raise ValueError(f"Для контура {contour} не задан путь")
    root = (repository / contour_entry["path"]).resolve()
    if repository not in root.parents and root != repository:
        raise ValueError(f"Путь контура выходит за границы репозитория: {contour}")
    return root


def analytical_identity(project: Path, registry: dict) -> dict:
    entry = registry.get("workspace", {}).get("analytical_repository", {})
    top = run_git(project, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != project:
        raise ValueError(f"аналитический проект не является корнем Git-репозитория: {project}")
    head = run_git(project, "rev-parse", "HEAD")
    branch = run_git(project, "symbolic-ref", "--quiet", "--short", "HEAD")
    remotes = run_git(project, "remote", "get-url", "--all", "origin")
    remote_urls = [line.strip() for line in remotes.stdout.splitlines() if line.strip()]
    accepted = entry.get("accepted_remote_urls", [])
    return {
        "repository": entry.get("id", "analytics"),
        "root": str(project),
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
        "origin_urls": remote_urls,
        "origin_matches_registry": bool(set(remote_urls) & set(accepted)) if accepted else True,
    }


def git_snapshot(repository: Path, entry: dict, contour: str | None) -> dict:
    top = run_git(repository, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise ValueError(f"Не найден Git-репозиторий: {repository}")
    actual_root = Path(top.stdout.strip()).resolve()
    if actual_root != repository:
        raise ValueError(f"Ожидался корень {repository}, Git сообщает {actual_root}")

    head = run_git(repository, "rev-parse", "HEAD")
    branch = run_git(repository, "symbolic-ref", "--quiet", "--short", "HEAD")
    status = subprocess.run(
        ["git", "-C", str(repository), "status", "--porcelain=v1", "-z"],
        capture_output=True,
        check=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    remotes = run_git(repository, "remote", "get-url", "--all", "origin")
    push_remote = run_git(repository, "remote", "get-url", "--push", "origin")
    index_path_result = run_git(repository, "rev-parse", "--git-path", "index")
    index_path = Path(index_path_result.stdout.strip()) if index_path_result.returncode == 0 else None
    if index_path is not None and not index_path.is_absolute():
        index_path = repository / index_path
    index_sha256 = hashlib.sha256(index_path.read_bytes()).hexdigest() if index_path and index_path.is_file() else None
    config_path_result = run_git(repository, "rev-parse", "--git-path", "config")
    config_path = Path(config_path_result.stdout.strip()) if config_path_result.returncode == 0 else None
    if config_path is not None and not config_path.is_absolute():
        config_path = repository / config_path
    config_sha256 = hashlib.sha256(config_path.read_bytes()).hexdigest() if config_path and config_path.is_file() else None
    remote_urls = [line.strip() for line in remotes.stdout.splitlines() if line.strip()]
    accepted = entry.get("accepted_remote_urls", [])
    remote_match = bool(set(remote_urls) & set(accepted)) if accepted else True
    entries = [item.decode("utf-8", errors="replace") for item in status.stdout.split(b"\0") if item]

    selected_root = contour_root(repository, entry, contour)
    if not selected_root.is_dir():
        raise ValueError(f"Каталог контура отсутствует: {selected_root}")
    instructions: list[str] = []
    for pattern in entry.get("instruction_patterns", []):
        if not isinstance(pattern, str):
            continue
        for path in sorted(selected_root.glob(pattern)):
            if path.is_file():
                relative = path.relative_to(repository).as_posix()
                if relative not in instructions:
                    instructions.append(relative)
                if len(instructions) >= 30:
                    break
        if len(instructions) >= 30:
            break

    expected_branch = entry.get("expected_branch")
    branch_name = branch.stdout.strip() if branch.returncode == 0 else None
    return {
        "repository": entry["id"],
        "root": str(repository),
        "contour": contour,
        "contour_root": str(selected_root),
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "branch": branch_name,
        "expected_branch": expected_branch,
        "branch_matches": expected_branch in (None, "") or branch_name == expected_branch,
        "worktree_state": "clean" if not entries else "dirty",
        "worktree_entries": entries,
        "index_sha256": index_sha256,
        "repository_config_sha256": config_sha256,
        "origin_urls": remote_urls,
        "push_url": push_remote.stdout.strip() if push_remote.returncode == 0 else None,
        "origin_matches_registry": remote_match,
        "instruction_files": instructions,
    }


def print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def status_command(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    registry = load_registry(project)
    entry = repository_entry(registry, args.repository)
    repository = resolve_repository(project, entry)
    snapshot = git_snapshot(repository, entry, args.contour)
    print_json(snapshot)
    return 0 if (
        snapshot["origin_matches_registry"]
        and snapshot["branch_matches"]
        and snapshot["worktree_state"] == "clean"
    ) else 1


def doctor_command(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    registry = load_registry(project)
    errors: list[str] = []
    warnings: list[str] = []
    reports: list[dict] = []
    try:
        analytical = analytical_identity(project, registry)
        if not analytical["origin_matches_registry"]:
            errors.append("аналитический проект: origin не совпадает с реестром")
    except ValueError as exc:
        analytical = None
        errors.append(str(exc))
    for entry in registry["repositories"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            errors.append("Некорректная запись репозитория")
            continue
        try:
            entry = repository_entry(registry, entry["id"])
            repository = resolve_repository(project, entry)
            report = git_snapshot(repository, entry, None)
            reports.append(report)
            if not report["origin_matches_registry"]:
                errors.append(f"{entry['id']}: origin не совпадает с реестром")
            if not report["branch_matches"]:
                errors.append(f"{entry['id']}: ожидалась ветка {report['expected_branch']}, найдена {report['branch']}")
            if report["worktree_state"] != "clean":
                errors.append(f"{entry['id']}: рабочее дерево изменено; исследование заблокировано до вмешательства владельца кода")
            for contour in entry.get("contours", {}):
                contour_root(repository, entry, contour)
                if not contour_root(repository, entry, contour).is_dir():
                    errors.append(f"{entry['id']}: отсутствует контур {contour}")
                    continue
                contour_report = git_snapshot(repository, entry, contour)
                if not contour_report["instruction_files"]:
                    warnings.append(f"{entry['id']}/{contour}: локальные инструкции SDD не найдены известными шаблонами")
        except ValueError as exc:
            errors.append(str(exc))
    print_json({
        "status": "error" if errors else "ok",
        "analytical": analytical,
        "repositories": reports,
        "warnings": warnings,
        "errors": errors,
    })
    return 1 if errors else 0


def begin_command(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    registry = load_registry(project)
    entry = repository_entry(registry, args.repository)
    repository = resolve_repository(project, entry)
    snapshot = git_snapshot(repository, entry, args.contour)
    if not snapshot["origin_matches_registry"]:
        raise ValueError("origin кодового репозитория не совпадает с реестром")
    if not snapshot["branch_matches"]:
        raise ValueError(f"Ожидалась ветка {snapshot['expected_branch']}, найдена {snapshot['branch']}")
    if snapshot["worktree_state"] != "clean":
        raise ValueError("Рабочее дерево роли code изменено; для исследования нужен чистый клон")
    state_dir = inspection_state_dir(project)
    state_dir.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{entry['id']}-{args.contour or 'root'}-{uuid.uuid4().hex[:8]}.json"
    path = state_dir / name
    payload = {
        "schema_version": 1,
        "kind": "code-inspection",
        "started_at": utc_now(),
        "analytical_root": str(project),
        "feature": args.feature,
        "query": args.query,
        "initial": snapshot,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)
    print_json(snapshot)
    return 0


def verify_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state).resolve()
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    project = Path(payload["analytical_root"]).resolve()
    initial = payload["initial"]
    registry = load_registry(project)
    entry = repository_entry(registry, initial["repository"])
    repository = resolve_repository(project, entry)
    current = git_snapshot(repository, entry, initial.get("contour"))
    changed = {
        key: {"before": initial.get(key), "after": current.get(key)}
        for key in (
            "head", "branch", "worktree_entries", "index_sha256",
            "repository_config_sha256", "origin_urls", "push_url",
        )
        if initial.get(key) != current.get(key)
    }
    payload["verified_at"] = utc_now()
    payload["final"] = current
    payload["result"] = "changed" if changed else "unchanged"
    payload["changes"] = changed
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print_json({"result": payload["result"], "changes": changed, "state": str(state_path)})
    return 1 if changed else 0


def locate_command(args: argparse.Namespace) -> int:
    if args.max_results < 1 or args.max_results > 500:
        raise ValueError("max-results должен быть от 1 до 500")
    project = Path(args.project).resolve()
    registry = load_registry(project)
    entry = repository_entry(registry, args.repository)
    repository = resolve_repository(project, entry)
    snapshot = git_snapshot(repository, entry, args.contour)
    if snapshot["worktree_state"] != "clean":
        raise ValueError("Рабочее дерево роли code изменено; поиск заблокирован до вмешательства владельца кода")
    search_root = Path(snapshot["contour_root"])
    command = [
        "git",
        "-C",
        str(repository),
        "grep",
        "-l",
        "-I",
        "--full-name",
        "-E" if args.regex else "-F",
        "-e",
        args.query,
        "--",
        search_root.relative_to(repository).as_posix(),
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode not in {0, 1}:
        raise ValueError(result.stderr.strip() or "Ошибка поиска по коду")
    matches = []
    for line in result.stdout.splitlines():
        path = (repository / line).resolve()
        try:
            matches.append(path.relative_to(repository).as_posix())
        except ValueError:
            continue
    matches = sorted(dict.fromkeys(matches))
    print_json({
        "repository": entry["id"],
        "head": snapshot["head"],
        "contour": args.contour,
        "query": args.query,
        "matches": matches[: args.max_results],
        "truncated": len(matches) > args.max_results,
        "total_matches": len(matches),
    })
    return 0


def setup_command(args: argparse.Namespace) -> int:
    raise ValueError(
        "рабочая область КОДА создаётся только командой "
        "python3 scripts/workspace.py bootstrap из корня coda-analyst-harness"
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Безопасное исследование соседнего кодового репозитория")
    commands = result.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor")
    doctor.add_argument("project")
    doctor.set_defaults(handler=doctor_command)

    status = commands.add_parser("status")
    status.add_argument("project")
    status.add_argument("--repository", default="code")
    status.add_argument("--contour", choices=("backend", "frontend"))
    status.set_defaults(handler=status_command)

    begin = commands.add_parser("begin")
    begin.add_argument("project")
    begin.add_argument("--repository", default="code")
    begin.add_argument("--contour", choices=("backend", "frontend"))
    begin.add_argument("--feature")
    begin.add_argument("--query")
    begin.set_defaults(handler=begin_command)

    verify = commands.add_parser("verify")
    verify.add_argument("state")
    verify.set_defaults(handler=verify_command)

    locate = commands.add_parser("locate")
    locate.add_argument("project")
    locate.add_argument("query")
    locate.add_argument("--repository", default="code")
    locate.add_argument("--contour", required=True, choices=("backend", "frontend"))
    locate.add_argument("--regex", action="store_true")
    locate.add_argument("--max-results", type=int, default=50)
    locate.set_defaults(handler=locate_command)

    setup = commands.add_parser("setup")
    setup.add_argument("project")
    setup.add_argument("--repository", default="code")
    setup.add_argument("--force", action="store_true")
    setup.set_defaults(handler=setup_command)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        return args.handler(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
