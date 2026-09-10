from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/expand-plantuml-includes.py"
SPEC = importlib.util.spec_from_file_location("path_test_expander", SCRIPT)
EXPANDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPANDER)


class ActualProgressPathTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.real = self.parent / "real"
        self.real.mkdir()
        self.alias = self.parent / "alias"
        self.alias.symlink_to(self.real, target_is_directory=True)
        (self.real / "includes").mkdir()
        self.source = self.real / "actual-progress.puml"
        self.source.write_text("@startgantt\n!include includes/feature.puml\n@endgantt\n", encoding="utf-8")
        (self.real / "includes/feature.puml").write_text("[Task] lasts 2 days\n", encoding="utf-8")

    def test_symlink_root_has_same_relative_markers(self) -> None:
        expected = EXPANDER.expand_file(self.source, [])
        actual = EXPANDER.expand_file(self.alias / self.source.name, [])
        self.assertEqual(actual, expected)
        self.assertIn("' BEGIN INCLUDE: includes/feature.puml", actual)

    def test_cli_matches_canonical_expansion(self) -> None:
        target = self.parent / "export.puml"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.alias / self.source.name), str(target)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected = "\n".join(EXPANDER.expand_file(self.source, [])).rstrip() + "\n"
        self.assertEqual(target.read_text(encoding="utf-8"), expected)

    def test_symlink_root_expands_staged_nested_includes(self) -> None:
        contents = {
            self.source: "!include includes/pending.puml\n",
            self.real / "includes/pending.puml": "!include feature.puml\n",
        }
        dependencies = []
        actual = EXPANDER.expand_file(self.alias / self.source.name, [], contents, dependencies)
        self.assertEqual(actual, EXPANDER.expand_file(self.source, [], contents))
        self.assertEqual(dependencies, [self.source, self.real / "includes/pending.puml", self.real / "includes/feature.puml"])

    def test_cycle_through_symlink_is_rejected(self) -> None:
        (self.real / "includes/feature.puml").write_text(
            "!include " + str(self.alias / self.source.name) + "\n", encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "include cycle detected"):
            EXPANDER.expand_file(self.alias / self.source.name, [])


if __name__ == "__main__":
    unittest.main()
