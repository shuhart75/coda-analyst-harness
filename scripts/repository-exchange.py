#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from workspace_entrypoint import (
    embedded_harness_paths as local_embedded_harness_paths,
    write_local_entrypoint,
)


SOURCE_REMOTE = "analyst-source-local"
BRANCH = "main"
FORBIDDEN_CONTENT_PATHS = (".workflow", ".vscode", "AGENTS.md")
ALLOWED_CONTENT_ROOTS = frozenset({
    ".gitattributes",
    ".github",
    ".gitignore",
    "LICENSE",
    "README.md",
    "assets",
    "baseline",
    "context",
    "features",
    "planning",
    "releases",
})
FORBIDDEN_LOCAL_COMPONENTS = frozenset({
    ".codex",
    ".gigacode",
    ".gigaide",
    ".idea",
    ".vscode",
    ".workflow",
    "__pycache__",
})
FORBIDDEN_LOCAL_NAMES = frozenset({".DS_Store", "GIGACODE.md", "Thumbs.db"})
ALLOWED_FEATURES_ROOT_FILES = frozenset({".gitkeep", "README.md"})
OPAQUE_CONTENT_PREFIXES = ("context/source-materials/",)
DELETION_APPROVALS_FILE = "exchange-deletion-approvals.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def root_path(explicit: str | None) -> Path:
    return Path(explicit).expanduser().resolve() if explicit else Path(__file__).resolve().parents[1]


def run(*args: str, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, check=False)


def git(repository: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return run("git", "-C", str(repository), *args, env=env)


def require_worktree_repository(path: Path, name: str) -> None:
    result = git(path, "rev-parse", "--show-toplevel")
    if result.returncode != 0 or Path(result.stdout.strip()).resolve() != path:
        raise ValueError(f"{name} не развёрнут как отдельный Git-репозиторий: {path}")


def require_source_mirror(path: Path, repository_id: str) -> None:
    result = git(path, "rev-parse", "--is-bare-repository")
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise ValueError(f"Репозиторий роли source ({repository_id}) не развёрнут как bare-зеркало: {path}")
    branch = git(path, "symbolic-ref", "--quiet", "HEAD")
    if branch.returncode != 0 or branch.stdout.strip() != f"refs/heads/{BRANCH}":
        raise ValueError(f"{repository_id}: bare-зеркало роли source должно указывать HEAD на main")
    push_url = git(path, "remote", "get-url", "--push", "origin")
    if push_url.returncode != 0 or push_url.stdout.strip() != "DISABLED_BY_CODA_ANALYST_HARNESS":
        raise ValueError(f"{repository_id}: запрет отправки роли source снят или повреждён")


def require_clean(path: Path, name: str) -> None:
    result = git(path, "status", "--porcelain=v1")
    if result.returncode != 0:
        raise ValueError(f"Не удалось проверить {name}: {result.stderr.strip()}")
    if result.stdout:
        raise ValueError(f"{name} содержит незакоммиченные изменения; сначала зафиксируй или убери их")


def porcelain_entries(path: Path) -> list[tuple[str, str]]:
    result = subprocess.run(
        ("git", "-C", str(path), "status", "--porcelain=v1", "-z"),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"Не удалось проверить рабочее дерево: {result.stderr.decode(errors='replace').strip()}")
    entries = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        entries.append((raw[:2].decode("ascii", errors="replace"), raw[3:].decode("utf-8", errors="surrogateescape")))
    return entries


def filesystem_alias(repository: Path, tracked_path: str) -> Path | None:
    expected = repository / tracked_path
    parent = expected.parent
    if not parent.is_dir():
        return None
    target = unicodedata.normalize("NFD", expected.name)
    matches = [item for item in parent.iterdir() if unicodedata.normalize("NFD", item.name) == target]
    return matches[0] if len(matches) == 1 and matches[0].is_file() else None


def blob_bytes(repository: Path, revision: str, path: str) -> bytes:
    result = subprocess.run(
        ("git", "-C", str(repository), "show", f"{revision}:{path}"),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"Не удалось прочитать {path} из {revision}")
    return result.stdout


def prepare_unicode_aliases(analytics: Path, source: Path, source_commit: str) -> list[str]:
    entries = porcelain_entries(analytics)
    if not entries:
        return []
    aliases: list[str] = []
    for status, path in entries:
        if status != " D" or path == unicodedata.normalize("NFC", path):
            raise ValueError(
                "Репозиторий роли analytics содержит обычные незакоммиченные изменения; "
                "сначала зафиксируй или убери их"
            )
        remains = git(source, "cat-file", "-e", f"{source_commit}:{path}")
        alias = filesystem_alias(analytics, path)
        if remains.returncode == 0 or alias is None or alias.read_bytes() != blob_bytes(analytics, "HEAD", path):
            raise ValueError(
                f"Нельзя безопасно сопоставить ошибочное Unicode-имя {path}; "
                "синхронизация остановлена без изменения файлов"
            )
        aliases.append(path)
    return aliases


def verify_unicode_aliases(analytics: Path, paths: list[str]) -> None:
    for path in paths:
        tracked = git(analytics, "ls-files", "--error-unmatch", "--", path)
        if tracked.returncode != 0:
            continue
        alias = filesystem_alias(analytics, path)
        if alias is None or alias.read_bytes() != blob_bytes(analytics, "HEAD", path):
            raise ValueError(
                f"Содержимое файла с ошибочным Unicode-именем изменилось после обновления: {path}"
            )


def require_branch(path: Path, name: str) -> None:
    result = git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    if result.returncode != 0 or result.stdout.strip() != BRANCH:
        current = result.stdout.strip() or "detached HEAD"
        raise ValueError(f"{name}: ожидалась ветка {BRANCH}, найдена {current}")


def tracked_paths(repository: Path, commit: str) -> list[str]:
    result = git(repository, "ls-tree", "-rz", "--name-only", commit)
    if result.returncode != 0:
        raise ValueError(f"Не удалось прочитать имена файлов {commit}: {result.stderr.strip()}")
    return [item for item in result.stdout.split("\0") if item]


def require_nfc_paths(repository: Path, commit: str, name: str) -> None:
    invalid = [
        path for path in tracked_paths(repository, commit)
        if path != unicodedata.normalize("NFC", path)
    ]
    if invalid:
        details = ", ".join(repr(path) for path in invalid[:5])
        suffix = "" if len(invalid) <= 5 else f" и ещё {len(invalid) - 5}"
        raise ValueError(
            f"{name} содержит имена файлов не в Unicode NFC: {details}{suffix}. "
            "Исправь имена в upstream до синхронизации"
        )


def embedded_harness_paths(path: Path) -> list[str]:
    return local_embedded_harness_paths(path)


def require_content_only(path: Path, name: str) -> None:
    embedded = embedded_harness_paths(path)
    if embedded:
        raise ValueError(f"{name} содержит встроенную обвязку: {', '.join(embedded)}")


def require_source_content_only(source: Path, commit: str, repository_id: str) -> None:
    roots = {path.split("/", 1)[0] for path in tracked_paths(source, commit)}
    embedded = [item for item in FORBIDDEN_CONTENT_PATHS if item in roots]
    if embedded:
        raise ValueError(f"{repository_id} в роли source содержит встроенную обвязку: {', '.join(embedded)}")


def analytics_content_violations(
    repository: Path,
    commit: str,
    *,
    allow_legacy_harness: bool = False,
) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    for path in tracked_paths(repository, commit):
        parts = Path(path).parts
        root = parts[0]
        if allow_legacy_harness and forbidden_content_path(path):
            continue
        if root not in ALLOWED_CONTENT_ROOTS:
            violations.append({
                "path": path,
                "reason": "корневой путь не входит в структуру аналитического репозитория",
            })
            continue
        if any(path.startswith(prefix) for prefix in OPAQUE_CONTENT_PREFIXES):
            continue
        if any(part in FORBIDDEN_LOCAL_COMPONENTS for part in parts):
            violations.append({
                "path": path,
                "reason": "локальные настройки инструмента не должны отслеживаться Git",
            })
            continue
        if any(part in FORBIDDEN_LOCAL_NAMES or part.endswith((".iml", ".orig")) for part in parts):
            violations.append({
                "path": path,
                "reason": "локальный или резервный файл не должен отслеживаться Git",
            })
            continue
        if root == "features" and len(parts) == 2 and parts[1] not in ALLOWED_FEATURES_ROOT_FILES:
            violations.append({
                "path": path,
                "reason": "в features разрешены только каталоги функциональностей",
            })
    return violations


def require_source_content_policy(source: Path, commit: str, repository_id: str) -> None:
    violations = analytics_content_violations(source, commit)
    if violations:
        raise ValueError(json.dumps({
            "status": "blocked",
            "reason": "source-content-policy",
            "message": f"{repository_id} в роли source содержит недопустимые пути",
            "violations": violations,
            "allowed_next_action": "исправить состав upstream-репозитория source",
        }, ensure_ascii=False))


def deleted_source_paths(analytics: Path, source_commit: str, analytics_commit: str) -> list[str]:
    merge_base = git(analytics, "merge-base", source_commit, analytics_commit)
    if merge_base.returncode != 0 or not merge_base.stdout.strip():
        raise ValueError("Не удалось определить общую историю ролей source и analytics")
    result = subprocess.run(
        (
            "git", "-C", str(analytics), "diff", "--name-only", "--diff-filter=D", "-z",
            "--no-renames", source_commit, analytics_commit, "--", ".",
        ),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(
            "Не удалось проверить удаления из роли source: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    candidates = [
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]
    base_commit = merge_base.stdout.strip()
    return [
        path
        for path in candidates
        if git(analytics, "cat-file", "-e", f"{base_commit}:{path}").returncode == 0
    ]


def deletion_approvals_path(root: Path) -> Path:
    return root / ".workspace-state" / DELETION_APPROVALS_FILE


def load_deletion_approvals(root: Path) -> dict[str, str]:
    path = deletion_approvals_path(root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать подтверждения удалений {path}: {exc}") from exc
    if payload.get("schema_version") != 1 or not isinstance(payload.get("approvals"), list):
        raise ValueError(f"Повреждён формат подтверждений удалений: {path}")
    approvals: dict[str, str] = {}
    for item in payload["approvals"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or not isinstance(item.get("source_blob"), str)
        ):
            raise ValueError(f"Повреждена запись подтверждения удаления: {path}")
        approvals[item["path"]] = item["source_blob"]
    return approvals


def source_blob_oid(source: Path, source_commit: str, path: str) -> str:
    result = git(source, "rev-parse", f"{source_commit}:{path}")
    if result.returncode != 0:
        raise ValueError(f"Путь отсутствует в роли source: {path}")
    return result.stdout.strip()


def unapproved_source_deletions(
    root: Path,
    source: Path,
    analytics: Path,
    source_commit: str,
    analytics_commit: str,
) -> list[str]:
    approvals = load_deletion_approvals(root)
    result = []
    for path in deleted_source_paths(analytics, source_commit, analytics_commit):
        if approvals.get(path) != source_blob_oid(source, source_commit, path):
            result.append(path)
    return result


def require_analytics_content_policy(
    root: Path,
    source: Path,
    analytics: Path,
    source_commit: str,
    analytics_commit: str,
    *,
    allow_legacy_harness: bool = False,
) -> None:
    violations = analytics_content_violations(
        analytics,
        analytics_commit,
        allow_legacy_harness=allow_legacy_harness,
    )
    deletions = unapproved_source_deletions(
        root,
        source,
        analytics,
        source_commit,
        analytics_commit,
    )
    if not violations and not deletions:
        return
    raise ValueError(json.dumps({
        "status": "blocked",
        "reason": "analytics-content-policy",
        "message": "Аналитическое дерево содержит недопустимые пути или неподтверждённые удаления",
        "violations": violations,
        "unapproved_source_deletions": deletions,
        "allowed_next_actions": [
            "удалить локальные и тестовые файлы из analytics, фиксируя только точные пути",
            "восстановить непреднамеренно удалённые файлы из source",
            "после явного решения аналитика подтвердить намеренное удаление отдельного пути",
        ],
        "forbidden_actions": [
            "git add -A",
            "git add .",
            "создание обратной заплаты",
            "отправка недопустимого дерева",
        ],
    }, ensure_ascii=False))


def update_ff(path: Path, name: str) -> None:
    fetched = git(path, "fetch", "origin", BRANCH)
    if fetched.returncode != 0:
        raise ValueError(f"{name}: fetch завершился ошибкой: {fetched.stderr.strip()}")
    merged = git(path, "merge", "--ff-only", f"origin/{BRANCH}")
    if merged.returncode != 0:
        raise ValueError(f"{name}: локальную ветку нельзя обновить fast-forward: {merged.stderr.strip()}")


def update_source_mirror(path: Path, repository_id: str) -> None:
    fetched = git(
        path,
        "fetch",
        "--prune",
        "origin",
        f"+refs/heads/{BRANCH}:refs/heads/{BRANCH}",
    )
    if fetched.returncode != 0:
        raise ValueError(f"{repository_id}: fetch роли source завершился ошибкой: {fetched.stderr.strip()}")


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
        raise ValueError(f"Не удалось прочитать локальное зеркало роли source: {fetched.stderr.strip()}")


def merge_source(documents: Path) -> None:
    merged = git(
        documents,
        "-c", "user.name=Coda Analyst Harness",
        "-c", "user.email=coda-analyst-harness@local.invalid",
        "merge", "--no-ff", f"{SOURCE_REMOTE}/{BRANCH}",
        "-m", f"Merge {SOURCE_REMOTE}/{BRANCH}",
    )
    if merged.returncode != 0:
        conflicts = [
            line.strip()
            for line in git(documents, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
            if line.strip()
        ]
        git(documents, "merge", "--abort")
        raise ValueError(json.dumps({
            "status": "blocked",
            "reason": "source-analytics-merge-conflict",
            "message": (
                "Роль source нельзя автоматически объединить с ролью analytics; "
                "слияние отменено, требуется осознанное разрешение конфликта"
            ),
            "conflicting_paths": conflicts,
            "allowed_next_action": "inspect-source-analytics-conflict",
            "forbidden_alternatives": [
                "repeat-code-update-as-fallback",
                "skip-source-merge",
                "overwrite-analytics-from-source",
            ],
        }, ensure_ascii=False))


def forbidden_content_path(path: str) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in FORBIDDEN_CONTENT_PATHS)


def conflict_stages(repository: Path, path: str) -> dict[int, str]:
    result = git(repository, "ls-files", "-u", "-z", "--", path)
    if result.returncode != 0:
        raise ValueError(f"Не удалось прочитать стадии конфликта {path}: {result.stderr.strip()}")
    stages: dict[int, str] = {}
    for item in result.stdout.split("\0"):
        if not item or "\t" not in item:
            continue
        metadata, _ = item.split("\t", 1)
        fields = metadata.split()
        if len(fields) == 3:
            stages[int(fields[2])] = fields[1]
    return stages


def inspect_conflict_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    handle = lock(root)
    try:
        source, documents, source_id, analytics_id = role_repositories(root)
        require_clean(documents, f"{analytics_id} (analytics)")
        require_branch(documents, f"{analytics_id} (analytics)")
        source_commit = git(source, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip()
        analytics_commit = git(documents, "rev-parse", "HEAD").stdout.strip()
        if not source_commit or not analytics_commit:
            raise ValueError("Не удалось определить ревизии source и analytics")

        with tempfile.TemporaryDirectory(prefix="coda-analyst-conflict-") as temporary:
            probe = Path(temporary) / "analytics"
            cloned = run("git", "clone", "--quiet", "--no-hardlinks", str(documents), str(probe))
            if cloned.returncode != 0:
                raise ValueError(f"Не удалось создать временную копию analytics: {cloned.stderr.strip()}")
            added = git(probe, "remote", "add", SOURCE_REMOTE, str(source))
            if added.returncode != 0:
                raise ValueError(f"Не удалось подключить временный source: {added.stderr.strip()}")
            fetched = git(probe, "fetch", "--quiet", SOURCE_REMOTE, source_commit)
            if fetched.returncode != 0:
                raise ValueError(f"Не удалось прочитать ревизию source: {fetched.stderr.strip()}")
            merged = git(probe, "merge", "--no-commit", "--no-ff", "FETCH_HEAD")
            if merged.returncode == 0:
                print(json.dumps({
                    "status": "no-conflict",
                    "source_commit": source_commit,
                    "analytics_commit": analytics_commit,
                    "conflicts": [],
                    "message": "Для текущих ревизий конфликт не воспроизводится; повтори синхронизацию",
                }, ensure_ascii=False, indent=2))
                return 0

            paths = [
                line.strip()
                for line in git(probe, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
                if line.strip()
            ]
            conflicts = []
            for path in paths:
                stages = conflict_stages(probe, path)
                analytics_present = 2 in stages
                source_present = 3 in stages
                if analytics_present and source_present:
                    kind = "both-modified"
                elif analytics_present:
                    kind = "source-deleted-analytics-modified"
                elif source_present:
                    kind = "analytics-deleted-source-modified"
                else:
                    kind = "complex"
                legacy_deletion = kind == "source-deleted-analytics-modified" and forbidden_content_path(path)
                conflicts.append({
                    "path": path,
                    "kind": kind,
                    "base_blob": stages.get(1),
                    "analytics_blob": stages.get(2),
                    "source_blob": stages.get(3),
                    "recommended_resolution": "accept-source-deletion" if legacy_deletion else "analyst-decision-required",
                    "reason": (
                        "Путь относится к устаревшей встроенной обвязке, запрещённой в роли analytics"
                        if legacy_deletion else None
                    ),
                })

        print(json.dumps({
            "status": "conflict-inspected",
            "source": {"repository": source_id, "commit": source_commit},
            "analytics": {"repository": analytics_id, "commit": analytics_commit},
            "real_repositories_changed": False,
            "conflicts": conflicts,
            "next_step": (
                "Для каждого analyst-decision-required запросить решение аналитика. "
                "Для accept-source-deletion удалить указанный устаревший служебный путь из analytics, "
                "зафиксировать удаление и повторить workspace.py sync."
            ),
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        handle.close()


def verified_reverse_patch(
    root: Path,
    source: Path,
    analytics: Path,
    source_id: str,
    analytics_id: str,
) -> dict:
    configure_source_remote(analytics, source)
    source_commit = git(source, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip()
    documents_commit = git(analytics, "rev-parse", "HEAD").stdout.strip()
    require_analytics_content_policy(root, source, analytics, source_commit, documents_commit)
    source_tree = git(source, "rev-parse", f"{source_commit}^{{tree}}").stdout.strip()
    documents_tree = git(analytics, "rev-parse", f"{documents_commit}^{{tree}}").stdout.strip()
    output_dir = root / "reverse-diffs"
    output_dir.mkdir(parents=True, exist_ok=True)
    latest = output_dir / "reverse-diff-latest.patch"
    metadata_path = output_dir / "reverse-diff-latest.json"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    patch_path = output_dir / f"reverse-diff-{timestamp}.patch"
    archived_metadata_path = output_dir / f"reverse-diff-{timestamp}.json"

    diff = git(analytics, "diff", "--binary", "--full-index", "--no-renames", source_commit, documents_commit, "--", ".")
    if diff.returncode != 0:
        raise ValueError(f"Не удалось построить обратную заплату: {diff.stderr.strip()}")

    if source_tree == documents_tree:
        latest.unlink(missing_ok=True)
        patch_path = None
        patch_payload = None
        patch_sha256 = None
    else:
        patch_payload = diff.stdout.encode("utf-8")
        patch_sha256 = hashlib.sha256(patch_payload).hexdigest()
        descriptor, raw_patch_path = tempfile.mkstemp(prefix="coda-analyst-reverse-", suffix=".patch")
        os.close(descriptor)
        verification_patch = Path(raw_patch_path)
        verification_patch.write_bytes(patch_payload)
        descriptor, raw_index_path = tempfile.mkstemp(prefix="coda-analyst-index-")
        os.close(descriptor)
        index_path = Path(raw_index_path)
        index_path.unlink(missing_ok=True)
        try:
            environment = {**os.environ, "GIT_INDEX_FILE": str(index_path)}
            read_tree = git(source, "read-tree", source_commit, env=environment)
            if read_tree.returncode != 0:
                raise ValueError(
                    f"Не удалось подготовить временный индекс роли source: "
                    f"{read_tree.stderr.strip()}"
                )
            checked = git(source, "apply", "--cached", "--check", str(verification_patch), env=environment)
            if checked.returncode != 0:
                raise ValueError(
                    f"Обратная заплата не применима к текущему состоянию роли source: "
                    f"{checked.stderr.strip()}"
                )
            applied = git(source, "apply", "--cached", str(verification_patch), env=environment)
            written = git(source, "write-tree", env=environment)
            if applied.returncode or written.returncode or written.stdout.strip() != documents_tree:
                raise ValueError("Проверка обратной заплаты не воспроизвела дерево роли analytics")
        finally:
            index_path.unlink(missing_ok=True)
            verification_patch.unlink(missing_ok=True)

    changed = git(analytics, "diff", "--name-only", "-z", source_commit, documents_commit, "--", ".")
    if changed.returncode != 0:
        raise ValueError(f"Не удалось получить состав обратной заплаты: {changed.stderr.strip()}")
    changed_paths = [item for item in changed.stdout.split("\0") if item]
    approved_deletions = deleted_source_paths(analytics, source_commit, documents_commit)
    metadata = {
        "schema_version": 2,
        "artifact_id": timestamp,
        "created_at": utc_now(),
        "source_repository": source_id,
        "analytics_repository": analytics_id,
        "source_commit": source_commit,
        "analytics_commit": documents_commit,
        "documents_commit": documents_commit,
        "source_tree": source_tree,
        "analytics_tree": documents_tree,
        "documents_tree": documents_tree,
        "repositories_identical": source_tree == documents_tree,
        "patch": str(patch_path) if patch_path else None,
        "latest_patch": str(latest) if patch_path else None,
        "metadata": str(archived_metadata_path),
        "latest_metadata": str(metadata_path),
        "patch_sha256": patch_sha256,
        "changed_path_count": len(changed_paths),
        "changed_paths": changed_paths,
        "approved_source_deletions": approved_deletions,
        "tree_verified": True,
        "content_policy_verified": True,
        "verified": True,
    }
    serialized = json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
    if patch_path and patch_payload is not None:
        with patch_path.open("xb") as handle:
            handle.write(patch_payload)
    with archived_metadata_path.open("x", encoding="utf-8") as handle:
        handle.write(serialized)
    if patch_path and patch_payload is not None:
        atomic_write(latest, patch_payload)
    atomic_write(metadata_path, serialized.encode("utf-8"))
    return metadata


def push_analytics(analytics: Path, analytics_id: str) -> None:
    pushed = git(analytics, "push", "origin", BRANCH)
    if pushed.returncode != 0:
        raise ValueError(
            f"{analytics_id} в роли analytics не удалось отправить. "
            "Автоматическое повторное слияние не выполняется; "
            f"повтори синхронизацию после проверки удалённой ветки: {pushed.stderr.strip()}"
        )


def workspace_roles(root: Path) -> dict:
    state_path = root / ".workspace-state/workspace.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать роли рабочей области {state_path}: {exc}") from exc
    roles = state.get("roles", {})
    if not isinstance(roles, dict):
        raise ValueError(f"Повреждены роли рабочей области: {state_path}")
    return roles


def workspace_role(root: Path, role: str) -> dict:
    item = workspace_roles(root).get(role, {})
    if not isinstance(item, dict):
        raise ValueError(f"Повреждена роль {role} в состоянии рабочей области")
    return item


def analytics_repository(root: Path) -> tuple[Path, str]:
    analytics_role = workspace_role(root, "analytics")
    analytics_id = analytics_role.get("repository")
    analytics_path = analytics_role.get("path")
    if not analytics_id or not analytics_path:
        raise ValueError("Роль analytics не настроена")
    analytics = Path(analytics_path).resolve()
    require_worktree_repository(analytics, f"{analytics_id} (analytics)")
    return analytics, analytics_id


def available_code_repository(root: Path) -> tuple[Path | None, str]:
    code_role = workspace_role(root, "code")
    if not code_role.get("repository"):
        return None, "disabled"
    if code_role.get("availability") == "disabled":
        return None, "disabled"
    code_path = code_role.get("path")
    if not code_path:
        return None, "absent"
    candidate = Path(code_path).resolve()
    if not candidate.exists():
        return None, "absent"
    result = git(candidate, "rev-parse", "--show-toplevel")
    if result.returncode != 0 or Path(result.stdout.strip()).resolve() != candidate:
        raise ValueError(
            f"Путь роли code существует, но не является отдельным Git-репозиторием: {candidate}"
        )
    return candidate, "ready"


def role_repositories(root: Path) -> tuple[Path, Path, str, str]:
    source_role = workspace_role(root, "source")
    analytics_role = workspace_role(root, "analytics")
    source_id = source_role.get("repository")
    analytics_id = analytics_role.get("repository")
    if not source_id:
        raise ValueError("Роль source отключена; обмен репозиториями недоступен")
    if source_role.get("availability") == "absent":
        raise ValueError("Репозиторий роли source отсутствует; обмен репозиториями недоступен")
    if not analytics_id:
        raise ValueError("Роль analytics не настроена")
    source_path = source_role.get("path")
    analytics_path = analytics_role.get("path")
    if not source_path or not analytics_path:
        raise ValueError("В состоянии рабочей области отсутствуют пути source или analytics")
    source = Path(source_path).resolve()
    analytics = Path(analytics_path).resolve()
    require_source_mirror(source, source_id)
    require_worktree_repository(analytics, f"{analytics_id} (analytics)")
    return source, analytics, source_id, analytics_id


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
        source, documents, source_id, analytics_id = role_repositories(root)
        update_source_mirror(source, source_id)
        source_commit = git(source, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip()
        require_nfc_paths(source, source_commit, f"{source_id} (source)")
        require_source_content_only(source, source_commit, source_id)
        require_source_content_policy(source, source_commit, source_id)
        unicode_aliases = prepare_unicode_aliases(documents, source, source_commit)
        if not unicode_aliases:
            require_clean(documents, f"{analytics_id} (analytics)")
        require_branch(documents, f"{analytics_id} (analytics)")
        update_ff(documents, f"{analytics_id} (analytics)")
        verify_unicode_aliases(documents, unicode_aliases)
        configure_source_remote(documents, source)
        analytics_commit = git(documents, "rev-parse", "HEAD").stdout.strip()
        require_analytics_content_policy(
            root,
            source,
            documents,
            source_commit,
            analytics_commit,
            allow_legacy_harness=True,
        )
        merge_source(documents)
        documents_commit = git(documents, "rev-parse", "HEAD").stdout.strip()
        require_nfc_paths(documents, documents_commit, f"{analytics_id} (analytics)")
        require_content_only(documents, analytics_id)
        require_analytics_content_policy(root, source, documents, source_commit, documents_commit)
        code_path, code_availability = available_code_repository(root)
        entrypoint = write_local_entrypoint(documents, root, code_path, code_availability)
        require_clean(documents, f"{analytics_id} (analytics)")
        metadata = verified_reverse_patch(root, source, documents, source_id, analytics_id)
        if not args.no_push:
            require_clean(documents, f"{analytics_id} (analytics)")
            current_commit = git(documents, "rev-parse", "HEAD").stdout.strip()
            if current_commit != metadata["analytics_commit"]:
                raise ValueError("Роль analytics изменилась после проверки обратной заплаты; отправка запрещена")
            require_analytics_content_policy(
                root,
                source,
                documents,
                source_commit,
                current_commit,
            )
            push_analytics(documents, analytics_id)
        completion = reverse_diff_completion(metadata)
        print(json.dumps({
            "status": completion["status"],
            "analytics_pushed": not args.no_push,
            "local_entrypoint": str(entrypoint),
            "reverse_diff": metadata,
            **completion,
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        handle.close()


def require_standalone_analytics_policy(analytics: Path, analytics_commit: str, analytics_id: str) -> None:
    violations = analytics_content_violations(analytics, analytics_commit)
    if violations:
        raise ValueError(json.dumps({
            "status": "blocked",
            "reason": "analytics-content-policy",
            "message": "Аналитическое дерево содержит недопустимые пути",
            "violations": violations,
            "unapproved_source_deletions": [],
            "allowed_next_action": "исправить точные пути в роли analytics",
            "forbidden_actions": ["git add -A", "git add .", "отправка недопустимого дерева"],
        }, ensure_ascii=False))
    require_content_only(analytics, analytics_id)


def unavailable_reverse_diff(root: Path, analytics: Path, analytics_id: str) -> dict:
    output_dir = root / "reverse-diffs"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "reverse-diff-latest.patch").unlink(missing_ok=True)
    analytics_commit = git(analytics, "rev-parse", "HEAD").stdout.strip()
    analytics_tree = git(analytics, "rev-parse", "HEAD^{tree}").stdout.strip()
    source_role = workspace_role(root, "source")
    reason = "source-role-absent" if source_role.get("repository") else "source-role-disabled"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archived_metadata_path = output_dir / f"reverse-diff-{timestamp}.json"
    latest_metadata_path = output_dir / "reverse-diff-latest.json"
    metadata = {
        "schema_version": 2,
        "artifact_id": timestamp,
        "created_at": utc_now(),
        "status": "unavailable",
        "reason": reason,
        "source_repository": source_role.get("repository"),
        "analytics_repository": analytics_id,
        "source_commit": None,
        "analytics_commit": analytics_commit,
        "documents_commit": analytics_commit,
        "source_tree": None,
        "analytics_tree": analytics_tree,
        "documents_tree": analytics_tree,
        "repositories_identical": None,
        "patch": None,
        "latest_patch": None,
        "metadata": str(archived_metadata_path),
        "latest_metadata": str(latest_metadata_path),
        "patch_sha256": None,
        "changed_path_count": None,
        "changed_paths": [],
        "approved_source_deletions": [],
        "tree_verified": False,
        "content_policy_verified": False,
        "verified": False,
    }
    serialized = json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
    with archived_metadata_path.open("x", encoding="utf-8") as handle:
        handle.write(serialized)
    atomic_write(latest_metadata_path, serialized.encode("utf-8"))
    return metadata


def reverse_diff_completion(metadata: dict) -> dict:
    identical = metadata.get("repositories_identical")
    if identical is True:
        return {
            "status": "fully-synchronized",
            "source_analytics_state": "identical",
            "all_repositories_synchronized": True,
            "report_message": "Все доступные репозитории синхронизированы; деревья source и analytics идентичны.",
            "next_action": None,
            "forbidden_claims": [],
        }
    if metadata.get("source_commit") is None:
        return {
            "status": "analytics-synchronized-source-unavailable",
            "source_analytics_state": "source-unavailable",
            "all_repositories_synchronized": False,
            "report_message": (
                "Локальный этап обмена выполнен: analytics обновлён; source отсутствует, "
                "поэтому равенство репозиториев не проверено."
            ),
            "next_action": "Продолжать работу в analytics; не заявлять о совпадении с source",
            "forbidden_claims": ["all-repositories-synchronized", "reverse-diff-verified"],
        }
    return {
        "status": "analytics-synchronized-reverse-diff-pending",
        "source_analytics_state": "reverse-diff-pending",
        "all_repositories_synchronized": False,
        "report_message": (
            "Локальный этап обмена выполнен: analytics обновлён; source не изменён; "
            "для достижения равенства требуется применить проверенную обратную заплату "
            "на машине с рабочим source."
        ),
        "next_action": "Передать проверенную обратную заплату на машину, где source является рабочим репозиторием",
        "forbidden_claims": ["all-repositories-synchronized", "source-updated"],
    }


def sync_analytics_only_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    handle = lock(root)
    try:
        analytics, analytics_id = analytics_repository(root)
        require_clean(analytics, f"{analytics_id} (analytics)")
        require_branch(analytics, f"{analytics_id} (analytics)")
        update_ff(analytics, f"{analytics_id} (analytics)")
        analytics_commit = git(analytics, "rev-parse", "HEAD").stdout.strip()
        require_nfc_paths(analytics, analytics_commit, f"{analytics_id} (analytics)")
        require_standalone_analytics_policy(analytics, analytics_commit, analytics_id)
        code_path, code_availability = available_code_repository(root)
        entrypoint = write_local_entrypoint(analytics, root, code_path, code_availability)
        require_clean(analytics, f"{analytics_id} (analytics)")
        metadata = unavailable_reverse_diff(root, analytics, analytics_id)
        if not args.no_push:
            current_commit = git(analytics, "rev-parse", "HEAD").stdout.strip()
            if current_commit != metadata["analytics_commit"]:
                raise ValueError("Роль analytics изменилась после проверки; отправка запрещена")
            require_clean(analytics, f"{analytics_id} (analytics)")
            require_standalone_analytics_policy(analytics, current_commit, analytics_id)
            push_analytics(analytics, analytics_id)
        completion = reverse_diff_completion(metadata)
        print(json.dumps({
            "status": completion["status"],
            "sync_mode": "analytics-only",
            "analytics_pushed": not args.no_push,
            "local_entrypoint": str(entrypoint),
            "reverse_diff": metadata,
            **completion,
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        handle.close()


def reverse_diff_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    handle = lock(root)
    try:
        source, documents, source_id, analytics_id = role_repositories(root)
        require_clean(documents, f"{analytics_id} (analytics)")
        require_branch(documents, f"{analytics_id} (analytics)")
        require_content_only(documents, f"{analytics_id} (analytics)")
        source_commit = git(source, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip()
        documents_commit = git(documents, "rev-parse", "HEAD").stdout.strip()
        require_nfc_paths(source, source_commit, f"{source_id} (source)")
        require_nfc_paths(documents, documents_commit, f"{analytics_id} (analytics)")
        require_source_content_only(source, source_commit, source_id)
        require_source_content_policy(source, source_commit, source_id)
        configure_source_remote(documents, source)
        require_analytics_content_policy(root, source, documents, source_commit, documents_commit)
        metadata = verified_reverse_patch(root, source, documents, source_id, analytics_id)
        print(json.dumps({"status": "created", "reverse_diff": metadata}, ensure_ascii=False, indent=2))
        return 0
    finally:
        handle.close()


def approve_deletion_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    handle = lock(root)
    try:
        source, documents, source_id, analytics_id = role_repositories(root)
        require_clean(documents, f"{analytics_id} (analytics)")
        require_branch(documents, f"{analytics_id} (analytics)")
        path = args.path.strip().strip("/")
        if not path or path != unicodedata.normalize("NFC", path):
            raise ValueError("Подтверждаемый путь должен быть непустым и записан в Unicode NFC")
        source_commit = git(source, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip()
        analytics_commit = git(documents, "rev-parse", "HEAD").stdout.strip()
        configure_source_remote(documents, source)
        if path not in deleted_source_paths(documents, source_commit, analytics_commit):
            raise ValueError(f"Путь не является удалением относительно роли source: {path}")
        blob = source_blob_oid(source, source_commit, path)
        approvals_path = deletion_approvals_path(root)
        approvals_path.parent.mkdir(parents=True, exist_ok=True)
        approvals = load_deletion_approvals(root)
        approvals[path] = blob
        payload = {
            "schema_version": 1,
            "updated_at": utc_now(),
            "approvals": [
                {
                    "path": approved_path,
                    "source_blob": source_blob,
                    "decision": "explicit-analyst-approval",
                }
                for approved_path, source_blob in sorted(approvals.items())
            ],
        }
        approvals_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({
            "status": "approved",
            "source_repository": source_id,
            "analytics_repository": analytics_id,
            "path": path,
            "source_blob": blob,
            "approval_file": str(approvals_path),
            "next_step": "повторить синхронизацию или создание обратной заплаты",
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        handle.close()


def status_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    source, documents, source_id, analytics_id = role_repositories(root)
    report = []
    trees: dict[str, str] = {}
    source_head = git(source, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip()
    source_tree = git(source, "rev-parse", f"{source_head}^{{tree}}").stdout.strip()
    configure_source_remote(documents, source)
    source_roots = {path.split("/", 1)[0] for path in tracked_paths(source, source_head)}
    source_embedded = [item for item in FORBIDDEN_CONTENT_PATHS if item in source_roots]
    trees["source"] = source_tree
    report.append({
        "role": "source",
        "repository": source_id,
        "branch": BRANCH,
        "commit": source_head,
        "storage": "bare-mirror",
        "worktree": None,
        "tree": source_tree,
        "embedded_harness_paths": source_embedded,
        "content_policy_violations": analytics_content_violations(source, source_head),
        "unapproved_source_deletions": [],
    })
    branch = git(documents, "symbolic-ref", "--quiet", "--short", "HEAD")
    head = git(documents, "rev-parse", "HEAD")
    dirty = git(documents, "status", "--porcelain=v1")
    tree = git(documents, "rev-parse", "HEAD^{tree}").stdout.strip()
    analytics_commit = head.stdout.strip()
    policy_violations = analytics_content_violations(documents, analytics_commit)
    unapproved_deletions = unapproved_source_deletions(
        root,
        source,
        documents,
        source_head,
        analytics_commit,
    )
    trees["analytics"] = tree
    report.append({
        "role": "analytics",
        "repository": analytics_id,
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
        "commit": head.stdout.strip(),
        "storage": "worktree",
        "worktree": "clean" if not dirty.stdout else "dirty",
        "tree": tree,
        "embedded_harness_paths": embedded_harness_paths(documents),
        "content_policy_violations": policy_violations,
        "unapproved_source_deletions": unapproved_deletions,
    })
    identical = trees.get("source") == trees.get("analytics")
    clean_content = not any(
        item["embedded_harness_paths"]
        or item.get("content_policy_violations")
        or item.get("unapproved_source_deletions")
        for item in report
    )
    print(json.dumps({"status": "ok" if clean_content else "invalid", "repositories_identical": identical, "repositories": report}, ensure_ascii=False, indent=2))
    return 0 if clean_content else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Обмен изменениями между ролями source и analytics")
    result.add_argument("--root", help="Корень coda-analyst-harness; обычно определяется автоматически")
    commands = result.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync")
    sync.add_argument("--no-push", action="store_true", help="Не отправлять итоговую ветку роли analytics")
    sync.set_defaults(handler=sync_command)
    analytics_only = commands.add_parser("sync-analytics-only")
    analytics_only.add_argument("--no-push", action="store_true", help="Не отправлять итоговую ветку роли analytics")
    analytics_only.set_defaults(handler=sync_analytics_only_command)
    reverse_diff = commands.add_parser("reverse-diff")
    reverse_diff.set_defaults(handler=reverse_diff_command)
    approve_deletion = commands.add_parser("approve-deletion")
    approve_deletion.add_argument(
        "--path",
        required=True,
        help="Точный путь, удаление которого явно подтвердил аналитик",
    )
    approve_deletion.set_defaults(handler=approve_deletion_command)
    inspect_conflict = commands.add_parser("inspect-source-analytics-conflict")
    inspect_conflict.set_defaults(handler=inspect_conflict_command)
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
