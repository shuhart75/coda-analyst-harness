#!/usr/bin/env python3
"""Reviewable, content-preserving migration to quarter-owned deliveries."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from project_layout import REGISTRY, layout, safe_path


def git(project, *args):
    return subprocess.check_output(["git", "-C", str(project), *args], text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(project):
    index = layout(project)
    if not index:
        raise ValueError("Project has not been migrated")
    seen = set()
    for key, d in index["deliveries"].items():
        for value in (key, d["feature_id"], d["delivery_id"]):
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value):
                raise ValueError("Invalid delivery identity")
        q = d.get("quarter")
        if q is not None and not re.fullmatch(r"\d{4}-Q[1-4]", q):
            raise ValueError("A delivery must belong to exactly one quarter")
        prefix = f"quarters/{q}/features" if q else "backlog/features"
        expected = f"{prefix}/{d['feature_id']}/deliveries/{d['delivery_id']}"
        if d["path"] != expected or expected in seen:
            raise ValueError("Noncanonical or duplicate delivery path")
        seen.add(expected)
        if not safe_path(project, expected).is_dir():
            raise ValueError(f"Missing delivery: {expected}")
    return {"status": "valid", "deliveries": len(seen)}


def plan(project, assignments):
    if layout(project):
        raise ValueError("Already migrated")
    if git(project, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Migration requires a clean tracked tree")
    tracked = git(project, "ls-files", "-z").rstrip("\0").split("\0")
    roots = {Path(p).parts[1] for p in tracked if p.startswith("features/")}
    if set(assignments) != roots:
        raise ValueError("Assignments must cover every existing feature exactly once")
    deliveries = {}
    for old, spec in assignments.items():
        feature = spec.get("feature_id", old)
        delivery = spec.get("delivery_id", old)
        q = spec.get("quarter")
        if q is not None and not re.fullmatch(r"\d{4}-Q[1-4]", q):
            raise ValueError("Invalid quarter assignment")
        for item in (old, feature, delivery):
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", item):
                raise ValueError("Invalid identity")
        prefix = f"quarters/{q}/features" if q else "backlog/features"
        deliveries[old] = {**spec, "feature_id": feature, "delivery_id": delivery,
                           "quarter": q, "path": f"{prefix}/{feature}/deliveries/{delivery}"}
    destinations = [v["path"] for v in deliveries.values()]
    if len(set(destinations)) != len(destinations):
        raise ValueError("Two old features cannot silently overwrite one delivery")
    moves = []
    for old in tracked:
        parts = Path(old).parts
        new = None
        if len(parts) > 2 and parts[0] == "features":
            new = deliveries[parts[1]]["path"] + "/" + "/".join(parts[2:])
        elif len(parts) > 2 and parts[0] == "planning" and re.fullmatch(r"\d{4}-Q[1-4]", parts[1]):
            new = "quarters/" + "/".join(parts[1:])
        if new:
            source = safe_path(project, old)
            if not source.is_file():
                raise ValueError("Only regular files may be migrated")
            if safe_path(project, new).exists():
                raise ValueError(f"Destination exists: {new}")
            moves.append({"source": old, "target": new, "sha256": digest(source)})
    return {"schema_version": 1, "head": git(project, "rev-parse", "HEAD"),
            "deliveries": deliveries, "moves": moves}


def apply(project, proposal, confirmed):
    if not confirmed:
        raise ValueError("Explicit analyst authorization is required")
    if git(project, "branch", "--show-current") in {"", "main", "master"}:
        raise ValueError("Migration requires a review branch")
    if git(project, "rev-parse", "HEAD") != proposal["head"]:
        raise ValueError("HEAD changed after migration review")
    journal = project / "migration-layout.json"
    if layout(project):
        if journal.exists() and json.loads(journal.read_text()) == proposal:
            return validate(project)
        raise ValueError("A different migration already exists")
    if journal.exists() and json.loads(journal.read_text()) != proposal:
        raise ValueError("Resume only the identical migration")
    # The plan cannot move arbitrary files or omit a tracked feature artifact.
    expected = {}
    tracked = git(project, "ls-files", "-z").rstrip("\0").split("\0")
    for old in tracked:
        parts = Path(old).parts
        if len(parts) > 2 and parts[0] == "features":
            d = proposal["deliveries"].get(parts[1])
            if not d:
                raise ValueError("Migration omits a feature")
            prefix = f"quarters/{d['quarter']}/features" if d.get("quarter") else "backlog/features"
            canonical = f"{prefix}/{d['feature_id']}/deliveries/{d['delivery_id']}"
            if d['path'] != canonical:
                raise ValueError("Noncanonical mapping")
            expected[old] = canonical + "/" + "/".join(parts[2:])
        elif len(parts) > 2 and parts[0] == "planning" and re.fullmatch(r"\d{4}-Q[1-4]", parts[1]):
            expected[old] = "quarters/" + "/".join(parts[1:])
    actual = {m['source']: m['target'] for m in proposal['moves']}
    if actual != expected or len(actual) != len(proposal['moves']) or len(set(actual.values())) != len(actual):
        raise ValueError("Migration moves differ from the complete canonical mapping")
    # Full preflight before the first move. Resume accepts only identical bytes.
    for move in proposal["moves"]:
        source, target = (safe_path(project, move[k]) for k in ("source", "target"))
        if source.exists() == target.exists():
            raise ValueError(f"Missing source or occupied destination: {move['source']}")
        if digest(source if source.exists() else target) != move["sha256"]:
            raise ValueError(f"Source changed: {move['source']}")
    journal.write_text(json.dumps(proposal, ensure_ascii=False, indent=2) + "\n")
    for move in proposal["moves"]:
        source, target = (project / move[k] for k in ("source", "target"))
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
    (project / REGISTRY).write_text(json.dumps({"schema_version": 1, "deliveries": proposal["deliveries"]}, ensure_ascii=False, indent=2) + "\n")
    for directory in sorted((project / "features").rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    for directory in sorted((project / "planning").rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    write_navigation(project, proposal["deliveries"])
    return validate(project)


def write_navigation(project, deliveries):
    import os
    by_feature, by_quarter = {}, {}
    for key, d in deliveries.items():
        by_feature.setdefault(d["feature_id"], []).append((key, d))
        by_quarter.setdefault(d.get("quarter"), []).append((key, d))
    for feature, entries in sorted(by_feature.items()):
        directory = project / "features" / feature
        directory.mkdir(parents=True, exist_ok=True)
        lines = [f"# {feature}", "", "Постоянная идентичность фичи. Рабочие документы находятся в поставках кварталов.", ""]
        for key, d in entries:
            target = project / d["path"]
            lines.append(f"- [{d.get('quarter') or 'Backlog'} · {d['delivery_id']}]({os.path.relpath(target, directory)}/): {d.get('basis', '')}")
        (directory / "README.md").write_text("\n".join(lines) + "\n")
    for q, entries in sorted(by_quarter.items(), key=lambda i: i[0] or ""):
        directory = project / (f"quarters/{q}" if q else "backlog")
        directory.mkdir(parents=True, exist_ok=True)
        lines = [f"# {q or 'Будущие поставки'}", "", "Рабочая область квартала. Фича сохраняет идентичность между кварталами; каждая поставка принадлежит одному кварталу.", ""]
        for key, d in entries:
            target = project / d["path"]
            title_path = target / "feature.md"
            title = title_path.read_text().splitlines()[0].lstrip("# ") if title_path.exists() else d["feature_id"]
            lines.append(f"- [{title}]({os.path.relpath(target, directory)}/) — поставка `{d['delivery_id']}`")
            feature_dir = target.parent.parent
            feature_dir.mkdir(parents=True, exist_ok=True)
            readme = feature_dir / "README.md"
            if not readme.exists():
                readme.write_text(f"# {title}\n\nПостоянная фича: `{d['feature_id']}`.\n\n")
            with readme.open("a") as stream:
                stream.write(f"- [Поставка {d['delivery_id']}](deliveries/{d['delivery_id']}/)\n")
        if q:
            lines += ["", "## Планирование", "", "- [Квартальный план](quarter/)", "- [Гант](gantt/)"]
        target = directory / "README.md"
        if target.exists():
            target = directory / "deliveries.md"
        target.write_text("\n".join(lines) + "\n")



def save(project, expected_head, paths, message, push=False):
    """Save one reviewed, pending layout migration; never impersonate a feature session."""
    import os
    import shlex
    from commit_message_policy import HOOK_MARKER, require_valid_commit_message

    project = project.resolve()
    branch = git(project, "branch", "--show-current")
    if branch in {"", "main", "master"}:
        raise ValueError("Migration save requires a review branch")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_head or "") or git(project, "rev-parse", "HEAD") != expected_head:
        raise ValueError("HEAD changed or expected-head is not a full commit")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("Commit message is required")
    require_valid_commit_message(message)
    if git(project, "diff", "--cached", "--name-only", "-z"):
        raise ValueError("Migration save requires an empty index; preserve and review existing staged work")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        location = Path(git(project, "rev-parse", "--path-format=absolute", "--git-path", marker))
        if location.exists():
            raise ValueError("Finish the existing Git operation before saving migration")
    journal_path = safe_path(project, "migration-layout.json")
    if not journal_path.is_file():
        raise ValueError("A pending migration journal is required")
    journal = json.loads(journal_path.read_text())
    if journal.get("schema_version") != 1 or journal.get("head") != expected_head:
        raise ValueError("Migration journal does not belong to the reviewed HEAD")
    validate(project)
    current_deliveries = layout(project)["deliveries"]
    if any(current_deliveries.get(key, {}).get("path") != item["path"] for key, item in journal.get("deliveries", {}).items()):
        raise ValueError("Migration journal paths differ from the validated delivery index")
    tracked = set(git(project, "ls-files", "-z").rstrip("\0").split("\0"))
    if "migration-layout.json" in tracked:
        raise ValueError("Migration already saved; use ordinary branch push to retry publication")
    if not isinstance(paths, dict) or not paths or not {"migration-layout.json", REGISTRY}.issubset(paths):
        raise ValueError("Exact reviewed paths including journal and delivery index are required")
    allowed = {"README.md", "LICENSE", "assets", "baseline", "context", "features", "planning", "quarters", "backlog", "releases", REGISTRY, "migration-layout.json", "migration-report.md", "baseline-review.md"}
    forbidden = {".git", ".workflow", ".workspace-state", ".codex", ".gigacode", ".gigaide", ".idea", ".vscode", "__pycache__", "AGENTS.md", "GIGACODE.md"}
    for name, checksum in paths.items():
        if not isinstance(name, str) or not name or Path(name).as_posix() != name:
            raise ValueError("Canonical relative file paths are required")
        parts = Path(name).parts
        if parts[0] not in allowed or any(p in forbidden or p.endswith((".orig", ".iml")) for p in parts):
            raise ValueError(f"Local settings or non-analytical path cannot be saved: {name}")
        target = safe_path(project, name)
        if checksum is None:
            if name not in tracked or target.exists():
                raise ValueError(f"Reviewed deletion is not a missing tracked file: {name}")
        elif not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum) or not target.is_file() or digest(target) != checksum:
            raise ValueError(f"Reviewed file changed: {name}")
    expected_moves = {}
    for name in tracked:
        parts = Path(name).parts
        if len(parts) > 2 and parts[0] == "features":
            entry = journal.get("deliveries", {}).get(parts[1])
            if entry is None:
                raise ValueError("Migration journal omits a tracked feature")
            expected_moves[name] = entry["path"] + "/" + "/".join(parts[2:])
        elif len(parts) > 2 and parts[0] == "planning" and re.fullmatch(r"\d{4}-Q[1-4]", parts[1]):
            expected_moves[name] = "quarters/" + "/".join(parts[1:])
    moves = journal.get("moves", [])
    if not expected_moves or len(moves) != len(expected_moves) or {m["source"]: m["target"] for m in moves} != expected_moves:
        raise ValueError("Migration journal must describe the complete tracked layout transition")
    # Every original moved artifact is proven against Git, not a mutable journal alone.
    for move in journal.get("moves", []):
        source, target = move["source"], move["target"]
        safe_path(project, source)
        safe_path(project, target)
        if source not in tracked or source not in paths or target not in paths:
            raise ValueError("Save scope must include every tracked migration source and destination")
        original = subprocess.check_output(["git", "-C", str(project), "show", f"{expected_head}:{source}"])
        if hashlib.sha256(original).hexdigest() != move["sha256"]:
            raise ValueError("Migration source does not match recorded Git bytes")
    hook = Path(git(project, "rev-parse", "--path-format=absolute", "--git-path", "hooks/commit-msg"))
    policy = Path(__file__).resolve().with_name("commit_message_policy.py")
    required = f'exec python3 {shlex.quote(str(policy))} "$1"'
    if not hook.is_file() or not os.access(hook, os.X_OK) or HOOK_MARKER not in hook.read_text() or required not in hook.read_text():
        raise ValueError("The managed commit-msg policy hook is required; do not bypass or replace custom hooks")
    git(project, "--literal-pathspecs", "add", "--", *sorted(paths))
    staged = set(git(project, "diff", "--cached", "--name-only", "-z").rstrip("\0").split("\0"))
    if not staged or not staged.issubset(paths):
        raise ValueError("Unexpected staged scope; preserve index for inspection")
    git(project, "commit", "-m", message)
    commit = git(project, "rev-parse", "HEAD")
    result = {"status": "committed", "branch": branch, "commit": commit, "paths": sorted(paths)}
    if push:
        published = subprocess.run(["git", "-C", str(project), "push", "--", "origin", f"HEAD:refs/heads/{branch}"], capture_output=True, text=True)
        result["status"] = "committed-and-pushed" if published.returncode == 0 else "committed-push-failed"
        if published.returncode:
            result["error"] = published.stderr.strip()
            result["next_action"] = "Retry ordinary git push of this branch; do not reset or rerun migration save"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "apply", "validate", "register", "feature-path", "quarter-path", "save"])
    parser.add_argument("project", type=Path)
    parser.add_argument("--assignments", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--analyst-confirmed", action="store_true")
    parser.add_argument("--feature")
    parser.add_argument("--delivery")
    parser.add_argument("--quarter")
    parser.add_argument("--key")
    parser.add_argument("--expected-head")
    parser.add_argument("--paths-file", type=Path)
    parser.add_argument("--message")
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()
    project = args.project.resolve()
    if args.action in {"feature-path", "quarter-path"}:
        from project_layout import feature_root, quarter_root
        print(feature_root(project, args.feature) if args.action == "feature-path" else quarter_root(project, args.quarter))
        return
    if args.action == "save":
        if args.paths_file is None:
            raise ValueError("--paths-file is required")
        result = save(project, args.expected_head, json.loads(args.paths_file.read_text()), args.message, args.push)
    elif args.action == "register":
        if not args.analyst_confirmed or git(project, "branch", "--show-current") in {"", "main", "master"}:
            raise ValueError("Register a confirmed delivery on a review branch")
        index = layout(project)
        if index is None:
            raise ValueError("Migrate the project before registering deliveries")
        for item in (args.feature, args.delivery, args.key or args.feature):
            if not item or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", item):
                raise ValueError("Specify feature, delivery and lookup key")
        if not args.quarter or not re.fullmatch(r"\d{4}-Q[1-4]", args.quarter):
            raise ValueError("One quarter is required")
        key = args.key or args.feature
        d = {"feature_id": args.feature, "delivery_id": args.delivery, "quarter": args.quarter,
             "path": f"quarters/{args.quarter}/features/{args.feature}/deliveries/{args.delivery}"}
        if key in index["deliveries"] or any(x['feature_id'] == args.feature and x['delivery_id'] == args.delivery for x in index['deliveries'].values()):
            raise ValueError("Delivery already registered; never move it to another quarter")
        target = safe_path(project, d['path'])
        if target.exists():
            raise ValueError("Delivery path already exists")
        target.mkdir(parents=True)
        index['deliveries'][key] = d
        (project / REGISTRY).write_text(json.dumps(index, ensure_ascii=False, indent=2) + '\n')
        result = {'status': 'registered', 'key': key, **d}
    elif args.action == "plan":
        result = plan(project, json.loads(args.assignments.read_text()))
    elif args.action == "apply":
        result = apply(project, json.loads(args.plan.read_text()), args.analyst_confirmed)
    else:
        result = validate(project)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
