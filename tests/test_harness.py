from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False, env=env)


class HarnessTests(unittest.TestCase):
    def scaffold(self, root: Path) -> Path:
        project = root / "project"
        result = run("bash", str(ROOT / "scripts/scaffold-project.sh"), str(project))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = run("git", "init", "-b", "main", str(project))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return project

    def scaffold_analyst_workspace(self, root: Path) -> tuple[Path, Path]:
        documents = root / "documents"
        result = run("bash", str(ROOT / "scripts/scaffold-project.sh"), str(documents))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for command in (
            ("git", "init", "-b", "main", str(documents)),
            ("git", "-C", str(documents), "config", "user.name", "Harness Test"),
            ("git", "-C", str(documents), "config", "user.email", "harness@example.test"),
            ("git", "-C", str(documents), "remote", "add", "origin", "ssh://git@stash.delta.sbrf.ru:7999/rscon/documents.git"),
            ("git", "-C", str(documents), "add", "."),
            ("git", "-C", str(documents), "commit", "-m", "initial"),
        ):
            result = run(*command)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        coda = root / "coda"
        (coda / "backend/src").mkdir(parents=True)
        (coda / "frontend/src").mkdir(parents=True)
        (coda / "backend/AGENTS.md").write_text("# Backend SDD\n", encoding="utf-8")
        (coda / "frontend/AGENTS.md").write_text("# Frontend SDD\n", encoding="utf-8")
        (coda / "backend/src/Registry.java").write_text("class Registry { String productCode; }\n", encoding="utf-8")
        (coda / "frontend/src/Registry.tsx").write_text("export const Registry = () => null;\n", encoding="utf-8")
        for command in (
            ("git", "init", "-b", "main", str(coda)),
            ("git", "-C", str(coda), "config", "user.name", "Harness Test"),
            ("git", "-C", str(coda), "config", "user.email", "harness@example.test"),
            ("git", "-C", str(coda), "remote", "add", "origin", "ssh://git@stash.delta.sbrf.ru:7999/rscon/coda.git"),
            ("git", "-C", str(coda), "add", "."),
            ("git", "-C", str(coda), "commit", "-m", "initial"),
        ):
            result = run(*command)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return documents, coda

    def write_pending_handoff_package(self, package: Path, package_id: str = "demo", revision: int = 1) -> None:
        package.mkdir(parents=True, exist_ok=True)
        request = package / "request.md"
        request.write_text("# Задание\n\nREQ-DEMO-001\n\nSCN-DEMO-001\n", encoding="utf-8")
        request_hash = hashlib.sha256(request.read_bytes()).hexdigest()
        statuses = ["pending", "delivered", "delivered-with-deviations", "partially-delivered", "no-change-required", "not-delivered", "rejected-package"]
        coverage_statuses = ["pending", "already-implemented", "implemented-as-required", "implemented-with-deviation", "implemented-with-scope-change", "partially-implemented", "not-implemented", "deferred", "blocked-dependency", "blocked-input-ambiguity", "not-applicable"]
        recommendations = ["pending", "no-action", "promote-to-baseline", "update-requirement", "keep-open", "defer", "move-to-other-change", "cancel", "investigate"]
        receipt = {
            "schema_version": 4,
            "package_id": package_id,
            "package_revision": revision,
            "request_id": package_id,
            "request_version": revision,
            "request_sha256": request_hash,
            "target_repository": "coda",
            "target_contour": "backend",
            "received_at": None,
            "completed_at": None,
            "source_before": {"commit": None, "branch": None, "working_tree_state": "unknown", "relevant_uncommitted_paths": []},
            "source_after": {"commit": None, "branch": None, "working_tree_state": "unknown", "relevant_uncommitted_paths": []},
            "status": "pending",
            "allowed_statuses": statuses,
            "allowed_coverage_statuses": coverage_statuses,
            "allowed_follow_up_recommendations": recommendations,
            "commits": [],
            "generated_or_changed_artifacts": [],
            "requirement_coverage": [{"requirement": "REQ-DEMO-001", "status": "pending", "behavior_before": None, "delivered_behavior": None, "deviation_from_input": None, "remaining_work": None, "follow_up_recommendation": "pending", "suggested_destination": None, "evidence": [], "commit_sha256": [], "verification": []}],
            "scenario_coverage": [{"scenario": "SCN-DEMO-001", "status": "pending", "behavior_before": None, "delivered_behavior": None, "deviation_from_input": None, "remaining_work": None, "follow_up_recommendation": "pending", "suggested_destination": None, "covered_by": ["REQ-DEMO-001"], "evidence": [], "commit_sha256": [], "verification": []}],
            "additional_deliveries": [],
            "remaining_work": [],
            "baseline_feedback": [],
            "requirements_feedback": [],
            "verification": [],
        }
        receipt_path = package / "receipt.template.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        manifest = {
            "schema_version": 6,
            "package_id": package_id,
            "package_revision": revision,
            "request": {"id": package_id, "version": revision},
            "target": {"repository": "coda", "contour": "backend"},
            "payload": [
                {"path": "request.md", "sha256": request_hash},
                {"path": "receipt.template.json", "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()},
            ],
            "requirements": ["REQ-DEMO-001"],
            "scenarios": ["SCN-DEMO-001"],
            "scenario_requirement_map": {"SCN-DEMO-001": ["REQ-DEMO-001"]},
            "delivery_policy": {"input": "immutable-comparison-point", "feedback": "receipt-then-analyst-review"},
            "allowed_package_statuses": statuses,
            "allowed_coverage_statuses": coverage_statuses,
            "allowed_follow_up_recommendations": recommendations,
        }
        (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_clean_scaffold_passes_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self.scaffold(Path(temp))
            env = {**os.environ, "CODA_ANALYST_STATE_ROOT": str(Path(temp) / "state")}
            result = run(sys.executable, str(ROOT / "scripts/harnessctl.py"), "doctor", str(project), env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_content_repository_has_no_embedded_harness(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self.scaffold(Path(temp))
            for path in ("AGENTS.md", ".workflow", ".vscode"):
                self.assertFalse((project / path).exists(), path)

    def test_structure_requires_complete_baseline_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self.scaffold(Path(temp))
            (project / "baseline/current/domain/aggregates.md").unlink()
            result = run(sys.executable, str(ROOT / "scripts/validate-structure.py"), str(project))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("baseline/current/domain/aggregates.md", result.stdout)

    def test_analyst_workspace_resolves_and_guards_code_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            documents, coda = self.scaffold_analyst_workspace(root)
            tool = ROOT / "scripts/code-inspect.py"

            result = run(sys.executable, str(tool), "doctor", str(documents))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["repositories"][0]["head"], run("git", "-C", str(coda), "rev-parse", "HEAD").stdout.strip())

            result = run(
                sys.executable,
                str(tool),
                "locate",
                str(documents),
                "productCode",
                "--contour",
                "backend",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            located = json.loads(result.stdout)
            self.assertEqual(located["matches"], ["backend/src/Registry.java"])
            self.assertFalse(located["truncated"])

            result = run(
                sys.executable,
                str(tool),
                "locate",
                str(documents),
                "product(Code|Id)",
                "--contour",
                "backend",
                "--regex",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["matches"], ["backend/src/Registry.java"])

            state_home = root / "state"
            env = {**os.environ, "XDG_STATE_HOME": str(state_home)}
            result = subprocess.run(
                [
                    sys.executable,
                    str(tool),
                    "begin",
                    str(documents),
                    "--contour",
                    "backend",
                    "--feature",
                    "registry",
                    "--query",
                    "Найти productCode",
                ],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = Path(result.stdout.splitlines()[0])
            self.assertTrue(state.is_file())
            result = run(sys.executable, str(tool), "verify", str(state))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            config_path = coda / ".git/config"
            config_before = config_path.read_bytes()
            config_path.write_bytes(config_before + b"\n# unexpected analyst-side change\n")
            result = run(sys.executable, str(tool), "verify", str(state))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("repository_config_sha256", result.stdout)
            config_path.write_bytes(config_before)

            result = run(sys.executable, str(tool), "setup", str(documents))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("scripts/workspace.py bootstrap", result.stdout)
            self.assertFalse((root / "AGENTS.md").exists())
            self.assertFalse((root / "rscon-analyst.code-workspace").exists())

            run_file = ROOT / "scripts/harnessctl.py"
            state_root = root / "workspace-state"
            run_env = {**os.environ, "CODA_ANALYST_STATE_ROOT": str(state_root)}
            result = run(
                sys.executable,
                str(run_file),
                "run-init",
                str(documents),
                "implementation",
                "--run-id",
                "code-root-test",
                "--role",
                "BE",
                env=run_env,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            run_payload = json.loads((state_root / "runs/code-root-test/run.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(run_payload["code_root"]), coda / "backend")
            run_payload["verifiers"] = [{
                "name": "forbidden-code-write",
                "argv": [sys.executable, "-c", "from pathlib import Path; Path('forbidden').write_text('x')"],
            }]
            run_path = state_root / "runs/code-root-test/run.json"
            run_path.write_text(json.dumps(run_payload), encoding="utf-8")
            result = run(sys.executable, str(run_file), "run-verify", str(run_path), env=run_env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("роли code запрещён", result.stderr)
            self.assertFalse((coda / "backend/forbidden").exists())

            source = coda / "backend/src/Registry.java"
            source.write_text(source.read_text(encoding="utf-8") + "// changed\n", encoding="utf-8")
            result = run(sys.executable, str(tool), "verify", str(state))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('"result": "changed"', result.stdout)
            result = run(sys.executable, str(tool), "begin", str(documents), "--contour", "backend")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("нужен чистый клон", result.stdout)
            result = run(sys.executable, str(tool), "doctor", str(documents))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("исследование заблокировано", result.stdout)
            result = run(sys.executable, str(tool), "locate", str(documents), "productCode", "--contour", "backend")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("поиск заблокирован", result.stdout)

    def test_scaffold_contains_per_item_developer_handoff_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self.scaffold(Path(temp))
            contract = ROOT / "core/developer-handoff.md"
            self.assertTrue(contract.exists())
            text = contract.read_text(encoding="utf-8")
            self.assertIn("returns/tasks.md", text)
            self.assertIn("returns/tasks/<task-id>.md", text)
            self.assertIn("returns/summary.md", text)
            self.assertTrue((ROOT / "scripts/requirements-exchange.py").exists())
            self.assertTrue((ROOT / "templates/exchange/AGENTS.template.md").exists())
            self.assertFalse((project / ".workflow").exists())

    def test_scaffold_contains_feature_delivery_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self.scaffold(Path(temp))
            templates = ROOT / "templates/handoff"
            required = (
                "handoff-root.feature.template.json",
                "handoff-root-feature-agents.template.md",
                "handoff-root-feature-readme.template.md",
                "feature-package-readme.template.md",
                "feature-request.template.md",
                "feature-manifest.template.json",
                "development-tasks-instruction.template.md",
                "development-tasks-index.template.md",
                "development-task-card.template.md",
                "decomposition-receipt.template.json",
                "implementation-receipt.template.json",
                "test-receipt.template.json",
            )
            for name in required:
                self.assertTrue((templates / name).is_file(), name)
            root_manifest = json.loads((templates / "handoff-root.feature.template.json").read_text(encoding="utf-8"))
            self.assertEqual(root_manifest["schema_version"], 3)
            self.assertEqual(root_manifest["package_kind"], "feature-delivery")
            self.assertEqual(root_manifest["transport_policy"], {
                "creation": "on-request",
                "repository_archives": "forbidden",
                "destination": "~/Downloads",
            })
            self.assertEqual(root_manifest["agent_contract"]["path"], "AGENTS.md")
            self.assertTrue(root_manifest["agent_contract"]["required"])
            registry = json.loads((ROOT / "templates/workflow/code-repos.template.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["schema_version"], 3)
            code = next(item for item in registry["repositories"] if item["id"] == "code")
            self.assertEqual(code["repository_id"], "coda")
            self.assertEqual(code["access"], "read-only")
            self.assertEqual(code["write_policy"]["allowed_paths"], ["requirements-exchange/**"])
            self.assertFalse(code["write_policy"]["user_prompt_can_override"])
            self.assertEqual(code["location"]["relative_to_analytical"], "../coda")
            self.assertEqual(code["contours"]["backend"]["path"], "backend")
            self.assertTrue((ROOT / "core/code-inspection.md").is_file())
            self.assertTrue((ROOT / "scripts/code-inspect.py").is_file())
            self.assertTrue((ROOT / "templates/research/code-evidence.template.yaml").is_file())
            contract = (ROOT / "core/developer-handoff.md").read_text(encoding="utf-8")
            self.assertIn("обязательный аудит", contract)
            self.assertIn("core/requirements-audit.md", contract)
            self.assertIn("контрольной суммой", contract)
            self.assertIn("returns/tasks.md", contract)
            self.assertIn("returns/tasks/<task-id>.md", contract)
            self.assertIn("returns/summary.md", contract)
            commands = (ROOT / "templates/workflow/command-cheatsheet.template.md").read_text(encoding="utf-8")
            self.assertIn("сформируй пакет для разработки", commands)
            self.assertIn("передаём в разработку", commands)
            self.assertIn("отдаём требования разработчикам", commands)
            self.assertIn("До явного подтверждения пакет не создаётся", commands)
            self.assertIn("проверь результаты разработки", commands)
            self.assertIn("уже содержит согласованную разработчиками декомпозицию", commands)
            self.assertIn("не создаёт срезы", commands)
            self.assertNotIn("features/<feature>/tasks/", commands)

    def test_scaffold_contains_requirements_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self.scaffold(Path(temp))
            self.assertTrue((ROOT / "core/requirements-profile.md").exists())
            self.assertTrue((ROOT / "core/requirements-audit.md").exists())
            self.assertTrue((ROOT / "core/requirements-wording.md").exists())
            self.assertTrue((ROOT / "scripts/validate-requirements-profile.py").exists())
            self.assertTrue((ROOT / "scripts/validate-requirements-wording.py").exists())
            active = (ROOT / "templates/requirements/feature-requirements.template.md").read_text(encoding="utf-8")
            legacy = (ROOT / "templates/requirements/feature-requirements.readable.template.md").read_text(encoding="utf-8")
            self.assertIn("Формат: **компактная спецификация функциональности**", active)
            self.assertNotIn("Статус:", active)
            self.assertIn("### REQ-<FEATURE>-001", active)
            self.assertIn("#### Сценарий:", active)
            self.assertIn("**Когда**", active)
            self.assertIn("## Влияние на соседние функциональности", active)
            self.assertNotIn("## Сводная трассировка", active)
            self.assertNotIn("ISO/IEC/IEEE 29148:2018", active)
            self.assertNotIn("Карточка среза", active)
            self.assertIn("Устаревшее имя шаблона", legacy)
            audit = (ROOT / "core/requirements-audit.md").read_text(encoding="utf-8")
            self.assertIn("Уровень 1. Отдельные правила", audit)
            self.assertIn("Уровень 2. Взаимодействие всего набора", audit)
            self.assertIn("Уровень 3. Готовность к передаче", audit)
            self.assertIn("проверку изолированного читателя", audit)
            run_loop = (ROOT / "core/run-loop.md").read_text(encoding="utf-8")
            self.assertIn("`Влияние на соседние функциональности`", run_loop)
            self.assertNotIn("`Доработки затронутых функциональностей`", run_loop)
            commands = (ROOT / "templates/workflow/command-catalog.template.md").read_text(encoding="utf-8")
            self.assertIn("проверь формулировки требований", commands)
            self.assertIn("validate-requirements-wording.py", commands)
            self.assertIn("сверь SberTrek и Jira", commands)
            self.assertIn("Трекеры задач: только чтение", commands)
            self.assertTrue((ROOT / "core/tracker-reading.md").exists())
            self.assertTrue((ROOT / "scripts/trackerctl.py").exists())
            tracker_contract = (ROOT / "core/tracker-reading.md").read_text(encoding="utf-8")
            self.assertIn("config-status", tracker_contract)
            self.assertIn("must_stop: true", tracker_contract)
            self.assertIn("`head`, `tail`, `grep`, `jq`", tracker_contract)
            self.assertIn("делегировать чтение", tracker_contract)
            self.assertIn("status: tracker-read-reconciled", tracker_contract)
            self.assertIn("snapshot-metadata", tracker_contract)
            self.assertIn("snapshot-issue", tracker_contract)
            self.assertIn("snapshot-not-found", tracker_contract)
            self.assertIn("--team-id B1", tracker_contract)
            self.assertIn("Один `team_id` может иметь не более одного account id", tracker_contract)
            self.assertIn("доказательством вызова", tracker_contract)
            self.assertIn("взаимно однозначно", tracker_contract)
            self.assertIn("run-status", tracker_contract)
            self.assertIn("result-status", tracker_contract)
            self.assertIn("field_observations", tracker_contract)
            self.assertIn("--checked-key", tracker_contract)
            self.assertIn("не составляет статус вручную", tracker_contract)
            self.assertIn("поэлементным правилом склейки", tracker_contract)
            self.assertIn("story point равен одному человекодню", tracker_contract)
            self.assertIn("Прочитанные напрямую данные MCP не являются отчётом", tracker_contract)
            self.assertIn("collection.not_found_keys", tracker_contract)
            self.assertIn("пересчитывать или пересказывать", tracker_contract)
            delegation = (ROOT / "core/agent-delegation.md").read_text(encoding="utf-8")
            self.assertIn("any task-tracker workflow step", delegation)
            llm_contract = (ROOT / "core/llm-contract.md").read_text(encoding="utf-8")
            self.assertIn("standalone command without `head`, `tail`, `grep`, `jq`", llm_contract)
            self.assertIn("Do not delegate tracker reading", llm_contract)
            self.assertIn("for that same `run_id`", llm_contract)
            self.assertIn("`value`, `absent` or `not-returned`", llm_contract)
            self.assertIn("never synthesize its flags or counts", llm_contract)
            self.assertIn("Merge paired issue fields independently", llm_contract)
            agent_rules = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("Mandatory tracker stop gate", agent_rules)
            self.assertIn("Emit the exact `response_contract.text` and nothing else", agent_rules)
            self.assertIn("subagent delegation is forbidden", agent_rules)
            self.assertIn("final_response_allowed: false", agent_rules)
            self.assertIn("Never assemble a completion status manually", agent_rules)
            self.assertIn("1 SP = 1 person-day", agent_rules)
            readme = (ROOT / "README.md").read_text(encoding="utf-8")
            self.assertIn("код `3` и `must_stop: true` запрещают любые MCP-вызовы", readme)
            self.assertIn("настраиваются отдельно для SberTrek и Jira", readme)
            self.assertIn("После каждого MCP-ответа", readme)
            self.assertIn("незакрытый seed", readme)
            self.assertIn("один сохранённый официальный статус", readme)
            self.assertIn("1 SP = 1 человекодень", readme)
            self.assertIn("Старые свободные маппинги участников очищаются", readme)

    def test_requirements_profile_validator_checks_opted_in_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self.scaffold(Path(temp))
            requirements = project / "features/demo/requirements.md"
            requirements.parent.mkdir(parents=True)
            requirements.write_text(
                "# Требования\n\n"
                "Редакция: `1`\n"
                "Формат: **компактная спецификация функциональности**\n"
                "Функциональность: `demo`\n\n"
                "## Назначение\n\nПолучить сохранённый результат.\n\n"
                "## Границы\n\nВходит только сохранение результата.\n\n"
                "## Требования\n\n"
                "### REQ-DEMO-001. Сохранение результата\n\n"
                "Система должна сохранить результат.\n\n"
                "#### Сценарий: успешное сохранение\n\n"
                "**Когда** пользователь сохраняет данные.\n\n"
                "**Тогда** система сохраняет результат.\n\n"
                "## Влияние на соседние функциональности\n\nВлияний нет.\n\n"
                "## Источники и открытые вопросы\n\nИсточник: решение. Вопросов нет.\n",
                encoding="utf-8",
            )
            tool = ROOT / "scripts/validate-requirements-profile.py"
            result = run(sys.executable, str(tool), str(project), "--feature", "demo")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            requirements.write_text(requirements.read_text(encoding="utf-8").replace("Система должна сохранить", "Система может сохранить"), encoding="utf-8")
            result = run(sys.executable, str(tool), str(project), "--feature", "demo")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("явная русская нормативная форма", result.stdout)

    def test_handoff_validator_accepts_per_item_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "package"
            self.write_pending_handoff_package(package)
            result = run(sys.executable, str(ROOT / "scripts/validate-handoff.py"), str(package))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_handoff_root_supersedes_unclaimed_revision_and_detects_package_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self.scaffold(Path(temp))
            tool = ROOT / "scripts/handoffctl.py"
            result = run(sys.executable, str(tool), "init", str(project), "demo", "demo-be-change", "--role", "BE", "--source-task-id", "CAND-DEMO-BE-001", "--source-task-path", "features/demo/tasks/be-change.md")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            root = project / "features/demo/handoffs/demo-be-change"
            for revision in (1, 2):
                result = run(sys.executable, str(tool), "add-revision", str(root), str(revision), "--replaces", str(revision - 1) if revision > 1 else "") if revision > 1 else run(sys.executable, str(tool), "add-revision", str(root), str(revision))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.write_pending_handoff_package(root / f"revisions/{revision:03d}/package", "demo-be-change", revision)
                result = run(sys.executable, str(tool), "publish", str(root), str(revision))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            manifest = json.loads((root / "handoff.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["active_revision"], 2)
            self.assertEqual(manifest["next_sdd_action"]["action"], "process")
            self.assertEqual(manifest["revisions"][0]["state"], "superseded")
            self.assertEqual(manifest["revisions"][0]["receipt"]["expectation"], "not-expected")
            self.assertNotIn("ready", manifest["allowed_revision_states"])
            result = run(sys.executable, str(tool), "validate", str(root))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            control_tool = root / ".control/handoffctl.py"
            result = run(sys.executable, str(control_tool), "claim", str(root), "2", "--by", "test-session")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            manifest = json.loads((root / "handoff.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["next_sdd_action"]["action"], "continue")
            self.assertEqual(manifest["next_sdd_action"]["claimed_by"], "test-session")
            request = root / "revisions/002/package/request.md"
            request.write_text(request.read_text(encoding="utf-8") + "\nизменение\n", encoding="utf-8")
            result = run(sys.executable, str(tool), "validate", str(root))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("immutable revision", result.stdout)

    def test_publish_stops_when_previous_revision_is_in_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self.scaffold(Path(temp))
            tool = ROOT / "scripts/handoffctl.py"
            result = run(sys.executable, str(tool), "init", str(project), "demo", "demo-be-change", "--role", "BE", "--source-task-id", "CAND-DEMO-BE-001", "--source-task-path", "features/demo/tasks/be-change.md")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            root = project / "features/demo/handoffs/demo-be-change"
            result = run(sys.executable, str(tool), "add-revision", str(root), "1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.write_pending_handoff_package(root / "revisions/001/package", "demo-be-change", 1)
            result = run(sys.executable, str(tool), "publish", str(root), "1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            control = root / ".control/handoffctl.py"
            result = run(sys.executable, str(control), "claim", str(root), "1", "--by", "developer")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            result = run(sys.executable, str(tool), "add-revision", str(root), "2", "--replaces", "1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.write_pending_handoff_package(root / "revisions/002/package", "demo-be-change", 2)
            result = run(sys.executable, str(tool), "publish", str(root), "2")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("in progress", result.stdout + result.stderr)
            self.assertFalse((root / "revisions/002.zip").exists())
            manifest = json.loads((root / "handoff.json").read_text(encoding="utf-8"))
            second = next(item for item in manifest["revisions"] if item["revision"] == 2)
            self.assertEqual(second["state"], "draft")
            self.assertIsNone(second["transport_path"])

    def test_transport_is_on_request_and_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = self.scaffold(base)
            tool = ROOT / "scripts/handoffctl.py"
            result = run(
                sys.executable,
                str(tool),
                "init",
                str(project),
                "demo",
                "demo-be-change",
                "--role",
                "BE",
                "--source-task-id",
                "CAND-DEMO-BE-001",
                "--source-task-path",
                "features/demo/tasks/be-change.md",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            root = project / "features/demo/handoffs/demo-be-change"
            result = run(sys.executable, str(tool), "add-revision", str(root), "1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.write_pending_handoff_package(root / "revisions/001/package", "demo-be-change", 1)
            result = run(sys.executable, str(tool), "publish", str(root), "1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(list(root.rglob("*.zip")), [])

            home = base / "home"
            env = {**os.environ, "HOME": str(home)}
            result = subprocess.run(
                [sys.executable, str(tool), "transport", str(root), "1"],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            archive = home / "Downloads/demo-be-change-r001.zip"
            self.assertTrue(archive.is_file())
            self.assertEqual(list(root.rglob("*.zip")), [])
            manifest = json.loads((root / "handoff.json").read_text(encoding="utf-8"))
            self.assertIsNone(manifest["revisions"][0]["transport_path"])
            self.assertIsNone(manifest["revisions"][0]["transport_sha256"])

            forbidden = root / "revisions/001.zip"
            forbidden.write_bytes(archive.read_bytes())
            result = run(sys.executable, str(tool), "validate", str(root))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("archives inside the handoff repository are forbidden", result.stdout + result.stderr)

    def test_handoff_validator_accepts_delivery_deviation_and_additional_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "package"
            self.write_pending_handoff_package(package)
            receipt_path = package / "receipt.json"
            receipt = json.loads((package / "receipt.template.json").read_text(encoding="utf-8"))
            commit = "0123456789abcdef0123456789abcdef01234567"
            evidence = [{"path": "src/main/Demo.java", "symbol": "Demo", "observation": "Поведение подтверждено кодом и тестом"}]
            receipt.update({
                "received_at": "2026-08-13T10:00:00Z",
                "completed_at": "2026-08-13T12:00:00Z",
                "source_before": {"commit": commit, "branch": "develop", "working_tree_state": "clean", "relevant_uncommitted_paths": []},
                "source_after": {"commit": commit, "branch": "develop", "working_tree_state": "clean", "relevant_uncommitted_paths": []},
                "status": "delivered-with-deviations",
                "commits": [{"sha256": commit, "summary": "Реализовать результат", "item_ids": ["REQ-DEMO-001", "SCN-DEMO-001", "ADD-DEMO-001"]}],
                "generated_or_changed_artifacts": ["src/main/Demo.java"],
                "additional_deliveries": [{"id": "ADD-DEMO-001", "title": "Дополнительное правило", "reason": "Уместно реализовать вместе с основной записью", "delivered_behavior": "Добавлено конечное правило", "follow_up_recommendation": "update-requirement", "suggested_destination": "demo", "evidence": evidence, "commit_sha256": [commit], "verification": ["unit-test"]}],
                "verification": [{"type": "tests", "status": "passed", "detail": "Тесты прошли"}],
            })
            for entry in receipt["requirement_coverage"] + receipt["scenario_coverage"]:
                entry.update({"status": "implemented-with-deviation", "behavior_before": "Поведение отсутствовало", "delivered_behavior": "Требуемый результат поставлен", "deviation_from_input": "Использован другой технический способ", "follow_up_recommendation": "update-requirement", "evidence": evidence, "commit_sha256": [commit], "verification": ["unit-test"]})
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            result = run(sys.executable, str(ROOT / "scripts/validate-handoff.py"), str(package), "--receipt", "receipt.json")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_active_mode_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self.scaffold(Path(temp))
            state_root = Path(temp) / "state"
            state_root.mkdir()
            active = state_root / "active-mode.md"
            active.write_text("# Active Mode\n\nmode: planning\n\n## Mode File\nmodes/requirements.md\n", encoding="utf-8")
            env = {**os.environ, "CODA_ANALYST_STATE_ROOT": str(state_root)}
            result = run(sys.executable, str(ROOT / "scripts/validate-workflow.py"), str(project), env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("active mode mismatch", result.stdout)

    def test_feature_scaffold_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self.scaffold(Path(temp))
            result = run("bash", str(ROOT / "scripts/scaffold-feature.sh"), str(project), "demo")
            self.assertEqual(result.returncode, 0)
            feature = project / "features/demo/feature.md"
            feature.write_text("USER CONTENT\n", encoding="utf-8")
            result = run("bash", str(ROOT / "scripts/scaffold-feature.sh"), str(project), "demo")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(feature.read_text(encoding="utf-8"), "USER CONTENT\n")

    def test_project_merge_adds_missing_structure_without_embedded_harness(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "existing"
            (project / "baseline/current/domain").mkdir(parents=True)
            result = run("bash", str(ROOT / "scripts/scaffold-project.sh"), str(project), "--merge")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((project / ".workflow").exists())
            self.assertFalse((project / "AGENTS.md").exists())
            self.assertTrue((project / "baseline/current/domain/aggregates.md").exists())

    def test_language_check_ignores_code_and_rejects_prose_anglicism(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self.scaffold(Path(temp))
            requirements = project / "features/demo/requirements.md"
            requirements.parent.mkdir(parents=True)
            requirements.write_text("# Требования\n\nScope работ. Код: `scope`.\n", encoding="utf-8")
            result = run(
                sys.executable,
                str(ROOT / "scripts/validate-language.py"),
                str(project),
                "--all",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout.count("'scope'"), 1)

    def test_run_escalates_after_iteration_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self.scaffold(Path(temp))
            state_root = Path(temp) / "state"
            env = {**os.environ, "CODA_ANALYST_STATE_ROOT": str(state_root)}
            result = run(
                sys.executable,
                str(ROOT / "scripts/harnessctl.py"),
                "run-init",
                str(project),
                "planning",
                "--run-id",
                "test-run",
                "--max-iterations",
                "2",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            run_file = state_root / "runs/test-run/run.json"
            for _ in range(2):
                result = run(sys.executable, str(ROOT / "scripts/harnessctl.py"), "run-advance", str(run_file), "fail", env=env)
                self.assertEqual(result.returncode, 0)
            payload = json.loads(run_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "escalated")

    def test_approved_plan_tampering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = self.scaffold(Path(temp))
            result = run("bash", str(ROOT / "scripts/scaffold-quarter.sh"), str(project), "2026-Q3")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = run(
                sys.executable,
                str(ROOT / "scripts/harnessctl.py"),
                "plan-approve",
                str(project),
                "2026-Q3",
                "--by",
                "owner",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = project / "planning/2026-Q3/plan-state.md"
            self.assertIn("## Immutability", state.read_text(encoding="utf-8"))
            quarter_plan = project / "planning/2026-Q3/gantt/quarter-plan.puml"
            quarter_plan.write_text(quarter_plan.read_text(encoding="utf-8") + "' tampered\n", encoding="utf-8")
            result = run(sys.executable, str(ROOT / "scripts/validate-planning.py"), str(project))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("approved plan was modified", result.stdout)

    def test_multi_user_command_synonyms_and_migration_are_documented(self) -> None:
        documents = [
            ROOT / "AGENTS.md",
            ROOT / "README.md",
            ROOT / "core/collaboration.md",
            ROOT / "templates/workflow/command-catalog.template.md",
            ROOT / "templates/workflow/command-cheatsheet.template.md",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in documents)
        for phrase in (
            "пишем требования по фиче",
            "работаем с фичой",
            "беру фичу",
            "начинаем работы над требованиями по",
            "сохрани работу",
            "закоммить изменения",
            "обнови мою ветку",
            "вливаем в основную ветку",
            "запрос на слияние принят",
            "ветка влита в main",
            "синкани репы",
            "отправь требования в разработку",
            "передай разрабам",
            "Миграция существующей установки",
        ):
            self.assertIn(phrase, combined)
        collaboration = (ROOT / "core/collaboration.md").read_text(encoding="utf-8")
        self.assertIn("require-main-for-delivery", collaboration)
        self.assertIn("автоматического коммита и отправки нет", collaboration)
        self.assertIn("не однопользовательский режим", collaboration)
        self.assertIn("автоматически закрывает рабочую сессию", collaboration)
        catalog = (ROOT / "templates/workflow/command-catalog.template.md").read_text(encoding="utf-8")
        self.assertIn("Заверши принятую рабочую ветку", catalog)
        self.assertIn("requirements-exchange.py prepare", catalog)
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Communicate with the analyst in Russian", agents)
        self.assertIn("does not create a merge request", agents)
        self.assertIn("merge-request creation form", agents)
        self.assertIn("run all three levels from `core/requirements-audit.md`", agents)


if __name__ == "__main__":
    unittest.main()
