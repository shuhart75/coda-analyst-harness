from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_requirements_exchange import requirements


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-requirements-wording.py"


class RequirementsWordingTests(unittest.TestCase):
    def validate(self, text: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            feature = project / "features/demo"
            feature.mkdir(parents=True)
            (feature / "requirements.md").write_text(text, encoding="utf-8")
            return subprocess.run(
                (sys.executable, str(SCRIPT), str(project), "--feature", "demo"),
                text=True,
                capture_output=True,
                check=False,
            )

    def test_concrete_scenario_passes(self) -> None:
        result = self.validate(requirements())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Формулировки требований проверены: 1", result.stdout)

    def test_ambiguous_role_is_rejected(self) -> None:
        text = requirements().replace(
            "**Когда** пользователь выполняет действие.",
            "**Когда** пользователь имеет любую роль Реестра.",
        )
        result = self.validate(text)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("неопределённая-роль", result.stdout)

    def test_placeholder_event_is_rejected(self) -> None:
        text = requirements().replace(
            "**Когда** пользователь выполняет действие.",
            "**Когда** наступает соответствующее событие жизненного цикла.",
        )
        result = self.validate(text)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("неявная-ссылка", result.stdout)
        self.assertIn("непроверяемое-условие", result.stdout)

    def test_generic_reused_when_is_rejected(self) -> None:
        text = requirements().replace(
            "**Когда** пользователь выполняет действие.",
            "**Когда** пользователь запрашивает доступ к данным или действию Реестра.",
        )
        result = self.validate(text)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("непроверяемое-условие", result.stdout)

    def test_normative_then_is_rejected(self) -> None:
        text = requirements().replace(
            "**Тогда** система показывает результат.",
            "**Тогда** система должна показать результат.",
        )
        result = self.validate(text)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("нормативный-результат", result.stdout)

    def test_undefined_standard_algorithm_is_rejected(self) -> None:
        text = requirements().replace(
            "**Когда** пользователь выполняет действие.",
            "**Когда** выполняется штатный алгоритм обработки.",
        )
        result = self.validate(text)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("неопределённый-алгоритм", result.stdout)

    def test_code_examples_are_not_checked_as_prose(self) -> None:
        text = requirements().replace(
            "## Влияние на соседние функциональности",
            "```text\nПользователь имеет любую роль Реестра\n```\n\n"
            "## Влияние на соседние функциональности",
        )
        result = self.validate(text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
