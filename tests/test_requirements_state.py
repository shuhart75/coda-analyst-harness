from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_requirements_exchange import requirements


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "requirementsctl.py"
EXCHANGE = ROOT / "scripts" / "requirements-exchange.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False)


class RequirementsStateTests(unittest.TestCase):
    def command(self, script: Path, *args: str) -> dict:
        result = run(sys.executable, str(script), *args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def project(self, root: Path) -> tuple[Path, Path]:
        project = root / "documents"
        feature = project / "features/demo"
        feature.mkdir(parents=True)
        (feature / "requirements.md").write_text(requirements(), encoding="utf-8")
        self.command(SCRIPT, "init", str(project), "demo")
        return project, feature

    def test_analyst_change_offers_once_after_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.project(Path(temp))
            self.command(SCRIPT, "begin-preparation", str(project), "demo")
            prepared = self.command(EXCHANGE, "prepare", str(project), "demo", "--analyst", "ivan")
            self.command(
                SCRIPT,
                "mark-published",
                str(project),
                "demo",
                "--manifest",
                prepared["manifest"],
                "--revision",
                "1",
                "--destination-role",
                "analytics",
            )
            with (feature / "requirements.md").open("a", encoding="utf-8") as handle:
                handle.write("\nАналитическое уточнение.\n")
            changed = self.command(
                SCRIPT, "record-change", str(project), "demo", "--origin", "analyst"
            )
            self.assertEqual(changed["next_action"], "offer-new-revision-once")
            offered = self.command(SCRIPT, "mark-offered", str(project), "demo")
            self.assertEqual(offered["next_action"], "await-analyst-decision-without-repeating-offer")
            declined = self.command(SCRIPT, "decline-revision", str(project), "demo")
            self.assertEqual(declined["next_action"], "wait-explicit-preparation-command")

    def test_developer_result_never_offers_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.project(Path(temp))
            with (feature / "requirements.md").open("a", encoding="utf-8") as handle:
                handle.write("\nФактическое уточнение.\n")
            changed = self.command(
                SCRIPT,
                "record-change",
                str(project),
                "demo",
                "--origin",
                "developer-result",
                "--return-id",
                "demo:001:returns/tasks/DEV-001.md:abc",
            )
            self.assertEqual(changed["next_action"], "continue-root-requirements")
            self.assertEqual(changed["state"]["last_change"]["origin"], "developer-result")
            self.assertNotIn("slice_derivation", changed["state"])

    def test_schema_one_state_is_migrated_without_slice_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.project(Path(temp))
            state_path = feature / "requirements-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["schema_version"] = 1
            state["slice_derivation"] = {"state": "stale", "requirements_sha256": None}
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            migrated = self.command(SCRIPT, "status", str(project), "demo")
            self.assertEqual(migrated["state"]["schema_version"], 2)
            self.assertNotIn("slice_derivation", migrated["state"])


if __name__ == "__main__":
    unittest.main()
