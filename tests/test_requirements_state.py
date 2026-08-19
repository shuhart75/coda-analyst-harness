from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False)


class RequirementsStateTests(unittest.TestCase):
    def command(self, script: str, *args: str) -> dict:
        result = run(sys.executable, str(ROOT / "scripts" / script), *args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout) if result.stdout.lstrip().startswith("{") else {}

    def scaffold(self, root: Path) -> tuple[Path, Path]:
        project = root / "project"
        created = run("bash", str(ROOT / "scripts/scaffold-project.sh"), str(project))
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        feature_created = run("bash", str(ROOT / "scripts/scaffold-feature.sh"), str(project), "demo")
        self.assertEqual(feature_created.returncode, 0, feature_created.stdout + feature_created.stderr)
        feature = project / "features/demo"
        self.assertTrue((feature / "requirements-state.json").is_file())
        self.assertEqual(list((feature / "slices").glob("*/slice.md")), [])
        (feature / "requirements.md").write_text(
            "# Демонстрация\n\nREQ-DEMO-001\n\nSCN-DEMO-001\n",
            encoding="utf-8",
        )
        return project, feature

    def publish_first_revision(self, project: Path, feature: Path) -> Path:
        recorded = self.command("requirementsctl.py", "record-change", str(project), "demo", "--origin", "analyst")
        self.assertEqual(recorded["next_action"], "continue-root-requirements")
        self.assertEqual(list((feature / "slices").glob("*/slice.md")), [])
        handoff = feature / "handoffs/demo-delivery"
        self.command("handoffctl.py", "init-feature", str(project), "demo", "demo-delivery")
        rejected = run(sys.executable, str(ROOT / "scripts/handoffctl.py"), "add-revision", str(handoff), "1")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("не разрешена", rejected.stdout)
        prepared = self.command("requirementsctl.py", "begin-preparation", str(project), "demo")
        self.assertEqual(prepared["next_action"], "derive-slices-and-publish")
        slice_root = feature / "slices/main"
        slice_root.mkdir(parents=True)
        (slice_root / "slice.md").write_text(
            "# Основной срез\n\nREQ-DEMO-001\n\nSCN-DEMO-001\n",
            encoding="utf-8",
        )
        self.command("handoffctl.py", "add-revision", str(handoff), "1")
        published = self.command("handoffctl.py", "publish", str(handoff), "1")
        self.assertEqual(published["state"], "sent")
        marked = self.command(
            "requirementsctl.py",
            "mark-published",
            str(project),
            "demo",
            "--package-id",
            "demo-delivery",
            "--revision",
            "1",
        )
        self.assertEqual(marked["state"]["slice_derivation"]["state"], "current")
        return handoff

    def register_implementation_receipt(self, handoff: Path, name: str = "result.json") -> Path:
        receipt = handoff / "revisions/001/returns/implementation-results" / name
        receipt.write_text("{}\n", encoding="utf-8")
        manifest_path = handoff / "handoff.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["revisions"][0]["implementation_results"].append({
            "path": receipt.relative_to(handoff).as_posix(),
            "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
        })
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return receipt

    def test_analyst_change_offers_once_and_decline_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.scaffold(Path(temp))
            handoff = self.publish_first_revision(project, feature)
            slice_path = feature / "slices/main/slice.md"
            slice_before = slice_path.read_bytes()

            with (feature / "requirements.md").open("a", encoding="utf-8") as handle:
                handle.write("\nАналитическое изменение.\n")
            changed = self.command("requirementsctl.py", "record-change", str(project), "demo", "--origin", "analyst")
            self.assertEqual(changed["next_action"], "offer-new-revision-once")
            self.assertEqual(changed["state"]["slice_derivation"]["state"], "stale")
            offered = self.command("requirementsctl.py", "mark-offered", str(project), "demo")
            self.assertEqual(offered["state"]["revision_offer"]["state"], "awaiting-decision")

            with (feature / "requirements.md").open("a", encoding="utf-8") as handle:
                handle.write("\nСледующее изменение до решения.\n")
            repeated = self.command("requirementsctl.py", "record-change", str(project), "demo", "--origin", "analyst")
            self.assertEqual(repeated["next_action"], "await-analyst-decision-without-repeating-offer")
            declined = self.command("requirementsctl.py", "decline-revision", str(project), "demo")
            self.assertEqual(declined["next_action"], "wait-explicit-preparation-command")

            with (feature / "requirements.md").open("a", encoding="utf-8") as handle:
                handle.write("\nИзменение после отказа.\n")
            suppressed = self.command("requirementsctl.py", "record-change", str(project), "demo", "--origin", "analyst")
            self.assertEqual(suppressed["next_action"], "wait-explicit-preparation-command")
            self.assertEqual(slice_path.read_bytes(), slice_before)
            self.assertEqual(sorted(path.name for path in (handoff / "revisions").iterdir()), ["001"])

            explicit = self.command("requirementsctl.py", "begin-preparation", str(project), "demo")
            self.assertEqual(explicit["next_action"], "derive-slices-and-publish")

    def test_receipt_change_preserves_pending_analyst_offer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.scaffold(Path(temp))
            handoff = self.publish_first_revision(project, feature)
            with (feature / "requirements.md").open("a", encoding="utf-8") as handle:
                handle.write("\nАналитическое изменение.\n")
            analyst_change = self.command(
                "requirementsctl.py", "record-change", str(project), "demo", "--origin", "analyst"
            )
            self.assertEqual(analyst_change["state"]["revision_offer"]["state"], "pending-offer")

            receipt = self.register_implementation_receipt(handoff, "later-result.json")
            with (feature / "requirements.md").open("a", encoding="utf-8") as handle:
                handle.write("\nУточнение по квитанции.\n")
            receipt_change = self.command(
                "requirementsctl.py",
                "record-change",
                str(project),
                "demo",
                "--origin",
                "developer-receipt",
                "--receipt",
                str(receipt),
            )
            self.assertEqual(receipt_change["next_action"], "offer-new-revision-once")
            self.assertEqual(receipt_change["state"]["revision_offer"]["state"], "pending-offer")

    def test_invalid_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.scaffold(Path(temp))
            state_path = feature / "requirements-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["revision_offer"]["reason"] = None
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            result = run(
                sys.executable,
                str(ROOT / "scripts/requirementsctl.py"),
                "status",
                str(project),
                "demo",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("отсутствует причина", result.stdout)

    def test_existing_unrecorded_divergence_requires_origin_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.scaffold(Path(temp))
            self.publish_first_revision(project, feature)
            (feature / "requirements-state.json").unlink()
            with (feature / "requirements.md").open("a", encoding="utf-8") as handle:
                handle.write("\nИзменение из прежнего рабочего процесса.\n")

            status = run(
                sys.executable,
                str(ROOT / "scripts/requirementsctl.py"),
                "status",
                str(project),
                "demo",
            )
            self.assertEqual(status.returncode, 1)
            self.assertEqual(json.loads(status.stdout)["next_action"], "record-requirements-change")

            classified = self.command(
                "requirementsctl.py", "record-change", str(project), "demo", "--origin", "analyst"
            )
            self.assertEqual(classified["next_action"], "offer-new-revision-once")

    def test_receipt_change_never_offers_or_builds_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project, feature = self.scaffold(Path(temp))
            handoff = self.publish_first_revision(project, feature)
            slice_path = feature / "slices/main/slice.md"
            slice_before = slice_path.read_bytes()
            receipt = handoff / "revisions/001/returns/implementation-results/result.json"
            receipt.write_text("{}\n", encoding="utf-8")
            unregistered = run(
                sys.executable,
                str(ROOT / "scripts/requirementsctl.py"),
                "record-change",
                str(project),
                "demo",
                "--origin",
                "developer-receipt",
                "--receipt",
                str(receipt),
            )
            self.assertNotEqual(unregistered.returncode, 0)
            self.assertIn("не зарегистрирована", unregistered.stdout)
            manifest_path = handoff / "handoff.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["revisions"][0]["implementation_results"].append({
                "path": receipt.relative_to(handoff).as_posix(),
                "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            })
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            with (feature / "requirements.md").open("a", encoding="utf-8") as handle:
                handle.write("\nФактический результат из квитанции.\n")

            recorded = self.command(
                "requirementsctl.py",
                "record-change",
                str(project),
                "demo",
                "--origin",
                "developer-receipt",
                "--receipt",
                str(receipt),
            )
            self.assertEqual(recorded["next_action"], "continue-root-requirements")
            self.assertEqual(recorded["state"]["last_change"]["origin"], "developer-receipt")
            self.assertEqual(recorded["state"]["revision_offer"]["state"], "not-needed")
            self.assertEqual(recorded["state"]["slice_derivation"]["state"], "stale")
            self.assertEqual(slice_path.read_bytes(), slice_before)
            self.assertEqual(sorted(path.name for path in (handoff / "revisions").iterdir()), ["001"])


if __name__ == "__main__":
    unittest.main()
