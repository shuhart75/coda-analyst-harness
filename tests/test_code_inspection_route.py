import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/code-inspect.py"


class CodeInspectionRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "analytics"
        self.project.mkdir()
        self.code = self.root / "nested" / "product code"
        self.code.mkdir(parents=True)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.test")
        self.git("remote", "add", "origin", "https://example.test/code.git")
        (self.code / "frontend").mkdir()
        self.filename = "frontend/status colors.txt"
        (self.code / self.filename).write_text("SBERDOCS_CREATE_ERROR\n")
        self.git("add", "frontend")
        self.git("commit", "-m", "Add status fixture")
        self.state = self.root / "state"
        self.state.mkdir()
        self.registry = {"schema_version": 3, "repositories": [{
            "id": "code", "access": "read-only",
            "location": {"relative_to_analytical": "../nested/product code",
                         "environment": "TEST_INSPECTION_CODE"},
            "expected_branch": "main",
            "accepted_remote_urls": ["https://example.test/code.git"],
            "contours": {"frontend": {"path": "frontend"}},
        }]}
        (self.state / "code-repos.json").write_text(json.dumps(self.registry))
        self.env = {**os.environ, "ANALYST_HARNESS_STATE_ROOT": str(self.state),
                    "CODA_ANALYST_STATE_ROOT": str(self.state)}
        self.env.pop("TEST_INSPECTION_CODE", None)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.code), *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    def call(self, *args, cwd=None, expected=0):
        result = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd,
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout

    def test_route_and_locate_are_independent_of_launch_directory(self):
        before = self.git("status", "--porcelain=v1")
        first = self.call("route", str(self.project), "--contour", "frontend", cwd=self.root)
        second = self.call("route", str(self.project), "--contour", "frontend", cwd=self.project)
        self.assertEqual(first, second)
        route = json.loads(first)
        self.assertEqual(route["root"], str(self.code))
        self.assertEqual(route["next_action"]["type"], "begin")
        result = json.loads(self.call("locate", str(self.project), "SBERDOCS_CREATE_ERROR",
                                      "--contour", "frontend"))
        self.assertEqual(result["matches"], [self.filename])
        self.assertEqual(result["head"], self.git("rev-parse", "HEAD"))
        self.assertEqual(self.git("status", "--porcelain=v1"), before)

    def test_zero_matches_and_environment_override(self):
        self.env["TEST_INSPECTION_CODE"] = str(self.code)
        self.registry["repositories"][0]["location"]["relative_to_analytical"] = "../missing"
        (self.state / "code-repos.json").write_text(json.dumps(self.registry))
        result = json.loads(self.call("locate", str(self.project), "ABSENT",
                                      "--contour", "frontend"))
        self.assertEqual(result["status"], "no-matches-in-selected-contour")
        self.assertEqual(result["root"], str(self.code))

    def test_wrong_branch_origin_and_dirty_tree_block_search(self):
        for mutation, restore in [
            (("branch", "-m", "other"), ("branch", "-m", "main")),
            (("remote", "set-url", "origin", "https://example.test/wrong.git"),
             ("remote", "set-url", "origin", "https://example.test/code.git")),
        ]:
            self.git(*mutation)
            self.call("locate", str(self.project), "SBERDOCS", "--contour", "frontend", expected=1)
            self.git(*restore)
        (self.code / "untracked").write_text("local")
        result = json.loads(self.call("route", str(self.project), expected=1))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["root"], str(self.code))


if __name__ == "__main__":
    unittest.main()
