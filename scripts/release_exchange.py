"""Release-bound exchange archives; cleanup is separate from baseline promotion."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

import baseline_releases as baseline
import delivery_stages as stages
from project_layout import feature_root


@lru_cache(maxsize=1)
def exchange():
    spec = importlib.util.spec_from_file_location("release_exchange_receiver", Path(__file__).with_name("requirements-exchange.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(path)


def _index_path(project, release):
    path = Path(project).resolve() / "releases" / baseline._id(release) / "exchange-archive-index.json"
    _plain_path(project, path)
    return path


def _plain_path(project, path):
    project, path = Path(project).resolve(), Path(path)
    if not path.resolve().is_relative_to(project):
        raise ValueError("Путь архива выходит за пределы аналитического проекта")
    current = path
    while current != project:
        if current.is_symlink():
            raise ValueError("Символьные ссылки в пути архива запрещены")
        current = current.parent


def _index(project, release):
    path = _index_path(project, release)
    data = _read(path) if path.exists() else {"schema_version": 1, "release_id": release, "deliveries": {}}
    if data.get("schema_version") != 1 or data.get("release_id") != release or not isinstance(data.get("deliveries"), dict):
        raise ValueError("Некорректный реестр архива обмена")
    return data


def validate_archive(project, record):
    project = Path(project).resolve()
    release = baseline._id(record["release_id"])
    key = baseline._id(record["delivery_key"])
    expected = f"releases/{release}/exchange-archive/{key}"
    contracts = f"releases/{release}/exchange-contracts/{key}"
    if record.get("archive_path") != expected or record.get("contracts_path") != contracts:
        raise ValueError("Архив указывает за пределы каталога релиза")
    for rel, checksum in ((expected, record["archive_hash"]), (contracts, record["contracts_hash"])):
        path = project / rel
        _plain_path(project, path)
        if baseline.tree_hash(path) != checksum:
            raise ValueError("Неизменяемый архив обмена повреждён")
    return project / expected


def find_archive(project, key):
    """Resolve historical transport identity without rebinding return_id."""
    baseline._id(key)
    found = []
    for path in sorted((Path(project) / "releases").glob("*/exchange-archive-index.json")):
        record = _index(project, path.parent.name)["deliveries"].get(key)
        if record:
            validate_archive(project, record)
            found.append(record)
    if len(found) > 1:
        raise ValueError("Поставка найдена в нескольких архивах релизов")
    return found[0] if found else None


def archived_root(project, key):
    record = find_archive(project, key)
    return validate_archive(project, record) if record else None


def scan_archives(project, analyst=None, all_owners=False):
    items = []
    module = exchange()
    for path in sorted((Path(project) / "releases").glob("*/exchange-archive-index.json")):
        for key, record in _index(project, path.parent.name)["deliveries"].items():
            root = validate_archive(project, record)
            manifest = _read(root / "manifest.json")
            owner = manifest.get("owner", {}).get("analyst_id")
            if not all_owners and owner != analyst:
                continue
            errors = module.validate_manifest(manifest, root)
            if errors:
                raise ValueError("; ".join(errors))
            revisions = []
            for entry in manifest["revisions"]:
                returns = root / entry["returns_path"]
                revisions.append({"revision": entry["revision"], "processing": module.revision_processing_status(root, entry),
                                  "return_ids": [f"{key}:{entry['revision']:03d}:{item.relative_to(root).as_posix()}:{module.sha256(item)}"
                                                 for item in sorted(returns.rglob("*")) if item.is_file()]})
            items.append({"status": "archived", "feature": key, "release_id": record["release_id"],
                          "archive_path": str(root), "owner": owner, "revisions": revisions})
    return {"status": "ok", "scope": "archived", "items": items}


def _release(project, release):
    entry = baseline._load(project)["releases"].get(baseline._id(release))
    if not entry:
        raise ValueError("Нет принятого baseline данного релиза")
    if entry["status"] != "promoted" or not entry.get("deployment"):
        raise ValueError("Архивирование разрешено после подтверждённого внедрения и обновления baseline")
    if baseline.tree_hash(Path(project) / entry["snapshot"]) != entry["preparation"]["candidate_hash"]:
        raise ValueError("Снимок baseline релиза изменён")
    return entry


def _closure(project, key, root):
    module = exchange()
    analytical = feature_root(project, key)
    state = _read(analytical / "requirements-state.json")
    registry = stages.registry(state)
    if not registry["stages"] or any(stage["state"] != "closed" for stage in registry["stages"]):
        raise ValueError("Незавершённая поставка остаётся в рабочем каталоге обмена")
    manifest = _read(root / "manifest.json")
    errors = module.validate_manifest(manifest, root)
    if errors:
        raise ValueError("; ".join(errors))
    module.require_manifest_binding(project, key, manifest)
    stages.require_manifest_history(state, manifest)
    latest = max(registry["revisions"], key=lambda item: item["entry"]["revision"])
    closure = registry["stages"][-1]["closure"]
    if not latest["publication_confirmed"] or manifest["active_revision"] != closure["revision"] or latest["entry"]["revision"] != closure["revision"]:
        raise ValueError("Есть новая или неподтверждённая редакция передачи")
    entry = next(item for item in manifest["revisions"] if item["revision"] == closure["revision"])
    status = module.revision_processing_status(root, entry)
    if status["state"] not in {"completed", "legacy-results-present"} or status["errors"]:
        raise ValueError("Нет корректного итогового возврата поставки")
    summary = root / entry["returns_path"] / "summary.md"
    return_id = f"{key}:{entry['revision']:03d}:{entry['returns_path']}/summary.md:{module.sha256(summary)}"
    reviews = [item for item in _read(analytical / "development-results-state.json")["processed"]
               if item.get("return_id") == return_id and item.get("decision") == "reviewed"]
    if (return_id != closure["return_id"] or entry["sha256"] != closure["requirements_sha256"]
            or not reviews or stages.checksum(reviews[-1]["review"]) != closure["review_sha256"]):
        raise ValueError("Итог или подробное решение изменились после закрытия поставки")
    return latest, closure


def _clone(remote, branch, directory):
    if not remote or not branch or remote.startswith("-") or branch.startswith("-"):
        raise ValueError("Нужно точное место публикации")
    result = subprocess.run(["git", "clone", "--quiet", "--single-branch", "--branch", branch, "--", remote, str(directory)], capture_output=True, text=True)
    if result.returncode:
        raise ValueError("Не удалось проверить опубликованную целевую ветку")


def _archive(project, release, key, decision):
    entry = _release(project, release)
    previous = find_archive(project, key)
    if previous:
        if previous["release_id"] != release or previous["scope_hash"] != entry["scope_hash"] or previous["decision"] != decision:
            raise ValueError("Повтор архивирования не совпадает с сохранённым решением")
        return previous
    module = exchange()
    state = _read(feature_root(project, key) / "requirements-state.json")
    records = stages.registry(state)["revisions"]
    if not records:
        raise ValueError("Нет опубликованной передачи для архивирования")
    publication = max(records, key=lambda item: item["entry"]["revision"])
    role = publication["destination_role"]
    with tempfile.TemporaryDirectory(prefix="exchange-archive-") as temporary:
        source = {"role": role}
        if role == "code":
            cached = _read(publication["manifest_path"])
            remote = cached.get("publication", {})
            if not publication["publication_confirmed"] or remote.get("state") != "merged":
                raise ValueError("Передача не принята в code")
            clone = Path(temporary) / "code"
            _clone(remote["repository_url"], remote["target_branch"], clone)
            if module.git(clone, "merge-base", "--is-ancestor", remote["target_commit"], "HEAD").returncode:
                raise ValueError("Целевая ветка не содержит опубликованный вход")
            exchange_root = clone / "requirements-exchange"
            source.update(repository_url=remote["repository_url"], target_branch=remote["target_branch"], commit=module.git_value(clone, "rev-parse", "HEAD"))
        else:
            exchange_root = project / "requirements-exchange"
            source.update(commit=module.git_value(project, "rev-parse", "HEAD"))
        module.require_plain_exchange(exchange_root)
        packet = exchange_root / key
        _, closure = _closure(project, key, packet)
        checksum = baseline.tree_hash(packet)
        if decision.get("packet_hash") != checksum:
            raise ValueError("Нужна проверка точного состава пакета и всех возвратов: packet_hash изменён или отсутствует")
        archive = project / "releases" / release / "exchange-archive" / key
        contracts = project / "releases" / release / "exchange-contracts" / key
        # Stage complete copies before publishing an immutable archive index.
        staged = Path(temporary) / "packet"
        shutil.copytree(packet, staged)
        staged_contracts = Path(temporary) / "contracts"
        staged_contracts.mkdir()
        for item in exchange_root.iterdir():
            if item.is_file():
                shutil.copy2(item, staged_contracts / item.name)
        contracts_hash = baseline.tree_hash(staged_contracts)
        if baseline.tree_hash(staged) != checksum or baseline.tree_hash(packet) != checksum:
            raise ValueError("Источник изменился во время архивирования")
        copies = ((staged, archive, checksum), (staged_contracts, contracts, contracts_hash))
        _index_path(project, release)
        for _, destination, digest in copies:
            _plain_path(project, destination)
            if destination.exists() and baseline.tree_hash(destination) != digest:
                raise ValueError("Незавершённый архив содержит другие данные; перезапись запрещена")
        for staged_path, destination, digest in copies:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copytree(staged_path, destination)
        record = {"schema_version": 1, "release_id": release, "delivery_key": key,
                  "scope_hash": entry["scope_hash"], "baseline_hash": entry["preparation"]["candidate_hash"],
                  "archive_path": archive.relative_to(project).as_posix(), "archive_hash": checksum,
                  "contracts_path": contracts.relative_to(project).as_posix(), "contracts_hash": contracts_hash,
                  "source": source, "closure": closure, "decision": decision}
        validate_archive(project, record)
        index = _index(project, release)
        index["deliveries"][key] = record
        _save(_index_path(project, release), index)
        return record


def review_exchange(project, release, review):
    """Explicit full release mapping; only complete deliveries are archived."""
    project = baseline._guard(project)
    entry = _release(project, release)
    if review.get("scope_hash") != entry["scope_hash"]:
        raise ValueError("Проверка обмена относится к другому составу релиза")
    tasks = {item["id"] for item in entry["observation"]["tasks"]}
    covered, decisions = set(), {}
    for item in review.get("deliveries", []):
        key = baseline._id(item["delivery_key"])
        if not feature_root(project, key).is_dir():
            raise ValueError("Поставка не найдена в аналитическом проекте")
        selected = item.get("task_ids")
        if (key in decisions or item.get("disposition") not in {"complete", "partial", "no-transfer"}
                or not baseline._text(item.get("evidence")) or not isinstance(selected, list)
                or not selected or len(set(selected)) != len(selected) or not set(selected) <= tasks):
            raise ValueError("Нужна проверяемая привязка поставки и её полноты к задачам релиза")
        decisions[key] = item
        covered.update(selected)
    other = set()
    for item in review.get("other_tasks", []):
        key = item.get("task_id")
        if key not in tasks or key in other or key in covered or not baseline._text(item.get("reason")):
            raise ValueError("Задачи без передачи требуют отдельного объяснения")
        other.add(key)
    if covered | other != tasks:
        raise ValueError("Проверка обмена не охватила полный состав релиза")
    previous = entry.get("exchange_cleanup", {})
    if previous.get("review") and previous["review"] != review:
        raise ValueError("Ранее сохранена другая проверка обмена релиза")
    results = {}
    for key, decision in decisions.items():
        disposition = decision["disposition"]
        if disposition == "complete":
            record = _archive(project, release, key, decision)
            old = previous.get("deliveries", {}).get(key, {})
            results[key] = {"state": old.get("state", "cleanup-pending"), "archive_path": record["archive_path"], **old}
        else:
            if disposition == "no-transfer":
                state_path = feature_root(project, key) / "requirements-state.json"
                if state_path.exists() and _read(state_path).get("last_published"):
                    raise ValueError("Нельзя объявить отсутствующей ранее опубликованную передачу")
            results[key] = {"state": "retained" if disposition == "partial" else "not-required", "decision": decision}
    state = baseline._load(project)
    state["releases"][release]["exchange_cleanup"] = {
        "state": "completed" if all(item["state"] in {"completed", "retained", "not-required"} for item in results.values()) else "cleanup-pending",
        "review": review, "deliveries": results}
    baseline._save(project, state)
    return state["releases"][release]["exchange_cleanup"]


def require_accepted_archive(project, release, key):
    """Read accepted analytics main in isolation, not a cached origin/main ref."""
    project = Path(project).resolve()
    entry = _release(project, release)
    record = find_archive(project, key)
    if not record or record["release_id"] != release or record["scope_hash"] != entry["scope_hash"]:
        raise ValueError("Нет архива данной поставки и релиза")
    module = exchange()
    remote = module.git_value(project, "remote", "get-url", "origin")
    with tempfile.TemporaryDirectory(prefix="verify-accepted-archive-") as temporary:
        clone = Path(temporary) / "analytics"
        _clone(remote, "main", clone)
        accepted = _index(clone, release)["deliveries"].get(key)
        if accepted != record:
            raise ValueError("Архив ещё не принят в analytics/main")
        validate_archive(clone, accepted)
        deployed = _release(clone, release)
        if deployed["scope_hash"] != record["scope_hash"] or deployed["preparation"]["candidate_hash"] != record["baseline_hash"]:
            raise ValueError("Архив не связан с принятым baseline релиза")
        _closure(clone, key, clone / record["archive_path"])
    return record


def cleanup(project, release, key):
    project = baseline._guard(project)
    record = require_accepted_archive(project, release, key)
    state = baseline._load(project)
    workflow = state["releases"][release].get("exchange_cleanup", {})
    if key not in workflow.get("deliveries", {}):
        raise ValueError("Нет сохранённого решения о завершении данной передачи")
    if record["source"]["role"] == "code":
        from exchange_cleanup import publish_cleanup
        result = publish_cleanup(project, release, key)
    else:
        source = project / "requirements-exchange" / key
        _plain_path(project, source)
        if source.exists():
            exchange().require_plain_exchange(source)
            if baseline.tree_hash(source) != record["archive_hash"]:
                raise ValueError("После архива появились новые требования или возвраты")
            # Only the archived exact packet is removed in the analytics work branch.
            shutil.rmtree(source)
        remote = exchange().git_value(project, "remote", "get-url", "origin")
        with tempfile.TemporaryDirectory(prefix="verify-analytics-cleanup-") as temporary:
            accepted = Path(temporary) / "analytics"
            _clone(remote, "main", accepted)
            accepted_packet = accepted / "requirements-exchange" / key
            _plain_path(accepted, accepted_packet)
            complete = not accepted_packet.exists()
        result = {"status": "completed" if complete else "awaiting-merge", "destination_role": "analytics",
                  "message": "Удаление принято в analytics/main" if complete else "Удаление подготовлено в рабочей ветке аналитики; требуется обычное сохранение и принятие"}
    workflow["deliveries"][key].update(state="completed" if result["status"] == "completed" else "cleanup-pending", publication=result)
    workflow["state"] = "completed" if all(item["state"] in {"completed", "retained", "not-required"} for item in workflow["deliveries"].values()) else "cleanup-pending"
    baseline._save(project, state)
    return result
