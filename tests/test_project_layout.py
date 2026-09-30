"""Migration contracts: history survives, ownership resolves, unsafe plans fail early."""
from __future__ import annotations

import copy
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import layoutctl
import project_layout as layout


class ProjectLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.git("init", "-b", "feature/layout")
        self.git("config", "user.name", "Layout tests")
        self.git("config", "user.email", "layout@example.invalid")
        self.files = {
            "features/kib-mvp/requirements.md": "# Требования MVP\n".encode(),
            "features/kib-mvp/feature.md": "# КИБ\n".encode(),
            "features/kib-next/requirements.md": "# Расширение КИБ\n".encode(),
            "features/kib-next/evidence.bin": bytes(range(256)),
            "features/registry/requirements.md": "# Реестр\n".encode(),
            "planning/2026-Q3/quarter/plan.md": b"approved Q3\n",
            "planning/2026-Q4/gantt/execution.md": b"actual\n",
            "baseline/current/domain/model.md": b"deployed domain\n",
            "requirements-exchange/kib/revisions/001/requirements.md": b"immutable delivered input\n",
        }
        for relative, content in self.files.items():
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.commit_all()
        self.assignments = {
            "kib-mvp": {"feature_id": "kib", "delivery_id": "mvp", "quarter": "2026-Q3"},
            "kib-next": {"feature_id": "kib", "delivery_id": "extension", "quarter": "2026-Q4"},
            "registry": {"feature_id": "registry", "delivery_id": "registry", "quarter": "2026-Q4"},
        }

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.project), *args], stderr=subprocess.DEVNULL, text=True).strip()

    def commit_all(self):
        self.git("add", "--all")
        self.git("commit", "-m", "Fixture baseline")

    def plan(self):
        return layoutctl.plan(self.project, self.assignments)

    def test_legacy_resolution_unchanged(self):
        self.assertIsNone(layout.layout(self.project))
        self.assertEqual(layout.feature_root(self.project, "kib-mvp"), self.project / "features/kib-mvp")
        self.assertEqual(layout.quarter_root(self.project, "2026-Q4"), self.project / "planning/2026-Q4")
        self.assertEqual(len(layout.feature_roots(self.project)), 3)
        self.assertEqual(len(layout.artifact_glob(self.project, "features/*/requirements.md")), 3)

    def test_migration_preserves_every_source_byte(self):
        proposal = self.plan()
        moved = {move["source"]: move["target"] for move in proposal["moves"]}
        layoutctl.apply(self.project, proposal, True)
        for relative, content in self.files.items():
            with self.subTest(path=relative):
                destination = self.project / moved.get(relative, relative)
                self.assertEqual(destination.read_bytes(), content)
        self.assertEqual(layoutctl.validate(self.project)["deliveries"], 3)

    def test_quarter_readme_is_not_destroyed_by_navigation(self):
        relative = "planning/2026-Q4/README.md"
        original = b"Quarter decisions and analyst approvals\n"
        (self.project / relative).write_bytes(original)
        self.commit_all()
        proposal = self.plan()
        layoutctl.apply(self.project, proposal, True)
        target = next(move["target"] for move in proposal["moves"] if move["source"] == relative)
        self.assertEqual((self.project / target).read_bytes(), original)

    def test_main_refused_without_moves(self):
        proposal = self.plan()
        self.git("branch", "-m", "main")
        with self.assertRaises(ValueError):
            layoutctl.apply(self.project, proposal, True)
        self.assertFalse((self.project / "quarters").exists())
        self.assertTrue((self.project / "features/kib-mvp/requirements.md").exists())

    def test_changed_source_blocks_all_moves(self):
        proposal = self.plan()
        source = self.project / proposal["moves"][-1]["source"]
        source.write_bytes(b"edited since review")
        with self.assertRaises(ValueError):
            layoutctl.apply(self.project, proposal, True)
        for move in proposal["moves"]:
            self.assertTrue((self.project / move["source"]).exists())
            self.assertFalse((self.project / move["target"]).exists())
        self.assertFalse((self.project / "migration-layout.json").exists())

    def test_same_plan_retry_has_no_additional_writes(self):
        proposal = self.plan()
        first = layoutctl.apply(self.project, proposal, True)
        before = {path.relative_to(self.project): path.read_bytes() for path in self.project.rglob("*")
                  if path.is_file() and ".git" not in path.relative_to(self.project).parts}
        self.assertEqual(layoutctl.apply(self.project, proposal, True), first)
        after = {path.relative_to(self.project): path.read_bytes() for path in self.project.rglob("*")
                 if path.is_file() and ".git" not in path.relative_to(self.project).parts}
        self.assertEqual(before, after)

    def test_unsafe_assignment_and_duplicate_destination_rejected(self):
        for bad in ("../escape", "/absolute", "kib/escape"):
            with self.subTest(bad=bad):
                assignments = copy.deepcopy(self.assignments)
                assignments["kib-mvp"]["feature_id"] = bad
                with self.assertRaises(ValueError):
                    layoutctl.plan(self.project, assignments)
        assignments = copy.deepcopy(self.assignments)
        assignments["kib-next"] = assignments["kib-mvp"].copy()
        with self.assertRaises(ValueError):
            layoutctl.plan(self.project, assignments)

    def test_tampered_move_cannot_relocate_baseline(self):
        proposal = self.plan()
        source = "baseline/current/domain/model.md"
        proposal["moves"].append({"source": source, "target": "unrelated/model.md",
                                  "sha256": hashlib.sha256((self.project / source).read_bytes()).hexdigest()})
        with self.assertRaises(ValueError):
            layoutctl.apply(self.project, proposal, True)
        self.assertTrue((self.project / source).exists())
        self.assertFalse((self.project / "quarters").exists())

    def test_two_quarters_resolve_one_feature_to_distinct_deliveries(self):
        layoutctl.apply(self.project, self.plan(), True)
        q3 = layout.feature_root(self.project, "kib", "2026-Q3")
        q4 = layout.feature_root(self.project, "kib", "2026-Q4")
        self.assertNotEqual(q3, q4)
        self.assertEqual(q3, layout.feature_root(self.project, "kib-mvp"))
        self.assertEqual(q4, layout.feature_root(self.project, "kib-next"))
        with self.assertRaises(ValueError):
            layout.feature_root(self.project, "kib")
        self.assertEqual(layout.delivery_key(self.project, q4 / "requirements.md"), "kib-next")

    def test_same_quarter_ambiguity_requires_delivery_identity(self):
        self.assignments["kib-mvp"]["quarter"] = "2026-Q4"
        layoutctl.apply(self.project, self.plan(), True)
        with self.assertRaises(ValueError):
            layout.feature_root(self.project, "kib", "2026-Q4")
        self.assertNotEqual(layout.feature_root(self.project, "kib-mvp"), layout.feature_root(self.project, "kib-next"))

    def test_old_protected_paths_resolve_to_migrated_files(self):
        layoutctl.apply(self.project, self.plan(), True)
        old = "features/kib-mvp/requirements.md"
        physical = layout.project_path(self.project, old)
        self.assertEqual(physical.read_bytes(), self.files[old])
        self.assertEqual(layout.logical_path(self.project, physical.relative_to(self.project).as_posix()), old)
        self.assertEqual(layout.project_path(self.project, "planning/2026-Q4/gantt/execution.md").read_bytes(), b"actual\n")
        self.assertEqual(len(layout.artifact_glob(self.project, "features/*/requirements.md")), 3)

    def test_historical_path_suffix_cannot_escape_delivery(self):
        layoutctl.apply(self.project, self.plan(), True)
        with self.assertRaises(ValueError):
            layout.project_path(self.project, "features/kib-mvp/../../../../escape")

    def test_navigation_preserves_authored_passport_and_delivery_contracts(self):
        proposal = self.plan()
        layoutctl.apply(self.project, proposal, True)
        passport = self.project / 'features/kib'
        documents = {'README.md': '# КИБ\n\nИстория и подтверждённое состояние.\n',
                     'requirements.md': '# Сводные требования\n\nДействующее и будущее поведение.\n',
                     'backlog.md': '# Отложенный объём\n'}
        for name, content in documents.items():
            (passport / name).write_text(content)
        contracts = {p: p.read_bytes() for p in layout.artifact_glob(self.project, 'features/*/requirements.md')}
        layoutctl.write_navigation(self.project, proposal['deliveries'])
        layoutctl.write_navigation(self.project, proposal['deliveries'])
        for name, content in documents.items():
            self.assertEqual((passport / name).read_text(), content)
        self.assertIn('2026-Q4', (passport / 'deliveries.md').read_text())
        self.assertEqual(contracts, {p: p.read_bytes() for p in layout.artifact_glob(self.project, 'features/*/requirements.md')})
        self.assertNotIn(passport / 'requirements.md', contracts)

    def test_execution_scope_allows_only_owning_passport_status(self):
        from execution_collaboration import passport_path_kind
        layoutctl.apply(self.project, self.plan(), True)
        scope = {'features': ['kib-next'], 'quarters': ['2026-Q4']}
        self.assertEqual(passport_path_kind(self.project, 'features/kib/README.md', scope), 'feature-passport-status')
        for path in ['features/other/README.md', 'features/kib/requirements.md', 'features/kib/backlog.md']:
            self.assertIsNone(passport_path_kind(self.project, path, scope))
        self.assertIsNone(passport_path_kind(self.project, 'features/kib/README.md',
                                            {'features': ['kib-next'], 'quarters': ['2026-Q3']}))

    def test_passport_language_checks_prose_but_not_link_destinations(self):
        from importlib import import_module
        validator = import_module('validate-language')
        layoutctl.apply(self.project, self.plan(), True)
        path = self.project / 'features/kib/requirements.md'
        path.write_text('[Проверка](../../baseline-review.md)\n[review](../../baseline-review.md)\n')
        self.assertIn(path, validator.requirement_files(self.project, None, True))
        self.assertEqual(validator.prose_lines(path), [(1, 'Проверка'), (2, 'review')])


if __name__ == "__main__":
    unittest.main()
