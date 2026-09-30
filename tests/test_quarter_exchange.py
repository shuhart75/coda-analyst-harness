from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("quarter_repository_exchange", ROOT / "scripts/repository-exchange.py")
exchange = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exchange)


class QuarterExchangeTests(unittest.TestCase):
    def test_quarter_content_allowed_but_native_settings_remain_forbidden(self):
        paths = [
            "delivery-index.json", "migration-layout.json", "backlog/unscheduled/requirements.md",
            "quarters/2026-Q4/features/kib/deliveries/extension/requirements.md",
            "quarters/2026-Q4/quarter/quarter-plan.md",
            "quarters/2026-Q4/features/kib/.gigacode/settings.json",
            "quarters/2026-Q4/GIGACODE.md",
        ]
        with patch.object(exchange, "tracked_paths", return_value=paths):
            violations = exchange.analytics_content_violations(Path("/unused"), "HEAD")
        self.assertEqual({item["path"] for item in violations}, set(paths[-2:]))

    def test_import_keeps_quarter_requirements_and_plans_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
            git("init", "-q")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            requirement = root / "quarters/2026-Q4/features/kib/deliveries/extension/requirements.md"
            requirement.parent.mkdir(parents=True)
            requirement.write_text("# REQ-KIB-001\n\n## Сценарий исходный\n")
            index = root / "delivery-index.json"
            index.write_text('{"schema_version":1}')
            plan = root / "quarters/2026-Q4/quarter/quarter-plan.md"
            plan.parent.mkdir(parents=True)
            plan.write_text("Original plan\n")
            git("add", "--", ".")
            git("commit", "-qm", "Initial content")
            before = git("rev-parse", "HEAD")
            requirement.write_text("# New scope\n")
            index.write_text('{"schema_version":1,"deliveries":{}}')
            plan.write_text("Changed plan\n")
            git("add", "--", ".")
            git("commit", "-qm", "Changed content")
            report = exchange.source_import_report(root, before, "HEAD")
            self.assertEqual(len(report["changed_paths"]), 3)
            self.assertTrue(all(item["protected_artifact"] for item in report["changed_paths"]))
            change = next(item for item in report["changed_paths"] if item["path"].endswith("requirements.md"))
            self.assertEqual(change["removed_requirements"], ["REQ-KIB-001"])
            self.assertEqual(change["removed_scenarios"], ["## Сценарий исходный"])


if __name__ == "__main__":
    unittest.main()
