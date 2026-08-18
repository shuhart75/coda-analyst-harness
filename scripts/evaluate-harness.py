#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from workspace_paths import eval_config_path


def run(command: list[str], cwd: Path | None = None) -> tuple[int, str]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    return result.returncode, result.stdout + result.stderr


def evaluate_project(project: Path) -> int:
    config = eval_config_path(project)
    if not config.exists():
        print(f"Missing project eval config: {config}")
        return 1
    payload = json.loads(config.read_text(encoding="utf-8"))
    results: list[dict[str, object]] = []
    for scenario in payload.get("scenarios", []):
        failures: list[str] = []
        for assertion in scenario.get("assertions", []):
            path = project / assertion["path"]
            assertion_type = assertion["type"]
            if assertion_type == "exists":
                if not path.exists():
                    failures.append(f"missing {assertion['path']}")
                continue
            if assertion_type == "not_exists":
                if path.exists():
                    failures.append(f"unexpected path {assertion['path']}")
                continue
            if not path.is_file():
                failures.append(f"missing file {assertion['path']}")
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            value = assertion.get("value", "")
            if assertion_type == "contains" and value not in text:
                failures.append(f"{assertion['path']} does not contain {value!r}")
            if assertion_type == "not_contains" and value in text:
                failures.append(f"{assertion['path']} unexpectedly contains {value!r}")
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
