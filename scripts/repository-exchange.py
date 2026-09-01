#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from commit_message_policy import require_valid_commit_message

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
ANALYTICS_SNAPSHOTS_DIR = "analytics-snapshots"
ANALYTICS_SNAPSHOT_REF_PREFIX = "refs/coda-analyst-harness/analytics-snapshots"
SNAPSHOT_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{12}Z-[a-z0-9-]+(?:-[0-9]+)?$")


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


def analytics_snapshots_path(root: Path) -> Path:
    return root / ".workspace-state" / ANALYTICS_SNAPSHOTS_DIR


def snapshot_metadata_path(root: Path, snapshot_id: str) -> Path:
    if not SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id):
        raise ValueError(f"Недопустимый идентификатор снимка: {snapshot_id}")
    return analytics_snapshots_path(root) / snapshot_id / "snapshot.json"


def write_snapshot_metadata(root: Path, metadata: dict) -> None:
    payload = json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
    atomic_write(
        snapshot_metadata_path(root, metadata["snapshot_id"]),
        payload.encode("utf-8"),
    )


def load_snapshot_metadata(root: Path, snapshot_id: str) -> dict:
    path = snapshot_metadata_path(root, snapshot_id)
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать снимок {snapshot_id}: {exc}") from exc
    if metadata.get("schema_version") != 1 or metadata.get("snapshot_id") != snapshot_id:
        raise ValueError(f"Повреждён формат снимка: {path}")
    return metadata


def snapshot_ref(snapshot_id: str, side: str) -> str:
    return f"{ANALYTICS_SNAPSHOT_REF_PREFIX}/{snapshot_id}/{side}"


def create_analytics_snapshot(
    root: Path,
    analytics: Path,
    analytics_id: str,
    operation: str,
    incoming_commit: str,
    incoming_label: str,
) -> dict:
    local_commit = git(analytics, "rev-parse", "HEAD").stdout.strip()
    if not local_commit or not incoming_commit:
        raise ValueError("Не удалось определить стороны защитного снимка analytics")
    for label, commit in (("local", local_commit), ("incoming", incoming_commit)):
        valid = git(analytics, "cat-file", "-e", f"{commit}^{{commit}}")
        if valid.returncode != 0:
            raise ValueError(f"Коммит стороны {label} недоступен для защитного снимка: {commit}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    slug = re.sub(r"[^a-z0-9-]+", "-", operation.lower()).strip("-") or "update"
    snapshot_id = f"{stamp}-{slug}"
    counter = 1
    while snapshot_metadata_path(root, snapshot_id).exists():
        snapshot_id = f"{stamp}-{slug}-{counter}"
        counter += 1

    merge_base_result = git(analytics, "merge-base", local_commit, incoming_commit)
    base_commit = merge_base_result.stdout.strip() if merge_base_result.returncode == 0 else None
    refs = {
        "local": snapshot_ref(snapshot_id, "local"),
        "incoming": snapshot_ref(snapshot_id, "incoming"),
        "base": snapshot_ref(snapshot_id, "base") if base_commit else None,
    }
    for side, commit in (("local", local_commit), ("incoming", incoming_commit), ("base", base_commit)):
        if not commit:
            continue
        updated = git(analytics, "update-ref", refs[side], commit)
        if updated.returncode != 0:
            raise ValueError(f"Не удалось создать локальную ссылку снимка {side}: {updated.stderr.strip()}")

    metadata = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "created_at": utc_now(),
        "status": "prepared",
        "operation": operation,
        "analytics_repository": analytics_id,
        "analytics_path": str(analytics),
        "incoming_label": incoming_label,
        "commits": {
            "local": local_commit,
            "incoming": incoming_commit,
            "base": base_commit,
            "result": None,
        },
        "refs": {**refs, "result": None},
        "ancestry_verified": False,
        "ancestor_checks": {"local": None, "incoming": None},
        "conflicts": [],
        "restore_policy": {
            "automatic_restore": False,
            "requires_exact_path": True,
            "allowed_sides": ["base", "local", "incoming"],
            "stages_changes": False,
            "commits_changes": False,
        },
    }
    write_snapshot_metadata(root, metadata)
    return metadata


def matching_snapshot(
    root: Path,
    operation: str,
    local_commit: str,
    incoming_commit: str,
) -> dict | None:
    snapshots = analytics_snapshots_path(root)
    if not snapshots.is_dir():
        return None
    for path in sorted(snapshots.glob("*/snapshot.json"), reverse=True):
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        commits = metadata.get("commits", {})
        if (
            metadata.get("operation") == operation
            and commits.get("local") == local_commit
            and commits.get("incoming") == incoming_commit
            and metadata.get("status") in {"prepared", "conflict"}
        ):
            return metadata
    return None


def snapshot_summary(root: Path, metadata: dict) -> dict:
    return {
        "snapshot_id": metadata["snapshot_id"],
        "status": metadata["status"],
        "operation": metadata["operation"],
        "metadata": str(snapshot_metadata_path(root, metadata["snapshot_id"])),
        "commits": metadata["commits"],
        "refs": metadata["refs"],
        "conflicting_paths": [item["path"] for item in metadata.get("conflicts", [])],
        "automatic_restore": False,
    }


def read_blob(repository: Path, oid: str) -> bytes:
    result = subprocess.run(
        ("git", "-C", str(repository), "cat-file", "blob", oid),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"Не удалось сохранить Git-объект {oid} в защитный снимок")
    return result.stdout


def archive_snapshot_conflicts(
    root: Path,
    metadata: dict,
    repository: Path,
    conflicts: list[dict],
) -> dict:
    snapshot_root = snapshot_metadata_path(root, metadata["snapshot_id"]).parent
    archived = []
    for index, conflict in enumerate(conflicts, start=1):
        versions = {}
        blob_keys = {
            "base": "base_blob",
            "local": "local_analytics_blob",
            "incoming": "incoming_blob",
        }
        if "analytics_blob" in conflict:
            blob_keys["local"] = "analytics_blob"
        if "remote_analytics_blob" in conflict:
            blob_keys["incoming"] = "remote_analytics_blob"
        if "source_blob" in conflict:
            blob_keys["incoming"] = "source_blob"
        for side, key in blob_keys.items():
            oid = conflict.get(key)
            if not oid:
                versions[side] = None
                continue
            relative = Path("files") / f"{index:04d}" / side
            payload = read_blob(repository, oid)
            atomic_write(snapshot_root / relative, payload)
            versions[side] = {
                "blob": oid,
                "file": str(relative),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        archived.append({**conflict, "saved_versions": versions})
    metadata["status"] = "conflict"
    metadata["conflicts"] = archived
    write_snapshot_metadata(root, metadata)
    return metadata


def finalize_analytics_snapshot(
    root: Path,
    analytics: Path,
    metadata: dict,
    result_commit: str,
) -> dict:
    checks = {
        side: git(analytics, "merge-base", "--is-ancestor", commit, result_commit).returncode == 0
        for side, commit in (
            ("local", metadata["commits"]["local"]),
            ("incoming", metadata["commits"]["incoming"]),
        )
    }
    metadata["commits"]["result"] = result_commit
    metadata["refs"]["result"] = snapshot_ref(metadata["snapshot_id"], "result")
    metadata["ancestor_checks"] = checks
    metadata["ancestry_verified"] = all(checks.values())
    metadata["status"] = "completed" if metadata["ancestry_verified"] else "invalid-result"
    updated = git(analytics, "update-ref", metadata["refs"]["result"], result_commit)
    if updated.returncode != 0:
        raise ValueError(f"Не удалось сохранить результат защитного снимка: {updated.stderr.strip()}")
    write_snapshot_metadata(root, metadata)
    if not metadata["ancestry_verified"]:
        raise ValueError(
            "Результат обновления analytics не содержит обе исходные линии; "
            f"защитный снимок {metadata['snapshot_id']} сохранён, дальнейшая работа остановлена"
        )
    return metadata


def merge_head(path: Path) -> str | None:
    result = git(path, "rev-parse", "--verify", "MERGE_HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def require_no_active_analytics_merge(root: Path, path: Path, analytics_id: str) -> None:
    incoming_commit = merge_head(path)
    if not incoming_commit:
        return
    conflicts = analytics_origin_conflict_records(path)
    local_commit = git(path, "rev-parse", "HEAD").stdout.strip()
    snapshot = matching_snapshot(
        root,
        "analytics-origin-merge",
        local_commit,
        incoming_commit,
    ) or create_analytics_snapshot(
        root,
        path,
        analytics_id,
        "analytics-origin-merge",
        incoming_commit,
        "MERGE_HEAD",
    )
    snapshot = archive_snapshot_conflicts(root, snapshot, path, conflicts)
    raise ValueError(json.dumps({
        "status": "blocked",
        "reason": "analytics-origin-merge-in-progress",
        "message": (
            f"В {analytics_id} (analytics) уже выполняется слияние. Обвязка не отменяет и не перезапускает его; "
            "требуется осознанно разрешить текущие конфликты"
        ),
        "incoming_commit": incoming_commit,
        "conflicting_paths": [item["path"] for item in conflicts],
        "protective_snapshot": snapshot_summary(root, snapshot),
        "allowed_next_action": "inspect-analytics-origin-conflict",
        "forbidden_actions": [
            "start-another-pull-or-merge",
            "git-reset",
            "git-rebase",
            "force-push",
            "git-add-all",
        ],
    }, ensure_ascii=False))


def update_analytics_from_origin(root: Path, path: Path, analytics_id: str) -> dict:
    name = f"{analytics_id} (analytics)"
    fetched = git(path, "fetch", "origin", BRANCH)
    if fetched.returncode != 0:
        raise ValueError(f"{name}: fetch завершился ошибкой: {fetched.stderr.strip()}")
    local_commit = git(path, "rev-parse", "HEAD").stdout.strip()
    remote_commit = git(path, "rev-parse", f"origin/{BRANCH}").stdout.strip()
    if not local_commit or not remote_commit:
        raise ValueError(f"{name}: не удалось определить локальный или удалённый коммит")
    if local_commit == remote_commit:
        return {
            "status": "current",
            "before": local_commit,
            "remote": remote_commit,
            "after": local_commit,
        }
    local_is_ancestor = git(path, "merge-base", "--is-ancestor", local_commit, remote_commit).returncode == 0
    remote_is_ancestor = git(path, "merge-base", "--is-ancestor", remote_commit, local_commit).returncode == 0
    if local_is_ancestor:
        snapshot = create_analytics_snapshot(
            root, path, analytics_id, "analytics-origin-fast-forward", remote_commit, f"origin/{BRANCH}"
        )
        merged = git(path, "merge", "--ff-only", f"origin/{BRANCH}")
        if merged.returncode != 0:
            raise ValueError(f"{name}: fast-forward завершился ошибкой: {merged.stderr.strip()}")
        snapshot = finalize_analytics_snapshot(root, path, snapshot, remote_commit)
        return {
            "status": "fast-forwarded",
            "before": local_commit,
            "remote": remote_commit,
            "after": remote_commit,
            "protective_snapshot": snapshot_summary(root, snapshot),
        }
    if remote_is_ancestor:
        return {
            "status": "local-ahead",
            "before": local_commit,
            "remote": remote_commit,
            "after": local_commit,
        }

    snapshot = create_analytics_snapshot(
        root, path, analytics_id, "analytics-origin-merge", remote_commit, f"origin/{BRANCH}"
    )
    merged = git(
        path,
        "-c", "user.name=Coda Analyst Harness",
        "-c", "user.email=coda-analyst-harness@local.invalid",
        "merge", "--no-ff", f"origin/{BRANCH}",
        "-m", f"Merge origin/{BRANCH}",
    )
    if merged.returncode != 0:
        conflicts = analytics_origin_conflict_records(path)
        snapshot = archive_snapshot_conflicts(root, snapshot, path, conflicts)
        aborted = git(path, "merge", "--abort")
        if aborted.returncode != 0:
            raise ValueError(
                f"{name}: конфликт сохранён в снимке {snapshot['snapshot_id']}, "
                f"но пробное слияние не удалось отменить: {aborted.stderr.strip()}"
            )
        raise ValueError(json.dumps({
            "status": "blocked",
            "reason": "analytics-origin-merge-conflict",
            "message": (
                "Локальные и удалённые коммиты роли analytics нельзя объединить автоматически; "
                "пробное слияние отменено без изменения рабочего дерева"
            ),
            "local_commit": local_commit,
            "remote_commit": remote_commit,
            "conflicting_paths": [item["path"] for item in conflicts],
            "protective_snapshot": snapshot_summary(root, snapshot),
            "allowed_next_action": "inspect-analytics-origin-conflict",
            "forbidden_actions": [
                "git-reset",
                "git-rebase",
                "force-push",
                "discard-local-history",
                "discard-remote-history",
                "git-add-all",
            ],
        }, ensure_ascii=False))
    after = git(path, "rev-parse", "HEAD").stdout.strip()
    snapshot = finalize_analytics_snapshot(root, path, snapshot, after)
    return {
        "status": "merged",
        "before": local_commit,
        "remote": remote_commit,
        "after": after,
        "protective_snapshot": snapshot_summary(root, snapshot),
    }


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


def merge_source(root: Path, documents: Path, analytics_id: str) -> dict:
    local_commit = git(documents, "rev-parse", "HEAD").stdout.strip()
    incoming_commit = git(documents, "rev-parse", f"{SOURCE_REMOTE}/{BRANCH}").stdout.strip()
    if git(documents, "merge-base", "--is-ancestor", incoming_commit, local_commit).returncode == 0:
        return {
            "status": "already-contained",
            "before": local_commit,
            "incoming": incoming_commit,
            "after": local_commit,
            "protective_snapshot": None,
        }
    snapshot = create_analytics_snapshot(
        root,
        documents,
        analytics_id,
        "source-analytics-merge",
        incoming_commit,
        f"{SOURCE_REMOTE}/{BRANCH}",
    )
    merged = git(
        documents,
        "-c", "user.name=Coda Analyst Harness",
        "-c", "user.email=coda-analyst-harness@local.invalid",
        "merge", "--no-ff", f"{SOURCE_REMOTE}/{BRANCH}",
        "-m", f"Merge {SOURCE_REMOTE}/{BRANCH}",
    )
    if merged.returncode != 0:
        paths = [
            line.strip()
            for line in git(documents, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
            if line.strip()
        ]
        conflicts = []
        for path in paths:
            stages = conflict_stages(documents, path)
            conflicts.append({
                "path": path,
                "kind": "source-analytics-conflict",
                "base_blob": stages.get(1),
                "analytics_blob": stages.get(2),
                "source_blob": stages.get(3),
            })
        snapshot = archive_snapshot_conflicts(root, snapshot, documents, conflicts)
        aborted = git(documents, "merge", "--abort")
        if aborted.returncode != 0:
            raise ValueError(
                f"Конфликт сохранён в снимке {snapshot['snapshot_id']}, "
                f"но слияние source не удалось отменить: {aborted.stderr.strip()}"
            )
        raise ValueError(json.dumps({
            "status": "blocked",
            "reason": "source-analytics-merge-conflict",
            "message": (
                "Роль source нельзя автоматически объединить с ролью analytics; "
                "слияние отменено, требуется осознанное разрешение конфликта"
            ),
            "conflicting_paths": paths,
            "protective_snapshot": snapshot_summary(root, snapshot),
            "allowed_next_action": "inspect-source-analytics-conflict",
            "forbidden_alternatives": [
                "repeat-code-update-as-fallback",
                "skip-source-merge",
                "overwrite-analytics-from-source",
            ],
        }, ensure_ascii=False))
    after = git(documents, "rev-parse", "HEAD").stdout.strip()
    snapshot = finalize_analytics_snapshot(root, documents, snapshot, after)
    return {
        "status": "merged",
        "before": local_commit,
        "incoming": incoming_commit,
        "after": after,
        "protective_snapshot": snapshot_summary(root, snapshot),
    }


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


def analytics_origin_conflict_records(repository: Path) -> list[dict]:
    paths = [
        line.strip()
        for line in git(repository, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
        if line.strip()
    ]
    conflicts = []
    for path in paths:
        stages = conflict_stages(repository, path)
        local_present = 2 in stages
        remote_present = 3 in stages
        if local_present and remote_present:
            kind = "both-modified"
        elif local_present:
            kind = "remote-deleted-local-modified"
        elif remote_present:
            kind = "local-deleted-remote-modified"
        else:
            kind = "complex"
        conflicts.append({
            "path": path,
            "kind": kind,
            "base_blob": stages.get(1),
            "local_analytics_blob": stages.get(2),
            "remote_analytics_blob": stages.get(3),
            "recommended_resolution": "analyst-decision-required",
        })
    return conflicts


def inspect_analytics_origin_conflict_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    handle = lock(root)
    try:
        analytics, analytics_id = analytics_repository(root)
        require_branch(analytics, f"{analytics_id} (analytics)")
        local_commit = git(analytics, "rev-parse", "HEAD").stdout.strip()
        active_merge = merge_head(analytics)
        if active_merge:
            conflicts = analytics_origin_conflict_records(analytics)
            snapshot = matching_snapshot(
                root, "analytics-origin-merge", local_commit, active_merge
            ) or create_analytics_snapshot(
                root,
                analytics,
                analytics_id,
                "analytics-origin-merge",
                active_merge,
                "MERGE_HEAD",
            )
            snapshot = archive_snapshot_conflicts(root, snapshot, analytics, conflicts)
            print(json.dumps({
                "status": "conflict-inspected",
                "reason": "analytics-origin-merge-in-progress",
                "analytics": {"repository": analytics_id, "local_commit": local_commit},
                "incoming_commit": active_merge,
                "existing_merge_in_progress": True,
                "inspection_changed_repository": False,
                "protective_snapshot": snapshot_summary(root, snapshot),
                "conflicts": conflicts,
                "next_step": (
                    "Для каждого пути запросить решение аналитика по одному; затем изменить только этот путь "
                    "и выполнить git add -- <точный-путь>. После разрешения всех конфликтов запустить проверки, "
                    "создать merge-коммит и повторить workspace.py sync."
                ),
                "forbidden_actions": ["git add -A", "git add .", "git reset", "git rebase", "force push"],
            }, ensure_ascii=False, indent=2))
            return 0

        require_clean(analytics, f"{analytics_id} (analytics)")
        remote_url_result = git(analytics, "remote", "get-url", "origin")
        if remote_url_result.returncode != 0 or not remote_url_result.stdout.strip():
            raise ValueError(f"{analytics_id}: не удалось определить удалённый адрес analytics")
        remote_url = remote_url_result.stdout.strip()
        with tempfile.TemporaryDirectory(prefix="coda-analyst-origin-conflict-") as temporary:
            probe = Path(temporary) / "analytics"
            cloned = run("git", "clone", "--quiet", "--no-hardlinks", str(analytics), str(probe))
            if cloned.returncode != 0:
                raise ValueError(f"Не удалось создать временную копию analytics: {cloned.stderr.strip()}")
            added = git(probe, "remote", "add", "analytics-upstream", remote_url)
            if added.returncode != 0:
                raise ValueError(f"Не удалось подключить удалённый analytics: {added.stderr.strip()}")
            fetched = git(probe, "fetch", "--quiet", "analytics-upstream", BRANCH)
            if fetched.returncode != 0:
                raise ValueError(f"Не удалось получить удалённый analytics: {fetched.stderr.strip()}")
            remote_commit = git(probe, "rev-parse", "FETCH_HEAD").stdout.strip()
            merged = git(
                probe,
                "-c", "user.name=Coda Analyst Harness",
                "-c", "user.email=coda-analyst-harness@local.invalid",
                "merge", "--no-commit", "--no-ff", "FETCH_HEAD",
            )
            if merged.returncode == 0:
                print(json.dumps({
                    "status": "no-conflict",
                    "reason": "analytics-origin-conflict-not-reproduced",
                    "analytics": {
                        "repository": analytics_id,
                        "local_commit": local_commit,
                        "remote_commit": remote_commit,
                    },
                    "existing_merge_in_progress": False,
                    "inspection_changed_repository": False,
                    "conflicts": [],
                    "message": "Конфликт не воспроизводится; повтори workspace.py sync",
                }, ensure_ascii=False, indent=2))
                return 0
            conflicts = analytics_origin_conflict_records(probe)
            snapshot = matching_snapshot(
                root, "analytics-origin-merge", local_commit, remote_commit
            ) or create_analytics_snapshot(
                root,
                analytics,
                analytics_id,
                "analytics-origin-merge",
                remote_commit,
                f"origin/{BRANCH}",
            )
            snapshot = archive_snapshot_conflicts(root, snapshot, probe, conflicts)

        print(json.dumps({
            "status": "conflict-inspected",
            "reason": "analytics-origin-merge-conflict",
            "analytics": {
                "repository": analytics_id,
                "local_commit": local_commit,
                "remote_commit": remote_commit,
            },
            "existing_merge_in_progress": False,
            "inspection_changed_repository": False,
            "protective_snapshot": snapshot_summary(root, snapshot),
            "conflicts": conflicts,
            "next_step": (
                "Запросить решение аналитика для каждого пути по одному. После решений начать обычный merge "
                "origin/main в analytics/main, применить решения только к точным путям, запустить проверки, "
                "создать merge-коммит и повторить workspace.py sync."
            ),
            "forbidden_actions": ["git add -A", "git add .", "git reset", "git rebase", "force push"],
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        handle.close()


def current_branch(repository: Path) -> str:
    result = git(repository, "symbolic-ref", "--quiet", "--short", "HEAD")
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError("Репозиторий analytics находится вне именованной ветки")
    return result.stdout.strip()


def protect_active_feature_merge(
    root: Path,
    analytics: Path,
    analytics_id: str,
    branch: str,
) -> None:
    incoming_commit = merge_head(analytics)
    if not incoming_commit:
        return
    local_commit = git(analytics, "rev-parse", "HEAD").stdout.strip()
    conflicts = analytics_origin_conflict_records(analytics)
    snapshot = matching_snapshot(
        root, "feature-main-merge", local_commit, incoming_commit
    ) or create_analytics_snapshot(
        root,
        analytics,
        analytics_id,
        "feature-main-merge",
        incoming_commit,
        f"origin/{BRANCH}",
    )
    snapshot = archive_snapshot_conflicts(root, snapshot, analytics, conflicts)
    raise ValueError(json.dumps({
        "status": "blocked",
        "reason": "feature-main-merge-in-progress",
        "branch": branch,
        "incoming_commit": incoming_commit,
        "conflicting_paths": [item["path"] for item in conflicts],
        "protective_snapshot": snapshot_summary(root, snapshot),
        "allowed_next_action": "разрешить каждый конфликтующий путь по отдельному решению аналитика",
        "forbidden_actions": ["git add -A", "git add .", "git reset", "git rebase", "force push"],
    }, ensure_ascii=False))


def update_feature_branch_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    handle = lock(root)
    try:
        analytics, analytics_id = analytics_repository(root)
        branch = current_branch(analytics)
        if not branch.startswith("feature/"):
            raise ValueError("Обновление рабочей ветки разрешено только для ветки feature/<feature>/<analyst>")
        protect_active_feature_merge(root, analytics, analytics_id, branch)
        require_clean(analytics, f"{analytics_id} (analytics)")
        fetched = git(analytics, "fetch", "origin", BRANCH)
        if fetched.returncode != 0:
            raise ValueError(f"Не удалось получить origin/{BRANCH}: {fetched.stderr.strip()}")
        local_commit = git(analytics, "rev-parse", "HEAD").stdout.strip()
        incoming_commit = git(analytics, "rev-parse", f"origin/{BRANCH}").stdout.strip()
        if git(analytics, "merge-base", "--is-ancestor", incoming_commit, local_commit).returncode == 0:
            print(json.dumps({
                "status": "current",
                "branch": branch,
                "before": local_commit,
                "incoming": incoming_commit,
                "after": local_commit,
                "protective_snapshot": None,
            }, ensure_ascii=False, indent=2))
            return 0

        operation = (
            "feature-main-fast-forward"
            if git(analytics, "merge-base", "--is-ancestor", local_commit, incoming_commit).returncode == 0
            else "feature-main-merge"
        )
        snapshot = create_analytics_snapshot(
            root,
            analytics,
            analytics_id,
            operation,
            incoming_commit,
            f"origin/{BRANCH}",
        )
        if operation == "feature-main-fast-forward":
            merged = git(analytics, "merge", "--ff-only", f"origin/{BRANCH}")
        else:
            merge_message = f"Merge origin/{BRANCH} into {branch}"
            require_valid_commit_message(merge_message)
            merged = git(
                analytics,
                "-c", "user.name=Coda Analyst Harness",
                "-c", "user.email=coda-analyst-harness@local.invalid",
                "merge", "--no-ff", f"origin/{BRANCH}",
                "-m", merge_message,
            )
        if merged.returncode != 0:
            conflicts = analytics_origin_conflict_records(analytics)
            snapshot = archive_snapshot_conflicts(root, snapshot, analytics, conflicts)
            aborted = git(analytics, "merge", "--abort")
            if aborted.returncode != 0:
                raise ValueError(
                    f"Конфликт сохранён в снимке {snapshot['snapshot_id']}, "
                    f"но слияние рабочей ветки не удалось отменить: {aborted.stderr.strip()}"
                )
            raise ValueError(json.dumps({
                "status": "blocked",
                "reason": "feature-main-merge-conflict",
                "branch": branch,
                "conflicting_paths": [item["path"] for item in conflicts],
                "protective_snapshot": snapshot_summary(root, snapshot),
                "allowed_next_action": "запросить у аналитика решение по одному пути",
                "forbidden_actions": ["git add -A", "git add .", "git reset", "git rebase", "force push"],
            }, ensure_ascii=False))
        after = git(analytics, "rev-parse", "HEAD").stdout.strip()
        snapshot = finalize_analytics_snapshot(root, analytics, snapshot, after)
        require_clean(analytics, f"{analytics_id} (analytics)")
        print(json.dumps({
            "status": "fast-forwarded" if operation.endswith("fast-forward") else "merged",
            "branch": branch,
            "before": local_commit,
            "incoming": incoming_commit,
            "after": after,
            "protective_snapshot": snapshot_summary(root, snapshot),
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        handle.close()


def fast_forward_analytics_main_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    handle = lock(root)
    try:
        analytics, analytics_id = analytics_repository(root)
        require_branch(analytics, f"{analytics_id} (analytics)")
        require_no_active_analytics_merge(root, analytics, analytics_id)
        require_clean(analytics, f"{analytics_id} (analytics)")
        fetched = git(analytics, "fetch", "origin", BRANCH)
        if fetched.returncode != 0:
            raise ValueError(f"Не удалось получить origin/{BRANCH}: {fetched.stderr.strip()}")
        local_commit = git(analytics, "rev-parse", "HEAD").stdout.strip()
        incoming_commit = git(analytics, "rev-parse", f"origin/{BRANCH}").stdout.strip()
        if local_commit == incoming_commit:
            print(json.dumps({
                "status": "current",
                "before": local_commit,
                "incoming": incoming_commit,
                "after": local_commit,
                "protective_snapshot": None,
            }, ensure_ascii=False, indent=2))
            return 0
        if git(analytics, "merge-base", "--is-ancestor", local_commit, incoming_commit).returncode != 0:
            raise ValueError(
                "Локальная main не является предком origin/main; быстрое обновление запрещено. "
                "Сохрани отдельную линию в рабочей ветке через миграцию"
            )
        snapshot = create_analytics_snapshot(
            root,
            analytics,
            analytics_id,
            "analytics-main-fast-forward",
            incoming_commit,
            f"origin/{BRANCH}",
        )
        merged = git(analytics, "merge", "--ff-only", f"origin/{BRANCH}")
        if merged.returncode != 0:
            raise ValueError(f"Быстрое обновление main завершилось ошибкой: {merged.stderr.strip()}")
        snapshot = finalize_analytics_snapshot(root, analytics, snapshot, incoming_commit)
        print(json.dumps({
            "status": "fast-forwarded",
            "before": local_commit,
            "incoming": incoming_commit,
            "after": incoming_commit,
            "protective_snapshot": snapshot_summary(root, snapshot),
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        handle.close()


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
            merged = git(
                probe,
                "-c", "user.name=Coda Analyst Harness",
                "-c", "user.email=coda-analyst-harness@local.invalid",
                "merge", "--no-commit", "--no-ff", "FETCH_HEAD",
            )
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

            snapshot = matching_snapshot(
                root, "source-analytics-merge", analytics_commit, source_commit
            ) or create_analytics_snapshot(
                root,
                documents,
                analytics_id,
                "source-analytics-merge",
                source_commit,
                f"{SOURCE_REMOTE}/{BRANCH}",
            )
            snapshot = archive_snapshot_conflicts(root, snapshot, probe, conflicts)

        print(json.dumps({
            "status": "conflict-inspected",
            "source": {"repository": source_id, "commit": source_commit},
            "analytics": {"repository": analytics_id, "commit": analytics_commit},
            "real_repositories_changed": False,
            "protective_snapshot": snapshot_summary(root, snapshot),
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

    diff_check = git(
        analytics,
        "diff", "--check", source_commit, documents_commit, "--", ".",
    )
    if diff_check.returncode != 0:
        detail = diff_check.stdout.strip() or diff_check.stderr.strip()
        raise ValueError(
            "Обратная заплата содержит ошибки пробельного оформления; "
            "исправь их в роли analytics и повтори синхронизацию:\n"
            f"{detail}"
        )

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
            checked = git(
                source,
                "apply", "--cached", "--check", "--binary", "--whitespace=error-all",
                str(verification_patch),
                env=environment,
            )
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
    revisions = git(
        analytics,
        "rev-list", "--reverse", "--topo-order", f"{source_commit}..{documents_commit}",
    )
    if revisions.returncode != 0:
        raise ValueError(f"Не удалось получить состав коммитов обратной заплаты: {revisions.stderr.strip()}")
    included_analytics_commits = []
    for commit in [item for item in revisions.stdout.splitlines() if item]:
        subject = git(analytics, "show", "-s", "--format=%s", commit)
        if subject.returncode != 0:
            raise ValueError(f"Не удалось прочитать коммит {commit}: {subject.stderr.strip()}")
        included_analytics_commits.append({"commit": commit, "subject": subject.stdout.strip()})
    included_features = sorted({
        parts[1]
        for path in changed_paths
        if len(parts := PurePosixPath(path).parts) >= 2 and parts[0] == "features"
    })
    approved_deletions = deleted_source_paths(analytics, source_commit, documents_commit)
    metadata = {
        "schema_version": 2,
        "artifact_id": timestamp,
        "created_at": utc_now(),
        "source_repository": source_id,
        "analytics_repository": analytics_id,
        "source_branch": BRANCH,
        "analytics_branch": BRANCH,
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
        "included_features": included_features,
        "included_analytics_commits": included_analytics_commits,
        "approved_source_deletions": approved_deletions,
        "diff_check_verified": True,
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
        require_no_active_analytics_merge(root, documents, analytics_id)
        unicode_aliases = prepare_unicode_aliases(documents, source, source_commit)
        if not unicode_aliases:
            require_clean(documents, f"{analytics_id} (analytics)")
        require_branch(documents, f"{analytics_id} (analytics)")
        analytics_origin_update = update_analytics_from_origin(root, documents, analytics_id)
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
        source_merge = merge_source(root, documents, analytics_id)
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
            "analytics_origin_update": analytics_origin_update,
            "source_merge": source_merge,
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
        "source_branch": BRANCH,
        "analytics_branch": BRANCH,
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
        "included_features": [],
        "included_analytics_commits": [],
        "approved_source_deletions": [],
        "diff_check_verified": False,
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
        require_no_active_analytics_merge(root, analytics, analytics_id)
        require_clean(analytics, f"{analytics_id} (analytics)")
        require_branch(analytics, f"{analytics_id} (analytics)")
        analytics_origin_update = update_analytics_from_origin(root, analytics, analytics_id)
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
            "analytics_origin_update": analytics_origin_update,
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


def exact_repository_path(value: str) -> str:
    if not value or "\\" in value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("Путь восстановления должен быть точным относительным путём Git")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise ValueError("Путь восстановления не должен выходить за пределы analytics")
    normalized = parsed.as_posix()
    if normalized != value:
        raise ValueError("Путь восстановления должен быть записан в канонической форме")
    return normalized


def list_analytics_snapshots_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    snapshots = []
    directory = analytics_snapshots_path(root)
    if directory.is_dir():
        for path in sorted(directory.glob("*/snapshot.json"), reverse=True):
            try:
                metadata = load_snapshot_metadata(root, path.parent.name)
            except ValueError:
                continue
            snapshots.append(snapshot_summary(root, metadata))
    print(json.dumps({
        "status": "ok",
        "snapshot_count": len(snapshots),
        "snapshots": snapshots,
    }, ensure_ascii=False, indent=2))
    return 0


def inspect_analytics_snapshot_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    metadata = load_snapshot_metadata(root, args.snapshot)
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return 0


def restore_analytics_snapshot_file_command(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    handle = lock(root)
    try:
        analytics, analytics_id = analytics_repository(root)
        require_no_active_analytics_merge(root, analytics, analytics_id)
        require_clean(analytics, f"{analytics_id} (analytics)")
        require_branch(analytics, f"{analytics_id} (analytics)")
        metadata = load_snapshot_metadata(root, args.snapshot)
        if metadata.get("analytics_repository") != analytics_id:
            raise ValueError("Снимок относится к другому репозиторию роли analytics")
        side = args.side
        commit = metadata.get("commits", {}).get(side)
        reference = metadata.get("refs", {}).get(side)
        if not commit or not reference:
            raise ValueError(f"В снимке отсутствует сторона {side}")
        actual = git(analytics, "rev-parse", "--verify", reference)
        if actual.returncode != 0 or actual.stdout.strip() != commit:
            raise ValueError(f"Локальная Git-ссылка стороны {side} отсутствует или изменена")

        relative = exact_repository_path(args.path)
        snapshot_commits = [
            item
            for key in ("base", "local", "incoming")
            if (item := metadata.get("commits", {}).get(key))
        ]
        path_was_present = any(
            git(analytics, "cat-file", "-e", f"{item}:{relative}").returncode == 0
            for item in snapshot_commits
        )
        if not path_was_present:
            raise ValueError(
                "Точный путь отсутствует во всех сторонах снимка; "
                "восстановление или удаление по нему запрещено"
            )
        target = analytics / relative
        target_parent = target.parent.resolve()
        try:
            target_parent.relative_to(analytics.resolve())
        except ValueError as exc:
            raise ValueError("Родительский каталог пути выходит за пределы analytics") from exc

        exists = git(analytics, "cat-file", "-e", f"{commit}:{relative}")
        if exists.returncode != 0:
            if target.is_dir() and not target.is_symlink():
                raise ValueError(f"Нельзя точечно удалить каталог при восстановлении файла: {relative}")
            target.unlink(missing_ok=True)
            action = "deleted-to-match-snapshot"
        else:
            object_type = git(analytics, "cat-file", "-t", f"{commit}:{relative}")
            if object_type.returncode != 0 or object_type.stdout.strip() != "blob":
                raise ValueError(f"Снимок содержит не обычный файл по пути: {relative}")
            tree_entry = git(analytics, "ls-tree", commit, "--", relative)
            fields = tree_entry.stdout.split(None, 3)
            if tree_entry.returncode != 0 or len(fields) < 4 or fields[0] not in {"100644", "100755"}:
                raise ValueError(f"Поддерживается восстановление только обычных файлов: {relative}")
            payload = subprocess.run(
                ("git", "-C", str(analytics), "show", f"{commit}:{relative}"),
                capture_output=True,
                check=False,
            )
            if payload.returncode != 0:
                raise ValueError(f"Не удалось прочитать файл из снимка: {relative}")
            atomic_write(target, payload.stdout)
            target.chmod(0o755 if fields[0] == "100755" else 0o644)
            action = "restored"

        print(json.dumps({
            "status": "file-restored-from-snapshot",
            "snapshot_id": metadata["snapshot_id"],
            "side": side,
            "commit": commit,
            "path": relative,
            "action": action,
            "staged": False,
            "committed": False,
            "next_step": "проверить точечное изменение, затем отдельно решить, фиксировать ли его",
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
    inspect_analytics_origin = commands.add_parser("inspect-analytics-origin-conflict")
    inspect_analytics_origin.set_defaults(handler=inspect_analytics_origin_conflict_command)
    update_feature = commands.add_parser("update-feature-branch")
    update_feature.set_defaults(handler=update_feature_branch_command)
    fast_forward_main = commands.add_parser("fast-forward-analytics-main")
    fast_forward_main.set_defaults(handler=fast_forward_analytics_main_command)
    snapshots = commands.add_parser("list-analytics-snapshots")
    snapshots.set_defaults(handler=list_analytics_snapshots_command)
    inspect_snapshot = commands.add_parser("inspect-analytics-snapshot")
    inspect_snapshot.add_argument("--snapshot", required=True, help="Идентификатор защитного снимка")
    inspect_snapshot.set_defaults(handler=inspect_analytics_snapshot_command)
    restore_snapshot = commands.add_parser("restore-analytics-snapshot-file")
    restore_snapshot.add_argument("--snapshot", required=True, help="Идентификатор защитного снимка")
    restore_snapshot.add_argument("--side", choices=("base", "local", "incoming"), required=True)
    restore_snapshot.add_argument("--path", required=True, help="Точный относительный путь в analytics")
    restore_snapshot.set_defaults(handler=restore_analytics_snapshot_file_command)
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
