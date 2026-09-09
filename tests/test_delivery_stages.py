from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_requirements_exchange as fixture


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class DeliveryStagesTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        runtime = self.root / "runtime"
        runtime.mkdir()
        write_json(runtime / "code-repos.json", {"schema_version": 2, "repositories": []})
        environment = patch.dict(os.environ, {
            "ANALYST_HARNESS_STATE_ROOT": str(self.root / "runtime"),
            "CODA_ANALYST_STATE_ROOT": str(self.root / "runtime"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.fixture = fixture.RequirementsExchangeTests()
        self.project, self.feature = self.fixture.prepare_project(self.root)
        self.state_path = self.feature / "requirements-state.json"
        self.requirements_path = self.feature / "requirements.md"

    def ctl(self, command: str, *arguments: str) -> dict:
        result = fixture.run(sys.executable, str(fixture.STATE_SCRIPT), command, str(self.project), "demo", *arguments)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def blocked(self, script: Path, command: str, *arguments: str, message: str = "") -> None:
        before = self.state_path.read_bytes()
        result = fixture.run(sys.executable, str(script), command, str(self.project), "demo", *arguments)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn(message, result.stdout + result.stderr)
        self.assertEqual(self.state_path.read_bytes(), before)

    def prepare(self) -> dict:
        return self.fixture.prepare(self.project)

    def mark(self, prepared: dict) -> dict:
        return self.ctl("mark-published", "--manifest", prepared["manifest"], "--revision", str(prepared["revision"]), "--destination-role", prepared["destination_role"])

    def review(self, prepared: dict) -> dict:
        self.fixture.write_receipt(prepared)
        returns = Path(prepared["requirements"]).parent / "returns"
        (returns / "tasks.md").write_text("# Tasks\n", encoding="utf-8")
        summary = returns / "summary.md"
        summary.write_text("# Summary\nREQ-DEMO-001: full, matches, passed.\n", encoding="utf-8")
        revision = prepared["revision"]
        checksum = hashlib.sha256(summary.read_bytes()).hexdigest()
        entry = next(item for item in read_json(Path(prepared["manifest"]))["revisions"] if item["revision"] == revision)
        review = {
            "schema_version": 1,
            "return_id": f"demo:{revision:03d}:revisions/{revision:03d}/returns/summary.md:{checksum}",
            "requirements_sha256": entry["sha256"],
            "items": [{
                "requirement": "REQ-DEMO-001", "implementation": "full", "conformity": "matches",
                "verification": "passed", "evidence": ["test report at implementation commit"],
                "actual_behavior": "The result is displayed", "accepted_behavior": "The result is displayed",
                "acceptance": "accepted", "reason": "Analyst accepted the verified result",
                "baseline": {"action": "none"}, "follow_up": {"action": "none"},
            }],
        }
        self.record_review(review)
        return review

    def record_review(self, review: dict) -> None:
        result = self.fixture.record_review(self.project, self.root / "review.json", review)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def close(self, review: dict) -> dict:
        return self.ctl("close-stage", "--return-id", review["return_id"], "--note", "Этап завершён решением аналитика", "--analyst-confirmed")

    def close_blocked(self, review: dict, message: str = "") -> None:
        self.blocked(fixture.STATE_SCRIPT, "close-stage", "--return-id", review["return_id"], "--note", "Закрыть", "--analyst-confirmed", message=message)

    def start_next(self) -> dict:
        return self.ctl("start-stage", "--stage-id", "stage-2", "--title", "Второй результат", "--goal", "Уточнить результат пользователю", "--analyst-confirmed")

    def write_next_scope(self) -> None:
        self.requirements_path.write_text(fixture.requirements().replace("stage-1", "stage-2").replace("Первый результат", "Второй результат").replace("Показать результат пользователю", "Уточнить результат пользователю"), encoding="utf-8")

    def test_start_requires_confirmation_and_preserves_unpublished_analyst_work(self) -> None:
        state = read_json(self.state_path)
        state.pop("delivery_stages")
        state["last_change"] = {"origin": "analyst", "recorded_at": "2026-09-08", "return_id": None}
        write_json(self.state_path, state)
        self.requirements_path.write_text(fixture.requirements().replace("Этап поставки: stage-1\n", ""), encoding="utf-8")
        original = self.requirements_path.read_bytes()
        arguments = ("--stage-id", "stage-1", "--title", "Первый результат", "--goal", "Показать результат пользователю")
        self.blocked(fixture.STATE_SCRIPT, "start-stage", *arguments, message="--analyst-confirmed")
        started = self.ctl("start-stage", *arguments, "--analyst-confirmed")["state"]
        self.assertEqual(started["last_change"], state["last_change"])
        self.assertEqual(self.requirements_path.read_bytes(), original)
        self.assertEqual(started["delivery_stages"]["stages"][0]["number"], 1)
        self.blocked(fixture.STATE_SCRIPT, "begin-preparation", message="Этап поставки")

    def test_parallel_stage_and_repeated_slug_are_rejected(self) -> None:
        self.blocked(fixture.STATE_SCRIPT, "start-stage", "--stage-id", "stage-2", "--title", "Следующий", "--goal", "Результат", "--analyst-confirmed", message="явно закрыт")
        prepared = self.prepare()
        self.mark(prepared)
        self.close(self.review(prepared))
        self.blocked(fixture.STATE_SCRIPT, "start-stage", "--stage-id", "stage-1", "--title", "Следующий", "--goal", "Результат", "--analyst-confirmed", message="уже использован")

    def test_scope_requires_one_exact_marker_title_and_goal(self) -> None:
        original = fixture.requirements()
        variants = [
            original.replace("Этап поставки: stage-1", "Этап поставки: stage-2"),
            original.replace("Этап поставки: stage-1", "**Этап поставки:** stage-1"),
            original + "\nЭтап поставки: stage-1\n",
            original.replace("Этап поставки: stage-1\n", "") + "\nЭтап поставки: stage-1\n",
            original.replace("Первый результат", "Другое название"),
            original.replace("Показать результат пользователю", "Другая цель"),
            original.replace("Этап поставки: stage-1", "```text\nЭтап поставки: stage-1\n```"),
            original.replace("Этап поставки: stage-1", "<!--\nЭтап поставки: stage-1\n-->"),
        ]
        for content in variants:
            with self.subTest(content=content):
                self.requirements_path.write_text(content, encoding="utf-8")
                self.blocked(fixture.STATE_SCRIPT, "begin-preparation")

    def test_revisions_are_global_and_supersede_only_their_stage(self) -> None:
        first = self.prepare()
        self.requirements_path.write_text(fixture.requirements("Система должна показать уточнённый результат."), encoding="utf-8")
        second = self.prepare()
        self.mark(second)
        review = self.review(second)
        closed = self.close(review)["state"]
        old_manifest = read_json(Path(second["manifest"]))
        originals = {path: path.read_bytes() for path in (self.project / "requirements-exchange/demo/revisions").rglob("*") if path.is_file()}
        self.assertEqual(old_manifest["revisions"][0]["state"], "superseded")
        self.assertEqual([entry["stage_revision"] for entry in old_manifest["revisions"]], [1, 2])
        self.assertFalse((self.project / "baseline").exists())
        self.start_next()
        self.write_next_scope()
        third = self.prepare()
        manifest = read_json(Path(third["manifest"]))
        self.assertEqual([entry["revision"] for entry in manifest["revisions"]], [1, 2, 3])
        self.assertEqual(manifest["revisions"][:2], old_manifest["revisions"])
        self.assertEqual((third["stage_id"], third["stage_revision"]), ("stage-2", 1))
        self.assertEqual(read_json(self.state_path)["delivery_stages"]["stages"][0], closed["delivery_stages"]["stages"][0])
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(first["revision"], 1)
        self.assertIn("REQ-DEMO-001", self.requirements_path.read_text(encoding="utf-8"))

    def test_close_requires_confirmation_publication_and_detailed_review(self) -> None:
        self.close_blocked({"return_id": "demo:001:revisions/001/returns/summary.md:unknown"}, "непереданный")
        prepared = self.prepare()
        review = self.review(prepared)
        self.close_blocked(review, "непереданный")
        self.mark(prepared)
        self.blocked(fixture.STATE_SCRIPT, "close-stage", "--return-id", review["return_id"], "--note", "Закрыть", message="--analyst-confirmed")
        results = self.feature / "development-results-state.json"
        state = read_json(results)
        state["processed"][0]["decision"] = "no-change"
        write_json(results, state)
        self.close_blocked(review, "reviewed")

    def test_close_revalidates_coverage_checksum_and_summary(self) -> None:
        prepared = self.prepare()
        self.mark(prepared)
        review = self.review(prepared)
        results_path = self.feature / "development-results-state.json"
        original = read_json(results_path)
        for field, value in (("items", []), ("requirements_sha256", "0" * 64)):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed["processed"][-1]["review"][field] = value
                write_json(results_path, changed)
                self.close_blocked(review)
        write_json(results_path, original)
        summary = Path(prepared["requirements"]).parent / "returns/summary.md"
        summary.write_text(summary.read_text(encoding="utf-8") + "Changed evidence\n", encoding="utf-8")
        self.close_blocked(review, "Summary в подтверждённом месте")

    def test_current_investigation_blocks_closure_until_review_resolves_it(self) -> None:
        prepared = self.prepare()
        self.mark(prepared)
        final_review = self.review(prepared)
        investigation = copy.deepcopy(final_review)
        residual = self.project / "investigation.md"
        residual.write_text("# Проверить результат\n", encoding="utf-8")
        investigation["items"][0].update(
            implementation="unknown", conformity="unknown", verification="unknown", acceptance="no-delivery",
            evidence=[], accepted_behavior="", follow_up={"action": "investigate", "path": "investigation.md", "reason": "Нужны доказательства"},
        )
        self.record_review(investigation)
        self.close_blocked(final_review, "Неизвестный результат")
        self.record_review(final_review)
        closed = self.close(final_review)["state"]["delivery_stages"]["stages"][0]
        self.assertEqual(closed["closure"]["return_id"], final_review["return_id"])
        self.assertEqual(len(read_json(self.feature / "development-results-state.json")["processed"]), 3)

    def test_no_delivery_can_close_only_with_explicit_residual_disposition(self) -> None:
        prepared = self.prepare()
        self.mark(prepared)
        review = self.review(prepared)
        (self.project / "archive.md").write_text("# Объём явно отменён аналитиком\n", encoding="utf-8")
        review["items"][0].update(
            implementation="none", conformity="not-applicable", verification="not-applicable", acceptance="no-delivery",
            evidence=[], accepted_behavior="", follow_up={"action": "archive", "path": "archive.md", "reason": "Явное решение аналитика"},
        )
        self.record_review(review)
        self.assertEqual(self.close(review)["next_action"], "start-next-delivery-stage")

    def test_close_rejects_analyst_scope_and_unrecorded_changes(self) -> None:
        prepared = self.prepare()
        self.mark(prepared)
        review = self.review(prepared)
        self.requirements_path.write_text(fixture.requirements() + "\nНовый объём.\n", encoding="utf-8")
        self.close_blocked(review, "аналитического объёма")
        self.ctl("record-change", "--origin", "analyst")
        self.close_blocked(review, "изменение объёма")
        self.ctl("begin-preparation")
        self.close_blocked(review, "подготовка новой редакции")
        self.fixture.authorize(self.project)
        self.close_blocked(review, "подготовка новой редакции")

    def test_recorded_developer_correction_can_close_against_sent_input(self) -> None:
        prepared = self.prepare()
        self.mark(prepared)
        review = self.review(prepared)
        self.requirements_path.write_text(fixture.requirements() + "\nПринятое уточнение результата.\n", encoding="utf-8")
        self.ctl("record-change", "--origin", "developer-result", "--return-id", review["return_id"])
        self.assertEqual(self.close(review)["next_action"], "start-next-delivery-stage")
        self.assertEqual(len(read_json(Path(prepared["manifest"]))["revisions"]), 1)

    def test_previous_summary_cannot_close_new_revision(self) -> None:
        first = self.prepare()
        self.mark(first)
        review = self.review(first)
        self.requirements_path.write_text(fixture.requirements("Система должна показать второй результат."), encoding="utf-8")
        second = self.prepare()
        self.mark(second)
        self.close_blocked(review, "последней переданной редакции")

    def test_closed_stage_cannot_be_published_or_reopened(self) -> None:
        prepared = self.prepare()
        self.mark(prepared)
        review = self.review(prepared)
        self.close(review)
        self.blocked(fixture.STATE_SCRIPT, "begin-preparation", message="Этап закрыт")
        self.blocked(fixture.SCRIPT, "prepare", "--analyst", "ivan", message="Этап закрыт")
        self.close_blocked(review, "Этап закрыт")

    def test_metadata_change_after_audit_blocks_confirmation(self) -> None:
        self.ctl("begin-preparation")
        self.ctl("record-audit", "--finding-count", "0", "--blocking-finding-count", "0", "--summary", "Аудит завершён")
        state = read_json(self.state_path)
        state["delivery_stages"]["stages"][0]["title"] = "результат"
        write_json(self.state_path, state)
        self.blocked(fixture.STATE_SCRIPT, "confirm-audit", message="Метаданные этапа изменились после аудита")

    def test_metadata_change_after_confirmation_blocks_prepare_and_mark(self) -> None:
        self.fixture.authorize(self.project)
        original = read_json(self.state_path)
        changed = copy.deepcopy(original)
        changed["delivery_stages"]["stages"][0]["goal"] = "результат пользователю"
        write_json(self.state_path, changed)
        self.blocked(fixture.SCRIPT, "prepare", "--analyst", "ivan", message="Метаданные этапа изменились после аудита")
        self.assertFalse((self.project / "requirements-exchange").exists())
        write_json(self.state_path, original)
        prepared = self.prepare()
        changed = read_json(self.state_path)
        changed["delivery_audit"]["stage"]["goal"] = "Другая цель"
        write_json(self.state_path, changed)
        self.blocked(fixture.STATE_SCRIPT, "mark-published", "--manifest", prepared["manifest"], "--revision", "1", "--destination-role", "analytics", message="Метаданные этапа")

    def test_manifest_stage_rebinding_is_rejected_without_rewriting_history(self) -> None:
        prepared = self.prepare()
        path = Path(prepared["manifest"])
        manifest = read_json(path)
        manifest["revisions"][0]["stage_revision"] = 2
        write_json(path, manifest)
        original = path.read_bytes()
        self.blocked(fixture.STATE_SCRIPT, "mark-published", "--manifest", str(path), "--revision", "1", "--destination-role", "analytics", message="Нельзя перепривязать")
        self.blocked(fixture.SCRIPT, "prepare", "--analyst", "ivan", message="Нельзя перепривязать")
        self.assertEqual(path.read_bytes(), original)

    def test_legacy_schema_four_audit_without_stage_is_readable_but_not_authorized(self) -> None:
        self.fixture.authorize(self.project)
        state = read_json(self.state_path)
        state.pop("delivery_stages")
        state["schema_version"] = 4
        state["delivery_audit"].pop("stage")
        state["delivery_audit"].pop("stage_sha256")
        write_json(self.state_path, state)
        self.assertEqual(self.ctl("status")["state"]["schema_version"], 5)
        self.assertEqual(self.ctl("stage-status")["next_action"], "register-delivery-stage")
        self.blocked(fixture.SCRIPT, "prepare", "--analyst", "ivan", message="register-delivery-stage")

    def test_legacy_manifests_are_readable_without_implicit_stage_migration(self) -> None:
        prepared = self.prepare()
        manifest_path = Path(prepared["manifest"])
        manifest = read_json(manifest_path)
        for entry in manifest["revisions"]:
            for key in ("stage_id", "stage_revision", "stage", "stage_sha256"):
                entry.pop(key)
        write_json(manifest_path, manifest)
        original = manifest_path.read_bytes()
        self.fixture.command("validate", str(manifest_path.parent.parent))
        self.fixture.command("scan", str(self.project), "--analyst", "ivan")
        self.blocked(fixture.SCRIPT, "prepare", "--analyst", "ivan", message="legacy-stage-migration-required")
        state = read_json(self.state_path)
        state.pop("delivery_stages")
        write_json(self.state_path, state)
        self.assertEqual(self.ctl("stage-status")["next_action"], "legacy-stage-migration-required")
        self.blocked(fixture.STATE_SCRIPT, "start-stage", "--stage-id", "stage-1", "--title", "Первый результат", "--goal", "Показать результат пользователю", "--analyst-confirmed", message="legacy-stage-migration-required")
        self.assertEqual(manifest_path.read_bytes(), original)

    def test_fallback_retry_keeps_revision_and_its_stage_identity(self) -> None:
        first = self.prepare()
        first_manifest = read_json(Path(first["manifest"]))
        second = self.fixture.command("prepare", str(self.project), "demo", "--analyst", "ivan")
        self.assertEqual(second["revision"], first["revision"])
        self.assertEqual(read_json(Path(second["manifest"])), first_manifest)
        self.assertEqual(len(read_json(self.state_path)["delivery_stages"]["revisions"]), 1)

    def test_fallback_to_code_retry_preserves_revision_and_blocks_close_until_merge(self) -> None:
        first = self.prepare()
        self.mark(first)
        review = self.review(first)
        _, code = self.fixture.prepare_code_repository(self.root)
        self.fixture.authorize(self.project)
        proposed = self.fixture.command("prepare", str(self.project), "demo", "--analyst", "ivan", "--code-root", str(code))
        self.assertFalse(proposed["publication_confirmed"])
        self.assertEqual((proposed["revision"], proposed["stage_revision"]), (1, 1))
        self.assertEqual(read_json(Path(first["manifest"]))["revisions"], read_json(Path(proposed["manifest"]))["revisions"])
        self.close_blocked(review)
        self.blocked(fixture.STATE_SCRIPT, "mark-published", "--manifest", proposed["manifest"], "--revision", "1", "--destination-role", "analytics", message="PR/MR")
        self.blocked(fixture.SCRIPT, "prepare", "--analyst", "ivan", message="Незавершённая передача")

    def test_mark_publication_repeat_is_noop_for_sent_and_in_progress(self) -> None:
        prepared = self.prepare()
        self.mark(prepared)
        original = self.state_path.read_bytes()
        manifest_path = Path(prepared["manifest"])
        for runtime_state in ("sent", "in-progress"):
            with self.subTest(state=runtime_state):
                manifest = read_json(manifest_path)
                manifest["revisions"][0]["state"] = runtime_state
                write_json(manifest_path, manifest)
                self.mark(prepared)
                self.assertEqual(self.state_path.read_bytes(), original)

    def test_close_is_bound_to_code_and_rejects_stale_analytics_copy(self) -> None:
        prepared = self.prepare()
        review = self.review(prepared)
        self.mark(prepared)
        _, code = self.fixture.prepare_code_repository(self.root)
        self.fixture.authorize(self.project)
        arguments = ("prepare", str(self.project), "demo", "--analyst", "ivan", "--code-root", str(code))
        proposed = self.fixture.command(*arguments)
        seed = self.root / "code-seed"
        self.fixture.git(seed, "fetch", "origin", proposed["request_branch"])
        self.fixture.git(seed, "merge", "--no-ff", "FETCH_HEAD", "-m", "Accept delivery")
        self.fixture.git(seed, "push", "origin", "main")
        self.fixture.git(code, "pull", "--ff-only")
        merged = self.fixture.command(*arguments)
        self.mark(merged)
        self.mark(merged)
        registry = self.root / "runtime/code-repos.json"
        registry.parent.mkdir(exist_ok=True)
        write_json(registry, {"repositories": [{"id": "code", "location": {"relative_to_analytical": "../coda"}}]})
        local_returns = Path(prepared["requirements"]).parent / "returns"
        code_returns = code / "requirements-exchange/demo/revisions/001/returns"
        shutil.copytree(local_returns, code_returns)
        code_summary = code_returns / "summary.md"
        original = code_summary.read_bytes()
        code_summary.write_bytes(original + b"Changed current code result\n")
        self.close_blocked(review, "Summary в подтверждённом месте")
        code_summary.write_bytes(original)
        (local_returns / "summary.md").write_bytes(original + b"Divergent analytics copy\n")
        self.close_blocked(review, "Summary в подтверждённом месте")
        (local_returns / "summary.md").write_bytes(original)
        self.close(review)

    def test_next_stage_rechecks_closure_review_and_summary(self) -> None:
        prepared = self.prepare()
        self.mark(prepared)
        review = self.review(prepared)
        self.close(review)
        arguments = ("--stage-id", "stage-2", "--title", "Второй", "--goal", "Продолжить", "--analyst-confirmed")
        results_path = self.feature / "development-results-state.json"
        original = read_json(results_path)
        changed = copy.deepcopy(original)
        later = copy.deepcopy(changed["processed"][-1])
        later["review"]["items"][0]["follow_up"] = {"action": "investigate"}
        changed["processed"].append(later)
        write_json(results_path, changed)
        self.blocked(fixture.STATE_SCRIPT, "start-stage", *arguments, message="изменилось подробное решение")
        write_json(results_path, original)
        summary = Path(prepared["requirements"]).parent / "returns/summary.md"
        summary.write_bytes(summary.read_bytes() + b"Late report\n")
        self.blocked(fixture.STATE_SCRIPT, "start-stage", *arguments, message="Summary в подтверждённом месте")

    def test_adopt_legacy_history_without_rewriting_inputs_or_resetting_revisions(self) -> None:
        prepared = self.prepare()
        self.mark(prepared)
        review = self.review(prepared)
        manifest_path = Path(prepared["manifest"])
        manifest = read_json(manifest_path)
        for key in ("stage_id", "stage_revision", "stage", "stage_sha256"):
            manifest["revisions"][0].pop(key)
        legacy_text = fixture.requirements().replace("Этап поставки: stage-1\n", "")
        legacy_hash = hashlib.sha256(legacy_text.encode()).hexdigest()
        self.requirements_path.write_text(legacy_text, encoding="utf-8")
        Path(prepared["requirements"]).write_text(legacy_text, encoding="utf-8")
        manifest["revisions"][0]["sha256"] = legacy_hash
        write_json(manifest_path, manifest)
        receipt_path = Path(prepared["requirements"]).parent / "returns/receipt.json"
        receipt = read_json(receipt_path)
        receipt["requirements_sha256"] = legacy_hash
        write_json(receipt_path, receipt)
        review["requirements_sha256"] = legacy_hash
        results_path = self.feature / "development-results-state.json"
        results = read_json(results_path)
        results["processed"][-1]["review"] = review
        write_json(results_path, results)
        state = read_json(self.state_path)
        state.pop("delivery_stages")
        state["requirements_sha256"] = legacy_hash
        state["last_published"]["requirements_sha256"] = legacy_hash
        for key in ("stage_id", "stage_revision", "stage_sha256"):
            state["last_published"].pop(key)
        write_json(self.state_path, state)
        originals = {path: path.read_bytes() for path in (self.project / "requirements-exchange").rglob("*") if path.is_file()}
        arguments = ("--stage-id", "stage-1", "--title", "Первый результат", "--goal", "Показать результат пользователю", "--manifest", str(manifest_path), "--revisions", "1")
        self.blocked(fixture.STATE_SCRIPT, "adopt-legacy-stage", *arguments, message="--analyst-confirmed")
        self.blocked(fixture.STATE_SCRIPT, "adopt-legacy-stage", *arguments[:-1], "2", "--analyst-confirmed", message="Явно перечисли")
        adopted = self.ctl("adopt-legacy-stage", *arguments, "--analyst-confirmed")["state"]
        self.assertEqual(adopted["last_published"]["revision"], 1)
        self.assertEqual(adopted["delivery_stages"]["revisions"][0]["entry"], manifest["revisions"][0])
        self.close(review)
        self.start_next()
        self.write_next_scope()
        next_delivery = self.prepare()
        self.assertEqual((next_delivery["revision"], next_delivery["stage_revision"]), (2, 1))
        for path, content in originals.items():
            if path != manifest_path:
                self.assertEqual(path.read_bytes(), content)
        self.assertEqual(read_json(manifest_path)["revisions"][0], manifest["revisions"][0])

    def test_schema_four_migration_preserves_registered_stage_and_invalidates_unbound_audit(self) -> None:
        self.fixture.authorize(self.project)
        bound = read_json(self.state_path)
        bound["schema_version"] = 4
        write_json(self.state_path, bound)
        migrated = self.ctl("status")["state"]
        self.assertEqual(migrated, {**bound, "schema_version": 5})
        unbound = copy.deepcopy(bound)
        unbound["delivery_audit"].pop("stage")
        unbound["delivery_audit"].pop("stage_sha256")
        write_json(self.state_path, unbound)
        migrated = self.ctl("status")["state"]
        self.assertEqual(migrated["delivery_stages"], bound["delivery_stages"])
        self.assertEqual(migrated["last_change"], bound["last_change"])
        self.assertEqual(migrated["delivery_audit"]["state"], "required")
        self.assertEqual(migrated["revision_offer"]["state"], "audit-required")


if __name__ == "__main__":
    unittest.main()
