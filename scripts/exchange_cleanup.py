"""Publish an archive-verified removal through an isolated code review branch.

The ordinary code checkout is never opened. Completion requires preservation of
our exact review commit in the target history (squash merges are not inferred).
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from pathlib import Path

from baseline_releases import digest
from commit_message_policy import require_valid_commit_message
from workspace import install_commit_message_hook

MESSAGE = "Убрать архивированную передачу требований из рабочего каталога"
AUTHOR = "Analyst Requirements Exchange <analyst-harness@local.invalid>"


def _git(root, *args, check=True):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if check and result.returncode:
        raise ValueError("Ошибка Git при очистке передачи: " + result.stderr.decode(errors="replace").strip())
    return result


def _value(root, *args):
    return _git(root, *args).stdout.decode().strip()


def _package_hash(root, commit, package):
    """Hash Git blobs without following links or executing checkout filters."""
    for ancestor in ("requirements-exchange", package):
        row = _git(root, "ls-tree", commit, "--", ancestor).stdout
        if not row:
            return None
        if not row.startswith(b"040000 tree "):
            raise ValueError("Каталог передачи должен быть деревом Git без ссылок")
    files = {}
    for row in _git(root, "ls-tree", "-rz", commit, "--", package + "/").stdout.split(b"\0"):
        if not row:
            continue
        metadata, path = row.split(b"\t", 1)
        mode, kind, oid = metadata.split()
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise ValueError("Ссылки и вложенные репозитории в передаче запрещены")
        relative = path.decode()[len(package) + 1:]
        files[relative] = hashlib.sha256(_git(root, "cat-file", "blob", oid.decode()).stdout).hexdigest()
    if not files:
        raise ValueError("Пустой пакет передачи")
    return digest(files)


def _validate_request(root, commit, source_commit, target_commit, package, archive_hash):
    parents = _value(root, "rev-list", "--parents", "-n", "1", commit).split()
    if len(parents) != 2:
        raise ValueError("Ветка очистки должна содержать обычный коммит удаления")
    parent = parents[1]
    if _git(root, "merge-base", "--is-ancestor", source_commit, parent, check=False).returncode:
        raise ValueError("Ветка очистки не продолжает архивированный источник")
    if _git(root, "merge-base", "--is-ancestor", parent, target_commit, check=False).returncode:
        raise ValueError("Родитель коммита очистки не принят в целевую ветку")
    if _value(root, "show", "-s", "--format=%B", commit) != MESSAGE or _value(root, "show", "-s", "--format=%an <%ae>", commit) != AUTHOR:
        raise ValueError("Обнаружена сторонняя ветка очистки")
    if _package_hash(root, parent, package) != archive_hash or _package_hash(root, commit, package) is not None:
        raise ValueError("Ветка очистки не соответствует точному архиву")
    rows = _git(root, "diff-tree", "--no-commit-id", "--no-renames", "--name-status", "-r", "-z", parent, commit).stdout.split(b"\0")
    rows = rows[:-1] if rows[-1] == b"" else rows
    if not rows or len(rows) % 2 or any(rows[i] != b"D" or not rows[i + 1].startswith((package + "/").encode()) for i in range(0, len(rows), 2)):
        raise ValueError("Ветка очистки меняет файлы за границами архивированного пакета")


def publish_cleanup(project: Path, release_id: str, delivery_key: str) -> dict:
    from release_exchange import require_accepted_archive

    record = require_accepted_archive(project, release_id, delivery_key)
    source = record["source"]
    if source.get("role") != "code":
        raise ValueError("Публикация очистки предназначена только для роли code")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", delivery_key):
        raise ValueError("Недопустимый ключ передачи")
    repository = source["repository_url"]
    target = source["target_branch"]
    source_commit = source["commit"]
    if not isinstance(repository, str) or not repository or repository.startswith("-"):
        raise ValueError("Некорректный архивный адрес репозитория")
    if not isinstance(source_commit, str) or not re.fullmatch(r"[a-fA-F0-9]{40,64}", source_commit):
        raise ValueError("Требуется полный архивный commit SHA")
    valid_ref = subprocess.run(["git", "check-ref-format", "--branch", target], capture_output=True)
    if valid_ref.returncode or target.startswith("-"):
        raise ValueError("Некорректная целевая ветка архива")
    identity = {key: record[key] for key in ("release_id", "delivery_key", "archive_path", "archive_hash", "source")}
    branch = "codex/cleanup-exchange-" + digest(identity)[:24]
    ref = "refs/heads/" + branch
    package = "requirements-exchange/" + delivery_key
    archive_hash = record["archive_hash"]

    def response(status, commit):
        return {"status": status, "repository_url": repository, "target_branch": target,
                "request_branch": branch, "request_commit": commit, "merge_request_created": False}

    def recheck():
        current = require_accepted_archive(project, release_id, delivery_key)
        if current != record:
            raise ValueError("Архив или baseline изменился перед публикацией очистки")

    with tempfile.TemporaryDirectory(prefix="exchange-cleanup-") as temporary:
        clone = Path(temporary) / "code"
        result = subprocess.run(["git", "clone", "--quiet", "--no-checkout", "--single-branch", "--branch", target, "--", repository, str(clone)], capture_output=True)
        if result.returncode:
            raise ValueError("Недоступен целевой репозиторий очистки: " + result.stderr.decode(errors="replace"))
        target_commit = _value(clone, "rev-parse", "HEAD")
        if _git(clone, "merge-base", "--is-ancestor", source_commit, target_commit, check=False).returncode:
            raise ValueError("Целевая ветка не содержит архивированный исходный коммит")
        remote = _git(clone, "ls-remote", "--heads", "origin", ref).stdout.decode().split()
        commit = remote[0] if remote else None
        if commit:
            _git(clone, "fetch", "--quiet", "origin", ref)
            if _value(clone, "rev-parse", "FETCH_HEAD") != commit:
                raise ValueError("Ветка очистки изменилась во время проверки")
            _validate_request(clone, commit, source_commit, target_commit, package, archive_hash)
        current_hash = _package_hash(clone, target_commit, package)
        if current_hash is None:
            if commit and not _git(clone, "merge-base", "--is-ancestor", commit, target_commit, check=False).returncode:
                recheck()
                return response("completed", commit)
            raise ValueError("Пакет отсутствует, но принятие точного коммита очистки не доказано; squash или удалённая ветка требуют ручного разбора")
        if current_hash != archive_hash:
            raise ValueError("Передача изменилась после архивирования: очистка запрещена")
        if commit:
            if not _git(clone, "merge-base", "--is-ancestor", commit, target_commit, check=False).returncode:
                raise ValueError("Пакет восстановлен после принятой очистки; требуется новый разбор")
            recheck()
            return response("awaiting-merge", commit)
        _git(clone, "switch", "--quiet", "-c", branch)
        _git(clone, "read-tree", "HEAD")
        _git(clone, "rm", "--cached", "-r", "--", package)
        require_valid_commit_message(MESSAGE)
        hook = Path(_value(clone, "rev-parse", "--path-format=absolute", "--git-path", "hooks/commit-msg"))
        if not hook.resolve().is_relative_to(clone.resolve()):
            raise ValueError("commit-msg hook должен находиться внутри временного клона")
        install_commit_message_hook(clone, Path(__file__).resolve().with_name("commit_message_policy.py"))
        _git(clone, "-c", "user.name=Analyst Requirements Exchange", "-c", "user.email=analyst-harness@local.invalid", "commit", "--quiet", "-m", MESSAGE)
        commit = _value(clone, "rev-parse", "HEAD")
        _validate_request(clone, commit, source_commit, target_commit, package, archive_hash)
        recheck()
        # Check the live target once more before publication; never push its ref.
        live_target = _git(clone, "ls-remote", "--heads", "origin", "refs/heads/" + target).stdout.decode().split()
        if not live_target or live_target[0] != target_commit:
            raise ValueError("Целевая ветка изменилась; повтори проверку очистки")
        pushed = _git(clone, "push", "origin", "HEAD:" + ref, check=False)
        if pushed.returncode:
            observed = _git(clone, "ls-remote", "--heads", "origin", ref, check=False)
            if observed.returncode or observed.stdout.decode().split()[:1] != [commit]:
                raise ValueError("Публикация очистки не подтверждена; повтори проверку той же ветки, без резервного удаления")
        return response("awaiting-merge", commit)
