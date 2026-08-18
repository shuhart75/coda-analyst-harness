from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False)


class HandoffLifecycleTests(unittest.TestCase):
    def valid_card(self, task: dict) -> str:
        text = (ROOT / "templates/handoff/development-task-card.template.md").read_text(encoding="utf-8")
        replacements = {
            "DEV-<BE|FE>-001": task["id"],
            "<Название задачи>": "Законченный технический результат",
            "<backend|frontend>": task["contour"],
            "<Законченный технический результат>": "Проверяемый результат поставки",
            "<Какой наблюдаемый результат обеспечивает задача.>": "Система предоставляет требуемое поведение.",
            "<Полные формулировки необходимых сценариев и влияний.>": " ".join(task.get("scenarios", []) + task.get("impacts", [])) or "Нет.",
            "<Пункт>": "Поставить требуемое поведение",
            "<Наблюдаемое условие>": "Поведение наблюдается в проверке",
            "<Проверка>": "Автоматическая проверка результата",
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        requirement_rows = "\n".join(f"| {item} | Полная формулировка требования {item}. |" for item in task.get("requirements", []))
        text = text.replace("|---|---|\n\n## Сценарии", f"|---|---|\n{requirement_rows}\n\n## Сценарии")
        text = text.replace("| предложена |", "| подтверждена разработкой |")
        text = text.replace("|---|---|---|\n\nОтсутствие", "|---|---|---|\n| Найденный модуль | не реализовано | `src/Demo.java` |\n\nОтсутствие")
        return text

    def valid_index(self, tasks: list[dict]) -> str:
        text = (ROOT / "templates/handoff/development-tasks-index.template.md").read_text(encoding="utf-8")
        text = text.replace("<Название>", "Демонстрационная функциональность").replace("<revision>", "1")
        rows = "\n".join(
            f"| {task['id']} | {task['contour']} | Проверяемый результат | - | - | нет | [{task['id']}]({task['card_path']}) |"
            for task in tasks
        )
        text = text.replace("|---|---|---|---:|---|---|---|\n", f"|---|---|---|---:|---|---|---|\n{rows}\n")
        text = text.replace("Состояние декомпозиции: **черновик**", "Состояние декомпозиции: **подтверждена разработкой**")
        return text

    def create_claimed_feature(self, root: Path) -> tuple[Path, Path]:
        project = root / "project"
        result = run("bash", str(ROOT / "scripts/scaffold-project.sh"), str(project))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        feature = project / "features/demo"
        slice_root = feature / "slices/list"
        slice_root.mkdir(parents=True)
        (feature / "requirements.md").write_text(
            "# Требования\n\nREQ-DEMO-001\n\nSCN-DEMO-001 покрывает REQ-DEMO-001.\n\nIMP-DEMO-001\n",
            encoding="utf-8",
        )
        (slice_root / "slice.md").write_text("# Срез\n\nREQ-DEMO-001\n\nSCN-DEMO-001\n", encoding="utf-8")
        tool = ROOT / "scripts/handoffctl.py"
        result = run(sys.executable, str(tool), "init-feature", str(project), "demo", "demo-feature")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        handoff = feature / "handoffs/demo-feature"
        for command in (
            ("add-revision", str(handoff), "1"),
            ("publish", str(handoff), "1"),
        ):
            result = run(sys.executable, str(tool), *command)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        control = handoff / ".control/handoffctl.py"
        result = run(sys.executable, str(control), "claim", str(handoff), "1", "--by", "developer-session")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return handoff, control

    def write_decomposition(self, handoff: Path, tasks: list[dict], coverage: dict) -> None:
        working = handoff / "revisions/001/returns/development-tasks"
        for task in tasks:
            (working / task["card_path"]).write_text(self.valid_card(task), encoding="utf-8")
        (working / "index.md").write_text(self.valid_index(tasks), encoding="utf-8")
        receipt = {
            "schema_version": 1,
            "kind": "technical-decomposition",
            "package_id": "demo-feature",
            "package_revision": 1,
            "decomposition_revision": 1,
            "status": "confirmed-by-development",
            "confirmed_by": "development-team",
            "confirmed_at": "2026-08-14T10:00:00Z",
            "source_revisions": {"backend": None, "frontend": None},
            "tasks": tasks,
            "coverage": coverage,
            "checks": [],
            "notes": None,
        }
        (handoff / "revisions/001/returns/decomposition-receipt.json").write_text(
            json.dumps(receipt), encoding="utf-8"
        )

    def test_invalid_decomposition_is_rejected_without_blocking_package(self) -> None:
        base_task = {
            "id": "DEV-BE-001",
            "contour": "backend",
            "decomposition_status": "confirmed-by-development",
            "card_path": "DEV-BE-001.md",
            "estimate_days": None,
            "estimate_source": None,
            "size_exception_reason": None,
            "jira_key": None,
            "requirements": ["REQ-DEMO-001"],
            "scenarios": ["SCN-DEMO-001"],
            "impacts": ["IMP-DEMO-001"],
            "dependencies": [],
        }
        cases = []
        oversized = dict(base_task, estimate_days=6, estimate_source="developer")
        cases.append(("oversized", [oversized], {
            "unassigned_requirements": [], "unassigned_scenarios": [], "unassigned_impacts": []
        }, "requires size_exception_reason"))
        unknown = dict(base_task, requirements=["REQ-UNKNOWN-001"])
        cases.append(("unknown requirement", [unknown], {
            "unassigned_requirements": ["REQ-DEMO-001"], "unassigned_scenarios": [], "unassigned_impacts": []
        }, "assigns unknown requirements"))
        first = dict(base_task, dependencies=["DEV-BE-002"])
        second = dict(base_task, id="DEV-BE-002", card_path="DEV-BE-002.md", dependencies=["DEV-BE-001"])
        cases.append(("cycle", [first, second], {
            "unassigned_requirements": [], "unassigned_scenarios": [], "unassigned_impacts": []
        }, "dependencies contain a cycle"))

        for name, tasks, coverage, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                handoff, control = self.create_claimed_feature(Path(temp))
                self.write_decomposition(handoff, tasks, coverage)
                result = run(sys.executable, str(control), "confirm-decomposition", str(handoff), "1")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stdout + result.stderr)
                manifest = json.loads((handoff / "handoff.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["next_sdd_action"]["action"], "continue")

    def test_card_without_permanent_developer_commands_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            handoff, control = self.create_claimed_feature(Path(temp))
            task = {
                "id": "DEV-BE-001",
                "contour": "backend",
                "decomposition_status": "confirmed-by-development",
                "card_path": "DEV-BE-001.md",
                "estimate_days": None,
                "estimate_source": None,
                "size_exception_reason": None,
                "jira_key": None,
                "requirements": ["REQ-DEMO-001"],
                "scenarios": ["SCN-DEMO-001"],
                "impacts": ["IMP-DEMO-001"],
                "dependencies": [],
            }
            self.write_decomposition(handoff, [task], {
                "unassigned_requirements": [],
                "unassigned_scenarios": [],
                "unassigned_impacts": [],
            })
            card = handoff / "revisions/001/returns/development-tasks/DEV-BE-001.md"
            card.write_text(card.read_text(encoding="utf-8").replace("Возьми DEV-BE-001 в разработку.", ""), encoding="utf-8")
            result = run(sys.executable, str(control), "confirm-decomposition", str(handoff), "1")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("developer command is missing", result.stdout + result.stderr)

    def test_unconfirmed_card_or_index_is_rejected(self) -> None:
        task = {
            "id": "DEV-BE-001",
            "contour": "backend",
            "decomposition_status": "confirmed-by-development",
            "card_path": "DEV-BE-001.md",
            "estimate_days": None,
            "estimate_source": None,
            "size_exception_reason": None,
            "jira_key": None,
            "requirements": ["REQ-DEMO-001"],
            "scenarios": ["SCN-DEMO-001"],
            "impacts": ["IMP-DEMO-001"],
            "dependencies": [],
        }
        coverage = {
            "unassigned_requirements": [],
            "unassigned_scenarios": [],
            "unassigned_impacts": [],
        }
        cases = (
            (
                "card",
                "DEV-BE-001.md",
                "| Состояние декомпозиции | подтверждена разработкой |",
                "| Состояние декомпозиции | предложена |",
                "card decomposition state must be confirmed-by-development",
            ),
            (
                "contour",
                "DEV-BE-001.md",
                "| Контур | `backend` |",
                "| Контур | `frontend` |",
                "card contour must be backend",
            ),
            (
                "index",
                "index.md",
                "Состояние декомпозиции: **подтверждена разработкой**",
                "Состояние декомпозиции: **черновик**",
                "development task index must have confirmed decomposition state",
            ),
        )
        for name, target_name, old, new, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                handoff, control = self.create_claimed_feature(Path(temp))
                self.write_decomposition(handoff, [task], coverage)
                target = handoff / "revisions/001/returns/development-tasks" / target_name
                target.write_text(target.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
                result = run(sys.executable, str(control), "confirm-decomposition", str(handoff), "1")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stdout + result.stderr)

    def test_legacy_section_requirements_are_packaged_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            result = run("bash", str(ROOT / "scripts/scaffold-project.sh"), str(project))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            feature = project / "features/demo"
            for slice_id in ("first", "second"):
                detailed = feature / f"slices/{slice_id}/requirements"
                detailed.mkdir(parents=True)
                (feature / f"slices/{slice_id}/slice.md").write_text(f"# Срез {slice_id}\n", encoding="utf-8")
                (detailed / "backend.md").write_text(f"# Серверные требования {slice_id}\n", encoding="utf-8")
            (feature / "requirements.md").write_text(
                """# Старая функциональность

## Общий контур функциональности

Текущее поведение и границы функциональности.

## Контроль срезов

## LEGACY-DEMO-001 — Первый результат

Карточка среза: `slices/first/slice.md`

**Назначение**

- Получить первый наблюдаемый результат.

**Критерии приемки**

1. Первый результат доступен.

## LEGACY-DEMO-002 — Второй результат

Карточка среза: `slices/second/slice.md`

**Назначение**

- Получить второй наблюдаемый результат.

**Критерии приемки**

1. Второй результат доступен.

## Доработки затронутых функциональностей

IMP-LEGACY-DEMO-001
""",
                encoding="utf-8",
            )
            tool = ROOT / "scripts/handoffctl.py"
            result = run(sys.executable, str(tool), "init-feature", str(project), "demo", "legacy-demo")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            handoff = feature / "handoffs/legacy-demo"
            result = run(sys.executable, str(tool), "add-revision", str(handoff), "1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            package = handoff / "revisions/001/package"
            manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["requirements"], ["LEGACY-DEMO-001", "LEGACY-DEMO-002"])
            self.assertEqual(manifest["traceability"]["mode"], "legacy-sections")
            self.assertEqual(manifest["slices"][0]["requirements"], ["LEGACY-DEMO-001"])
            self.assertEqual(manifest["slices"][1]["requirements"], ["LEGACY-DEMO-002"])
            self.assertIn("slices/first/requirements/backend.md", [item["path"] for item in manifest["payload"]])
            request = (package / "request.md").read_text(encoding="utf-8")
            self.assertIn("Получить первый наблюдаемый результат", request)
            self.assertIn("Первый результат доступен", request)
            index = (handoff / "revisions/001/returns/development-tasks/index.md").read_text(encoding="utf-8")
            self.assertIn("Старая функциональность", index)
            self.assertNotIn("<Название>", index)
            result = run(sys.executable, str(tool), "publish", str(handoff), "1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            manifest["requirements"] = ["LEGACY-DEMO-001"]
            (package / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            result = run(sys.executable, str(tool), "transport", str(handoff), "1", "--force")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("immutable package files differ", result.stdout + result.stderr)

    def test_feature_slice_manifest_expands_identifier_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            result = run("bash", str(ROOT / "scripts/scaffold-project.sh"), str(project))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            feature = project / "features/demo"
            slice_root = feature / "slices/list"
            slice_root.mkdir(parents=True)
            (feature / "requirements.md").write_text(
                "# Требования\n\nREQ-DEMO-001\nREQ-DEMO-002\nREQ-DEMO-003\n\n"
                "SCN-DEMO-001\nSCN-DEMO-002\nSCN-DEMO-003\n",
                encoding="utf-8",
            )
            (slice_root / "slice.md").write_text(
                "# Срез\n\nREQ-DEMO-001 — REQ-DEMO-003\n\nSCN-DEMO-001 — SCN-DEMO-003\n",
                encoding="utf-8",
            )
            tool = ROOT / "scripts/handoffctl.py"
            result = run(sys.executable, str(tool), "init-feature", str(project), "demo", "demo-feature")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            handoff = feature / "handoffs/demo-feature"
            root_readme = (handoff / "README.md").read_text(encoding="utf-8")
            self.assertIn("Формальный договор для SDD находится в `AGENTS.md`", root_readme)
            self.assertIn("Обработай активную редакцию пакета.", root_readme)
            self.assertNotIn("## Обязательная инструкция SDD", root_readme)
            self.assertNotIn("python .control/handoffctl.py", root_readme)
            agent_text = (handoff / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("# Контракт SDD", agent_text)
            self.assertIn("## Обязательный порядок начала работы", agent_text)
            self.assertIn("prepare-implementation", agent_text)
            root_manifest = json.loads((handoff / "handoff.json").read_text(encoding="utf-8"))
            self.assertEqual(root_manifest["schema_version"], 3)
            self.assertEqual(root_manifest["agent_contract"]["path"], "AGENTS.md")
            self.assertEqual(
                root_manifest["agent_contract"]["sha256"],
                hashlib.sha256((handoff / "AGENTS.md").read_bytes()).hexdigest(),
            )
            result = run(sys.executable, str(tool), "add-revision", str(handoff), "1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            instruction = (handoff / "revisions/001/returns/development-tasks/README.md").read_text(encoding="utf-8")
            self.assertIn("## Порядок подтверждения декомпозиции", instruction)
            self.assertIn("Наличие этих слов только в блоке коротких команд подтверждением не является", instruction)
            package_manifest = json.loads((handoff / "revisions/001/package/manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                package_manifest["slices"][0]["requirements"],
                ["REQ-DEMO-001", "REQ-DEMO-002", "REQ-DEMO-003"],
            )
            self.assertEqual(
                package_manifest["slices"][0]["scenarios"],
                ["SCN-DEMO-001", "SCN-DEMO-002", "SCN-DEMO-003"],
            )

    def test_feature_agent_contract_is_required_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            result = run("bash", str(ROOT / "scripts/scaffold-project.sh"), str(project))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            feature = project / "features/demo"
            feature.mkdir(parents=True)
            (feature / "requirements.md").write_text("# Требования\n\nREQ-DEMO-001\n", encoding="utf-8")
            tool = ROOT / "scripts/handoffctl.py"
            result = run(sys.executable, str(tool), "init-feature", str(project), "demo", "demo-feature")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            handoff = feature / "handoffs/demo-feature"
            result = run(sys.executable, str(tool), "validate", str(handoff))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            status = run(sys.executable, str(tool), "status", str(handoff))
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            status_payload = json.loads(status.stdout)
            self.assertEqual(status_payload["agent_contract"]["path"], "AGENTS.md")

            agents = handoff / "AGENTS.md"
            original = agents.read_text(encoding="utf-8")
            agents.write_text(original + "\nНесогласованное изменение.\n", encoding="utf-8")
            result = run(sys.executable, str(tool), "validate", str(handoff))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("agent contract checksum mismatch", result.stdout + result.stderr)

            agents.write_text(original, encoding="utf-8")
            agents.unlink()
            result = run(sys.executable, str(tool), "validate", str(handoff))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("agent contract file is missing", result.stdout + result.stderr)

    def test_feature_decomposition_delivery_and_testing_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            result = run("bash", str(ROOT / "scripts/scaffold-project.sh"), str(project))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            feature = project / "features/demo"
            slice_root = feature / "slices/list"
            slice_root.mkdir(parents=True)
            (feature / "requirements.md").write_text(
                "# Требования\n\nREQ-DEMO-001\n\nSCN-DEMO-001 покрывает REQ-DEMO-001.\n\nIMP-DEMO-001\n",
                encoding="utf-8",
            )
            (slice_root / "slice.md").write_text(
                "# Срез\n\nREQ-DEMO-001\n\nSCN-DEMO-001\n",
                encoding="utf-8",
            )
            tool = ROOT / "scripts/handoffctl.py"
            result = run(sys.executable, str(tool), "init-feature", str(project), "demo", "demo-feature")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            handoff = feature / "handoffs/demo-feature"
            result = run(sys.executable, str(tool), "add-revision", str(handoff), "1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            package_manifest = json.loads((handoff / "revisions/001/package/manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(package_manifest["requirements"], ["REQ-DEMO-001"])
            self.assertEqual(package_manifest["scenarios"], ["SCN-DEMO-001"])
            self.assertEqual(package_manifest["impacts"], ["IMP-DEMO-001"])
            self.assertEqual(package_manifest["slices"][0]["id"], "list")
            self.assertEqual(
                [item["path"] for item in package_manifest["payload"]],
                ["README.md", "request.md", "requirements.md", "slices/list/slice.md"],
            )
            request_text = (handoff / "revisions/001/package/request.md").read_text(encoding="utf-8")
            self.assertIn("# Требования", request_text)
            self.assertIn("slices/list/slice.md", request_text)
            self.assertNotIn("<Законченный пользовательский", request_text)
            result = run(sys.executable, str(tool), "publish", str(handoff), "1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            control = handoff / ".control/handoffctl.py"
            result = run(sys.executable, str(control), "claim", str(handoff), "1", "--by", "developer-session")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            working = handoff / "revisions/001/returns/development-tasks"
            decomposition = {
                "schema_version": 1,
                "kind": "technical-decomposition",
                "package_id": "demo-feature",
                "package_revision": 1,
                "decomposition_revision": 1,
                "status": "confirmed-by-development",
                "confirmed_by": "development-team",
                "confirmed_at": "2026-08-14T10:00:00Z",
                "source_revisions": {"backend": None, "frontend": None},
                "tasks": [{
                    "id": "DEV-BE-001",
                    "contour": "backend",
                    "decomposition_status": "confirmed-by-development",
                    "card_path": "DEV-BE-001.md",
                    "estimate_days": None,
                    "estimate_source": None,
                    "size_exception_reason": None,
                    "jira_key": "KODA-1",
                    "requirements": ["REQ-DEMO-001"],
                    "scenarios": ["SCN-DEMO-001"],
                    "impacts": ["IMP-DEMO-001"],
                    "dependencies": [],
                }],
                "coverage": {
                    "unassigned_requirements": [],
                    "unassigned_scenarios": [],
                    "unassigned_impacts": [],
                },
                "checks": [{"name": "coverage", "status": "passed"}],
                "notes": None,
            }
            (working / "DEV-BE-001.md").write_text(self.valid_card(decomposition["tasks"][0]), encoding="utf-8")
            (working / "index.md").write_text(self.valid_index(decomposition["tasks"]), encoding="utf-8")
            (handoff / "revisions/001/returns/decomposition-receipt.json").write_text(
                json.dumps(decomposition), encoding="utf-8"
            )
            result = run(sys.executable, str(control), "confirm-decomposition", str(handoff), "1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            root_manifest = json.loads((handoff / "handoff.json").read_text(encoding="utf-8"))
            self.assertEqual(root_manifest["revisions"][0]["state"], "in-progress")
            self.assertEqual(root_manifest["next_sdd_action"]["action"], "continue")
            self.assertEqual(root_manifest["revisions"][0]["decomposition"]["status"], "confirmed-by-development")
            snapshot = handoff / "revisions/001/returns/decomposition-snapshots/001"
            self.assertTrue((snapshot / "DEV-BE-001.md").is_file())

            result = run(
                sys.executable, str(control), "prepare-implementation", str(handoff), "1", "DEV-BE-001",
                "--decomposition-revision", "1",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            implementation_path = handoff / "revisions/001/returns/implementation-results/DEV-BE-001/001/receipt.json"
            implementation = json.loads(implementation_path.read_text(encoding="utf-8"))
            self.assertEqual(implementation["jira_key"], "KODA-1")
            self.assertEqual([item["requirement"] for item in implementation["requirement_results"]], ["REQ-DEMO-001"])
            self.assertEqual([item["scenario"] for item in implementation["scenario_results"]], ["SCN-DEMO-001"])
            implementation.update({
                "status": "delivered",
                "requirement_results": [{"requirement": "REQ-DEMO-001", "status": "implemented-as-required", "evidence": []}],
                "scenario_results": [{"scenario": "SCN-DEMO-001", "status": "implemented-as-required", "evidence": []}],
            })
            implementation_path.write_text(json.dumps(implementation), encoding="utf-8")
            result = run(
                sys.executable, str(control), "register-implementation", str(handoff), "1", "DEV-BE-001",
                "--decomposition-revision", "1",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            duplicate = run(
                sys.executable, str(control), "register-implementation", str(handoff), "1", "DEV-BE-001",
                "--decomposition-revision", "1",
            )
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertIn("already registered", duplicate.stdout + duplicate.stderr)

            result = run(sys.executable, str(control), "prepare-test", str(handoff), "1", "list")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            test_path = handoff / "revisions/001/returns/test-results/list/001/receipt.json"
            test_receipt = json.loads(test_path.read_text(encoding="utf-8"))
            self.assertEqual(test_receipt["decomposition_revision"], 1)
            self.assertEqual(test_receipt["related_tasks"], ["DEV-BE-001"])
            self.assertEqual([item["requirement"] for item in test_receipt["requirement_results"]], ["REQ-DEMO-001"])
            self.assertEqual([item["scenario"] for item in test_receipt["scenario_results"]], ["SCN-DEMO-001"])
            test_receipt.update({
                "status": "passed",
                "related_tasks": ["DEV-BE-001"],
                "implementation_receipts": ["revisions/001/returns/implementation-results/DEV-BE-001/999/receipt.json"],
                "requirement_results": [{"requirement": "REQ-DEMO-001", "status": "passed", "evidence": []}],
                "scenario_results": [{"scenario": "SCN-DEMO-001", "status": "passed", "evidence": []}],
            })
            test_path.write_text(json.dumps(test_receipt), encoding="utf-8")
            invalid_test = run(
                sys.executable, str(control), "register-test", str(handoff), "1", "list",
                "--decomposition-revision", "1",
            )
            self.assertNotEqual(invalid_test.returncode, 0)
            self.assertIn("unregistered implementation receipt", invalid_test.stdout + invalid_test.stderr)
            test_receipt["implementation_receipts"] = [implementation_path.relative_to(handoff).as_posix()]
            test_path.write_text(json.dumps(test_receipt), encoding="utf-8")
            result = run(
                sys.executable, str(control), "register-test", str(handoff), "1", "list",
                "--decomposition-revision", "1",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = run(sys.executable, str(control), "validate", str(handoff))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            root_manifest = json.loads((handoff / "handoff.json").read_text(encoding="utf-8"))
            self.assertEqual(root_manifest["next_sdd_action"]["action"], "continue")
            self.assertEqual(len(root_manifest["revisions"][0]["implementation_results"]), 1)
            self.assertEqual(len(root_manifest["revisions"][0]["test_results"]), 1)

    def test_delivery_receipt_and_analyst_review_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            result = run("bash", str(ROOT / "scripts/scaffold-project.sh"), str(project))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            tool = ROOT / "scripts/handoffctl.py"
            result = run(sys.executable, str(tool), "init", str(project), "demo", "demo-be-change", "--role", "BE", "--source-task-id", "CAND-DEMO-BE-001", "--source-task-path", "features/demo/tasks/be-change.md")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            handoff = project / "features/demo/handoffs/demo-be-change"
            result = run(sys.executable, str(tool), "add-revision", str(handoff), "1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            package = handoff / "revisions/001/package"
            request = package / "request.md"
            request.write_text("# Задание\n\nREQ-DEMO-001\n\nSCN-DEMO-001\n", encoding="utf-8")
            request_hash = hashlib.sha256(request.read_bytes()).hexdigest()
            statuses = ["pending", "delivered", "delivered-with-deviations", "partially-delivered", "no-change-required", "not-delivered", "rejected-package"]
            coverage = ["pending", "already-implemented", "implemented-as-required", "implemented-with-deviation", "implemented-with-scope-change", "partially-implemented", "not-implemented", "deferred", "blocked-dependency", "blocked-input-ambiguity", "not-applicable"]
            recommendations = ["pending", "no-action", "promote-to-baseline", "update-requirement", "keep-open", "defer", "move-to-other-change", "cancel", "investigate"]
            receipt_template = {
                "schema_version": 4,
                "package_id": "demo-be-change",
                "package_revision": 1,
                "request_id": "demo-be-change",
                "request_version": 1,
                "request_sha256": request_hash,
                "target_repository": "coda",
                "target_contour": "backend",
                "received_at": None,
                "completed_at": None,
                "source_before": {"commit": None, "branch": None, "working_tree_state": "unknown", "relevant_uncommitted_paths": []},
                "source_after": {"commit": None, "branch": None, "working_tree_state": "unknown", "relevant_uncommitted_paths": []},
                "status": "pending",
                "allowed_statuses": statuses,
                "allowed_coverage_statuses": coverage,
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
            receipt_template_path = package / "receipt.template.json"
            receipt_template_path.write_text(json.dumps(receipt_template), encoding="utf-8")
            manifest = {
                "schema_version": 6,
                "package_id": "demo-be-change",
                "package_revision": 1,
                "request": {"id": "demo-be-change", "version": 1},
                "target": {"repository": "coda", "contour": "backend"},
                "payload": [
                    {"path": "request.md", "sha256": request_hash},
                    {"path": "receipt.template.json", "sha256": hashlib.sha256(receipt_template_path.read_bytes()).hexdigest()},
                ],
                "requirements": ["REQ-DEMO-001"],
                "scenarios": ["SCN-DEMO-001"],
                "scenario_requirement_map": {"SCN-DEMO-001": ["REQ-DEMO-001"]},
                "delivery_policy": {"input": "immutable-comparison-point", "feedback": "receipt-then-analyst-review"},
                "allowed_package_statuses": statuses,
                "allowed_coverage_statuses": coverage,
                "allowed_follow_up_recommendations": recommendations,
            }
            (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            result = run(sys.executable, str(tool), "publish", str(handoff), "1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            control = handoff / ".control/handoffctl.py"
            result = run(sys.executable, str(control), "claim", str(handoff), "1", "--by", "developer-session")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = run(sys.executable, str(control), "set-state", str(handoff), "1", "paused", "--reason", "Получены новые вводные")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            root_manifest = json.loads((handoff / "handoff.json").read_text(encoding="utf-8"))
            self.assertEqual(root_manifest["next_sdd_action"]["action"], "wait")
            self.assertEqual(root_manifest["revisions"][0]["receipt"]["expectation"], "optional")
            result = run(sys.executable, str(control), "resume", str(handoff), "1", "--reason", "Продолжить согласованную работу")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            root_manifest = json.loads((handoff / "handoff.json").read_text(encoding="utf-8"))
            self.assertEqual(root_manifest["next_sdd_action"]["action"], "continue")
            self.assertEqual(root_manifest["revisions"][0]["state"], "in-progress")

            commit = "0123456789abcdef0123456789abcdef01234567"
            evidence = [{"path": "src/Demo.java", "symbol": "Demo", "observation": "Поставленное поведение подтверждено"}]
            receipt = json.loads(json.dumps(receipt_template))
            receipt.update({
                "received_at": "2026-08-13T10:00:00Z",
                "completed_at": "2026-08-13T12:00:00Z",
                "source_before": {"commit": commit, "branch": "develop", "working_tree_state": "clean", "relevant_uncommitted_paths": []},
                "source_after": {"commit": commit, "branch": "develop", "working_tree_state": "clean", "relevant_uncommitted_paths": []},
                "status": "delivered-with-deviations",
                "commits": [{"sha256": commit, "summary": "Поставить основной и дополнительный результат", "item_ids": ["REQ-DEMO-001", "SCN-DEMO-001", "ADD-DEMO-001"]}],
                "generated_or_changed_artifacts": ["src/Demo.java"],
                "additional_deliveries": [{"id": "ADD-DEMO-001", "title": "Дополнительный результат", "reason": "Целесообразно выполнить вместе", "delivered_behavior": "Дополнительное правило реализовано", "follow_up_recommendation": "update-requirement", "suggested_destination": "demo", "evidence": evidence, "commit_sha256": [commit], "verification": ["unit-test"]}],
                "verification": [{"type": "tests", "status": "passed", "detail": "Проверки выполнены"}],
            })
            for item in receipt["requirement_coverage"] + receipt["scenario_coverage"]:
                item.update({"status": "implemented-with-deviation", "behavior_before": "Результат отсутствовал", "delivered_behavior": "Результат поставлен", "deviation_from_input": "Изменён технический подход", "follow_up_recommendation": "update-requirement", "evidence": evidence, "commit_sha256": [commit], "verification": ["unit-test"]})
            receipt_path = handoff / "revisions/001/returns/receipt.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            result = run(sys.executable, str(control), "register-receipt", str(handoff), "1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            review = {
                "schema_version": 1,
                "package_id": "demo-be-change",
                "package_revision": 1,
                "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                "status": "approved",
                "allowed_statuses": ["draft", "approved"],
                "allowed_dispositions": ["pending", "promote-to-baseline", "update-requirement", "keep-open", "defer", "move-to-other-change", "cancel", "investigate", "no-action"],
                "requirement_dispositions": [{"requirement": "REQ-DEMO-001", "disposition": "update-requirement", "destination": "features/demo/requirements.md", "reason": "Принять фактический результат", "applied_changes": []}],
                "additional_delivery_dispositions": [{"additional_delivery": "ADD-DEMO-001", "disposition": "update-requirement", "destination": "features/demo/requirements.md", "reason": "Зафиксировать дополнительное правило", "applied_changes": []}],
                "baseline_updates": [],
                "requirement_updates": [],
                "deferred_or_moved_work": [],
                "cancelled_work": [],
                "consistency_updates": [],
                "approved_by": "analyst",
                "approved_at": "2026-08-13T13:00:00Z",
                "notes": None,
            }
            review_path = handoff / "revisions/001/returns/analyst-review.json"
            review_path.write_text(json.dumps(review), encoding="utf-8")
            result = run(sys.executable, str(control), "register-review", str(handoff), "1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = run(sys.executable, str(control), "validate", str(handoff))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            root_manifest = json.loads((handoff / "handoff.json").read_text(encoding="utf-8"))
            self.assertIsNone(root_manifest["active_revision"])
            self.assertEqual(root_manifest["revisions"][0]["state"], "reviewed")
            self.assertEqual(root_manifest["next_sdd_action"]["action"], "wait")


if __name__ == "__main__":
    unittest.main()
