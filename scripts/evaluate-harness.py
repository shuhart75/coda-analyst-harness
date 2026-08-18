#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def run(command: list[str], cwd: Path | None = None) -> tuple[int, str]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    return result.returncode, result.stdout + result.stderr


def evaluate_project(project: Path) -> int:
    config = project / ".workflow/evals/golden-scenarios.json"
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
    source = Path(__file__).resolve().parents[1]
    metrics: dict[str, object] = {"scenarios": []}
    scenarios: list[dict[str, object]] = metrics["scenarios"]  # type: ignore[assignment]

    with tempfile.TemporaryDirectory(prefix="analyst-harness-eval-") as temp:
        project = Path(temp) / "project"
        code, output = run(["bash", str(source / "scripts/scaffold-project.sh"), str(project)])
        scenarios.append({"name": "clean-scaffold", "passed": code == 0, "output": output[-1000:]})

        code, output = run([sys.executable, str(project / ".workflow/tools/harnessctl.py"), "doctor", str(project)])
        scenarios.append({"name": "clean-doctor", "passed": code == 0, "output": output[-1000:]})

        active = project / ".workflow/active-mode.md"
        active.write_text(active.read_text(encoding="utf-8").replace("modes/planning.md", "modes/requirements.md"), encoding="utf-8")
        code, output = run([sys.executable, str(project / ".workflow/tools/validate-workflow.py"), str(project)])
        scenarios.append({"name": "seeded-mode-mismatch", "passed": code != 0, "output": output[-1000:]})

        active.write_text(active.read_text(encoding="utf-8").replace("modes/requirements.md", "modes/planning.md"), encoding="utf-8")
        marker = project / "features/demo"
        marker.mkdir(parents=True)
        (marker / "feature.md").write_text("USER CONTENT\n", encoding="utf-8")
        code, output = run(["bash", str(source / "scripts/scaffold-feature.sh"), str(project), "demo"])
        preserved = (marker / "feature.md").read_text(encoding="utf-8") == "USER CONTENT\n"
        scenarios.append({"name": "destructive-scaffold-blocked", "passed": code != 0 and preserved, "output": output[-1000:]})

    passed = sum(1 for item in scenarios if item["passed"])
    metrics["passed"] = passed
    metrics["total"] = len(scenarios)
    metrics["score"] = passed / len(scenarios) if scenarios else 0
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0 if passed == len(scenarios) else 1


if __name__ == "__main__":
    raise SystemExit(main())
