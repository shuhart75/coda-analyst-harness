from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from commit_message_policy import has_tracker_identifier, require_valid_commit_message
from workspace import install_commit_message_hook


class CommitMessagePolicyTests(unittest.TestCase):
    def test_descriptive_message_is_allowed(self) -> None:
        require_valid_commit_message(
            "Усилить проверку сообщений коммитов\n\nДобавить машинный запрет и тесты."
        )

    def test_tracker_identifiers_are_rejected_everywhere(self) -> None:
        messages = (
            "Обновить RSCON-123",
            "Обновить rscon-123",
            "Обновить требования\n\nСвязано с RSCON_ABC-456.",
            "Merge feature/RSCON-789 into main",
            "См. https://tracker.example/browse/RSCON-321",
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertTrue(has_tracker_identifier(message))
                with self.assertRaises(ValueError):
                    require_valid_commit_message(message)

    def test_cli_rejects_without_repeating_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            message = Path(temporary) / "message"
            message.write_text("Обновить RSCON-123\n", encoding="utf-8")
            result = subprocess.run(
                (sys.executable, str(ROOT / "scripts/commit_message_policy.py"), str(message)),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Сообщение коммита отклонено", result.stderr)
            self.assertNotIn("RSCON-123", result.stderr)

    def test_existing_custom_hook_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            subprocess.run(("git", "init", "-b", "main", str(repository)), check=True, capture_output=True)
            hook = repository / ".git/hooks/commit-msg"
            original = "#!/bin/sh\nexit 0\n"
            hook.write_text(original, encoding="utf-8")
            hook.chmod(0o755)
            with self.assertRaises(ValueError):
                install_commit_message_hook(
                    repository, ROOT / "scripts/commit_message_policy.py"
                )
            self.assertEqual(hook.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
