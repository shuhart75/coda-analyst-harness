from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile


PENDING = "worktree-recovery-pending"


def require_idle(repository: Path, api) -> None:
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-apply", "rebase-merge", "sequencer"):
        result = api.git(repository, "rev-parse", "--git-path", marker)
        if result.returncode:
            raise ValueError("Не удалось проверить незавершённые Git-операции")
        path = Path(result.stdout.strip())
        if (path if path.is_absolute() else repository / path).exists():
            raise ValueError("В analytics есть незавершённая Git-операция")


def checked_bytes(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments], capture_output=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace"))
    return result.stdout


def capture(repository: Path, expected: dict[str, str], api) -> dict:
    require_idle(repository, api)
    if checked_bytes(repository, "diff", "--cached", "--name-only", "-z"):
        raise ValueError("Индекс должен быть пустым; подготовленные к коммиту изменения не переносятся")
    if api.changed_paths(repository) != set(expected):
        raise ValueError("Список изменённых файлов не совпадает с подтверждённым; повтори диагностику")
    tracked = set(checked_bytes(repository, "ls-files", "-z").decode().split("\0"))
    files = {}
    for relative, checksum in sorted(expected.items()):
        api.exact_path(relative)
        if relative not in tracked:
            raise ValueError(f"Перенос поддерживает только отслеживаемые файлы: {relative}")
        path = repository / relative
        if path.resolve() != path or not path.is_file():
            raise ValueError(f"Удалённый файл или символическая ссылка не поддерживается: {relative}")
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Необычный тип файла: {relative}")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != checksum:
            raise ValueError(f"Содержимое изменилось после проверки: {relative}")
        files[relative] = {"sha256": checksum, "mode": stat.S_IMODE(metadata.st_mode),
                           "base64": base64.b64encode(content).decode("ascii")}
    return {
        "files": files,
        "index_entries": base64.b64encode(checked_bytes(repository, "ls-files", "--stage", "-z")).decode("ascii"),
    }


def recover_worktree(args, api) -> int:
    root = api.root_path(args.root)
    analytics, analytics_id = api.analytics_repository(root)
    state = api.load_state(root)
    feature = api.validate_slug(args.feature, "Идентификатор функциональности")
    api.require_feature(analytics, feature)
    if not args.analyst_confirmed:
        raise ValueError("Требуется явное подтверждение принадлежности всех изменений")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args.expected_head):
        raise ValueError("Укажи полный проверенный хеш HEAD")
    expected = dict(args.expected_file)
    if not expected or len(expected) != len(args.expected_file) or any(
        not re.fullmatch(r"[0-9a-f]{64}", checksum) for checksum in expected.values()
    ):
        raise ValueError("Нужен точный список файлов с SHA-256 без повторов")
    local = args.expected_head
    work = state.get("active_work")
    if work and not (
        work.get("worktree_recovery") and work.get("feature") == feature
        and work.get("started_from") == local and work.get("status") in {PENDING, "active"}
    ):
        raise ValueError("Есть другая активная работа; перенос не разрешён")
    branch = api.current_branch(analytics)
    if branch not in ({api.BRANCH, work["branch"]} if work else {api.BRANCH}):
        raise ValueError("Перенос начинается только из main")
    if api.head(analytics) != local or api.head(analytics, api.BRANCH) != local:
        raise ValueError("HEAD или main изменились после диагностики")
    before = capture(analytics, expected, api)
    if not work:
        remote = api.fetch_main(analytics)
        if api.branch_relation(analytics, local, remote) not in {"current", "behind"}:
            raise ValueError("В main есть непринятые коммиты; нужен отдельный разбор истории")
        target = api.next_feature_branch(analytics, feature, state["analyst_id"])
        if api.current_branch(analytics) != api.BRANCH or api.head(analytics) != local:
            raise ValueError("Рабочая область изменилась во время проверки")
        if capture(analytics, expected, api) != before:
            raise ValueError("Файлы или индекс изменились во время проверки")
        directory = root / ".workspace-state" / "worktree-recoveries"
        directory.mkdir(parents=True, exist_ok=True)
        snapshot_dir = Path(tempfile.mkdtemp(prefix="recovery-", dir=directory))
        snapshot_path = snapshot_dir / "snapshot.json"
        snapshot = {"schema_version": 1, "project_root": str(analytics), "head": local,
                    "feature": feature, "branch": target, "collaboration_before": state,
                    "preserved": before}
        payload = (json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with snapshot_path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        work = {
            "feature": feature, "branch": target, "status": PENDING,
            "started_at": api.utc_now(), "started_from": local,
            "origin_main_at_start": remote, "migration": False,
            "worktree_recovery": {"snapshot": str(snapshot_path),
                                  "sha256": hashlib.sha256(payload).hexdigest()},
        }
        state["active_work"] = work
        api.write_state(root, state)
    recovery = work["worktree_recovery"]
    snapshot_path = Path(recovery["snapshot"])
    if not snapshot_path.resolve().is_relative_to(root / ".workspace-state" / "worktree-recoveries"):
        raise ValueError("Путь снимка выходит за пределы локального каталога восстановления")
    payload = snapshot_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != recovery["sha256"]:
        raise ValueError("Контрольная сумма защитного снимка не совпадает")
    snapshot = json.loads(payload)
    target = work["branch"]
    if (snapshot["project_root"], snapshot["head"], snapshot["feature"], snapshot["branch"]) != (
        str(analytics), local, feature, target
    ) or snapshot["preserved"] != before:
        raise ValueError("Снимок и текущее состояние не совпадают; автоматическое восстановление запрещено")
    if branch != target:
        exists = api.git(analytics, "show-ref", "--verify", "--quiet", f"refs/heads/{target}")
        if exists.returncode == 0:
            if api.head(analytics, target) != local:
                raise ValueError("Зарегистрированная ветка изменилась")
            switched = api.git(analytics, "switch", target)
        elif exists.returncode == 1:
            switched = api.git(analytics, "switch", "--no-track", "-c", target, local)
        else:
            raise ValueError("Не удалось проверить ветку восстановления")
        if switched.returncode:
            raise ValueError(f"Перенос зарегистрирован; повтори ту же команду: {switched.stderr.strip()}")
    if (api.current_branch(analytics) != target or api.head(analytics) != local
            or api.head(analytics, api.BRANCH) != local or capture(analytics, expected, api) != before):
        raise ValueError("Проверка после переключения не пройдена; снимок сохранён, автоматического отката нет")
    work["status"] = "active"
    api.write_state(root, state)
    print(json.dumps({
        "status": "dirty-main-work-recovered", "analytics_repository": analytics_id,
        "feature": feature, "branch": target, "preserved_commit": local,
        "preserved_paths": sorted(expected), "snapshot": str(snapshot_path),
        "automatic_commit_created": False, "automatic_push_performed": False,
        "main_unchanged": True, "files_unchanged": True, "index_entries_unchanged": True,
        "next_action": "проверить и исправить изменения в их режиме; затем save, update и submit",
    }, ensure_ascii=False, indent=2))
    return 0
