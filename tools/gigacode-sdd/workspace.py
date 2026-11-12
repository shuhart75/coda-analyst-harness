"""Git isolation and review evidence for local SDD execution (stdlib only).

These checks protect orchestration invariants, not arbitrary hostile code execution.
Workers must additionally receive appropriate filesystem/network restrictions.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import subprocess
import tempfile


class WorkspaceError(RuntimeError):
    pass


def _run(root, args, *, env=None, data=None, base_env=None):
    merged = dict(os.environ if base_env is None else base_env)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        merged.pop(key, None)
    if env:
        merged.update(env)
    merged.setdefault("GIT_OPTIONAL_LOCKS", "0")
    result = subprocess.run(["git", "-C", str(root), *args], input=data,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=merged)
    if result.returncode:
        raise WorkspaceError(f"git {args[0]} failed ({result.returncode}): "
                             + result.stderr.decode("utf-8", "replace")[-2000:])
    return result.stdout


def git(root, *args):
    return _run(root, list(args)).decode("utf-8", "surrogateescape").strip()


def _names(raw):
    return [x.decode("utf-8", "surrogateescape") for x in raw.split(b"\0") if x]


def _secret(path):
    name = PurePosixPath(path).name.lower()
    return ((".gigacode" in PurePosixPath(path).parts and name == "settings.json")
            or name in {".npmrc", ".pypirc", "settings-security.xml", "pg.env", "pg-super.env"}
            or name == ".env" or name.startswith(".env.")
            or name.endswith((".pem", ".key", ".p12", ".pfx", ".jks"))
            or name.startswith((".npmrc-", ".nexus-"))
            or name in {"m2-settings-stand.xml", "credentials.json"})


def _forbidden(path, allow_input=False):
    parts = PurePosixPath(path).parts
    if not parts or path.startswith("/") or ".." in parts or "\\" in path or PureWindowsPath(path).drive:
        return True
    if any(p in {".git", ".gigacode", ".codex", ".gigaide", ".idea"} for p in parts):
        return True
    if any(p in {"GIGACODE.md", "AGENTS.md"} for p in parts) or _secret(path):
        return True
    if "requirements-exchange" in parts and not allow_input:
        tail = parts[parts.index("requirements-exchange") + 1:]
        if "returns" not in tail:
            return True
    return False


def _safe_path(root, relative):
    rel = PurePosixPath(relative)
    if (not rel.parts or rel.is_absolute() or ".." in rel.parts or ".git" in rel.parts
            or "\\" in relative or PureWindowsPath(relative).drive):
        raise WorkspaceError(f"Unsafe path: {relative}")
    root = Path(root).resolve()
    path = root.joinpath(*rel.parts)
    try:
        path.resolve().relative_to(root)
    except ValueError:
        raise WorkspaceError(f"Path escapes workspace: {relative}") from None
    for parent in path.parents:
        if parent == root:
            break
        if (parent / ".git").exists():
            raise WorkspaceError(f"Embedded repository: {relative}")
    return path


def _tracked(root):
    raw = _run(root, ["ls-files", "--stage", "-z"])
    paths = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, _, stage = meta.split()
        if mode == b"160000":
            raise WorkspaceError("Submodules are not supported in managed SDD snapshots")
        if stage != b"0":
            raise WorkspaceError("Resolve the existing Git conflict before snapshotting")
        paths.append(path.decode("utf-8", "surrogateescape"))
    return paths


def _instruction_paths(root):
    """Only local instruction artifacts, never local permissions or auth settings."""
    root = Path(root)
    result = set()
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in {
            ".git", "node_modules", "target", "dist", ".venv", "venv", "build",
            "pgdata", "pgsql", "m2repo", "npm-cache", ".m2", ".npm", "logs", "backups"}]
        current = Path(current)
        for name in ("GIGACODE.md", "AGENTS.md"):
            if name in files:
                result.add((current / name).relative_to(root).as_posix())
        if current.name == ".gigacode":
            dirs[:] = [d for d in dirs if d in {"commands", "skills", "agents", "context"}]
        if ".gigacode" in current.relative_to(root).parts:
            parts = current.relative_to(root).parts
            pos = parts.index(".gigacode")
            if len(parts) > pos + 1 and parts[pos + 1] in {"commands", "skills", "agents", "context"}:
                result.update((current / n).relative_to(root).as_posix()
                              for n in files if not _secret(n))
    return result


def _settings_paths(root):
    # Permission state is part of the before/after evidence even when Git ignores it.
    paths = set()
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in {
            ".git", "node_modules", "target", "dist", ".venv", "venv", "build",
            "pgdata", "pgsql", "m2repo", "npm-cache", ".m2", ".npm", "logs", "backups"}]
        if Path(current).name == ".gigacode":
            if "settings.json" in files:
                paths.add((Path(current) / "settings.json").relative_to(root).as_posix())
            dirs[:] = []
    return paths


def snapshot(root, *, normalize_settings=False):
    root = Path(root).resolve()
    paths = set(_tracked(root))
    paths.update(_names(_run(root, ["ls-files", "--others", "--exclude-standard", "-z"])))
    paths.update(_instruction_paths(root))
    paths.update(_settings_paths(root))
    files = {}
    for relative in sorted(paths):
        path = _safe_path(root, relative)
        if not path.exists() and not path.is_symlink():
            files[relative] = {"missing": True}
            continue
        info = path.lstat()
        if ".gigacode" in PurePosixPath(relative).parts and path.name == "settings.json":
            raw = path.read_bytes()
            if normalize_settings:
                from policy import _settings
                try:
                    _, settings = _settings(path)
                except ValueError as error:
                    raise WorkspaceError(str(error)) from None
                # CLI serialization metadata is not a permission change.
                # All other keys, values and list ordering remain significant.
                settings.pop("$version", None)
                raw = json.dumps(settings, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8")
            files[relative] = {"sha256": hashlib.sha256(raw).hexdigest(),
                               "mode": stat.S_IMODE(info.st_mode)}
        elif _secret(relative):
            # Detect changes without reading or logging credential contents.
            files[relative] = {"redacted": True, "size": info.st_size,
                               "mtime_ns": info.st_mtime_ns, "mode": stat.S_IMODE(info.st_mode)}
        elif path.is_symlink():
            files[relative] = {"symlink": os.readlink(path)}
        elif path.is_file():
            files[relative] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                               "mode": stat.S_IMODE(info.st_mode)}
        else:
            raise WorkspaceError(f"Not an ordinary file: {relative}")
    return {"head": git(root, "rev-parse", "HEAD"),
            "status": _run(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"]).decode("utf-8", "surrogateescape"),
            "files": files}


def _changed(root, base):
    names = set(_names(_run(root, ["diff", "--name-only", "--no-renames", "-z", base, "--"])))
    names.update(_names(_run(root, ["ls-files", "--others", "--exclude-standard", "-z"])))
    return sorted(names)


def _allowed(relative, owners):
    return any(relative == owner.rstrip("/") or relative.startswith(owner.rstrip("/") + "/")
               for owner in owners)


def _write_patch(root, base, paths, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sdd-index-") as temp:
        env = {"GIT_INDEX_FILE": str(Path(temp) / "index")}
        _run(root, ["read-tree", base], env=env)
        if paths:
            # Stage only owned paths into an isolated index; real index is unchanged.
            _run(root, ["add", "-A", "--", *paths], env=env)
        patch = _run(root, ["diff", "--cached", "--binary", "--full-index", "--no-ext-diff",
                            "--no-renames", base, "--"], env=env)
    output.write_bytes(patch)
    output.chmod(0o600)
    return patch


def export_patch(root, base, allowed_paths, output):
    root = Path(root).resolve()
    expected = git(root, "rev-parse", f"{base}^{{commit}}")
    if git(root, "rev-parse", "HEAD") != expected:
        raise WorkspaceError("Worker changed HEAD; patch export refused")
    if not allowed_paths or any(not isinstance(p, str) or p in {"", ".", "/"}
                                or ".." in PurePosixPath(p).parts
                                or PurePosixPath(p).is_absolute() or "\\" in p
                                or PureWindowsPath(p).drive for p in allowed_paths):
        raise WorkspaceError("Explicit repository-relative ownership paths are required")
    paths = _changed(root, expected)
    for name in paths:
        _safe_path(root, name)
        if _forbidden(name):
            raise WorkspaceError(f"Protected path changed: {name}")
        if not _allowed(name, allowed_paths):
            raise WorkspaceError(f"Change outside task ownership: {name}")
    patch = _write_patch(root, expected, paths, output)
    return {"paths": paths, "sha256": hashlib.sha256(patch).hexdigest(),
            "base": expected, "patch": str(Path(output).resolve())}


def _copy_file(source, dest, relative):
    path = _safe_path(source, relative)
    target = _safe_path(dest, relative)
    if not path.is_file() and not path.is_symlink():
        raise WorkspaceError(f"Not an ordinary file: {relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        target.unlink()
    if path.is_symlink():
        target.unlink(missing_ok=True)
        target.symlink_to(os.readlink(path))
        _safe_path(dest, relative)
    else:
        shutil.copy2(path, target)


def _copy_hooks(source, dest):
    try:
        configured = git(source, "config", "--path", "--get", "core.hooksPath")
    except WorkspaceError:
        configured = ""
    gitdir = Path(git(source, "rev-parse", "--absolute-git-dir")).resolve()
    hooks = Path(configured) if configured else gitdir / "hooks"
    if not hooks.is_absolute():
        hooks = source / hooks
    hooks = hooks.resolve()
    if not hooks.exists():
        return []
    if not any(hooks.is_relative_to(bound) for bound in (source, gitdir)):
        raise WorkspaceError("External core.hooksPath cannot be relocated automatically")
    # Keep a custom directory at the same relative location. Husky and other
    # hooks resolve helpers relative to $0; moving them into .git/hooks breaks
    # those semantics and may silently disable the hook.
    if hooks.is_relative_to(gitdir):
        hook_relative = Path(".git") / hooks.relative_to(gitdir)
    else:
        hook_relative = hooks.relative_to(source)
    destination = dest / hook_relative
    copied = []
    for file in hooks.rglob("*"):
        if file.is_dir() or file.name.endswith(".sample"):
            continue
        if not file.resolve().is_relative_to(hooks) or not file.is_file():
            raise WorkspaceError("Hook has an external or non-file dependency")
        content = file.read_bytes()
        if str(source).encode() in content or str(gitdir).encode() in content:
            raise WorkspaceError("Hook embeds the source checkout path and cannot be relocated")
        relative = file.relative_to(hooks)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, target)
        copied.append(relative.as_posix())
    if copied:
        git(dest, "config", "core.hooksPath", hook_relative.as_posix())
    return sorted(copied)



def _copy_permissions(source, dest):
    """Carry restrictions into isolated copies without transferring auth/MCP."""
    from policy import _settings
    copied = []
    for name in sorted(_settings_paths(source)):
        path = _safe_path(source, name)
        try:
            _, data = _settings(path)
        except ValueError as error:
            raise WorkspaceError(str(error)) from None
        if "permissions" not in data:
            continue
        target = _safe_path(dest, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise WorkspaceError("Permission settings cannot be a symlink")
        target.write_text(json.dumps({"permissions": data["permissions"]},
                                    ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        target.chmod(0o600)
        copied.append(name)
    return copied


def clone_snapshot(source, dest, ref="HEAD", include_dirty=False):
    source, dest = Path(source).resolve(), Path(dest).absolute()
    if dest.exists() or dest.is_symlink():
        raise WorkspaceError(f"Destination already exists: {dest}")
    try:
        dest.resolve().relative_to(source)
    except ValueError:
        pass
    else:
        raise WorkspaceError("Managed clone must be outside the source checkout")
    before = snapshot(source)
    resolved = git(source, "rev-parse", f"{ref}^{{commit}}")
    if include_dirty and resolved != before["head"]:
        raise WorkspaceError("Dirty snapshot requires the source HEAD")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # A file://-like clone protocol is unnecessary: this is a local path only.
    _run(source, ["clone", "--no-local", "--no-hardlinks", "--no-checkout", str(source), str(dest)])
    git(dest, "checkout", "--detach", resolved)
    git(dest, "remote", "remove", "origin")
    hooks = _copy_hooks(source, dest)
    copied, skipped = [], []
    if include_dirty:
        tracked_changes = _names(_run(source, ["diff", "--name-only", "--no-renames", "-z", "HEAD", "--"]))
        for name in tracked_changes:
            _safe_path(source, name)
            if _secret(name) or ".git" in PurePosixPath(name).parts:
                raise WorkspaceError(f"Cannot transfer modified secret: {name}")
        with tempfile.TemporaryDirectory(prefix="sdd-dirty-") as temp:
            patch = Path(temp) / "changes.patch"
            _write_patch(source, resolved, tracked_changes, patch)
            apply_patch(dest, patch)
        copied.extend(tracked_changes)
        for name in _names(_run(source, ["ls-files", "--others", "--exclude-standard", "-z"])):
            if _secret(name) or any(part in {".gigacode", ".codex", ".gigaide", ".idea"}
                                    for part in PurePosixPath(name).parts):
                skipped.append(name)
                continue
            _copy_file(source, dest, name)
            copied.append(name)
    permissions = _copy_permissions(source, dest)
    instructions = sorted(_instruction_paths(source))
    for name in instructions:
        _copy_file(source, dest, name)
    # Locally copied agent context is never included in generated product patches.
    exclude = dest / ".git" / "info" / "exclude"
    with exclude.open("a", encoding="utf-8") as stream:
        stream.write("\n# Local SDD agent context\n.gigacode/\n**/.gigacode/\n")
        for name in instructions:
            if name not in _tracked(dest):
                stream.write("/" + name + "\n")
    if snapshot(source) != before:
        raise WorkspaceError("Source changed during snapshot; clone retained but not usable")
    return {"root": str(dest.resolve()), "head": resolved, "copied": sorted(set(copied)),
            "instructions": instructions, "skipped": skipped, "hooks": hooks,
            "permission_settings": permissions}


def apply_patch(root, patch):
    data = Path(patch).read_bytes()
    if not data:
        return
    _run(root, ["apply", "--check", "--binary", "-"], data=data)
    _run(root, ["apply", "--binary", "-"], data=data)


def commit_changes(root, message, allow_input=False, env=None):
    if not isinstance(message, str) or not message.strip():
        raise WorkspaceError("Commit message is required")
    paths = _changed(root, "HEAD")
    # Initial managed baseline must stop tracking local permission overlays.
    # Keep their sanitized working files and restrictions, never commit them.
    local_settings = [name for name in _tracked(root)
                      if allow_input and PurePosixPath(name).name == "settings.json"
                      and ".gigacode" in PurePosixPath(name).parts]
    paths = [name for name in paths if name not in local_settings]
    for name in paths:
        _safe_path(root, name)
        if _forbidden(name, allow_input=allow_input):
            raise WorkspaceError(f"Protected path cannot be committed: {name}")
    if not paths and not local_settings:
        return git(root, "rev-parse", "HEAD")
    # Honor repository hooks. Never use --no-verify, commit-tree, or broad staging.
    if paths:
        git(root, "add", "-A", "--", *paths)
    if local_settings:
        # Only this fresh managed clone's index changes. info/exclude already
        # keeps the files local after the baseline removes them from tracking.
        git(root, "rm", "--cached", "--", *local_settings)
        staged = set(_names(_run(root, ["diff", "--cached", "--name-only", "-z"])))
        if staged != set(paths) | set(local_settings):
            raise WorkspaceError("Unexpected staged paths in initial managed snapshot")
    _run(root, ["-c", "user.name=SDD Runner", "-c", "user.email=sdd-runner@localhost",
                "commit", "-m", message,
                *([] if local_settings else ["--", *paths])], base_env=env)
    return git(root, "rev-parse", "HEAD")
