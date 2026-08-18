#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from workspace_paths import (
    active_mode_path,
    approved_plans_path,
    code_registry_path,
    ensure_local_state,
    harness_root,
    project_rules_path,
    run_state_path,
    runs_path,
)


RUN_STAGES = {
    "planning": [
        "intake",
        "delta",
        "role-stories",
        "estimates",
        "dependencies",
        "capacity-schedule",
        "plan-review",
        "approved",
    ],
    "requirements": [
        "context",
        "root-requirements",
        "slices",
        "detail-packs",
        "cross-feature-impact",
        "task-candidates",
        "tail-cleanup",
        "verified",
    ],
    "implementation": [
        "orient",
        "code-research",
        "plan",
        "act",
        "verify",
        "review",
        "checkpoint",
        "complete",
    ],
    "qa": [
        "orient",
        "coverage",
        "test-design",
        "execute",
        "classify",
        "route-gaps",
        "verify",
        "complete",
    ],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def run_tool(project: Path, name: str, extra: list[str] | None = None) -> int:
    command = [sys.executable, str(harness_root() / "scripts" / name), str(project)]
    command.extend(extra or [])
    return subprocess.run(command, check=False).returncode


def doctor_command(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    codes = [
        run_tool(project, "validate-structure.py"),
        run_tool(project, "validate-workflow.py"),
        run_tool(project, "validate-links.py"),
        run_tool(project, "validate-context.py", ["--strict-features"] if args.strict else []),
        run_tool(project, "validate-requirements-profile.py"),
        run_tool(project, "validate-planning.py"),
        run_tool(project, "validate-trace.py", ["--strict"] if args.strict else []),
    ]
    handoff_tool = harness_root() / "scripts/handoffctl.py"
    for manifest in sorted(project.glob("features/*/handoffs/*/handoff.json")):
        codes.append(
            subprocess.run(
                [sys.executable, str(handoff_tool), "validate", str(manifest.parent)],
                check=False,
            ).returncode
        )
    return 1 if any(codes) else 0


def language_check_command(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    extra: list[str] = []
    if args.feature:
        extra.extend(["--feature", args.feature])
    if args.all:
        extra.append("--all")
    return run_tool(project, "validate-language.py", extra)


def requirements_check_command(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    extra = ["--feature", args.feature] if args.feature else []
    return run_tool(project, "validate-requirements-profile.py", extra)


def session_brief_command(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    ensure_local_state()
    active = active_mode_path().read_text(encoding="utf-8", errors="ignore")
    mode = next((line.split(":", 1)[1].strip() for line in active.splitlines() if line.startswith("mode:")), "unknown")
    harness_paths = [
        harness_root() / "AGENTS.md",
        harness_root() / "core/llm-contract.md",
        harness_root() / "core/code-inspection.md",
        code_registry_path(),
        harness_root() / f"modes/{mode}.md",
    ]
    paths: list[str] = []
    if args.feature:
        paths.extend(
            [
                f"features/{args.feature}/context-summary.md",
                f"features/{args.feature}/artifact-map.md",
                f"features/{args.feature}/feature.md",
                f"features/{args.feature}/requirements.md",
            ]
        )
    if args.feature and args.slice:
        paths.extend(
            [
                f"features/{args.feature}/slices/{args.slice}/context-summary.md",
                f"features/{args.feature}/slices/{args.slice}/slice.md",
                f"features/{args.feature}/slices/{args.slice}/requirements/frontend.md",
                f"features/{args.feature}/slices/{args.slice}/requirements/backend.md",
            ]
        )
    existing = [path for path in paths if (project / path).exists()]
    output = run_state_path() / "session-brief.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "# Session Brief\n\n"
        f"Generated: `{utc_now()}`  \nActive mode: `{mode}`  \n"
        f"Feature: `{args.feature or '-'}`  \nSlice: `{args.slice or '-'}`\n\n"
        "## Read First\n\n"
        + "".join(f"- `{path}`\n" for path in harness_paths if path.exists())
        + "".join(f"- `{path}`\n" for path in existing)
        + "".join(f"- `{path}`\n" for path in sorted(project_rules_path(project).glob("*.md")))
        + "\n## Guardrails\n\n"
        "- Repository artifacts remain the source of truth.\n"
        "- Respect the active mode write boundary.\n"
        "- Approved quarter and commander plans are immutable baselines.\n"
        "- Route later scope into task candidates and actual-progress.\n",
        encoding="utf-8",
    )
    print(output)
    return 0


def run_init_command(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    ensure_local_state()
    stages = RUN_STAGES[args.kind]
    run_id = args.run_id or f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{args.kind}"
    run_dir = runs_path() / run_id
    if run_dir.exists():
        raise SystemExit(f"Run already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    code_root = args.code_root
    if not code_root and args.role:
        repos_path = code_registry_path()
        if repos_path.exists():
            registry = json.loads(repos_path.read_text(encoding="utf-8"))
            repositories = registry.get("repositories", {})
            if registry.get("schema_version") == 2 and isinstance(repositories, list):
                coda = next((item for item in repositories if isinstance(item, dict) and item.get("id") == "coda"), None)
                if coda:
                    location = coda.get("location", {})
                    environment = location.get("environment")
                    configured = os.environ.get(environment, "") if isinstance(environment, str) else ""
                    base = Path(configured).expanduser() if configured else project / location.get("relative_to_documents", "../coda")
                    contour = {"BE": "backend", "FE": "frontend"}.get(args.role)
                    if contour:
                        contour_path = coda.get("contours", {}).get(contour, {}).get("path")
                        if contour_path:
                            base = base / contour_path
                    code_root = str(base.resolve())
            elif isinstance(repositories, dict):
                role_key = {"AN": "requirements", "BE": "backend", "FE": "frontend", "QA": "qa"}.get(args.role)
                candidates = repositories.get(role_key, []) if role_key else []
                if candidates:
                    code_root = candidates[0]
    input_paths: list[Path] = []
    if args.feature:
        input_paths.extend(
            [
                project / "features" / args.feature / "requirements.md",
                project / "features" / args.feature / "feature.md",
            ]
        )
    if args.feature and args.slice:
        input_paths.extend(
            [
                project / "features" / args.feature / "slices" / args.slice / "slice.md",
                project / "features" / args.feature / "slices" / args.slice / "requirements/frontend.md",
                project / "features" / args.feature / "slices" / args.slice / "requirements/backend.md",
            ]
        )
    payload = {
        "schema_version": 1,
        "id": run_id,
        "kind": args.kind,
        "status": "active",
        "stage": stages[0],
        "stage_index": 0,
        "stages": stages,
        "feature": args.feature,
        "slice": args.slice,
        "role": args.role,
        "project_root": str(project),
        "code_root": code_root,
        "max_iterations_per_stage": args.max_iterations,
        "iteration": 0,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "events": [],
        "verifiers": [],
        "input_hashes": {str(path.relative_to(project)): sha256(path) for path in input_paths if path.is_file()},
        "planning_rules": {
            "approved_is_immutable": True,
            "fe_lag_after_be_open_days": 3,
            "risk_buffer_min_percent": 20,
            "efficiency_defaults": {"AN": 0.8, "BE": 0.7, "FE": 0.65, "QA": 0.8},
        } if args.kind == "planning" else {},
    }
    (run_dir / "run.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run_dir / "work-packet.md").write_text(
        f"# Work Packet — {run_id}\n\n"
        f"Kind: `{args.kind}`  \nFeature: `{args.feature or '-'}`  \nSlice: `{args.slice or '-'}`\n\n"
        "## Objective\n\n- \n\n## Inputs\n\n"
        + (f"- `features/{args.feature}/requirements.md`\n" if args.feature else "")
        + (f"- `features/{args.feature}/slices/{args.slice}/slice.md`\n" if args.feature and args.slice else "")
        + (f"- Code root: `{code_root}`\n" if code_root else "")
        + "\n## Required Verification\n\n- Configure deterministic verifier argv entries in `run.json`.\n",
        encoding="utf-8",
    )
    print(run_dir / "run.json")
    return 0


def run_advance_command(args: argparse.Namespace) -> int:
    path = Path(args.run).resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload["status"] != "active":
        raise SystemExit(f"Run is not active: {payload['status']}")
    event = {"at": utc_now(), "stage": payload["stage"], "result": args.result, "evidence": args.evidence}
    payload.setdefault("events", []).append(event)
    if args.result == "pass":
        if payload["stage_index"] + 1 >= len(payload["stages"]):
            payload["status"] = "complete"
        else:
            payload["stage_index"] += 1
            payload["stage"] = payload["stages"][payload["stage_index"]]
            payload["iteration"] = 0
    else:
        payload["iteration"] += 1
        if payload["iteration"] >= payload["max_iterations_per_stage"]:
            payload["status"] = "escalated"
    payload["updated_at"] = utc_now()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{payload['status']}: {payload['stage']} iteration={payload['iteration']}")
    return 0


def run_verify_command(args: argparse.Namespace) -> int:
    path = Path(args.run).resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    project_root = Path(args.project or payload.get("project_root") or Path.cwd()).resolve()
    base = Path(payload.get("code_root") or project_root).resolve()
    results: list[dict[str, object]] = []
    failed = False
    for rel, expected in payload.get("input_hashes", {}).items():
        source = project_root / rel
        actual = sha256(source) if source.is_file() else "missing"
        if actual != expected:
            results.append({"name": f"input freshness: {rel}", "returncode": 3, "output": "source artifact changed after run initialization"})
            failed = True
    for verifier in payload.get("verifiers", []):
        name = verifier.get("name", "check")
        argv = verifier.get("argv", [])
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
            results.append({"name": name, "returncode": 2, "output": "invalid argv"})
            failed = True
            continue
        cwd = (base / verifier.get("cwd", ".")).resolve()
        result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False)
        results.append(
            {
                "name": name,
                "argv": argv,
                "cwd": str(cwd),
                "returncode": result.returncode,
                "output": (result.stdout + result.stderr)[-4000:],
            }
        )
        failed = failed or result.returncode != 0
    payload.setdefault("events", []).append(
        {"at": utc_now(), "stage": payload["stage"], "result": "verify-fail" if failed else "verify-pass", "checks": results}
    )
    payload["updated_at"] = utc_now()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for item in results:
        print(f"{item['name']}: exit={item['returncode']}")
    if not results:
        print("No verifiers configured")
        return 1
    return 1 if failed else 0


def plan_approve_command(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    quarter_dir = project / "planning" / args.quarter
    state = quarter_dir / "plan-state.md"
    if not state.exists():
        raise SystemExit(f"Missing plan state: {state}")
    check_codes = [
        run_tool(project, "validate-workflow.py"),
        run_tool(project, "validate-planning.py"),
        run_tool(project, "validate-links.py"),
    ]
    if any(check_codes):
        print("Plan approval blocked by validation errors")
        return 1
    text = state.read_text(encoding="utf-8")
    text = __import__("re").sub(r"^Status:[^\S\r\n]*.*$", "Status: `approved`", text, count=1, flags=__import__("re").MULTILINE)
    text = __import__("re").sub(r"^Approver:[^\S\r\n]*.*$", f"Approver: `{args.by}`", text, count=1, flags=__import__("re").MULTILINE)
    text = __import__("re").sub(r"^Approved at:[^\S\r\n]*.*$", f"Approved at: `{utc_now()}`", text, count=1, flags=__import__("re").MULTILINE)
    state.write_text(text, encoding="utf-8")
    targets = [quarter_dir / "gantt/quarter-plan.puml", quarter_dir / "gantt/commander-plan.puml"]
    targets.extend(sorted((quarter_dir / "gantt/includes/quarter-plan").glob("*.puml")))
    targets.extend(sorted((quarter_dir / "gantt/includes/commander-plan").glob("*.puml")))
    order_file = quarter_dir / "gantt/order.txt"
    feature_slugs = [line.strip() for line in order_file.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")] if order_file.exists() else []
    actualization_baseline: dict[str, list[list[str]]] = {}
    for feature_slug in feature_slugs:
        actualization = project / "features" / feature_slug / "planning/actualization.md"
        if not actualization.exists():
            continue
        baseline_rows: list[list[str]] = []
        for line in actualization.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.startswith("| STORY-"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) >= 4:
                baseline_rows.append([cells[0], cells[2], cells[3]])
        if baseline_rows:
            actualization_baseline[str(actualization.relative_to(project))] = baseline_rows
    snapshot = {
        "schema_version": 1,
        "quarter": args.quarter,
        "approved_by": args.by,
        "approved_at": utc_now(),
        "files": {str(path.relative_to(project)): sha256(path) for path in targets if path.is_file()},
        "actualization_baseline": actualization_baseline,
    }
    snapshot_path = approved_plans_path(project) / f"{args.quarter}.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(snapshot_path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Internal analyst-harness runtime")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("project")
    doctor.add_argument("--strict", action="store_true")
    doctor.set_defaults(func=doctor_command)

    language = sub.add_parser("language-check")
    language.add_argument("project")
    language.add_argument("--feature")
    language.add_argument("--all", action="store_true")
    language.set_defaults(func=language_check_command)

    requirements = sub.add_parser("requirements-check")
    requirements.add_argument("project")
    requirements.add_argument("--feature")
    requirements.set_defaults(func=requirements_check_command)

    brief = sub.add_parser("session-brief")
    brief.add_argument("project")
    brief.add_argument("--feature")
    brief.add_argument("--slice")
    brief.set_defaults(func=session_brief_command)

    run_init = sub.add_parser("run-init")
    run_init.add_argument("project")
    run_init.add_argument("kind", choices=sorted(RUN_STAGES))
    run_init.add_argument("--run-id")
    run_init.add_argument("--feature")
    run_init.add_argument("--slice")
    run_init.add_argument("--code-root")
    run_init.add_argument("--role", choices=("AN", "BE", "FE", "QA"))
    run_init.add_argument("--max-iterations", type=int, default=3)
    run_init.set_defaults(func=run_init_command)

    advance = sub.add_parser("run-advance")
    advance.add_argument("run")
    advance.add_argument("result", choices=("pass", "fail"))
    advance.add_argument("--evidence", default="")
    advance.set_defaults(func=run_advance_command)

    verify = sub.add_parser("run-verify")
    verify.add_argument("run")
    verify.add_argument("--project")
    verify.set_defaults(func=run_verify_command)

    approve = sub.add_parser("plan-approve")
    approve.add_argument("project")
    approve.add_argument("quarter")
    approve.add_argument("--by", required=True)
    approve.set_defaults(func=plan_approve_command)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
