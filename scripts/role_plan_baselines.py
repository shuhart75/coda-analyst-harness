from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
import hashlib
import json
import re

from workspace_paths import approved_plans_path


@dataclass(frozen=True)
class RoleBaseline:
    role: str
    alias: str
    path: str
    view: str
    start: date
    finish: date
    duration: int
    decision_source: str
    decision_sha256: str


def local_path(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise ValueError("PLAN: нужен относительный путь внутри проекта")
    relative = Path(value)
    path = root / relative
    if relative.is_absolute() or ".." in relative.parts or path.resolve() != path:
        raise ValueError(f"PLAN: недопустимый путь: {value}")
    return path


def source_calendar(text: str, path: Path) -> set[date]:
    closed = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                closed.add(date.fromisoformat(line.strip().replace("/", "-")))
    directives = [line.strip().lower() for line in text.splitlines()
                  if re.search(r"\b(?:is|are)\s+(?:open|closed)\b", line, re.IGNORECASE)
                  and not line.lstrip().startswith("'")]
    expected = {"saturday are closed", "sunday are closed"}
    expected.update(f"{value.isoformat().replace('-', '/')} is closed" for value in closed)
    normalized = [line.replace("-", "/") for line in directives]
    if set(normalized) != expected or len(normalized) != len(expected):
        raise ValueError("PLAN: календарь исходной диаграммы не совпадает с closed-days.txt и рабочей неделей")
    return closed


def read_bar(text: str, role: str, alias: str, path: str, view: str,
             closed: set[date], decision_source: str, decision_sha256: str) -> RoleBaseline:
    declarations = re.findall(rf"^\s*\[[^\]\r\n]+\]\s+as\s+\[{re.escape(alias)}\](.*)$", text, re.MULTILINE)
    if len(declarations) != 1:
        raise ValueError(f"PLAN: {path}: alias {alias} отсутствует или повторяется")
    start_match = re.fullmatch(r"\s*(?:on\s+\{[A-Za-z0-9_]+\}\s+)?starts\s+(\d{4}[/-]\d{2}[/-]\d{2})\s*", declarations[0])
    commands = re.findall(rf"^\s*\[{re.escape(alias)}\]\s+(.+?)\s*$", text, re.MULTILINE)
    ends = [re.fullmatch(r"ends\s+(\d{4}[/-]\d{2}[/-]\d{2})", command) for command in commands]
    ends = [match for match in ends if match]
    if not start_match or len(ends) != 1 or any(not re.fullmatch(
        r"ends\s+\d{4}[/-]\d{2}[/-]\d{2}|is colored in [A-Za-z0-9#]+|is \d+% completed", command
    ) for command in commands):
        raise ValueError(f"PLAN: {alias}: поддерживаются только явные абсолютные starts/ends без зависимостей")
    start = date.fromisoformat(start_match[1].replace("/", "-"))
    finish = date.fromisoformat(ends[0][1].replace("/", "-"))
    duration = sum((start + timedelta(days=offset)).weekday() < 5
                   and start + timedelta(days=offset) not in closed
                   for offset in range((finish - start).days + 1))
    if duration <= 0:
        raise ValueError(f"PLAN: {alias}: исходная полоса не содержит рабочих дней")
    return RoleBaseline(role, alias, path, view, start, finish, duration, decision_source, decision_sha256)


def load_role_baselines(root: Path, quarter: str, decisions: dict, expand) -> dict[str, list[RoleBaseline]]:
    gantt = root / "planning" / quarter / "gantt"
    result = {}
    claimed = set()
    for slug, decision in decisions.items():
        if not isinstance(decision, dict) or set(decision) != {
            "analyst_confirmed", "reason", "source", "view", "sha256", "bars",
        }:
            raise ValueError(f"PLAN: {slug}: неверные поля role_baselines")
        if decision["analyst_confirmed"] is not True:
            raise ValueError(f"PLAN: {slug}: требуется подтверждение аналитика")
        reason = decision["reason"]
        if not isinstance(reason, str) or not reason.strip() or any(ord(char) < 32 for char in reason):
            raise ValueError(f"PLAN: {slug}: нужна непустая однострочная reason")
        source = local_path(root, decision["source"])
        if not source.is_file() or not source.read_text(encoding="utf-8").strip():
            raise ValueError(f"PLAN: {slug}: источник решения отсутствует или пуст")
        decision_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        view = decision["view"]
        if view not in ("quarter-plan", "commander-plan"):
            raise ValueError(f"PLAN: {slug}: нужен quarter-plan или commander-plan")
        plan = gantt / f"{view}.puml"
        expanded, dependencies = expand(plan)
        if any(line.lstrip().startswith("!") and line.strip() != "!$now = %now()" for line in expanded.splitlines()):
            raise ValueError(f"PLAN: {slug}: условные и вычисляемые исходники не поддерживаются")
        if len(dependencies) != len(set(dependencies)):
            raise ValueError(f"PLAN: {slug}: повторное подключение исходника")
        if any(not path.is_relative_to(gantt) for path in dependencies):
            raise ValueError(f"PLAN: {slug}: исходники должны находиться внутри квартала")
        calendar = gantt / "closed-days.txt"
        expected_paths = {path.relative_to(root).as_posix() for path in dependencies + [calendar]}
        hashes = decision["sha256"]
        if not isinstance(hashes, dict) or set(hashes) != expected_paths:
            raise ValueError(f"PLAN: {slug}: sha256 должен покрывать диаграмму, все includes и closed-days.txt")
        for relative, checksum in hashes.items():
            path = local_path(root, relative)
            if path == calendar and checksum is None and not path.exists():
                continue
            if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise ValueError(f"PLAN: {relative}: неверный sha256")
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
                raise ValueError(f"PLAN: {relative}: sha256 изменился; нужна проверка исходного плана")
        for snapshot_path in approved_plans_path(root).glob("*.json"):
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            for relative, checksum in snapshot.get("files", {}).items():
                if relative in hashes and hashes[relative] != checksum:
                    raise ValueError(f"PLAN: {relative}: approved plan was modified")
        closed = source_calendar(expanded, calendar)
        bars = decision["bars"]
        if not isinstance(bars, list) or not bars:
            raise ValueError(f"PLAN: {slug}: нужен непустой список bars")
        roles = set()
        result[slug] = []
        for bar in bars:
            if not isinstance(bar, dict) or set(bar) != {"role", "alias", "path"}:
                raise ValueError(f"PLAN: {slug}: полоса требует role, alias, path")
            role, alias = bar["role"], bar["alias"]
            if not isinstance(role, str) or role not in {"AN", "BE", "FE", "QA"} or role in roles:
                raise ValueError(f"PLAN: {slug}: неизвестная или повторная роль")
            if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
                raise ValueError(f"PLAN: {slug}: неверный alias")
            path = local_path(root, bar["path"])
            if path not in dependencies or (path != plan and not path.is_relative_to(gantt / "includes" / view)):
                raise ValueError(f"PLAN: {slug}: полоса должна быть подключена выбранным исходным планом")
            if (view, alias) in claimed:
                raise ValueError(f"PLAN: {slug}: alias уже назначен другой фиче")
            baseline = read_bar(path.read_text(encoding="utf-8"), role, alias, bar["path"], view, closed, decision["source"], decision_sha256)
            expanded_bar = read_bar(expanded, role, alias, bar["path"], view, closed, decision["source"], decision_sha256)
            if expanded_bar != baseline:
                raise ValueError(f"PLAN: {alias}: полоса переопределена другим include")
            if len(re.findall(rf"\bas\s+\[{re.escape(alias)}\]", expanded)) != 1:
                raise ValueError(f"PLAN: {alias}: повторный alias в исходной диаграмме")
            result[slug].append(baseline)
            roles.add(role)
            claimed.add((view, alias))
    return result
