"""Release completion observations and reviewed, deployment-bound baseline promotion.

No tracker status is interpreted here. Callers supply complete membership and
explicit closed booleans with evidence from their reviewed tracker run.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

SECTIONS = ("domain", "requirements", "ui", "api", "data", "decisions")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError("Недопустимый идентификатор релиза")
    return value


def _guard(project):
    project = Path(project).resolve()
    result = subprocess.run(["git", "-C", str(project), "symbolic-ref", "--short", "HEAD"], capture_output=True, text=True)
    root = subprocess.run(["git", "-C", str(project), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if root.returncode or Path(root.stdout.strip()).resolve() != project:
        raise ValueError("PROJECT_ROOT должен быть корнем самостоятельного Git-репозитория")
    if result.returncode or result.stdout.strip() in {"main", "master"}:
        raise ValueError("Запись baseline требует рабочую ветку, отличную от main/master")
    return project


def _path(project):
    return Path(project) / "releases" / "baseline-status.json"


def _load(project):
    path = _path(project)
    value = json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "releases": {}}
    if (value.get("schema_version") != 1 or not isinstance(value.get("releases"), dict)
            or not isinstance(value.get("reconciliations", {}), dict)):
        raise ValueError("Неизвестный формат реестра baseline")
    return value


def _save(project, state):
    path = _path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        name = stream.name
    Path(name).replace(path)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _observation(value):
    release_id = _id(value.get("release_id"))
    tasks = value.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("tasks должен быть полным списком участников релиза")
    normalized = []
    for task in tasks:
        if not isinstance(task, dict) or not _text(task.get("id")):
            raise ValueError("Каждая задача требует id")
        closed = task.get("closed")
        if closed is not None and type(closed) is not bool:
            raise ValueError("closed должен быть true, false или null; статусы не угадываются")
        normalized.append({"id": task["id"], "closed": closed, "evidence": task.get("evidence")})
    if len({t["id"] for t in normalized}) != len(normalized):
        raise ValueError("Повторяющиеся задачи в составе релиза")
    return {"release_id": release_id, "membership_complete": value.get("membership_complete") is True,
            "membership_evidence": value.get("membership_evidence"),
            "tasks": sorted(normalized, key=lambda task: task["id"])}


def _eligible(observation):
    return bool(observation["membership_complete"] and _text(observation["membership_evidence"])
                and observation["tasks"] and all(task["closed"] is True and _text(task["evidence"]) for task in observation["tasks"]))


def observe(project, observation):
    """Persist a reviewed release observation; changing scope invalidates preparation."""
    project = _guard(project)
    observation = _observation(observation)
    state = _load(project)
    release_id = observation["release_id"]
    previous = state["releases"].get(release_id, {})
    scope_hash = digest(observation)
    if previous.get("scope_hash") == scope_hash:
        return previous
    entry = {"observation": observation, "scope_hash": scope_hash,
             "status": "candidate" if _eligible(observation) else "blocked",
             "history": previous.get("history", [])}
    if previous:
        entry["history"] = entry["history"] + [{key: val for key, val in previous.items() if key != "history"}]
    state["releases"][release_id] = entry
    _save(project, state)
    return entry


def scan(project):
    """Read-only list of unprocessed closed releases, including deferred candidates."""
    return [{"release_id": key, **value} for key, value in _load(project)["releases"].items()
            if value["status"] != "promoted" and _eligible(value["observation"])]


def defer(project, release_id, reason):
    project = _guard(project)
    if not _text(reason):
        raise ValueError("Укажите причину отсрочки")
    state = _load(project)
    entry = state["releases"][_id(release_id)]
    if entry["status"] == "promoted":
        raise ValueError("Релиз уже отражён в baseline")
    entry.update(status="deferred", deferred_reason=reason)
    _save(project, state)
    return entry


def tree_hash(path):
    path = Path(path)
    if not path.is_dir() or path.is_symlink():
        raise ValueError(f"Нет каталога baseline: {path}")
    files = {}
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise ValueError("Ссылки в baseline запрещены")
        if item.is_file():
            files[item.relative_to(path).as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()
    if not files:
        raise ValueError(f"Пустой раздел baseline: {path}")
    return digest(files)


def review_hashes(candidate):
    return {section: tree_hash(Path(candidate) / section) for section in SECTIONS}


def prepare(project, release_id, candidate, review):
    """Bind an explicitly reviewed complete candidate to observed scope and old baseline.

    review: {scope_hash, sections: {name: tree hash}, consistency_evidence: str}.
    Candidate must be a release-owned directory, never baseline/current.
    """
    project = _guard(project)
    release_id = _id(release_id)
    candidate = Path(candidate).resolve()
    release_root = (project / "releases" / release_id).resolve()
    if not candidate.is_relative_to(release_root) or candidate == release_root:
        raise ValueError("Кандидат должен находиться внутри каталога данного релиза")
    state = _load(project)
    entry = state["releases"][release_id]
    if not _eligible(entry["observation"]) or entry["status"] == "promoted":
        raise ValueError("Нет необработанного релиза с подтверждённо закрытым полным составом")
    if review.get("scope_hash") != entry["scope_hash"] or review.get("sections") != review_hashes(candidate) or not _text(review.get("consistency_evidence")):
        raise ValueError("Требуется актуальная проверка состава, всех разделов и согласованности")
    preparation = {"candidate": candidate.relative_to(project).as_posix(), "candidate_hash": tree_hash(candidate),
                   "base_hash": tree_hash(project / "baseline" / "current"), "review": review}
    entry.update(status="prepared", preparation=preparation)
    _save(project, state)
    return entry


def promote(project, release_id, scope_hash, deployment, analyst_confirmed=False):
    """Promote only an unchanged reviewed candidate with explicit deployment confirmation."""
    project = _guard(project)
    state = _load(project)
    release_id = _id(release_id)
    entry = state["releases"][release_id]
    if analyst_confirmed is not True or scope_hash != entry["scope_hash"]:
        raise ValueError("Требуется подтверждение аналитиком точного состава релиза")
    if not isinstance(deployment, dict) or not all(_text(deployment.get(key)) for key in ("version", "environment", "evidence")):
        raise ValueError("Нужны версия, среда и доказательство внедрения")
    if entry["status"] == "promoted":
        if deployment != entry["deployment"] or tree_hash(project / entry["snapshot"]) != entry["preparation"]["candidate_hash"]:
            raise ValueError("Повторная команда не совпадает с неизменяемой записью")
        return entry
    if entry["status"] != "prepared" or not _eligible(entry["observation"]):
        raise ValueError("Baseline не подготовлен")
    preparation = entry["preparation"]
    candidate = project / preparation["candidate"]
    if tree_hash(candidate) != preparation["candidate_hash"] or review_hashes(candidate) != preparation["review"]["sections"]:
        raise ValueError("Кандидат изменился после проверки")
    snapshot = _install_snapshot(project, preparation, release_id)
    entry.update(status="promoted", deployment=deployment, snapshot=snapshot.relative_to(project).as_posix())
    _save(project, state)
    return entry



def _install_snapshot(project, preparation, snapshot_name):
    candidate = project / preparation["candidate"]
    current = project / "baseline" / "current"
    current_hash = tree_hash(current)
    if current_hash not in {preparation["base_hash"], preparation["candidate_hash"]}:
        raise ValueError("Baseline изменился после подготовки")
    versions = project / "baseline" / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    snapshot = versions / snapshot_name
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.exists():
        if tree_hash(snapshot) != preparation["candidate_hash"]:
            raise ValueError("Неизменяемый снимок уже существует с другим содержимым")
    else:
        with tempfile.TemporaryDirectory(dir=versions) as temporary:
            staged = Path(temporary) / "snapshot"
            shutil.copytree(candidate, staged)
            staged.rename(snapshot)
    if current_hash != preparation["candidate_hash"]:
        # Preserve the prior deployed state even if legacy baseline had no snapshot.
        prior = versions / ("before-" + preparation["base_hash"])
        if prior.exists():
            if tree_hash(prior) != preparation["base_hash"]:
                raise ValueError("Снимок предыдущего baseline повреждён")
        else:
            with tempfile.TemporaryDirectory(dir=versions) as temporary:
                staged_prior = Path(temporary) / "prior"
                shutil.copytree(current, staged_prior)
                staged_prior.rename(prior)
        backup = current.parent / ".baseline-current-before-promotion"
        if backup.exists():
            raise ValueError("Незавершённая предыдущая замена baseline; требуется восстановление")
        with tempfile.TemporaryDirectory(dir=current.parent) as temporary:
            staged = Path(temporary) / "current"
            shutil.copytree(snapshot, staged)
            current.rename(backup)
            try:
                staged.rename(current)
            except BaseException:
                backup.rename(current)
                raise
            shutil.rmtree(backup)
    return snapshot

def observe_run(project, run_id, review_file):
    """Observe a verified release run using a separate explicit closure review.

    Review: {run_id, reconciled_sha256, history_review, tasks: [
      {id, closed: bool|null, analyst_confirmed: true,
       source: {file, sha256, quote}}]}.
    Missing closure decisions become unknown, never inferred from development/QA.
    """
    from tracker_workflow import verified_result, run_root, digest_object

    project = _guard(project)
    completion, result = verified_result(run_id)
    scope = result.get("scope", {})
    if scope.get("kind") != "release" or scope.get("intent") != "update-planning" or len(scope.get("ids", [])) != 1:
        raise ValueError("Нужен завершённый run актуализации одного релиза")
    if completion.get("application_state", {}).get("state") == "paused":
        raise ValueError("Применение tracker-run приостановлено")
    review_file = Path(review_file).resolve()
    review_raw = review_file.read_bytes()
    review = json.loads(review_raw)
    reconciled_hash = completion["reconciled_sha256"]
    if review.get("run_id") != run_id or review.get("reconciled_sha256") != reconciled_hash:
        raise ValueError("Проверка закрытия относится к другому результату сверки")
    history_path = Path(review["history_review"]).resolve()
    history = json.loads(history_path.read_text())
    expected = (run_root(run_id) / "history" / (digest_object(history) + ".json")).resolve()
    if history_path != expected or history.get("run_id") != run_id or history.get("reconciled_sha256") != reconciled_hash:
        raise ValueError("Требуется неизменённый сохранённый history-review данного run")
    if history.get("status") != "history-review-ready" or history.get("project_root") != str(project):
        raise ValueError("История не проверена либо относится к другому проекту")
    provider = scope.get("provider")
    if provider not in {"jira", "sbertrek"}:
        raise ValueError("Нет однозначного провайдера релиза")
    field = provider + "_key"
    records = [*result.get("issues", []), *result.get("skipped", []), *result.get("excluded", [])]
    record_keys = {item[field] for item in records if _text(item.get(field))}
    membership = history.get("release_membership_evidence") or result.get("release_membership_evidence") or {}
    raw_keys = membership.get("keys", [])
    if not isinstance(raw_keys, list) or not all(_text(key) for key in raw_keys):
        raise ValueError("Некорректный состав релиза в доказательстве membership")
    membership_keys = set(raw_keys)
    keys = record_keys | membership_keys
    complete = (membership.get("complete") is True and membership.get("release") == scope["ids"][0]
                and len(raw_keys) == len(membership_keys) and membership_keys == record_keys)
    decisions = review.get("tasks", [])
    if not isinstance(decisions, list):
        raise ValueError("tasks проверки закрытия должен быть списком")
    by_key = {}
    for decision in decisions:
        key = decision.get("id")
        if key not in keys or key in by_key:
            raise ValueError("Повторная или посторонняя задача в проверке закрытия")
        closed = decision.get("closed")
        if closed is not None and type(closed) is not bool:
            raise ValueError("Закрытие требует явного true, false или null")
        evidence = None
        if closed is not None and decision.get("analyst_confirmed") is True:
            source = decision.get("source", {})
            if all(_text(source.get(name)) for name in ("file", "sha256", "quote")):
                raw = Path(source["file"]).read_bytes()
                if hashlib.sha256(raw).hexdigest() != source["sha256"] or source["quote"] not in raw.decode("utf-8"):
                    raise ValueError("Изменилось доказательство решения о закрытии")
                evidence = json.dumps({"run_id": run_id, "reconciled_sha256": reconciled_hash,
                                       "closure_review_sha256": hashlib.sha256(review_raw).hexdigest(),
                                       "source": source}, ensure_ascii=False, sort_keys=True)
        by_key[key] = {"id": key, "closed": closed if evidence else None, "evidence": evidence}
    observation = {"release_id": scope["ids"][0], "membership_complete": complete,
                   "membership_evidence": json.dumps({"run_id": run_id, "reconciled_sha256": reconciled_hash,
                                                       "history_review_sha256": hashlib.sha256(history_path.read_bytes()).hexdigest(),
                                                       "membership": membership}, ensure_ascii=False, sort_keys=True),
                   "tasks": [by_key.get(key, {"id": key, "closed": None, "evidence": None}) for key in sorted(keys)]}
    return observe(project, observation)


def _reconciliation_source(project, source, quote=False):
    """Only project-owned, byte-bound evidence; no source interpretation."""
    from project_layout import safe_path

    if not isinstance(source, dict) or not _text(source.get("path")):
        raise ValueError("Нужен путь доказательства исторической консолидации")
    path = safe_path(project, source["path"])
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != source.get("sha256"):
        raise ValueError("Доказательство консолидации отсутствует или изменилось")
    if quote and (not _text(source.get("quote")) or source["quote"] not in path.read_text()):
        raise ValueError("Нет точной цитаты решения аналитика")


def _reconciliation_review(project, review):
    from project_layout import layout, feature_root

    if not isinstance(review, dict) or review.get("kind") != "historical-baseline-reconciliation":
        raise ValueError("Требуется отдельная проверка исторической консолидации")
    if review.get("environment") != "ПРОМ":
        raise ValueError("Консолидация описывает только подтверждённый ПРОМ")
    version = review.get("product_release_version")
    if version is not None and not _text(version):
        raise ValueError("Версия продукта должна быть явной строкой либо null")
    _reconciliation_source(project, review.get("analyst_decision"), quote=True)
    if not _text(review.get("consistency_evidence")):
        raise ValueError("Нужна проверка согласованности всех разделов")
    scope = review.get("scope")
    if not isinstance(scope, list) or not scope:
        raise ValueError("Нужен непустой точный состав фич и поставок")
    index = layout(project)
    seen = set()
    for item in scope:
        feature, delivery = _id(item.get("feature_id")), _id(item.get("delivery_key"))
        if delivery in seen:
            raise ValueError("Повторная поставка в составе консолидации")
        seen.add(delivery)
        if index is not None:
            target = index["deliveries"].get(delivery)
            if target is None or target["feature_id"] != feature or target.get("quarter") != item.get("quarter"):
                raise ValueError("Идентичность фичи, поставки или квартала не совпадает с реестром")
        elif feature != delivery or not feature_root(project, feature).is_dir():
            raise ValueError("Нет подтверждённой идентичности legacy-фичи")
        basis = item.get("deployment_basis")
        if basis not in {"analyst-confirmed", "completed-quarter-gantt"}:
            raise ValueError("Нужен явный источник решения о внедрении")
        sources = item.get("evidence")
        if not isinstance(sources, list) or not sources:
            raise ValueError("Каждая поставка требует доказательств задач или Ганта")
        kinds = set()
        for source in sources:
            if source.get("kind") not in {"tasks", "gantt"}:
                raise ValueError("Поддерживаются доказательства tasks и gantt")
            kinds.add(source["kind"])
            _reconciliation_source(project, source)
        if basis == "completed-quarter-gantt" and "gantt" not in kinds:
            raise ValueError("Решение по завершённому квартальному Ганту требует его снимок")
    if review.get("scope_hash") != digest(scope):
        raise ValueError("Состав консолидации изменился после проверки")


def prepare_reconciliation(project, documentation_version, candidate, review):
    """Register historical documentation consolidation separately from known releases.

    Candidate lives in releases/baseline-reconciliations/<documentation_version>/.
    No product release identity or full release membership is inferred or created.
    """
    project = _guard(project)
    documentation_version = _id(documentation_version)
    _reconciliation_review(project, review)
    candidate = Path(candidate).resolve()
    root = (project / "releases" / "baseline-reconciliations" / documentation_version).resolve()
    if not candidate.is_relative_to(root) or candidate == root:
        raise ValueError("Кандидат должен находиться внутри каталога данной консолидации")
    if review.get("sections") != review_hashes(candidate) or review.get("candidate_hash") != tree_hash(candidate):
        raise ValueError("Требуется проверка точного кандидата и всех шести разделов")
    state = _load(project)
    records = state.setdefault("reconciliations", {})
    existing = records.get(documentation_version)
    review_hash = digest(review)
    relative = candidate.relative_to(project).as_posix()
    if existing and existing["status"] == "promoted":
        if existing["review_hash"] != review_hash or existing["preparation"]["candidate"] != relative:
            raise ValueError("Для изменения опубликованной консолидации нужна новая версия документации")
        if tree_hash(project / existing["snapshot"]) != review["candidate_hash"]:
            raise ValueError("Неизменяемый снимок консолидации повреждён")
        return existing
    if review.get("base_hash") != tree_hash(project / "baseline" / "current"):
        raise ValueError("Исходный baseline изменился после проверки")
    history = []
    if existing:
        if existing["status"] != "prepared" or review["base_hash"] != existing["preparation"]["base_hash"]:
            raise ValueError("Повторная подготовка требует прежний исходный baseline")
        if existing["review_hash"] == review_hash and existing["preparation"]["candidate"] == relative:
            return existing
        history = [*existing.get("history", []), {key: value for key, value in existing.items() if key != "history"}]
    entry = {"kind": "historical-baseline-reconciliation", "status": "prepared",
             "documentation_version": documentation_version, "review_hash": review_hash,
             "scope_hash": review["scope_hash"], "review": copy.deepcopy(review),
             "environment": "ПРОМ", "product_release_version": review.get("product_release_version"),
             "preparation": {"candidate": relative, "candidate_hash": review["candidate_hash"],
                             "base_hash": review["base_hash"]}}
    if history:
        entry["history"] = history
    records[documentation_version] = entry
    _save(project, state)
    return entry


def promote_reconciliation(project, documentation_version, review_hash, analyst_confirmed=False):
    """Promote reviewed deployed documentation without marking any release processed."""
    project = _guard(project)
    state = _load(project)
    documentation_version = _id(documentation_version)
    entry = state.get("reconciliations", {})[documentation_version]
    if analyst_confirmed is not True or review_hash != entry["review_hash"] or digest(entry["review"]) != review_hash:
        raise ValueError("Нужно подтверждение аналитика точной проверки консолидации")
    if entry["status"] == "promoted":
        if tree_hash(project / entry["snapshot"]) != entry["preparation"]["candidate_hash"]:
            raise ValueError("Неизменяемый снимок консолидации повреждён")
        return entry
    if entry["status"] != "prepared":
        raise ValueError("Консолидация не подготовлена")
    _reconciliation_review(project, entry["review"])
    preparation = entry["preparation"]
    candidate = project / preparation["candidate"]
    if tree_hash(candidate) != preparation["candidate_hash"] or review_hashes(candidate) != entry["review"]["sections"]:
        raise ValueError("Кандидат консолидации изменился после проверки")
    snapshot = _install_snapshot(project, preparation, "documentation/" + documentation_version)
    entry.update(status="promoted", snapshot=snapshot.relative_to(project).as_posix())
    _save(project, state)
    return entry
