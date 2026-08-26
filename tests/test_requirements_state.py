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

    def authorize(self, project: Path, feature: str = "demo") -> None:
        self.command(SCRIPT, "begin-preparation", str(project), feature)
        audited = self.command(
            SCRIPT,
            "record-audit",
            str(project),
            feature,
            "--finding-count",
            "1",
            "--blocking-finding-count",
            "0",
            "--summary",
            "Одно неблокирующее замечание устранено",
        )
        self.assertEqual(audited["next_action"], "show-audit-and-request-analyst-confirmation")
        self.command(SCRIPT, "confirm-audit", str(project), feature)

    def test_analyst_change_offers_once_after_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.project(Path(temp))
            self.authorize(project)
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
            self.assertEqual(migrated["state"]["schema_version"], 4)
            self.assertNotIn("slice_derivation", migrated["state"])

    def test_old_publication_authorization_migrates_to_required_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.project(Path(temp))
            state_path = feature / "requirements-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["schema_version"] = 2
            state.pop("delivery_audit", None)
            state["revision_offer"] = {
                "state": "preparation-authorized",
                "offered_at": None,
                "reason": "Старое разрешение",
            }
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            migrated = self.command(SCRIPT, "status", str(project), "demo")
            self.assertEqual(migrated["state"]["schema_version"], 4)
            self.assertEqual(migrated["state"]["revision_offer"]["state"], "audit-required")
            self.assertEqual(migrated["state"]["delivery_audit"]["state"], "required")
            self.assertEqual(
                migrated["state"]["delivery_audit"]["method"],
                "three-level-cross-requirement-v1",
            )

    def test_schema_three_confirmed_audit_is_invalidated_before_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.project(Path(temp))
            state_path = feature / "requirements-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["schema_version"] = 3
            state["revision_offer"] = {
                "state": "preparation-authorized",
                "offered_at": None,
                "reason": "Старое подтверждение",
            }
            state["delivery_audit"] = {
                "state": "confirmed",
                "requirements_sha256": state["requirements_sha256"],
                "audited_at": "2026-08-01T10:00:00+00:00",
                "confirmed_at": "2026-08-01T10:01:00+00:00",
                "finding_count": 0,
                "blocking_finding_count": 0,
                "summary": "Старый аудит",
            }
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            migrated = self.command(SCRIPT, "status", str(project), "demo")
            self.assertEqual(migrated["state"]["schema_version"], 4)
            self.assertEqual(migrated["state"]["revision_offer"]["state"], "audit-required")
            self.assertEqual(migrated["state"]["delivery_audit"]["state"], "required")

    def test_legacy_handoff_publication_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.project(Path(temp))
            state_path = feature / "requirements-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["schema_version"] = 1
            state["last_published"] = {
                "package_id": "demo-delivery",
                "revision": 1,
                "requirements_sha256": state["requirements_sha256"],
                "published_at": "2026-08-15T15:33:23+00:00",
            }
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            migrated = self.command(SCRIPT, "status", str(project), "demo")
            published = migrated["state"]["last_published"]
            self.assertEqual(published["legacy_format"], "feature-handoff")
            self.assertEqual(
                published["manifest_path"],
                "features/demo/handoffs/demo-delivery/handoff.json",
            )

    def test_audit_confirmation_is_required_and_bound_to_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.project(Path(temp))
            started = self.command(SCRIPT, "begin-preparation", str(project), "demo")
            self.assertEqual(started["next_action"], "audit-requirements-before-publication")
            audited = self.command(
                SCRIPT,
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
            self.assertEqual(audited["state"]["delivery_audit"]["state"], "awaiting-confirmation")
            self.assertEqual(
                audited["state"]["delivery_audit"]["levels"],
                {"individual": "complete", "system": "complete", "delivery": "complete"},
            )
            with (feature / "requirements.md").open("a", encoding="utf-8") as handle:
                handle.write("\nИзменение после аудита.\n")
            result = run(sys.executable, str(SCRIPT), "confirm-audit", str(project), "demo")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("аудит заново", result.stdout)

    def test_blocking_audit_cannot_be_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, _ = self.project(Path(temp))
            self.command(SCRIPT, "begin-preparation", str(project), "demo")
            blocked = self.command(
                SCRIPT,
                "record-audit",
                str(project),
                "demo",
                "--finding-count",
                "2",
                "--blocking-finding-count",
                "1",
                "--summary",
                "Не определено поведение при конфликте",
            )
            self.assertEqual(blocked["next_action"], "resolve-blocking-audit-findings")
            result = run(sys.executable, str(SCRIPT), "confirm-audit", str(project), "demo")
            self.assertNotEqual(result.returncode, 0)

    def test_audit_classifies_resolved_findings_and_accepted_risks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, _ = self.project(Path(temp))
            self.command(SCRIPT, "begin-preparation", str(project), "demo")
            audited = self.command(
                SCRIPT,
                "record-audit",
                str(project),
                "demo",
                "--finding-count",
                "3",
                "--accepted-risk-count",
                "1",
                "--blocking-finding-count",
                "0",
                "--summary",
                "Два замечания исправлены, один риск принят",
            )
            audit = audited["state"]["delivery_audit"]
            self.assertEqual(audit["resolved_finding_count"], 2)
            self.assertEqual(audit["accepted_risk_count"], 1)
            self.assertEqual(audit["blocking_finding_count"], 0)
            confirmed = self.command(SCRIPT, "confirm-audit", str(project), "demo")
            self.assertEqual(confirmed["state"]["delivery_audit"]["state"], "confirmed")

    def test_recorded_change_invalidates_confirmed_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.project(Path(temp))
            self.authorize(project)
            with (feature / "requirements.md").open("a", encoding="utf-8") as handle:
                handle.write("\nУточнение аналитика.\n")
            changed = self.command(
                SCRIPT, "record-change", str(project), "demo", "--origin", "analyst"
            )
            self.assertEqual(changed["state"]["delivery_audit"]["state"], "not-requested")


if __name__ == "__main__":
    unittest.main()
