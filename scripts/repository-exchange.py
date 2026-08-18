#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


SOURCE_REMOTE = "changeswork-copy-local"
BRANCH = "main"
FORBIDDEN_CONTENT_PATHS = (".workflow", ".vscode", "AGENTS.md")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def root_path(explicit: str | None) -> Path:
    return Path(explicit).expanduser().resolve() if explicit else Path(__file__).resolve().parents[1]


def run(*args: str, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, check=False)


def git(repository: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return run("git", "-C", str(repository), *args, env=env)


def require_repository(path: Path, name: str) -> None:
    result = git(path, "rev-parse", "--show-toplevel")
    if result.returncode != 0 or Path(result.stdout.strip()).resolve() != path:
        raise ValueError(f"{name} не развёрнут как отдельный Git-репозиторий: {path}")


def require_clean(path: Path, name: str) -> None:
    result = git(path, "status", "--porcelain=v1")
    if result.returncode != 0:
        raise ValueError(f"Не удалось проверить {name}: {result.stderr.strip()}")
    if result.stdout:
        raise ValueError(f"{name} содержит незакоммиченные изменения; сначала зафиксируй или убери их")


def require_branch(path: Path, name: str) -> None:
    result = git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    if result.returncode != 0 or result.stdout.strip() != BRANCH:
        current = result.stdout.strip() or "detached HEAD"
        raise ValueError(f"{name}: ожидалась ветка {BRANCH}, найдена {current}")


def embedded_harness_paths(path: Path) -> list[str]:
    return [item for item in FORBIDDEN_CONTENT_PATHS if (path / item).exists()]


def require_content_only(path: Path, name: str) -> None:
    embedded = embedded_harness_paths(path)
    if embedded:
        raise ValueError(f"{name} содержит встроенную обвязку: {', '.join(embedded)}")


def update_ff(path: Path, name: str) -> None:
    fetched = git(path, "fetch", "origin", BRANCH)
    if fetched.returncode != 0:
        raise ValueError(f"{name}: fetch завершился ошибкой: {fetched.stderr.strip()}")
    merged = git(path, "merge", "--ff-only", f"origin/{BRANCH}")
    if merged.returncode != 0:
        raise ValueError(f"{name}: локальную ветку нельзя обновить fast-forward: {merged.stderr.strip()}")


def configure_source_remote(documents: Path, source: Path) -> None:
    current = git(documents, "remote", "get-url", SOURCE_REMOTE)
    if current.returncode == 0:
        changed = git(documents, "remote", "set-url", SOURCE_REMOTE, str(source))
    else:
        changed = git(documents, "remote", "add", SOURCE_REMOTE, str(source))
    if changed.returncode != 0:
        raise ValueError(f"Не удалось настроить локальный источник: {changed.stderr.strip()}")
    fetched = git(documents, "fetch", SOURCE_REMOTE, BRANCH)
    if fetched.returncode != 0:
        raise ValueError(f"Не удалось прочитать локальный changeswork-copy: {fetched.stderr.strip()}")


def merge_source(documents: Path) -> None:
    merged = git(
        documents,
        "-c", "user.name=Coda Analyst Harness",
        "-c", "user.email=coda-analyst-harness@local.invalid",
        "merge", "--no-ff", f"{SOURCE_REMOTE}/{BRANCH}",
        "-m", f"Merge {SOURCE_REMOTE}/{BRANCH}",
    )
    if merged.returncode != 0:
        git(documents, "merge", "--abort")
        raise ValueError(
            "changeswork-copy нельзя автоматически объединить с documents; "
            "слияние отменено, требуется осознанное разрешение конфликта"
        )


def verified_reverse_patch(root: Path, source: Path, documents: Path) -> dict:
    configure_source_remote(documents, source)
    source_commit = git(source, "rev-parse", "HEAD").stdout.strip()
    documents_commit = git(documents, "rev-parse", "HEAD").stdout.strip()
    source_tree = git(source, "rev-parse", f"{source_commit}^{{tree}}").stdout.strip()
    documents_tree = git(documents, "rev-parse", f"{documents_commit}^{{tree}}").stdout.strip()
    output_dir = root / "reverse-diffs"
    output_dir.mkdir(parents=True, exist_ok=True)
    latest = output_dir / "reverse-diff-latest.patch"
    metadata_path = output_dir / "reverse-diff-latest.json"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    patch_path = output_dir / f"reverse-diff-{timestamp}.patch"

    diff = git(documents, "diff", "--binary", "--full-index", "--no-renames", source_commit, documents_commit, "--", ".")
    if diff.returncode != 0:
        raise ValueError(f"Не удалось построить обратную заплату: {diff.stderr.strip()}")

    if source_tree == documents_tree:
        latest.unlink(missing_ok=True)
        patch_path = None
    else:
        patch_path.write_text(diff.stdout, encoding="utf-8")
        latest.write_text(diff.stdout, encoding="utf-8")
        checked = git(source, "apply", "--check", str(patch_path))
        if checked.returncode != 0:
            raise ValueError(f"Обратная заплата не применима к текущему changeswork-copy: {checked.stderr.strip()}")
        descriptor, raw_index_path = tempfile.mkstemp(prefix="coda-analyst-index-")
        os.close(descriptor)
        index_path = Path(raw_index_path)
        index_path.unlink(missing_ok=True)
        try:
            environment = {**os.environ, "GIT_INDEX_FILE": str(index_path)}
            read_tree = git(source, "read-tree", source_commit, env=environment)
            applied = git(source, "apply", "--cached", str(patch_path), env=environment)
            written = git(source, "write-tree", env=environment)
            if read_tree.returncode or applied.returncode or written.returncode or written.stdout.strip() != documents_tree:
                raise ValueError("Проверка обратной заплаты не воспроизвела дерево documents")
        finally:
            index_path.unlink(missing_ok=True)

    metadata = {
        "schema_version": 1,
        "created_at": utc_now(),
        "source_repository": "changeswork-copy",
        "source_commit": source_commit,
        "documents_commit": documents_commit,
        "source_tree": source_tree,
        "documents_tree": documents_tree,
        "repositories_identical": source_tree == documents_tree,
        "patch": str(patch_path) if patch_path else None,
        "latest_patch": str(latest) if patch_path else None,
        "verified": True,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata


def push_documents(documents: Path) -> None:
    pushed = git(documents, "push", "origin", BRANCH)
    if pushed.returncode != 0:
        raise ValueError(
            "documents не удалось отправить. Автоматическое повторное слияние не выполняется; "
            f"повтори синхронизацию после проверки удалённой ветки: {pushed.stderr.strip()}"
        )


def repositories(root: Path) -> tuple[Path, Path]:
    source = root / "changeswork-copy"
    documents = root / "documents"
    require_repository(source, "changeswork-copy")
    require_repository(documents, "documents")
    return source, documents


def lock(root: Path):
    state = root / ".workspace-state"
    state.mkdir(parents=True, exist_ok=True)
    handle = (state / "repository-exchange.lock").open("w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise ValueError("Другой процесс уже обновляет репозитории") from exc
    return handle


def sync_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    handle = lock(root)
    try:
        source, documents = repositories(root)
        for path, name in ((source, "changeswork-copy"), (documents, "documents")):
            require_clean(path, name)
            require_branch(path, name)
        update_ff(source, "changeswork-copy")
        update_ff(documents, "documents")
        require_content_only(source, "changeswork-copy")
        configure_source_remote(documents, source)
        merge_source(documents)
        require_content_only(documents, "documents")
        metadata = verified_reverse_patch(root, source, documents)
        if not args.no_push:
            push_documents(documents)
        print(json.dumps({
            "status": "synchronized",
            "documents_pushed": not args.no_push,
            "reverse_diff": metadata,
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        handle.close()


def reverse_diff_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    handle = lock(root)
    try:
        source, documents = repositories(root)
        for path, name in ((source, "changeswork-copy"), (documents, "documents")):
            require_clean(path, name)
            require_branch(path, name)
            require_content_only(path, name)
        metadata = verified_reverse_patch(root, source, documents)
        print(json.dumps({"status": "created", "reverse_diff": metadata}, ensure_ascii=False, indent=2))
        return 0
    finally:
        handle.close()


def status_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    source, documents = repositories(root)
    report = []
    trees: dict[str, str] = {}
    for path, name in ((source, "changeswork-copy"), (documents, "documents")):
        branch = git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
        head = git(path, "rev-parse", "HEAD")
        dirty = git(path, "status", "--porcelain=v1")
        tree = git(path, "rev-parse", "HEAD^{tree}").stdout.strip()
        trees[name] = tree
        report.append({
            "id": name,
            "branch": branch.stdout.strip() if branch.returncode == 0 else None,
            "commit": head.stdout.strip(),
            "worktree": "clean" if not dirty.stdout else "dirty",
            "tree": tree,
            "embedded_harness_paths": embedded_harness_paths(path),
        })
    identical = trees.get("changeswork-copy") == trees.get("documents")
    clean_content = not any(item["embedded_harness_paths"] for item in report)
    print(json.dumps({"status": "ok" if clean_content else "invalid", "repositories_identical": identical, "repositories": report}, ensure_ascii=False, indent=2))
    return 0 if clean_content else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Обмен изменениями между changeswork-copy и documents")
    result.add_argument("--root", help="Корень coda-analyst-harness; обычно определяется автоматически")
    commands = result.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync")
    sync.add_argument("--no-push", action="store_true", help="Не отправлять итоговую ветку documents")
    sync.set_defaults(handler=sync_command)
    reverse_diff = commands.add_parser("reverse-diff")
    reverse_diff.set_defaults(handler=reverse_diff_command)
    status = commands.add_parser("status")
    status.set_defaults(handler=status_command)
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
