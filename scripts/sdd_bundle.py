"""Offline, fail-closed upstream OpenSpec bundle validation.

analyst-sdd-receiver:v1

This validates the transport profile, not installed OpenSpec/GigaCode behavior.
All repository inspection reads Git objects; it never changes a code checkout.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from urllib.parse import urlsplit

PROFILE = "openspec-spec-driven-v1"
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
REQS = re.compile(r"^### (REQ-[A-Z0-9-]+)\.\s+.+$", re.MULTILINE)
SECTIONS = {"ADDED", "MODIFIED", "REMOVED", "RENAMED"}


def _fail(message):
    raise ValueError("SDD: " + message)


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _path(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        _fail("invalid relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", "..", ".git"} for part in path.parts) or str(path) != value:
        _fail("unsafe relative path: " + value)
    return value


def _plain(root):
    for item in [root, *root.parents]:
        if item.is_symlink():
            _fail("symlink path: " + str(item))


def _files(root):
    _plain(root)
    if not root.is_dir():
        _fail("bundle directory is absent")
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            _fail("non-regular bundle entry: " + str(path))
        if path.is_file():
            result[_path(path.relative_to(root).as_posix())] = _hash(path.read_bytes())
    return result


def _json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail("duplicate JSON key: " + key)
            result[key] = value
        return result
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail("cannot read package.json: " + str(exc))


def _string(value, label):
    if not isinstance(value, str) or not value.strip():
        _fail(label + " must be a nonempty string")
    return value


def _slug(value):
    if not isinstance(value, str) or not SLUG.fullmatch(value):
        _fail("invalid capability/change identifier")
    return value


def _digest(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        _fail("invalid SHA256")
    return value


def _repository(value):
    _string(value, "repository")
    if any(char.isspace() for char in value) or "\x00" in value:
        _fail("invalid repository identity")
    parsed = urlsplit(value)
    if parsed.password or (parsed.scheme in {"http", "https"} and parsed.username) or parsed.query or parsed.fragment:
        _fail("repository identity must not contain credentials/query/fragment")
    return value


def _proposal(text):
    for heading in ("Why", "What Changes", "Capabilities", "Impact"):
        if len(re.findall(r"^## " + heading + r"\s*$", text, re.MULTILINE)) != 1:
            _fail("proposal requires exactly one ## " + heading)
    result = {}
    for kind in ("New", "Modified"):
        matches = list(re.finditer(r"^### " + kind + r" Capabilities\s*$", text, re.MULTILINE))
        if len(matches) != 1:
            _fail("proposal requires ### " + kind + " Capabilities")
        tail = text[matches[0].end():]
        body = re.split(r"^#{1,3} ", tail, maxsplit=1, flags=re.MULTILINE)[0]
        entries = re.findall(r"^- `([^`]+)`: (\S.*)$", body, re.MULTILINE)
        names = [_slug(name) for name, _ in entries]
        if len(names) != len(set(names)):
            _fail("duplicate proposal capability")
        result[kind.lower()] = set(names)
    if result["new"] & result["modified"] or not (result["new"] | result["modified"]):
        _fail("proposal capabilities must be nonempty and disjoint")
    return result


def _requirements(text, delta=True):
    """Return {(section,name): scenario names}, plus rename source -> target."""
    result, renames = {}, {}
    if delta:
        headings = list(re.finditer(r"^## (.+?)\s*$", text, re.MULTILINE))
        if not headings or text[:headings[0].start()].strip():
            _fail("delta must start with a supported Requirements section")
        sections = []
        seen = set()
        for idx, match in enumerate(headings):
            title = match.group(1)
            kind = title.removesuffix(" Requirements")
            if kind not in SECTIONS or title != kind + " Requirements" or kind in seen:
                _fail("unsupported or duplicate delta section: " + title)
            seen.add(kind)
            sections.append((kind, text[match.end():headings[idx + 1].start() if idx + 1 < len(headings) else len(text)]))
    else:
        sections = [("MAIN", text)]
    for kind, body in sections:
        if kind == "RENAMED":
            pairs = re.findall(r"^- FROM: `### Requirement: ([^`\n]+)`\s*\n- TO: `### Requirement: ([^`\n]+)`\s*$", body, re.MULTILINE)
            residue = re.sub(r"^- FROM: `### Requirement: ([^`\n]+)`\s*\n- TO: `### Requirement: ([^`\n]+)`\s*$", "", body, flags=re.MULTILINE)
            if not pairs or residue.strip():
                _fail("RENAMED requires exact FROM/TO pairs")
            for source, target in pairs:
                if source == target or source in renames or target in renames.values():
                    _fail("duplicate or no-op rename")
                renames[source] = target
                result[(kind, target)] = set()
            continue
        headings = list(re.finditer(r"^### Requirement: (\S.*?)\s*$", body, re.MULTILINE))
        if not headings or (delta and body[:headings[0].start()].strip()):
            _fail("missing Requirement block")
        for idx, match in enumerate(headings):
            name = match.group(1)
            block = body[match.end():headings[idx + 1].start() if idx + 1 < len(headings) else len(body)]
            key = (kind, name)
            if key in result:
                _fail("duplicate requirement: " + name)
            if kind == "REMOVED":
                for field in ("Reason", "Migration"):
                    if not re.search(r"^\*\*" + field + r"\*\*: \S.+$", block, re.MULTILINE):
                        _fail("REMOVED requires " + field)
                result[key] = set()
                continue
            scenarios = list(re.finditer(r"^#### Scenario: (\S.*?)\s*$", block, re.MULTILINE))
            if not scenarios or not re.search(r"\b(?:SHALL|MUST)\b", block[:scenarios[0].start()]):
                _fail("requirement needs SHALL/MUST and scenarios: " + name)
            names = set()
            for number, scenario in enumerate(scenarios):
                scenario_name = scenario.group(1)
                if scenario_name in names:
                    _fail("duplicate scenario: " + scenario_name)
                names.add(scenario_name)
                steps = block[scenario.end():scenarios[number + 1].start() if number + 1 < len(scenarios) else len(block)]
                for token in ("WHEN", "THEN"):
                    if not re.search(r"^- \*\*" + token + r"\*\* \S.+$", steps, re.MULTILINE):
                        _fail("scenario requires bullet " + token)
            if re.search(r"^#{1,6}\s+Scenario:", block, re.MULTILINE) and len(re.findall(r"^#{1,6}\s+Scenario:", block, re.MULTILINE)) != len(scenarios):
                _fail("Scenario requires exactly four hashes")
            result[key] = names
    behavior_names = [name for (kind, name) in result if kind != "RENAMED"]
    if len(behavior_names) != len(set(behavior_names)):
        _fail("requirement occurs in conflicting delta sections")
    return result, renames


def _metadata(text):
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"(schema|created):\s*([^\s#]+)\s*", line)
        if not match or match[1] in values:
            _fail("metadata supports only one schema and created field")
        values[match[1]] = match[2]
    if values.get("schema") != "spec-driven":
        _fail("unsupported OpenSpec schema")
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", values.get("created", "")):
            _fail("metadata requires created: YYYY-MM-DD")
        datetime.date.fromisoformat(values.get("created", ""))
    except ValueError:
        _fail("metadata requires created: YYYY-MM-DD")


def inspect(root: Path, requirements_text: str) -> dict:
    root = Path(root)
    files = _files(root)
    package = _json(root / "package.json")
    if not isinstance(package, dict) or type(package.get("schema_version")) is not int or package.get("schema_version") != 1 or package.get("profile") != PROFILE:
        _fail("unsupported package schema/profile")
    if package.get("requirements_sha256") != _hash(requirements_text.encode("utf-8")):
        _fail("requirements checksum mismatch")
    req_ids = set(REQS.findall(requirements_text))
    if not req_ids:
        _fail("no authored REQ definitions")
    changes = package.get("changes")
    if not isinstance(changes, list) or not changes:
        _fail("changes must be a nonempty list")
    expected, covered, identities, targets = {"package.json"}, set(), set(), set()
    for change in changes:
        if not isinstance(change, dict):
            _fail("invalid change")
        contour, change_id = change.get("contour"), _slug(change.get("change_id"))
        if contour not in {"backend", "frontend"} or (contour, change_id) in identities:
            _fail("invalid or duplicate change contour/identity")
        identities.add((contour, change_id))
        _path(change.get("target_root"))
        if PurePosixPath(change["target_root"]).name != "openspec":
            _fail("target_root must name an openspec directory")
        target = (change["target_root"], change_id)
        if target in targets:
            _fail("duplicate receiver target")
        targets.add(target)
        _repository(change.get("repository"))
        if not isinstance(change.get("code_commit"), str) or not COMMIT.fullmatch(change["code_commit"]):
            _fail("code_commit must be a full commit SHA")
        context, bases = change.get("context_files"), change.get("base_specs")
        if not isinstance(context, dict) or not context or not isinstance(bases, dict):
            _fail("context_files must be nonempty and base_specs must be an object")
        if change["target_root"] + "/config.yaml" not in context:
            _fail("context_files must pin the target OpenSpec config.yaml")
        if not any(not path.startswith(change["target_root"] + "/") for path in context if isinstance(path, str)):
            _fail("context_files must pin code or project rules outside OpenSpec")
        for path, digest in context.items():
            _path(path)
            _digest(digest)
        for capability, digest in bases.items():
            _slug(capability)
            if digest is not None:
                _digest(digest)
        removals = change.get("removed_scenarios", {})
        if not isinstance(removals, dict):
            _fail("removed_scenarios must be an object")
        for capability, requirements in removals.items():
            if capability not in bases or not isinstance(requirements, dict) or not requirements:
                _fail("removed_scenarios requires known capability and requirement objects")
            for requirement, scenarios in requirements.items():
                _string(requirement, "removed requirement")
                if not isinstance(scenarios, dict) or not scenarios:
                    _fail("removed_scenarios requires nonempty scenario objects")
                for scenario, reason in scenarios.items():
                    _string(scenario, "removed scenario")
                    _string(reason, "scenario removal reason")
        prefix = contour + "/" + change_id + "/"
        needed = {prefix + ".openspec.yaml", prefix + "proposal.md"}
        if not needed <= files.keys():
            _fail("missing proposal or .openspec.yaml")
        _metadata((root / (prefix + ".openspec.yaml")).read_text(encoding="utf-8"))
        caps = _proposal((root / (prefix + "proposal.md")).read_text(encoding="utf-8"))
        if set(bases) != caps["new"] | caps["modified"]:
            _fail("base_specs does not match proposal capabilities")
        blocks = {}
        for capability, digest in bases.items():
            if (capability in caps["new"]) != (digest is None):
                _fail("new capability requires absent base; modified requires base hash")
            path = prefix + "specs/" + capability + "/spec.md"
            needed.add(path)
            if path not in files:
                _fail("missing capability spec: " + path)
            parsed, _ = _requirements((root / path).read_text(encoding="utf-8"))
            if digest is None and any(kind != "ADDED" for kind, _ in parsed):
                _fail("new capabilities only support ADDED Requirements")
            for (kind, name), scenarios in parsed.items():
                key = (capability, name)
                blocks.setdefault(key, set()).update(scenarios)
        expected.update(needed)
        coverage = change.get("coverage")
        if not isinstance(coverage, list) or not coverage:
            _fail("coverage must be nonempty")
        mapped = {}
        for entry in coverage:
            if not isinstance(entry, dict) or entry.get("req_id") not in req_ids:
                _fail("coverage references unknown REQ")
            key = (entry.get("capability"), entry.get("requirement"))
            scenarios = entry.get("scenarios")
            if any(not isinstance(value, str) for value in key) or key not in blocks or not isinstance(scenarios, list) or any(not isinstance(x, str) for x in scenarios) or len(scenarios) != len(set(scenarios)) or not set(scenarios) <= blocks[key]:
                _fail("coverage references unknown requirement/scenario")
            if blocks[key] and not scenarios:
                _fail("behavior coverage must name scenarios")
            mapped.setdefault(key, set()).update(scenarios)
            covered.add(entry["req_id"])
        if mapped != blocks:
            _fail("coverage must cover every requirement and scenario")
    if covered != req_ids:
        _fail("coverage must cover every authored REQ")
    if set(files) != expected:
        _fail("unexpected bundle files: " + ", ".join(sorted(set(files) - expected)))
    # Reject unexpected directories too, including otherwise invisible empty tasks/.
    allowed_dirs = {str(p) for name in expected for p in PurePosixPath(name).parents if str(p) != "."}
    if any(p.relative_to(root).as_posix() not in allowed_dirs for p in root.rglob("*") if p.is_dir()):
        _fail("unexpected bundle directory")
    return {"profile": PROFILE, "sha256": _hash(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()), "files": files}


def verify_descriptor(root: Path, requirements_text: str, expected: dict) -> dict:
    actual = inspect(root, requirements_text)
    if actual != expected:
        _fail("bundle descriptor mismatch")
    return actual


def _git(root, *args, allow_failure=False):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if result.returncode and not allow_failure:
        _fail("Git source verification failed: " + result.stderr.decode(errors="replace").strip())
    return result


def _blob(root, commit, path):
    _path(path)
    listing = _git(root, "ls-tree", "-z", commit, "--", path).stdout
    if not listing:
        return None
    entries = listing.split(b"\x00")[:-1]
    if len(entries) != 1:
        _fail("ambiguous source path")
    metadata, name = entries[0].split(b"\t", 1)
    mode, kind, oid = metadata.split()
    if name.decode() != path or kind != b"blob" or mode not in {b"100644", b"100755"}:
        _fail("source must be a regular Git blob: " + path)
    return _git(root, "cat-file", "blob", oid.decode()).stdout


def verify_sources(root: Path, descriptor: dict, code_root: Path, *, allowed_receiver_paths=()) -> dict:
    """Verify pinned and current source blobs; read-only, never fetch or checkout."""
    root, code_root = Path(root), Path(code_root)
    if descriptor.get("profile") != PROFILE or descriptor.get("files") != _files(root):
        _fail("bundle changed before source verification")
    if descriptor.get("sha256") != _hash(json.dumps(descriptor["files"], sort_keys=True, separators=(",", ":")).encode()):
        _fail("invalid descriptor digest")
    package = _json(root / "package.json")
    origin = _git(code_root, "remote", "get-url", "origin").stdout.decode().strip()
    head = _git(code_root, "rev-parse", "HEAD").stdout.decode().strip()
    permitted = {_path(path) for path in allowed_receiver_paths}
    legitimate = {change["target_root"] + "/changes/" + change["change_id"] for change in package["changes"]}
    if not permitted <= legitimate:
        _fail("only exact receiver change roots may be allowed dirty")
    pinned_paths = {path for change in package["changes"] for path in change["context_files"]}
    pinned_paths.update(change["target_root"] + "/specs/" + capability + "/spec.md"
                        for change in package["changes"] for capability in change["base_specs"])
    if any(path == prefix or path.startswith(prefix + "/") for path in pinned_paths for prefix in permitted):
        _fail("receiver dirty exception must not cover pinned source files")
    dirty = _git(code_root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout.split(b"\x00")
    index = 0
    while index < len(dirty):
        entry = dirty[index]
        index += 1
        if not entry:
            continue
        paths = [entry[3:].decode("utf-8")]
        if b"R" in entry[:2] or b"C" in entry[:2]:
            if index >= len(dirty):
                _fail("invalid Git status")
            paths.append(dirty[index].decode("utf-8"))
            index += 1
        for path in paths:
            if not any(path.startswith(prefix + "/") for prefix in permitted):
                _fail("source checkout must be clean outside verified receiver changes")
    for change in package["changes"]:
        if origin != change["repository"]:
            _fail("repository origin differs from package identity")
        commit = change["code_commit"]
        if _git(code_root, "merge-base", "--is-ancestor", commit, head, allow_failure=True).returncode:
            _fail("pinned code commit is not an ancestor of HEAD")
        for path, digest in change["context_files"].items():
            for revision in {commit, head}:
                blob = _blob(code_root, revision, path)
                if blob is None or _hash(blob) != digest:
                    _fail("stale context file: " + path)
        removals = change.get("removed_scenarios", {})
        if not isinstance(removals, dict):
            _fail("removed_scenarios must be an object")
        consumed = {}
        for capability, digest in change["base_specs"].items():
            path = change["target_root"] + "/specs/" + capability + "/spec.md"
            original = None
            for revision in {commit, head}:
                blob = _blob(code_root, revision, path)
                if (None if blob is None else _hash(blob)) != digest:
                    _fail("stale base spec: " + path)
                original = blob
            if original is None:
                continue
            old_blocks, _ = _requirements(original.decode("utf-8"), delta=False)
            old = {name: scenarios for (_, name), scenarios in old_blocks.items()}
            delta_path = root / change["contour"] / change["change_id"] / "specs" / capability / "spec.md"
            blocks, renames = _requirements(delta_path.read_text(encoding="utf-8"))
            for source, target in renames.items():
                if source not in old or target in old:
                    _fail("rename must reference existing source and new target")
            reverse = {target: source for source, target in renames.items()}
            for (kind, name), scenarios in blocks.items():
                source = reverse.get(name, name)
                if kind == "ADDED" and name in old:
                    _fail("ADDED requirement already exists")
                if kind in {"MODIFIED", "REMOVED"} and source not in old:
                    _fail(kind + " requirement does not exist in base")
                if kind == "MODIFIED":
                    missing = old[source] - scenarios
                    declared = removals.get(capability, {}).get(name, {})
                    if not isinstance(declared, dict) or set(declared) != missing or any(not isinstance(reason, str) or not reason.strip() for reason in declared.values()):
                        _fail("MODIFIED must preserve old scenarios or explicitly justify each removal")
                    if missing:
                        consumed.setdefault(capability, {})[name] = declared
            if any(name in renames for kind, name in blocks if kind in {"MODIFIED", "REMOVED"}):
                _fail("renamed requirements must use the target name for modification")
        if removals != consumed:
            _fail("unused removed_scenarios claims")
    return {"status": "source-verified", "head": head, "runtime_validation": "not-performed"}


def copy_snapshot(src: Path, dst: Path, expected: dict, requirements_text: str) -> None:
    verify_descriptor(src, requirements_text, expected)
    _plain(Path(dst))
    if Path(dst).exists():
        _fail("snapshot destination already exists")
    try:
        shutil.copytree(src, dst, symlinks=True)
        verify_descriptor(dst, requirements_text, expected)
        verify_descriptor(src, requirements_text, expected)
    except Exception:
        if Path(dst).is_dir() and not Path(dst).is_symlink():
            shutil.rmtree(dst)
        raise
