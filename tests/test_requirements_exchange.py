from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "requirements-exchange.py"
STATE_SCRIPT = ROOT / "scripts" / "requirementsctl.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False)


def requirements(rule: str = "Система должна показать результат.") -> str:
    return f"""# Демонстрационная функциональность

Редакция: `1`
Формат: **компактная спецификация функциональности**
Функциональность: `demo`

## Назначение

Пользователь должен получить однозначный наблюдаемый результат.

## Границы

В объём входит отображение результата. Другие действия не входят.

## Требования

### REQ-DEMO-001. Отображение результата

{rule}

#### Сценарий: успешное действие

**Когда** пользователь выполняет действие.

**Тогда** система показывает результат.

## Влияние на соседние функциональности

Влияний нет.

## Источники и открытые вопросы

Источник: решение аналитика.

Открытых вопросов нет.
"""


class RequirementsExchangeTests(unittest.TestCase):
    def prepare_project(self, root: Path) -> tuple[Path, Path]:
        project = root / "documents"
        feature = project / "features" / "demo"
        feature.mkdir(parents=True)
        (feature / "requirements.md").write_text(requirements(), encoding="utf-8")
        return project, feature

    def command(self, *args: str, env: dict[str, str] | None = None) -> dict:
        result = subprocess.run(
            (sys.executable, str(SCRIPT), *args),
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def authorize(self, project: Path, feature: str = "demo") -> None:
        commands = (
            ("begin-preparation", str(project), feature),
            (
                "record-audit", str(project), feature,
                "--finding-count", "0",
                "--blocking-finding-count", "0",
                "--summary", "Проверки пройдены",
            ),
            ("confirm-audit", str(project), feature),
        )
        for arguments in commands:
            result = run(sys.executable, str(STATE_SCRIPT), *arguments)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def prepare(self, project: Path, *extra: str, env: dict[str, str] | None = None) -> dict:
        self.authorize(project)
        return self.command(
            "prepare", str(project), "demo", "--analyst", "ivan", *extra, env=env
        )

    def write_receipt(self, prepared: dict, *, sha256: str | None = None) -> Path:
        requirements_path = Path(prepared["requirements"])
        feature_exchange = requirements_path.parents[2]
        manifest = json.loads((feature_exchange / "manifest.json").read_text(encoding="utf-8"))
        revision = prepared["revision"]
        entry = next(item for item in manifest["revisions"] if item["revision"] == revision)
        returns = requirements_path.parent / "returns"
        returns.mkdir(exist_ok=True)
        receipt = {
            "schema_version": 1,
            "kind": "requirements-revision-receipt",
            "feature": "demo",
            "requirements_revision": revision,
            "requirements_sha256": sha256 or entry["sha256"],
            "received_at": "2026-08-28T12:00:00+00:00",
            "received_by": "coda-sdd",
            "state": "accepted",
        }
        path = returns / "receipt.json"
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def git(self, repository: Path, *args: str) -> str:
        result = run("git", "-C", str(repository), *args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def prepare_code_repository(self, root: Path, *, with_exchange: bool = True) -> tuple[Path, Path]:
        remote = root / "code.git"
        seed = root / "code-seed"
        code = root / "coda"
        result = run("git", "init", "--bare", str(remote))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = run("git", "init", "-b", "main", str(seed))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.git(seed, "config", "user.name", "Test")
        self.git(seed, "config", "user.email", "test@example.com")
        if with_exchange:
            exchange = seed / "requirements-exchange"
            exchange.mkdir()
            (exchange / "README.md").write_text("# Каталог обмена\n", encoding="utf-8")
            self.git(seed, "add", "--", "requirements-exchange/README.md")
        else:
            (seed / "README.md").write_text("# Код\n", encoding="utf-8")
            self.git(seed, "add", "--", "README.md")
        self.git(seed, "commit", "-m", "Initial")
        self.git(seed, "remote", "add", "origin", str(remote))
        self.git(seed, "push", "-u", "origin", "main")
        self.git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
        result = run("git", "clone", "--quiet", str(remote), str(code))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return remote, code

    def test_missing_code_uses_analytics_and_creates_no_slices(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.prepare_project(Path(temp))
            (feature / "requirements_iso.md").write_text(
                "# Архив прежних требований\n",
                encoding="utf-8",
            )
            result = self.prepare(project)
            self.assertEqual(result["destination_role"], "analytics")
            exchange = project / "requirements-exchange"
            self.assertEqual(Path(result["exchange_root"]), exchange)
            self.assertTrue((exchange / "AGENTS.md").is_file())
            self.assertTrue((exchange / "demo/manifest.json").is_file())
            self.assertTrue((exchange / "demo/revisions/001/requirements.md").is_file())
            self.assertFalse((exchange / "demo/revisions/001/requirements_iso.md").exists())
            self.assertNotIn(
                "requirements_iso.md",
                [path.name for path in (exchange / "demo/revisions/001").iterdir()],
            )
            manifest = json.loads((exchange / "demo/manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 3)
            self.assertEqual(manifest["sdd_contract"], "../AGENTS.md")
            self.assertEqual(manifest["developer_sdd"]["input_role"], "business-requirements")
            self.assertEqual(
                manifest["developer_sdd"]["revision_receipt"],
                "required-before-other-returns",
            )
            self.assertEqual(
                manifest["developer_sdd"]["contours"],
                "backend-and-frontend-are-separate",
            )
            self.assertEqual(manifest["revisions"][0]["returns_contract_version"], 1)
            self.assertTrue((exchange / "receipt.template.json").is_file())
            self.assertFalse((exchange / "demo/revisions/001/returns").exists())
            self.assertFalse((project / "features/demo/slices").exists())

    def test_prepare_without_confirmed_audit_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, _ = self.prepare_project(Path(temp))
            result = run(
                sys.executable,
                str(SCRIPT),
                "prepare",
                str(project),
                "demo",
                "--analyst",
                "ivan",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("аудит", result.stdout.lower())
            self.assertFalse((project / "requirements-exchange").exists())

    def test_prepare_after_unconfirmed_audit_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, _ = self.prepare_project(Path(temp))
            begin = run(sys.executable, str(STATE_SCRIPT), "begin-preparation", str(project), "demo")
            self.assertEqual(begin.returncode, 0, begin.stdout + begin.stderr)
            audit = run(
                sys.executable,
                str(STATE_SCRIPT),
                "record-audit",
                str(project),
                "demo",
                "--finding-count",
                "0",
                "--blocking-finding-count",
                "0",
                "--summary",
                "Замечаний нет",
            )
            self.assertEqual(audit.returncode, 0, audit.stdout + audit.stderr)
            result = run(
                sys.executable, str(SCRIPT), "prepare", str(project), "demo", "--analyst", "ivan"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("не подтверждён", result.stdout)

    def test_prepare_after_requirements_change_invalidates_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.prepare_project(Path(temp))
            self.authorize(project)
            with (feature / "requirements.md").open("a", encoding="utf-8") as handle:
                handle.write("\nИзменение после подтверждения.\n")
            result = run(
                sys.executable, str(SCRIPT), "prepare", str(project), "demo", "--analyst", "ivan"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("не подтверждён", result.stdout)
            self.assertFalse((project / "requirements-exchange").exists())

    def test_prepare_with_damaged_audit_state_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.prepare_project(Path(temp))
            self.authorize(project)
            state_path = feature / "requirements-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["delivery_audit"]["summary"] = ""
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            result = run(
                sys.executable, str(SCRIPT), "prepare", str(project), "demo", "--analyst", "ivan"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((project / "requirements-exchange").exists())

    def test_missing_code_exchange_does_not_create_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, _ = self.prepare_project(root)
            code = root / "coda"
            code.mkdir()
            result = self.prepare(project, "--code-root", str(code))
            self.assertEqual(result["destination_role"], "analytics")
            self.assertFalse((code / "requirements-exchange").exists())

    def test_existing_writable_code_exchange_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, _ = self.prepare_project(root)
            remote, code = self.prepare_code_repository(root)
            before_head = self.git(code, "rev-parse", "HEAD")
            before_status = self.git(code, "status", "--porcelain=v1")
            environment = os.environ.copy()
            environment["CODA_ANALYST_STATE_ROOT"] = str(root / "state")
            result = self.prepare(project, "--code-root", str(code), env=environment)
            self.assertEqual(result["destination_role"], "code")
            self.assertEqual(result["repository_path"], "requirements-exchange/demo")
            self.assertEqual(
                result["requirements_repository_path"],
                "requirements-exchange/demo/revisions/001/requirements.md",
            )
            self.assertNotIn("exchange_root", result)
            self.assertNotIn("requirements", result)
            self.assertFalse((project / "requirements-exchange").exists())
            self.assertEqual(self.git(code, "rev-parse", "HEAD"), before_head)
            self.assertEqual(self.git(code, "status", "--porcelain=v1"), before_status)
            self.assertTrue(Path(result["manifest"]).is_file())
            inspect = root / "inspect"
            clone = run("git", "clone", "--quiet", str(remote), str(inspect))
            self.assertEqual(clone.returncode, 0, clone.stdout + clone.stderr)
            self.assertTrue((inspect / "requirements-exchange/demo/revisions/001/requirements.md").is_file())
            self.assertTrue((inspect / "requirements-exchange/receipt.template.json").is_file())

    def test_rejected_code_push_falls_back_without_dirtying_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, _ = self.prepare_project(root)
            remote, code = self.prepare_code_repository(root)
            hook = remote / "hooks/pre-receive"
            hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            hook.chmod(0o755)
            before_head = self.git(code, "rev-parse", "HEAD")
            result = self.prepare(project, "--code-root", str(code))
            self.assertEqual(result["destination_role"], "analytics")
            self.assertIn("отклонила отправку", result["selection_reason"])
            self.assertTrue((project / "requirements-exchange/demo/revisions/001/requirements.md").is_file())
            self.assertEqual(self.git(code, "rev-parse", "HEAD"), before_head)
            self.assertEqual(self.git(code, "status", "--porcelain=v1"), "")

    def test_remote_without_exchange_falls_back_and_does_not_create_code_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, _ = self.prepare_project(root)
            remote, code = self.prepare_code_repository(root, with_exchange=False)
            result = self.prepare(project, "--code-root", str(code))
            self.assertEqual(result["destination_role"], "analytics")
            inspect = root / "inspect"
            clone = run("git", "clone", "--quiet", str(remote), str(inspect))
            self.assertEqual(clone.returncode, 0, clone.stdout + clone.stderr)
            self.assertFalse((inspect / "requirements-exchange").exists())

    def test_invalid_remote_manifest_blocks_instead_of_splitting_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, _ = self.prepare_project(root)
            remote, code = self.prepare_code_repository(root)
            seed = root / "corrupt"
            clone = run("git", "clone", "--quiet", str(remote), str(seed))
            self.assertEqual(clone.returncode, 0, clone.stdout + clone.stderr)
            manifest = seed / "requirements-exchange/demo/manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"schema_version": 999}\n', encoding="utf-8")
            self.git(seed, "config", "user.name", "Test")
            self.git(seed, "config", "user.email", "test@example.com")
            self.git(seed, "add", "--", "requirements-exchange/demo/manifest.json")
            self.git(seed, "commit", "-m", "Corrupt exchange")
            self.git(seed, "push", "origin", "main")

            self.authorize(project)

            result = run(
                sys.executable,
                str(SCRIPT),
                "prepare",
                str(project),
                "demo",
                "--analyst",
                "ivan",
                "--code-root",
                str(code),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("manifest.json", result.stdout)
            self.assertFalse((project / "requirements-exchange").exists())

    def test_invalid_feature_cannot_escape_exchange_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, _ = self.prepare_project(Path(temp))
            result = run(
                sys.executable,
                str(SCRIPT),
                "prepare",
                str(project),
                "../escape",
                "--analyst",
                "ivan",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Идентификатор функциональности", result.stdout)

    def test_new_revision_preserves_previous_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.prepare_project(Path(temp))
            first = self.prepare(project)
            first_path = Path(first["requirements"])
            first_bytes = first_path.read_bytes()
            (feature / "requirements.md").write_text(
                requirements("Система должна показать обновлённый результат."),
                encoding="utf-8",
            )
            second = self.prepare(project)
            self.assertEqual(second["revision"], 2)
            self.assertEqual(first_path.read_bytes(), first_bytes)
            manifest = json.loads(Path(second["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["revisions"][0]["state"], "superseded")
            self.assertEqual(manifest["revisions"][1]["state"], "sent")

    def test_schema_one_manifest_is_upgraded_when_new_revision_is_added(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.prepare_project(Path(temp))
            first = self.prepare(project)
            manifest_path = Path(first["manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = 1
            manifest.pop("developer_sdd")
            manifest["requested_returns"] = {
                "tasks": {
                    "path": "revisions/<NNN>/returns/tasks.md",
                    "meaning": "Уже согласованная разработчиками разбивка по задачам",
                    "estimate": "optional",
                    "jira_key": "optional",
                },
                "task_results": {
                    "path": "revisions/<NNN>/returns/tasks/<task-id>.md",
                    "frequency": "После выполнения или существенного изменения каждой задачи",
                },
                "summary": {
                    "path": "revisions/<NNN>/returns/summary.md",
                    "meaning": "Итоговое покрытие всех требований активной редакции",
                },
            }
            manifest["traceability"] = {
                "requirement_pattern": "REQ-*",
                "chain": "REQ-* -> tasks.md -> task result -> summary.md",
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (feature / "requirements.md").write_text(
                requirements("Система должна показать обновлённый результат."),
                encoding="utf-8",
            )
            second = self.prepare(project)
            self.assertEqual(second["revision"], 2)
            upgraded = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(upgraded["schema_version"], 3)
            self.assertIn("developer_sdd", upgraded)
            self.assertEqual(len(upgraded["revisions"]), 2)
            self.assertEqual(upgraded["revisions"][0]["returns_contract_version"], 0)
            self.assertEqual(upgraded["revisions"][1]["returns_contract_version"], 1)

    def test_schema_two_returns_remain_valid_legacy_history_after_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.prepare_project(Path(temp))
            first = self.prepare(project)
            manifest_path = Path(first["manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = 2
            manifest["requested_returns"].pop("receipt")
            manifest["developer_sdd"].pop("revision_receipt")
            manifest["revisions"][0].pop("returns_contract_version")
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            legacy_returns = Path(first["requirements"]).parent / "returns"
            legacy_returns.mkdir()
            (legacy_returns / "tasks.md").write_text(
                "# Историческая декомпозиция\n", encoding="utf-8"
            )

            (feature / "requirements.md").write_text(
                requirements("Система должна показать новый результат."), encoding="utf-8"
            )
            second = self.prepare(project)
            upgraded = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(upgraded["schema_version"], 3)
            self.assertEqual(upgraded["revisions"][0]["returns_contract_version"], 0)
            self.assertEqual(upgraded["revisions"][1]["returns_contract_version"], 1)
            validated = self.command("validate", str(project / "requirements-exchange"))
            states = {
                item["revision"]: item["state"] for item in validated["processing"]
            }
            self.assertEqual(states, {1: "legacy-results-present", 2: "new"})

    def test_managed_receiver_contract_is_refreshed_for_new_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.prepare_project(Path(temp))
            first = self.prepare(project)
            exchange = Path(first["manifest"]).parents[1]
            agents = exchange / "AGENTS.md"
            agents.write_text(
                "# Договор SDD для обмена требованиями\n\nВерсия договора: `1`\n",
                encoding="utf-8",
            )
            (feature / "requirements.md").write_text(
                requirements("Система должна показать обновлённый результат."),
                encoding="utf-8",
            )
            self.prepare(project)
            refreshed = agents.read_text(encoding="utf-8")
            self.assertIn("Версия договора: `3`", refreshed)
            self.assertIn("бизнес-контрактом, а не готовым локальным `spec.md`", refreshed)
            self.assertIn("Не объединяй клиентскую и серверную работу", refreshed)
            self.assertIn("returns/receipt.json", refreshed)

    def test_scan_filters_by_owner_and_records_processing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, _ = self.prepare_project(Path(temp))
            prepared = self.prepare(project)
            self.write_receipt(prepared)
            returns = Path(prepared["requirements"]).parent / "returns"
            (returns / "tasks.md").write_text("# Задачи разработки\n", encoding="utf-8")
            scan = self.command("scan", str(project), "--analyst", "ivan")
            self.assertEqual(scan["items"][0]["processing"]["state"], "decomposed")
            self.assertEqual(scan["new_result_count"], 2)
            return_id = next(
                item["return_id"]
                for item in scan["items"][0]["new_returns"]
                if item["relative_path"].endswith("tasks.md")
            )
            self.command(
                "record-processed",
                str(project),
                "demo",
                "--return-id",
                return_id,
                "--decision",
                "no-change",
                "--analyst",
                "ivan",
            )
            repeated_scan = self.command("scan", str(project), "--analyst", "ivan")
            self.assertEqual(repeated_scan["new_result_count"], 1)
            self.assertTrue(
                repeated_scan["items"][0]["new_returns"][0]["relative_path"].endswith("receipt.json")
            )

    def test_processing_stages_are_derived_from_return_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, _ = self.prepare_project(Path(temp))
            prepared = self.prepare(project)

            scan = self.command("scan", str(project), "--analyst", "ivan")
            self.assertEqual(scan["items"][0]["processing"]["state"], "new")

            self.write_receipt(prepared)
            scan = self.command("scan", str(project), "--analyst", "ivan")
            self.assertEqual(scan["items"][0]["processing"]["state"], "accepted")

            returns = Path(prepared["requirements"]).parent / "returns"
            (returns / "tasks.md").write_text("# Задачи разработки\n", encoding="utf-8")
            scan = self.command("scan", str(project), "--analyst", "ivan")
            self.assertEqual(scan["items"][0]["processing"]["state"], "decomposed")

            (returns / "tasks").mkdir()
            (returns / "tasks/DEV-001.md").write_text(
                "# Результат DEV-001\n\nREQ-DEMO-001\n", encoding="utf-8"
            )
            scan = self.command("scan", str(project), "--analyst", "ivan")
            self.assertEqual(scan["items"][0]["processing"]["state"], "in-progress")

            (returns / "summary.md").write_text(
                "# Итог\n\n| Требование | Состояние |\n|---|---|\n| REQ-DEMO-001 | реализовано |\n",
                encoding="utf-8",
            )
            scan = self.command("scan", str(project), "--analyst", "ivan")
            self.assertEqual(scan["items"][0]["processing"]["state"], "completed")
            validated = self.command("validate", str(project / "requirements-exchange"))
            self.assertEqual(validated["processing"][0]["state"], "completed")

    def test_returns_before_receipt_are_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, _ = self.prepare_project(Path(temp))
            prepared = self.prepare(project)
            returns = Path(prepared["requirements"]).parent / "returns"
            returns.mkdir()
            (returns / "tasks.md").write_text("# Задачи разработки\n", encoding="utf-8")

            scan = self.command("scan", str(project), "--analyst", "ivan")
            self.assertEqual(scan["items"][0]["status"], "invalid-return-lifecycle")
            self.assertIn("до квитанции", scan["items"][0]["errors"][0])
            result = run(
                sys.executable,
                str(SCRIPT),
                "validate",
                str(project / "requirements-exchange"),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("до квитанции", result.stdout)

    def test_receipt_must_match_revision_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, _ = self.prepare_project(Path(temp))
            prepared = self.prepare(project)
            self.write_receipt(prepared, sha256="0" * 64)
            scan = self.command("scan", str(project), "--analyst", "ivan")
            self.assertEqual(scan["items"][0]["processing"]["state"], "invalid")
            self.assertTrue(any("requirements_sha256" in error for error in scan["items"][0]["errors"]))

    def test_summary_must_cover_every_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, _ = self.prepare_project(Path(temp))
            prepared = self.prepare(project)
            self.write_receipt(prepared)
            returns = Path(prepared["requirements"]).parent / "returns"
            (returns / "tasks.md").write_text("# Задачи разработки\n", encoding="utf-8")
            (returns / "summary.md").write_text(
                "# Итог\n\nТребования не перечислены.\n", encoding="utf-8"
            )
            scan = self.command("scan", str(project), "--analyst", "ivan")
            self.assertEqual(scan["items"][0]["processing"]["state"], "invalid")
            self.assertTrue(any("REQ-DEMO-001" in error for error in scan["items"][0]["errors"]))

    def test_new_revision_requires_its_own_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.prepare_project(Path(temp))
            first = self.prepare(project)
            self.write_receipt(first)
            (feature / "requirements.md").write_text(
                requirements("Система должна показать новый результат."), encoding="utf-8"
            )
            second = self.prepare(project)
            self.assertEqual(second["revision"], 2)
            scan = self.command("scan", str(project), "--analyst", "ivan")
            self.assertEqual(scan["items"][0]["revision"], 2)
            self.assertEqual(scan["items"][0]["processing"]["state"], "new")

    def test_owned_scan_requires_identity_but_explicit_all_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, _ = self.prepare_project(Path(temp))
            environment = {
                **os.environ,
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "ANALYST_HARNESS_STATE_ROOT": str(Path(temp) / "empty-state"),
                "CODA_ANALYST_STATE_ROOT": str(Path(temp) / "empty-state"),
            }
            environment.pop("CODA_ANALYST_ID", None)
            result = subprocess.run(
                (sys.executable, str(SCRIPT), "scan", str(project)),
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("текущего аналитика", result.stdout)
            all_result = subprocess.run(
                (sys.executable, str(SCRIPT), "scan", str(project), "--all"),
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(all_result.returncode, 0, all_result.stdout + all_result.stderr)
            self.assertEqual(json.loads(all_result.stdout)["scope"], "all")


if __name__ == "__main__":
    unittest.main()
