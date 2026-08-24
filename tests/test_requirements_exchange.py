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


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False)


def requirements(rule: str = "Система должна показать результат.") -> str:
    return f"""# Требования по функциональности — Демонстрация

Статус: **черновик**
Редакция: `1`
Формат: **последовательный человекочитаемый**
Функциональность: `demo`

## Кратко о функциональности

Понятное описание.

## Цель и ожидаемый результат

Получить результат.

## Границы

Входит один сценарий.

## Текущее и требуемое состояние

Сейчас результата нет. После изменения результат есть.

## Участники, внешние системы и данные

Пользователь и система.

## Работа с результатом

### Результат

Результат показан.

### Участники и начальные условия

Пользователь открыл страницу.

### Основной ход

1. Пользователь выполняет действие.

### Правила

**REQ-DEMO-001. Отображение результата**

{rule}

### Исключения и ошибки

Ошибок нет.

### Примеры приёмки

**AC-DEMO-001. Успешное действие**

- Дано: открыта страница.
- Когда: пользователь выполняет действие.
- Тогда: система показывает результат.
- Требования: `REQ-DEMO-001`.

### Влияния

Нет.

## Общие правила

Нет.

## Ошибки и пограничные случаи

Нет.

## Нефункциональные требования

Нет применимых требований.

## Доработки затронутых функциональностей

Нет.

## Подчистка устаревшего поведения

Нет.

## Сводная трассировка

| Требование | Источник | Примеры приёмки | Задачи разработки | Результат реализации |
|---|---|---|---|---|
| REQ-DEMO-001 | решение | AC-DEMO-001 | задача не получена | результат не получен |

## Открытые вопросы

Нет.
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
            project, _ = self.prepare_project(Path(temp))
            result = self.command("prepare", str(project), "demo", "--analyst", "ivan")
            self.assertEqual(result["destination_role"], "analytics")
            exchange = project / "requirements-exchange"
            self.assertEqual(Path(result["exchange_root"]), exchange)
            self.assertTrue((exchange / "AGENTS.md").is_file())
            self.assertTrue((exchange / "demo/manifest.json").is_file())
            self.assertTrue((exchange / "demo/revisions/001/requirements.md").is_file())
            manifest = json.loads((exchange / "demo/manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["sdd_contract"], "../AGENTS.md")
            self.assertFalse((exchange / "demo/revisions/001/returns").exists())
            self.assertFalse((project / "features/demo/slices").exists())

    def test_missing_code_exchange_does_not_create_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, _ = self.prepare_project(root)
            code = root / "coda"
            code.mkdir()
            result = self.command(
                "prepare", str(project), "demo", "--analyst", "ivan", "--code-root", str(code)
            )
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
            result = self.command(
                "prepare", str(project), "demo", "--analyst", "ivan", "--code-root", str(code),
                env=environment,
            )
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

    def test_rejected_code_push_falls_back_without_dirtying_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, _ = self.prepare_project(root)
            remote, code = self.prepare_code_repository(root)
            hook = remote / "hooks/pre-receive"
            hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            hook.chmod(0o755)
            before_head = self.git(code, "rev-parse", "HEAD")
            result = self.command(
                "prepare", str(project), "demo", "--analyst", "ivan", "--code-root", str(code)
            )
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
            result = self.command(
                "prepare", str(project), "demo", "--analyst", "ivan", "--code-root", str(code)
            )
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
            first = self.command("prepare", str(project), "demo", "--analyst", "ivan")
            first_path = Path(first["requirements"])
            first_bytes = first_path.read_bytes()
            (feature / "requirements.md").write_text(
                requirements("Система должна показать обновлённый результат."),
                encoding="utf-8",
            )
            second = self.command("prepare", str(project), "demo", "--analyst", "ivan")
            self.assertEqual(second["revision"], 2)
            self.assertEqual(first_path.read_bytes(), first_bytes)
            manifest = json.loads(Path(second["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["revisions"][0]["state"], "superseded")
            self.assertEqual(manifest["revisions"][1]["state"], "sent")

    def test_scan_filters_by_owner_and_records_processing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, _ = self.prepare_project(Path(temp))
            prepared = self.command("prepare", str(project), "demo", "--analyst", "ivan")
            returns = Path(prepared["requirements"]).parent / "returns"
            returns.mkdir()
            (returns / "tasks.md").write_text("# Задачи разработки\n", encoding="utf-8")
            scan = self.command("scan", str(project), "--analyst", "ivan")
            self.assertEqual(scan["new_result_count"], 1)
            return_id = scan["items"][0]["new_returns"][0]["return_id"]
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
            self.assertEqual(repeated_scan["new_result_count"], 0)

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
