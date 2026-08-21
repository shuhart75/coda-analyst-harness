#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import re
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


BRANCH = "main"
STATE_FILE = "collaboration.json"
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


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


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)


def git(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run("git", "-C", str(repository), *args)


def state_path(root: Path) -> Path:
    return root / ".workspace-state" / STATE_FILE


def workspace_state(root: Path) -> dict:
    path = root / ".workspace-state/workspace.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать состояние рабочей области {path}: {exc}") from exc
    return payload


def analytics_repository(root: Path) -> tuple[Path, str]:
    role = workspace_state(root).get("roles", {}).get("analytics", {})
    repository_id = role.get("repository")
    path = role.get("path")
    if not repository_id or not path:
        raise ValueError("Роль analytics не настроена")
    repository = Path(path).resolve()
    top = git(repository, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != repository:
        raise ValueError(f"Роль analytics не является отдельным Git-репозиторием: {repository}")
    return repository, repository_id


def load_state(root: Path, *, required: bool = True) -> dict | None:
    path = state_path(root)
    if not path.is_file():
        if required:
            raise ValueError(
                "Совместная работа ещё не настроена. Это незавершённая одноразовая миграция, "
                "а не разрешение работать в analytics/main; сначала выполни migrate"
            )
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать настройку совместной работы {path}: {exc}") from exc
    if payload.get("schema_version") != 1 or payload.get("mode") != "multi-user-branches":
        raise ValueError(f"Повреждена настройка совместной работы: {path}")
    return payload


def write_state(root: Path, payload: dict) -> None:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def current_branch(repository: Path) -> str:
    result = git(repository, "symbolic-ref", "--quiet", "--short", "HEAD")
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError("Роль analytics находится вне именованной ветки")
    return result.stdout.strip()


def head(repository: Path, revision: str = "HEAD") -> str:
    result = git(repository, "rev-parse", revision)
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError(f"Не удалось определить коммит {revision}")
    return result.stdout.strip()


def active_merge(repository: Path) -> bool:
    return git(repository, "rev-parse", "--verify", "MERGE_HEAD").returncode == 0


def fetch_main(repository: Path) -> str:
    fetched = git(repository, "fetch", "origin", BRANCH)
    if fetched.returncode != 0:
        raise ValueError(f"Не удалось получить origin/{BRANCH}: {fetched.stderr.strip()}")
    return head(repository, f"origin/{BRANCH}")


def is_ancestor(repository: Path, older: str, newer: str) -> bool:
    return git(repository, "merge-base", "--is-ancestor", older, newer).returncode == 0


def branch_relation(repository: Path, local: str, remote: str) -> str:
    if local == remote:
        return "current"
    if is_ancestor(repository, local, remote):
        return "behind"
    if is_ancestor(repository, remote, local):
        return "ahead"
    return "diverged"


def exact_path(value: str) -> str:
    parsed = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or parsed.is_absolute()
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or parsed.as_posix() != value
    ):
        raise ValueError(f"Недопустимый точный путь: {value!r}")
    return value


def nul_paths(result: subprocess.CompletedProcess[bytes], label: str) -> set[str]:
    if result.returncode != 0:
        raise ValueError(f"Не удалось определить {label}: {result.stderr.decode(errors='replace').strip()}")
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    }


def changed_paths(repository: Path) -> set[str]:
    tracked = subprocess.run(
        ("git", "-C", str(repository), "diff", "--name-only", "-z", "HEAD", "--", "."),
        capture_output=True,
        check=False,
    )
    untracked = subprocess.run(
        ("git", "-C", str(repository), "ls-files", "--others", "--exclude-standard", "-z"),
        capture_output=True,
        check=False,
    )
    return nul_paths(tracked, "изменённые отслеживаемые пути") | nul_paths(untracked, "новые пути")


def require_clean(repository: Path) -> None:
    paths = changed_paths(repository)
    if paths:
        raise ValueError(
            "Рабочая ветка содержит несохранённые изменения: " + ", ".join(sorted(paths))
        )


def validate_slug(value: str, label: str) -> str:
    if not SLUG_PATTERN.fullmatch(value):
        raise ValueError(f"{label} должен состоять из строчных латинских букв, цифр и дефисов")
    return value


def feature_branch(feature: str, analyst: str) -> str:
    return f"feature/{validate_slug(feature, 'Идентификатор функциональности')}/{validate_slug(analyst, 'Идентификатор аналитика')}"


def require_feature(repository: Path, feature: str) -> None:
    if not (repository / "features" / feature).is_dir():
        raise ValueError(f"Функциональность не найдена в analytics: features/{feature}")


def new_state(analyst: str) -> dict:
    return {
        "schema_version": 1,
        "mode": "multi-user-branches",
        "analyst_id": analyst,
        "configured_at": utc_now(),
        "active_work": None,
        "completed_work": [],
    }


def run_exchange(root: Path, command: str) -> dict:
    result = run(
        sys.executable,
        str(Path(__file__).with_name("repository-exchange.py")),
        "--root",
        str(root),
        command,
    )
    output = result.stdout.strip() or result.stderr.strip()
    if result.returncode != 0:
        raise ValueError(output)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Команда обмена вернула некорректный результат: {output}") from exc


def migrate_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    analytics, analytics_id = analytics_repository(root)
    analyst = validate_slug(args.analyst, "Идентификатор аналитика")
    if load_state(root, required=False):
        raise ValueError("Многопользовательский режим уже настроен")
    if active_merge(analytics):
        raise ValueError(
            "В analytics уже выполняется слияние. Сначала вызови inspect-analytics-origin-conflict, "
            "разреши каждый путь и заверши либо осознанно отмени именно это слияние"
        )
    branch = current_branch(analytics)
    remote = fetch_main(analytics)
    local = head(analytics)
    relation = branch_relation(analytics, local, remote) if branch == BRANCH else "non-main"
    dirty = sorted(changed_paths(analytics))
    needs_preservation_branch = branch != BRANCH or bool(dirty) or relation in {"ahead", "diverged"}
    if needs_preservation_branch and not args.feature:
        print(json.dumps({
            "status": "feature-required",
            "analytics_repository": analytics_id,
            "current_branch": branch,
            "main_relation": relation,
            "dirty_paths": dirty,
            "message": (
                "Обнаружена отдельная локальная работа. Укажи одну функциональность, "
                "к которой относятся эти изменения; ничего не перенесено и не изменено."
            ),
        }, ensure_ascii=False, indent=2))
        return 2

    state = new_state(analyst)
    if needs_preservation_branch:
        feature = validate_slug(args.feature, "Идентификатор функциональности")
        require_feature(analytics, feature)
        target = feature_branch(feature, analyst)
        if branch != target:
            local_exists = git(analytics, "show-ref", "--verify", "--quiet", f"refs/heads/{target}").returncode == 0
            remote_exists = git(analytics, "ls-remote", "--exit-code", "--heads", "origin", target).returncode == 0
            if local_exists or remote_exists:
                raise ValueError(f"Рабочая ветка уже существует, автоматическое присоединение запрещено: {target}")
            switched = git(analytics, "switch", "--no-track", "-c", target)
            if switched.returncode != 0:
                raise ValueError(f"Не удалось сохранить работу в ветке {target}: {switched.stderr.strip()}")
        state["active_work"] = {
            "feature": feature,
            "branch": target,
            "status": "active",
            "started_at": utc_now(),
            "started_from": local,
            "origin_main_at_start": remote,
            "migration": True,
        }
        write_state(root, state)
        print(json.dumps({
            "status": "migrated-work-preserved",
            "analytics_repository": analytics_id,
            "analyst_id": analyst,
            "previous_branch": branch,
            "branch": target,
            "main_relation": relation,
            "dirty_paths": dirty,
            "automatic_commit_created": False,
            "automatic_push_performed": False,
            "next_action": "проверить и сохранить работу, затем обновить рабочую ветку",
        }, ensure_ascii=False, indent=2))
        return 0

    main_update = run_exchange(root, "fast-forward-analytics-main") if relation == "behind" else None
    write_state(root, state)
    print(json.dumps({
        "status": "migrated-clean",
        "analytics_repository": analytics_id,
        "analyst_id": analyst,
        "branch": BRANCH,
        "main_relation_before": relation,
        "main_update": main_update,
        "next_action": "начать работу над функциональностью отдельной командой",
    }, ensure_ascii=False, indent=2))
    return 0


def start_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    analytics, analytics_id = analytics_repository(root)
    state = load_state(root)
    if state.get("active_work"):
        raise ValueError(f"Уже есть активная работа: {state['active_work']['branch']}")
    if active_merge(analytics):
        raise ValueError("В analytics выполняется незавершённое слияние")
    require_clean(analytics)
    feature = validate_slug(args.feature, "Идентификатор функциональности")
    require_feature(analytics, feature)
    remote = fetch_main(analytics)
    target = feature_branch(feature, state["analyst_id"])
    if git(analytics, "show-ref", "--verify", "--quiet", f"refs/heads/{target}").returncode == 0:
        raise ValueError(f"Локальная рабочая ветка уже существует: {target}")
    if git(analytics, "ls-remote", "--exit-code", "--heads", "origin", target).returncode == 0:
        raise ValueError(f"Удалённая рабочая ветка уже существует: {target}")
    switched = git(analytics, "switch", "--no-track", "-c", target, remote)
    if switched.returncode != 0:
        raise ValueError(f"Не удалось создать рабочую ветку {target}: {switched.stderr.strip()}")
    state["active_work"] = {
        "feature": feature,
        "branch": target,
        "status": "active",
        "started_at": utc_now(),
        "started_from": remote,
        "origin_main_at_start": remote,
        "migration": False,
    }
    write_state(root, state)
    print(json.dumps({
        "status": "feature-work-started",
        "analytics_repository": analytics_id,
        "feature": feature,
        "branch": target,
        "started_from": remote,
        "next_action": f"работать только с features/{feature}/requirements.md до явной передачи",
    }, ensure_ascii=False, indent=2))
    return 0


def require_active_work(root: Path, analytics: Path, state: dict) -> dict:
    work = state.get("active_work")
    if not isinstance(work, dict):
        raise ValueError("Активная работа над функциональностью не зарегистрирована")
    branch = current_branch(analytics)
    if branch != work.get("branch"):
        raise ValueError(
            f"Текущая ветка {branch} не совпадает с зарегистрированной {work.get('branch')}"
        )
    return work


def save_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    analytics, analytics_id = analytics_repository(root)
    state = load_state(root)
    work = require_active_work(root, analytics, state)
    if active_merge(analytics):
        raise ValueError("Нельзя сохранять работу до завершения текущего слияния")
    actual = changed_paths(analytics)
    requested = {exact_path(item) for item in args.path}
    if not actual:
        print(json.dumps({
            "status": "nothing-to-save",
            "branch": work["branch"],
        }, ensure_ascii=False, indent=2))
        return 0
    if requested != actual:
        raise ValueError(json.dumps({
            "status": "blocked",
            "reason": "exact-path-set-mismatch",
            "actual_paths": sorted(actual),
            "requested_paths": sorted(requested),
            "message": "Перед сохранением нужно осознанно перечислить все и только изменённые пути",
        }, ensure_ascii=False))
    for path in sorted(requested):
        staged = git(analytics, "add", "--", path)
        if staged.returncode != 0:
            raise ValueError(f"Не удалось проиндексировать точный путь {path}: {staged.stderr.strip()}")
    checked = git(analytics, "diff", "--cached", "--check")
    if checked.returncode != 0:
        raise ValueError(f"Проверка индексированных изменений завершилась ошибкой: {checked.stdout.strip()}")
    committed = git(analytics, "commit", "-m", args.message)
    if committed.returncode != 0:
        raise ValueError(f"Не удалось создать коммит: {committed.stderr.strip()}")
    commit = head(analytics)
    pushed = git(analytics, "push", "--set-upstream", "origin", f"HEAD:{work['branch']}")
    if pushed.returncode != 0:
        raise ValueError(
            f"Коммит {commit} сохранён локально, но рабочую ветку не удалось отправить: {pushed.stderr.strip()}"
        )
    work["last_saved_at"] = utc_now()
    work["last_saved_commit"] = commit
    state["active_work"] = work
    write_state(root, state)
    print(json.dumps({
        "status": "feature-work-saved",
        "analytics_repository": analytics_id,
        "feature": work["feature"],
        "branch": work["branch"],
        "commit": commit,
        "paths": sorted(requested),
        "pushed": True,
    }, ensure_ascii=False, indent=2))
    return 0


def update_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    analytics, _ = analytics_repository(root)
    state = load_state(root)
    work = require_active_work(root, analytics, state)
    require_clean(analytics)
    updated = run_exchange(root, "update-feature-branch")
    pushed = git(analytics, "push", "--set-upstream", "origin", f"HEAD:{work['branch']}")
    if pushed.returncode != 0:
        raise ValueError(f"Рабочую ветку не удалось отправить после обновления: {pushed.stderr.strip()}")
    work["last_updated_at"] = utc_now()
    work["last_updated_commit"] = head(analytics)
    state["active_work"] = work
    write_state(root, state)
    print(json.dumps({
        "status": "feature-work-updated",
        "feature": work["feature"],
        "branch": work["branch"],
        "update": updated,
        "pushed": True,
    }, ensure_ascii=False, indent=2))
    return 0


def submit_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    analytics, _ = analytics_repository(root)
    state = load_state(root)
    work = require_active_work(root, analytics, state)
    require_clean(analytics)
    remote = fetch_main(analytics)
    local = head(analytics)
    if not is_ancestor(analytics, remote, local):
        raise ValueError("Рабочая ветка отстаёт от origin/main; сначала обнови рабочую ветку")
    pushed = git(analytics, "push", "--set-upstream", "origin", f"HEAD:{work['branch']}")
    if pushed.returncode != 0:
        raise ValueError(f"Не удалось отправить рабочую ветку: {pushed.stderr.strip()}")
    work["status"] = "awaiting-merge"
    work["submitted_at"] = utc_now()
    work["submitted_commit"] = local
    state["active_work"] = work
    write_state(root, state)
    print(json.dumps({
        "status": "feature-work-awaiting-merge",
        "feature": work["feature"],
        "branch": work["branch"],
        "commit": local,
        "message": "Рабочая ветка отправлена; требуется принять её в documents/main через запрос на слияние",
        "package_created": False,
    }, ensure_ascii=False, indent=2))
    return 0


def finish_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    analytics, _ = analytics_repository(root)
    state = load_state(root)
    work = state.get("active_work")
    if not isinstance(work, dict):
        raise ValueError("Активная работа над функциональностью не зарегистрирована")
    branch = current_branch(analytics)
    if branch not in {work.get("branch"), BRANCH}:
        raise ValueError(
            f"Текущая ветка {branch} не совпадает с рабочей {work.get('branch')} или main"
        )
    require_clean(analytics)
    remote = fetch_main(analytics)
    work_commit = head(analytics, str(work["branch"]))
    if not is_ancestor(analytics, work_commit, remote):
        print(json.dumps({
            "status": "blocked",
            "reason": "feature-branch-not-merged",
            "feature": work["feature"],
            "branch": work["branch"],
            "work_commit": work_commit,
            "origin_main": remote,
            "message": "Рабочая ветка ещё не принята в documents/main; пакет создавать нельзя",
        }, ensure_ascii=False, indent=2))
        return 2
    if branch != BRANCH:
        switched = git(analytics, "switch", BRANCH)
        if switched.returncode != 0:
            raise ValueError(f"Не удалось перейти на main после принятия ветки: {switched.stderr.strip()}")
    main_update = run_exchange(root, "fast-forward-analytics-main")
    completed = {
        **work,
        "status": "merged",
        "merged_at": utc_now(),
        "origin_main": head(analytics),
    }
    state["completed_work"] = [*state.get("completed_work", []), completed][-50:]
    state["active_work"] = None
    write_state(root, state)
    print(json.dumps({
        "status": "feature-work-finished",
        "feature": work["feature"],
        "branch": work["branch"],
        "current_branch": BRANCH,
        "main_update": main_update,
        "next_action": "разрешено сформировать пакет для разработки из актуальной main",
    }, ensure_ascii=False, indent=2))
    return 0


def require_main_for_delivery_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    analytics, _ = analytics_repository(root)
    state = load_state(root, required=False)
    if state is None:
        print(json.dumps({
            "status": "blocked",
            "reason": "collaboration-migration-required",
            "delivery_allowed": False,
            "message": (
                "Совместная работа ещё не настроена. Сначала выполни одноразовую миграцию; "
                "пакет и производные материалы создавать нельзя."
            ),
        }, ensure_ascii=False, indent=2))
        return 2
    if state.get("active_work"):
        raise ValueError(
            f"Есть незавершённая рабочая ветка {state['active_work']['branch']}; "
            "сначала она должна быть принята и завершена"
        )
    require_clean(analytics)
    if current_branch(analytics) != BRANCH:
        raise ValueError("Пакет для разработки разрешено формировать только из main")
    remote = fetch_main(analytics)
    local = head(analytics)
    if local != remote:
        raise ValueError("Локальная main не совпадает с origin/main; сначала обнови main")
    feature = validate_slug(args.feature, "Идентификатор функциональности")
    require_feature(analytics, feature)
    print(json.dumps({
        "status": "delivery-precondition-passed",
        "delivery_allowed": True,
        "feature": feature,
        "branch": BRANCH,
        "commit": local,
    }, ensure_ascii=False, indent=2))
    return 0


def status_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    analytics, analytics_id = analytics_repository(root)
    state = load_state(root, required=False)
    configured = state is not None
    print(json.dumps({
        "status": "configured" if configured else "migration-required",
        "analytics_repository": analytics_id,
        "current_branch": current_branch(analytics),
        "dirty_paths": sorted(changed_paths(analytics)),
        "collaboration": state,
        "feature_work_allowed": configured,
        "required_next_action": None if configured else "запросить идентификатор аналитика и выполнить migrate",
    }, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Совместная работа аналитиков в documents")
    result.add_argument("--root", help="Корень coda-analyst-harness")
    commands = result.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--analyst", required=True)
    migrate.add_argument("--feature")
    migrate.set_defaults(handler=migrate_command)
    start = commands.add_parser("start")
    start.add_argument("--feature", required=True)
    start.set_defaults(handler=start_command)
    save = commands.add_parser("save")
    save.add_argument("--message", required=True)
    save.add_argument("--path", action="append", default=[], required=True)
    save.set_defaults(handler=save_command)
    update = commands.add_parser("update")
    update.set_defaults(handler=update_command)
    submit = commands.add_parser("submit")
    submit.set_defaults(handler=submit_command)
    finish = commands.add_parser("finish")
    finish.set_defaults(handler=finish_command)
    delivery = commands.add_parser("require-main-for-delivery")
    delivery.add_argument("--feature", required=True)
    delivery.set_defaults(handler=require_main_for_delivery_command)
    status = commands.add_parser("status")
    status.set_defaults(handler=status_command)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        if args.command == "status":
            return args.handler(args)
        with workspace_operation_lock(root_path(args.root)):
            return args.handler(args)
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
