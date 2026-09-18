from __future__ import annotations

from pathlib import Path
import json
import re

from actual_progress_scope import load_forecast_scope, valid_slug
from actual_progress_layout import load_layout
from tracker_registry import read_registry


def inside_project(project: Path, path: Path) -> Path:
    if not path.resolve().is_relative_to(project):
        raise ValueError(f"Источник выходит за пределы проекта: {path}")
    return path


def select_features(project: Path, quarter: str | None, feature: str | None) -> dict[str, dict]:
    project = project.expanduser().resolve()
    if not project.is_dir():
        raise ValueError(f"Проект не найден: {project}")
    if not quarter and not feature:
        raise ValueError("Укажи --quarter или --feature")
    if feature and not valid_slug(feature):
        raise ValueError("Некорректный slug фичи")
    selected: dict[str, dict] = {}
    if quarter:
        if not re.fullmatch(r"[0-9]{4}-Q[1-4]", quarter):
            raise ValueError("Квартал должен иметь вид YYYY-QN")
        gantt = inside_project(project, project / "planning" / quarter / "gantt")
        if not gantt.is_dir():
            raise ValueError(f"Гант квартала не найден: {gantt}")
        scope = load_forecast_scope(project, quarter)
        selections: dict[str, list[str]] = {}
        for view in ("quarter-plan", "commander-plan", "actual-progress"):
            directory = inside_project(project, gantt / "includes" / view)
            for path in sorted(directory.glob("FEATURE-*.puml")):
                inside_project(project, path)
                slug = path.stem.removeprefix("FEATURE-")
                if not valid_slug(slug):
                    raise ValueError(f"Некорректный slug include: {path}")
                selections.setdefault(slug, []).append(path.relative_to(project).as_posix())
        for slug in set(scope.features) | set(scope.exclusions) | set(scope.preserved) | set(scope.role_baselines):
            selections.setdefault(slug, []).append((gantt / "actual-progress-features.json").relative_to(project).as_posix())
        for slug, sources in sorted(selections.items()):
            name = scope.features.get(slug, slug)
            entry = selected.setdefault(name, {"feature": name, "sources": [], "forecast_state": None})
            entry["sources"].extend(sources)
            if slug in scope.exclusions:
                entry["forecast_state"] = "outside-quarter"
            elif slug in scope.preserved:
                entry["forecast_state"] = "preserve-existing"
        layout = load_layout(gantt)
        if layout is not None:
            ordered = {}
            for priority, section in enumerate(layout.sections, start=1):
                name = scope.features.get(section["feature"], section["feature"])
                if name in ordered:
                    raise ValueError(f"Несколько разделов соответствуют одной фиче: {name}")
                entry = selected.get(name, {"feature": name, "sources": [], "forecast_state": None})
                entry["sources"].append((gantt / "actual-progress-layout.json").relative_to(project).as_posix())
                entry.update(title=section["title"], priority=priority, order_confirmed=True)
                ordered[name] = entry
            selected = ordered
        if not selected:
            raise ValueError("В квартале нет явной выборки FEATURE/includes или конфигурации")
        if feature:
            if feature not in selected:
                raise ValueError(f"Фича {feature} не включена в выбранный квартал")
            selected = {feature: selected[feature]}
    else:
        selected[feature] = {"feature": feature, "sources": ["explicit-feature"], "forecast_state": None}
    return selected


def preview_scope(project: Path, provider: str, quarter: str | None, feature: str | None) -> dict:
    project = project.expanduser().resolve()
    if provider not in {"jira", "sbertrek"}:
        raise ValueError("Требуется явный провайдер jira или sbertrek")
    selected = select_features(project, quarter, feature)
    references: dict[str, list[dict]] = {}
    omitted = []
    identity_requests = []
    epics = {}
    limitations = ["known-registry-tasks-only", "new-epic-members-not-discovered"]
    column = "Jira" if provider == "jira" else "SberTrek"
    if quarter and any(not entry.get("order_confirmed") for entry in selected.values()):
        limitations.append("feature-order-and-roster-need-confirmation")
    for name, entry in selected.items():
        directory = inside_project(project, project / "features" / name)
        if not directory.is_dir():
            raise ValueError(f"Фича не найдена: {directory}")
        association = inside_project(project, directory / "execution/tracker-scope.json")
        entry["epics"] = {"jira": [], "sbertrek": []}
        if association.exists():
            config = json.loads(association.read_text(encoding="utf-8"))
            if (not isinstance(config, dict) or config.get("schema_version") != 1
                    or not isinstance(config.get("epics", {}), dict)
                    or set(config.get("epics", {})) - {"jira", "sbertrek"}):
                raise ValueError(f"Некорректная конфигурация эпиков: {association}")
            for source, keys in config.get("epics", {}).items():
                if not isinstance(keys, list) or any(not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*-[1-9][0-9]*", key) for key in keys):
                    raise ValueError(f"Некорректные ключи эпиков: {association}")
                entry["epics"][source] = list(dict.fromkeys(keys))
            entry["sources"].append(association.relative_to(project).as_posix())
        epics[name] = entry["epics"][provider]
        if not epics[name]:
            other = "sbertrek" if provider == "jira" else "jira"
            identity_requests.extend({"feature": name, "kind": "epic", "source_provider": other,
                                      "source_key": key, "target_provider": provider,
                                      "purpose": "identity-only", "for_choices": ["epics", "combined"]}
                                     for key in entry["epics"][other])
        registry = inside_project(project, directory / "execution/tasks.md")
        paths = ([registry] if registry.is_file() else []) + sorted(directory.glob("slices/*/execution/tasks.md"))
        entry["registries"] = [path.relative_to(project).as_posix() for path in paths]
        if not paths and entry["forecast_state"] is None:
            limitations.append(f"execution-registry-missing:{name}")
        for path in paths:
            inside_project(project, path)
            tables, note = read_registry(path)
            if note:
                limitations.append(f"{note}:{path.relative_to(project).as_posix()}")
            rows = [row for table in tables for row in table]
            for index, row in enumerate(rows, start=1):
                reference = {
                    "feature": name, "registry": path.relative_to(project).as_posix(),
                    "row": index, "task_id": row.get("Task ID") or row.get("Jira") or "",
                    "role": row.get("Role", ""), "status": row.get("Status", ""),
                }
                kind = row.get("Kind", "").casefold()
                if row.get("Role", "").upper() not in {"AN", "BE", "FE", "QA"}:
                    omitted.append({**reference, "reason": "outside-supported-roles"})
                    continue
                if kind != "real":
                    omitted.append({**reference, "reason": "virtual-work" if kind == "virtual" else "kind-not-confirmed"})
                    continue
                key = row.get(column, "").strip()
                if key in {"", "-", "—"}:
                    other = "sbertrek" if provider == "jira" else "jira"
                    other_key = row.get("SberTrek" if other == "sbertrek" else "Jira", "").strip()
                    matched = re.fullmatch(r"([A-Z][A-Z0-9_]*-[1-9][0-9]*)(?:/(AN|BE|FE|QA))?", other_key)
                    if matched:
                        if matched.group(2) and matched.group(2) != row.get("Role", "").upper():
                            raise ValueError(f"Роль в ключе не совпадает с Role: {path}: {other_key}")
                        identity_requests.append({**reference, "kind": "task", "source_provider": other,
                                                  "source_key": matched.group(1), "target_provider": provider,
                                                  "purpose": "identity-only", "for_choices": ["tasks", "combined"]})
                    omitted.append({**reference, "reason": f"no-confirmed-{provider}-key"})
                    continue
                legacy = re.fullmatch(r"([A-Z][A-Z0-9_]*-[1-9][0-9]*)/(AN|BE|FE|QA)", key)
                if legacy:
                    if legacy.group(2) != row.get("Role", "").upper():
                        raise ValueError(f"Роль в ключе не совпадает с Role: {path}: {key}")
                    key = legacy.group(1)
                if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[1-9][0-9]*", key):
                    raise ValueError(f"Неверный подтверждённый ключ {column}: {path}: {key}")
                references.setdefault(key, []).append(reference)
    shared = {key: sorted({item["feature"] for item in rows}) for key, rows in references.items()
              if len({item["feature"] for item in rows}) > 1}
    if shared:
        limitations.append("task-feature-mapping-needs-review")
    if len(references) > 50:
        limitations.append(f"requested-keys-exceed-response-limit:{len(references)}:50")
    if omitted:
        limitations.append("registry-rows-outside-tracker-scope")
    return {
        "status": "tracker-scope-preview",
        "project_root": str(project), "quarter": quarter,
        "scope": {"kind": "tasks", "provider": provider, "ids": sorted(references), "intent": "update-planning"},
        "features": list(selected.values()),
        "epics": epics, "identity_requests": identity_requests,
        "scope_choices": [
            {"id": "tasks", "label": "Актуализировать по номерам задач в фиче"},
            {"id": "epics", "label": "По эпикам", "epics": epics, "confirm_or_supply_epics": True},
            {"id": "combined", "label": "И по эпикам и по задачам", "epics": epics, "confirm_or_supply_epics": True},
        ],
        "references": {key: references[key] for key in sorted(references)},
        "omitted": omitted, "shared_keys": shared, "limitations": sorted(set(limitations)),
        "requires_analyst_confirmation": True, "tracker_calls_performed": False,
        "next_action": {"type": "confirm-scope" if references else "clarify-tracker-scope",
                        "required_choice": ["tasks", "epics", "combined"],
                        "epic_confirmation_for": ["epics", "combined"],
                        "identity_resolution_required": bool(identity_requests)},
    }
