from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re

from workspace_paths import approved_plans_path


@dataclass
class ForecastScope:
    features: dict[str, str]
    exclusions: dict[str, dict]
    baselines: dict[str, Path]


def valid_slug(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]*", value) is not None


def unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Повторяющийся ключ JSON: {key}")
        result[key] = value
    return result


def load_forecast_scope(project_root: Path, quarter_id: str) -> ForecastScope:
    project_root = project_root.resolve()
    gantt_dir = project_root / "planning" / quarter_id / "gantt"
    config_path = gantt_dir / "actual-progress-features.json"
    if not config_path.exists():
        return ForecastScope({}, {}, {})
    payload = json.loads(config_path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] not in {1, 2}:
        raise ValueError(f"{config_path}: ожидается schema_version=1 или 2")
    allowed = {"schema_version", "features"}
    if payload["schema_version"] == 2:
        allowed.add("forecast_exclusions")
    if set(payload) - allowed or not isinstance(payload.get("features"), dict):
        raise ValueError(f"{config_path}: неверные поля конфигурации")
    feature_map = payload["features"]
    if any(not valid_slug(value) for pair in feature_map.items() for value in pair):
        raise ValueError(f"{config_path}: неверный slug")
    exclusions = payload.get("forecast_exclusions", {})
    if not isinstance(exclusions, dict) or any(not valid_slug(slug) for slug in exclusions):
        raise ValueError(f"{config_path}: неверный forecast_exclusions")

    baselines = {}
    for slug, decision in exclusions.items():
        if not isinstance(decision, dict) or set(decision) != {"state", "reason", "source", "analyst_confirmed"}:
            raise ValueError(f"{config_path}: {slug}: нужны state, reason, source, analyst_confirmed")
        if decision["state"] != "outside-quarter" or decision["analyst_confirmed"] is not True:
            raise ValueError(f"{config_path}: {slug}: исключение требует outside-quarter и подтверждения аналитика")
        for field in ("reason", "source"):
            value = decision[field]
            if not isinstance(value, str) or not value.strip() or len(value.splitlines()) != 1 or any(ord(char) < 32 for char in value):
                raise ValueError(f"{config_path}: {slug}: {field} должен быть непустой строкой без переводов строк")
        source = Path(decision["source"])
        resolved_source = (project_root / source).resolve()
        if source.is_absolute() or ".." in source.parts or not resolved_source.is_relative_to(project_root):
            raise ValueError(f"{config_path}: {slug}: source должен быть относительным путём внутри проекта")
        if not resolved_source.is_file() or not resolved_source.read_text(encoding="utf-8").strip():
            raise ValueError(f"{config_path}: {slug}: источник решения не найден или пуст: {source}")
        feature_dir = project_root / "features" / feature_map.get(slug, slug)
        if not feature_dir.is_dir():
            raise ValueError(f"{feature_dir}: функциональность не найдена; проверь actual-progress-features.json")
        evidence = []
        actualization = feature_dir / "planning/actualization.md"
        for path in (actualization, feature_dir / "execution-context.md"):
            if path.exists():
                evidence.append(path)
        for execution_dir in [feature_dir / "execution", *feature_dir.glob("slices/*/execution")]:
            if execution_dir.is_dir():
                evidence.extend(path for path in execution_dir.rglob("*") if path.is_file())
        for name in {slug, feature_dir.name}:
            overlay_path = gantt_dir / "includes/actual-progress" / f"FEATURE-{name}.puml"
            if overlay_path.exists():
                evidence.append(overlay_path)
        if evidence:
            paths = ", ".join(sorted(str(path.relative_to(project_root)) for path in evidence))
            raise ValueError(f"{config_path}: {slug}: исключение плановой фичи не может скрыть execution-источники или прежний Гант: {paths}")
        relative_map = actualization.relative_to(project_root).as_posix()
        for snapshot_path in approved_plans_path(project_root).glob("*.json"):
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            if relative_map in snapshot.get("actualization_baseline", {}):
                raise ValueError(f"{actualization}: approved actualization baseline was modified; исключение запрещено")
        for view in ("commander-plan", "quarter-plan"):
            baseline = gantt_dir / "includes" / view / f"FEATURE-{slug}.puml"
            if baseline.exists():
                if not baseline.is_file() or not baseline.read_text(encoding="utf-8").strip():
                    raise ValueError(f"{baseline}: исходный план пуст или недоступен")
                baselines[slug] = baseline
                break
        if slug not in baselines:
            raise ValueError(f"{config_path}: {slug}: нет исходного commander-plan/quarter-plan include")
    return ForecastScope(feature_map, exclusions, baselines)
