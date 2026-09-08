#!/usr/bin/env python3
from __future__ import annotations

import json
import importlib.util
import re
import sys
from pathlib import Path

from workspace_paths import eval_config_path
from workspace_entrypoint import is_local_entrypoint


ASSERTION_TYPES = {"exists", "not_exists", "contains", "not_contains", "valid_json", "plantuml_structure"}


def reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def project_path(project: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError("assertion path must be a nonempty relative path")
    path = (project / value).resolve()
    if not path.is_relative_to(project.resolve()):
        raise ValueError(f"path outside project: {value}")
    return path


def plantuml_structure(path: Path, project: Path) -> None:
    spec = importlib.util.spec_from_file_location("plantuml_includes", Path(__file__).with_name("expand-plantuml-includes.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    visited: set[Path] = set()

    def check_includes(current: Path, stack: list[Path]) -> None:
        if current in stack:
            raise ValueError("PlantUML include cycle: " + " -> ".join(str(item.relative_to(project)) for item in stack + [current]))
        if current in visited:
            return
        visited.add(current)
        for line in current.read_text(encoding="utf-8").splitlines():
            match = module.INCLUDE_RE.match(line)
            if match:
                target = module.clean_include_target(match.group(1))
                if target.startswith(("http:", "https:", "<", "$", "%")) or Path(target).is_absolute():
                    raise ValueError(f"unsupported non-local include: {target}")
                included = project_path(project, str(current.parent.relative_to(project) / target))
                check_includes(included, stack + [current])
            elif line.lstrip().startswith("!include"):
                raise ValueError(f"unsupported include directive: {line.strip()}")

    check_includes(path, [])
    active: str | None = None
    pairs = 0
    in_comment = False
    for line in module.expand_file(path, []):
        stripped = line.strip()
        if in_comment:
            in_comment = "'/" not in stripped
            continue
        if stripped.startswith("/'"):
            in_comment = "'/" not in stripped[2:]
            continue
        match = re.match(r"^@(start|end)([a-z]+)\b", stripped)
        if not match:
            continue
        action, kind = match.groups()
        if action == "start":
            if active is not None:
                raise ValueError("nested PlantUML start markers")
            active = kind
        else:
            if active != kind:
                raise ValueError(f"unmatched @end{kind}")
            active = None
            pairs += 1
    if in_comment or active is not None or pairs == 0:
        raise ValueError("PlantUML requires closed comments and at least one complete start/end pair")


def check_assertion(project: Path, assertion: object) -> None:
    if not isinstance(assertion, dict):
        raise ValueError("assertion must be an object")
    kind = assertion.get("type")
    if not isinstance(kind, str) or kind not in ASSERTION_TYPES:
        raise ValueError(f"unknown assertion type: {kind!r}")
    path = project_path(project, assertion.get("path"))
    if kind == "exists":
        if not path.exists():
            raise ValueError(f"missing {assertion['path']}")
    elif kind == "not_exists":
        if path.exists() and not (assertion["path"] == "AGENTS.md" and is_local_entrypoint(path)):
            raise ValueError(f"unexpected path {assertion['path']}")
    elif kind == "valid_json":
        json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_json_constant)
    elif kind == "plantuml_structure":
        plantuml_structure(path, project)
    else:
        value = assertion.get("value")
        if not isinstance(value, str) or not value:
            raise ValueError(f"{kind} requires a nonempty string value")
        text = path.read_text(encoding="utf-8")
        if kind == "contains" and value not in text:
            raise ValueError(f"{assertion['path']} does not contain {value!r}")
        if kind == "not_contains" and value in text:
            raise ValueError(f"{assertion['path']} unexpectedly contains {value!r}")


def evaluate_project(project: Path) -> int:
    project = project.resolve()
    config = eval_config_path(project)
    if not config.exists():
        print(f"Missing project eval config: {config}")
        return 1
    try:
        payload = json.loads(config.read_text(encoding="utf-8"), parse_constant=reject_json_constant)
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("expected evaluation schema_version 1")
        scenarios = payload.get("scenarios")
        if not isinstance(scenarios, list) or not scenarios:
            raise ValueError("scenarios must be a nonempty list")
        names: set[str] = set()
        for scenario in scenarios:
            if not isinstance(scenario, dict) or not isinstance(scenario.get("name"), str) or not scenario["name"].strip():
                raise ValueError("scenario requires a nonempty name")
            if scenario["name"] in names:
                raise ValueError(f"duplicate scenario: {scenario['name']}")
            names.add(scenario["name"])
            if not isinstance(scenario.get("assertions"), list) or not scenario["assertions"]:
                raise ValueError(f"{scenario['name']}: assertions must be a nonempty list")
    except (OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({"project": str(project), "error": str(exc)}, ensure_ascii=False))
        return 1
    results: list[dict[str, object]] = []
    for scenario in scenarios:
        failures: list[str] = []
        for assertion in scenario.get("assertions", []):
            try:
                check_assertion(project, assertion)
            except (OSError, UnicodeError, ValueError, RecursionError) as exc:
                failures.append(f"{assertion!r}: {exc}")
        results.append({"name": scenario["name"], "passed": not failures, "failures": failures})
    passed = sum(1 for item in results if item["passed"])
    report = {"project": str(project), "passed": passed, "total": len(results), "score": passed / len(results) if results else 0, "scenarios": results}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed == len(results) else 1


def main() -> int:
    if len(sys.argv) > 1:
        return evaluate_project(Path(sys.argv[1]).resolve())
    print("Usage: evaluate-harness.py <documents-repository>")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
