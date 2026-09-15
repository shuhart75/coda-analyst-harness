from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re

from actual_progress_scope import unique_keys


@dataclass
class ActualLayout:
    project_start: date
    sections: list[dict[str, str]]

    @property
    def priorities(self) -> dict[str, int]:
        return {section["feature"]: index for index, section in enumerate(self.sections)}

    @property
    def titles(self) -> dict[str, str]:
        return {section["feature"]: section["title"] for section in self.sections}

    @property
    def execution_features(self) -> list[str]:
        return [section["feature"] for section in self.sections
                if Path(section["include"]).name.startswith("FEATURE-")]

    def includes(self, gantt: Path) -> list[Path]:
        return list(dict.fromkeys(gantt / section["include"] for section in self.sections))


def load_layout(gantt: Path) -> ActualLayout | None:
    gantt = gantt.resolve()
    path = gantt / "actual-progress-layout.json"
    if path.is_symlink():
        raise ValueError(f"{path}: символическая ссылка недопустима")
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
    if (not isinstance(payload, dict)
            or set(payload) != {"schema_version", "analyst_confirmed", "source", "project_start", "sections"}
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
            or payload["analyst_confirmed"] is not True):
        raise ValueError(f"{path}: требуется подтверждённое описание диаграммы схемы 1")
    raw_source = payload["source"]
    if (not isinstance(raw_source, str) or not raw_source or Path(raw_source).is_absolute()
            or ".." in Path(raw_source).parts):
        raise ValueError(f"{path}: неверный источник решения")
    source = gantt / raw_source
    if source.resolve() != source or not source.is_file() or not source.read_text(encoding="utf-8").strip():
        raise ValueError(f"{path}: нужен непустой источник решения внутри каталога Ганта")
    raw_start = payload["project_start"]
    if not isinstance(raw_start, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_start):
        raise ValueError(f"{path}: project_start требует YYYY-MM-DD")
    start = date.fromisoformat(raw_start)
    sections = payload["sections"]
    if not isinstance(sections, list) or not sections:
        raise ValueError(f"{path}: sections должен быть непустым списком")
    features, titles, seen_includes = set(), set(), set()
    previous_include = None
    for section in sections:
        if not isinstance(section, dict) or set(section) != {"feature", "title", "include"}:
            raise ValueError(f"{path}: раздел требует feature, title, include")
        feature, title, include = (section[key] for key in ("feature", "title", "include"))
        if not isinstance(feature, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", feature) or feature in features:
            raise ValueError(f"{path}: неверная или повторная фича")
        if (not isinstance(title, str) or not title.strip() or title != title.strip()
                or len(title.splitlines()) != 1 or title in titles or "--" in title):
            raise ValueError(f"{path}: неверное или повторное название раздела")
        expected = f"includes/actual-progress/FEATURE-{feature}.puml"
        if not isinstance(include, str) or (include != expected and not re.fullmatch(
                r"includes/actual-progress/FORECAST-[a-zA-Z0-9_-]+\.puml", include)):
            raise ValueError(f"{path}: include не соответствует фиче или сохранённому FORECAST")
        if (gantt / include).resolve() != gantt / include:
            raise ValueError(f"{path}: ссылка в пути include недопустима")
        if include in seen_includes and (include != previous_include or include == expected):
            raise ValueError(f"{path}: общий FORECAST должен занимать последовательные разделы")
        features.add(feature)
        titles.add(title)
        seen_includes.add(include)
        previous_include = include
    return ActualLayout(start, sections)


def validate_layout(layout: ActualLayout, gantt: Path, outputs: dict[Path, str], preserved: set[Path]) -> None:
    expected = {gantt / section["include"] for section in layout.sections
                if Path(section["include"]).name.startswith("FEATURE-")}
    existing = set((gantt / "includes/actual-progress").glob("FEATURE-*.puml"))
    if set(outputs) != expected or existing - expected:
        raise ValueError("Состав actual-progress не совпадает с подтверждённым описанием диаграммы")
    forecasts = set(layout.includes(gantt)) - expected
    if forecasts != preserved:
        raise ValueError("FORECAST в описании диаграммы не совпадает с реестром сохранённого прогноза")
    for path in forecasts:
        expected_titles = [section["title"] for section in layout.sections if gantt / section["include"] == path]
        actual_titles = re.findall(r"^-- (.+?) --\s*$", path.read_text(encoding="utf-8"), re.MULTILINE)
        if actual_titles != expected_titles:
            raise ValueError(f"{path}: названия или порядок разделов FORECAST отличаются от описания")
