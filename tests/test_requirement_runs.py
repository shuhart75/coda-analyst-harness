from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RequirementRunTests(unittest.TestCase):
    def test_authoring_run_ends_before_publication_and_keeps_legacy_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            env = {**os.environ, "ANALYST_HARNESS_STATE_ROOT": str(root / "state"), "CODA_ANALYST_STATE_ROOT": str(root / "state")}
            result = subprocess.run([sys.executable, str(ROOT / "scripts/harnessctl.py"), "run-init", str(project), "requirements", "--run-id", "authoring"], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            path = root / "state/runs/authoring/run.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("delivery-scope", payload["stages"])
            self.assertEqual(payload["stages"][-1], "verified")
            self.assertFalse({"slices", "detail-packs", "task-candidates", "sent", "implemented", "deployed"}.intersection(payload["stages"]))
            payload.update(stages=["context", "slices", "verified"], stage="context", stage_index=0)
            path.write_text(json.dumps(payload), encoding="utf-8")
            result = subprocess.run([sys.executable, str(ROOT / "scripts/harnessctl.py"), "run-advance", str(path), "pass"], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["stage"], "slices")

    def test_new_authoring_run_rejects_slice_without_creating_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {**os.environ, "ANALYST_HARNESS_STATE_ROOT": str(root / "state"), "CODA_ANALYST_STATE_ROOT": str(root / "state")}
            result = subprocess.run([sys.executable, str(ROOT / "scripts/harnessctl.py"), "run-init", str(root), "requirements", "--slice", "legacy"], env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "state").exists())


if __name__ == "__main__":
    unittest.main()
