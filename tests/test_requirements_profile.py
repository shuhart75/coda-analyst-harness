from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_requirements_exchange import requirements


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-requirements-profile.py"


class RequirementsProfileTests(unittest.TestCase):
    def validate(self, text: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            feature = project / "features" / "demo"
            feature.mkdir(parents=True)
            (feature / "requirements.md").write_text(text, encoding="utf-8")
            return subprocess.run(
                (sys.executable, str(SCRIPT), str(project), "--feature", "demo"),
                text=True,
                capture_output=True,
                check=False,
            )

    def test_compact_profile_accepts_only_required_sections(self) -> None:
        result = self.validate(requirements())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Компактный профиль проверен: 1", result.stdout)

    def test_process_chapters_inside_requirements_are_accepted(self) -> None:
        text = requirements().replace(
            "### REQ-DEMO-001. Сохранение результата",
            "## Основной процесс\n\n"
            "Пользователь выполняет действие и получает наблюдаемый результат.\n\n"
            "### REQ-DEMO-001. Сохранение результата",
        )
        result = self.validate(text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_requirement_after_process_chapter_is_checked(self) -> None:
        extra = """
## Ошибочный исход

### REQ-DEMO-002. Отклонение ошибочного действия

Система должна отклонить ошибочное действие.

#### Сценарий: ошибочное действие

**Когда** пользователь выполняет ошибочное действие.

**Тогда** система показывает отказ.

"""
        text = requirements().replace(
            "## Влияние на соседние функциональности",
            extra + "## Влияние на соседние функциональности",
        )
        result = self.validate(text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_each_requirement_requires_nested_when_then_scenario(self) -> None:
        text = requirements().replace(
            "#### Сценарий: успешное действие\n\n"
            "**Когда** пользователь выполняет действие.\n\n"
            "**Тогда** система показывает результат.\n\n",
            "",
        )
        result = self.validate(text)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REQ-DEMO-001: отсутствует вложенный сценарий", result.stdout)

    def test_english_normative_keywords_are_rejected(self) -> None:
        text = requirements().replace("**Когда**", "WHEN").replace("**Тогда**", "THEN")
        result = self.validate(text)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ключевое слово Когда", result.stdout)
        self.assertIn("нормативные заголовки и ключевые слова", result.stdout)

    def test_unknown_requirement_reference_is_rejected(self) -> None:
        text = requirements().replace("Влияний нет.", "Связано с `REQ-DEMO-999`.")
        result = self.validate(text)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ссылка на неизвестное требование: REQ-DEMO-999", result.stdout)

    def test_duplicate_requirement_is_rejected(self) -> None:
        duplicate = """
### REQ-DEMO-001. Повторное правило

Система должна отклонить повтор.

#### Сценарий: повтор

**Когда** действие повторяется.

**Тогда** система отклоняет повтор.

"""
        text = requirements().replace(
            "## Влияние на соседние функциональности",
            duplicate + "## Влияние на соседние функциональности",
        )
        result = self.validate(text)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("определения REQ-* должны быть уникальными", result.stdout)

    def test_missing_required_section_is_rejected(self) -> None:
        text = requirements().replace(
            "## Влияние на соседние функциональности\n\nВлияний нет.\n\n",
            "",
        )
        result = self.validate(text)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("отсутствует раздел: Влияние на соседние функциональности", result.stdout)


if __name__ == "__main__":
    unittest.main()
