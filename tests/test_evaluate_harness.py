from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class EvaluateHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project"
        self.config = self.project / "context/evals/golden-scenarios.json"
        self.config.parent.mkdir(parents=True)

    def evaluate(self, assertions: list[object]) -> subprocess.CompletedProcess[str]:
        self.config.write_text(json.dumps({"schema_version": 1, "scenarios": [{"name": "probe", "assertions": assertions}]}), encoding="utf-8")
        return self.invoke()

    def invoke(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(ROOT / "scripts/evaluate-harness.py"), str(self.project)], text=True, capture_output=True, check=False)

    def assert_failed(self, result: subprocess.CompletedProcess[str], message: str) -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(message, result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        json.loads(result.stdout)

    def test_existing_assertions_and_json_validation(self) -> None:
        (self.project / "data.json").write_text('{"value": "present"}', encoding="utf-8")
        result = self.evaluate([
            {"type": "exists", "path": "data.json"},
            {"type": "not_exists", "path": "absent"},
            {"type": "contains", "path": "data.json", "value": "present"},
            {"type": "not_contains", "path": "data.json", "value": "missing"},
            {"type": "valid_json", "path": "data.json"},
        ])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for invalid in ('{"value":}', '{"value": NaN}', '{"value": Infinity}'):
            with self.subTest(invalid=invalid):
                (self.project / "data.json").write_text(invalid, encoding="utf-8")
                self.assert_failed(self.evaluate([{"type": "valid_json", "path": "data.json"}]), "data.json")

    def test_invalid_configuration_cannot_pass(self) -> None:
        for payload in ([], {}, {"schema_version": 1, "scenarios": []},
                        {"schema_version": 1, "scenarios": [{"name": "empty", "assertions": []}]},
                        {"schema_version": 1, "scenarios": [{"name": "x", "assertions": [None]}] * 2}):
            with self.subTest(payload=payload):
                self.config.write_text(json.dumps(payload), encoding="utf-8")
                self.assert_failed(self.invoke(), "error")
        self.config.write_text("{", encoding="utf-8")
        self.assert_failed(self.invoke(), "error")

    def test_unknown_or_malformed_assertions_cannot_pass(self) -> None:
        for assertion in (None, {}, {"type": []}, {"type": "typo", "path": "context"},
                          {"type": "contains", "path": "context"},
                          {"type": "exists", "path": "../outside"},
                          {"type": "exists", "path": str(self.project)}):
            with self.subTest(assertion=assertion):
                self.assert_failed(self.evaluate([assertion]), "failures")

    def test_plantuml_roots_allow_shared_and_unused_fragments(self) -> None:
        (self.project / "common.puml").write_text("' shared fragment\n", encoding="utf-8")
        (self.project / "left.puml").write_text('!include "common.puml"\n', encoding="utf-8")
        (self.project / "right.puml").write_text("!include common.puml\n", encoding="utf-8")
        (self.project / "unused.puml").write_text("' reusable fragment\n", encoding="utf-8")
        (self.project / "diagram.puml").write_text("@startgantt\n!include left.puml\n!include right.puml\n@endgantt\n", encoding="utf-8")
        result = self.evaluate([{"type": "plantuml_structure", "path": "diagram.puml"}])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_plantuml_missing_include_and_cycle_fail(self) -> None:
        (self.project / "diagram.puml").write_text("@startuml\n!include fragment.puml\n@enduml\n", encoding="utf-8")
        assertion = {"type": "plantuml_structure", "path": "diagram.puml"}
        self.assert_failed(self.evaluate([assertion]), "fragment.puml")
        (self.project / "fragment.puml").write_text("!include diagram.puml\n", encoding="utf-8")
        self.assert_failed(self.evaluate([assertion]), "include cycle")

    def test_plantuml_marker_errors_fail(self) -> None:
        for text in ("@startgantt\n@enduml\n", "@startuml\n", "@enduml\n",
                     "@startuml\n@startgantt\n@endgantt\n@enduml\n", "' empty\n"):
            with self.subTest(text=text):
                (self.project / "diagram.puml").write_text(text, encoding="utf-8")
                self.assert_failed(self.evaluate([{"type": "plantuml_structure", "path": "diagram.puml"}]), "failures")

    def test_plantuml_does_not_read_outside_project(self) -> None:
        outside = self.project.parent / "outside.puml"
        outside.write_text("' external\n", encoding="utf-8")
        (self.project / "linked.puml").symlink_to(outside)
        for target in ("../outside.puml", "linked.puml", "https://example.invalid/test.puml", "<stdlib>"):
            with self.subTest(target=target):
                (self.project / "diagram.puml").write_text(f"@startuml\n!include {target}\n@enduml\n", encoding="utf-8")
                self.assert_failed(self.evaluate([{"type": "plantuml_structure", "path": "diagram.puml"}]), "failures")


if __name__ == "__main__":
    unittest.main()
