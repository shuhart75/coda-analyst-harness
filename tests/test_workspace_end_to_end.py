from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, check=False)


class CodaWorkspaceEndToEndTests(unittest.TestCase):
    def git_identity(self, repository: Path) -> None:
        for key, value in (("user.name", "Harness Test"), ("user.email", "harness@example.test")):
            result = run("git", "-C", str(repository), "config", key, value)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def commit_all(self, repository: Path, message: str) -> str:
        self.assertEqual(run("git", "-C", str(repository), "add", "-A").returncode, 0)
        committed = run("git", "-C", str(repository), "commit", "-m", message)
        self.assertEqual(committed.returncode, 0, committed.stdout + committed.stderr)
        return run("git", "-C", str(repository), "rev-parse", "HEAD").stdout.strip()

    def bare_remote(self, worktree: Path, remote: Path) -> None:
        cloned = run("git", "clone", "--bare", str(worktree), str(remote))
        self.assertEqual(cloned.returncode, 0, cloned.stdout + cloned.stderr)
        self.assertEqual(run("git", "-C", str(worktree), "remote", "add", "origin", str(remote)).returncode, 0)

    def test_real_clone_migrates_and_supports_both_entrypoints_with_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_work = root / "source-work"
            scaffold = run("bash", str(ROOT / "scripts/scaffold-project.sh"), str(source_work))
            self.assertEqual(scaffold.returncode, 0, scaffold.stdout + scaffold.stderr)
            self.assertEqual(run("git", "init", "-b", "main", str(source_work)).returncode, 0)
            self.git_identity(source_work)
            (source_work / "AGENTS.md").write_text("# Старая встроенная обвязка\n", encoding="utf-8")
            (source_work / ".workflow").mkdir()
            (source_work / ".workflow/active-mode.md").write_text("mode: requirements\n", encoding="utf-8")
            (source_work / ".vscode").mkdir()
            (source_work / ".vscode/settings.json").write_text("{}\n", encoding="utf-8")
            old_name = "context/source-materials/Маршруты_согласовании\u0306.md"
            new_name = unicodedata.normalize("NFC", old_name)
            (source_work / old_name).write_text("маршрут\n", encoding="utf-8")
            self.commit_all(source_work, "legacy project with embedded harness")
            source_remote = root / "changeswork-copy.git"
            self.bare_remote(source_work, source_remote)

            documents_remote = root / "documents.git"
            cloned_documents = run("git", "clone", "--bare", str(source_remote), str(documents_remote))
            self.assertEqual(cloned_documents.returncode, 0, cloned_documents.stdout + cloned_documents.stderr)
            documents_work = root / "documents-work"
            self.assertEqual(run("git", "clone", str(documents_remote), str(documents_work)).returncode, 0)
            self.git_identity(documents_work)
            (documents_work / "context/analytics-only.md").write_text("изменение аналитика\n", encoding="utf-8")
            self.commit_all(documents_work, "analytics change before bootstrap")
            self.assertEqual(run("git", "-C", str(documents_work), "push", "origin", "main").returncode, 0)

            for relative in ("AGENTS.md", ".workflow/active-mode.md", ".vscode/settings.json"):
                (source_work / relative).unlink()
            (source_work / ".workflow").rmdir()
            (source_work / ".vscode").rmdir()
            (source_work / old_name).rename(source_work / new_name)
            (source_work / "context/source-only.md").write_text("изменение источника\n", encoding="utf-8")
            source_head = self.commit_all(source_work, "remove harness and normalize path")
            self.assertEqual(run("git", "-C", str(source_work), "push", "origin", "main").returncode, 0)

            code_work = root / "code-work"
            (code_work / "backend").mkdir(parents=True)
            (code_work / "frontend").mkdir()
            (code_work / "backend/AGENTS.md").write_text("# Backend SDD\n", encoding="utf-8")
            (code_work / "frontend/AGENTS.md").write_text("# Frontend SDD\n", encoding="utf-8")
            (code_work / "backend/Registry.java").write_text("class Registry { String productCode; }\n", encoding="utf-8")
            (code_work / "frontend/Registry.tsx").write_text("export const Registry = () => null;\n", encoding="utf-8")
            self.assertEqual(run("git", "init", "-b", "main", str(code_work)).returncode, 0)
            self.git_identity(code_work)
            code_head = self.commit_all(code_work, "code")
            code_remote = root / "coda.git"
            self.bare_remote(code_work, code_remote)

            harness = root / "coda-analyst-harness"
            cloned_harness = run("git", "clone", "--no-local", str(ROOT), str(harness))
            self.assertEqual(cloned_harness.returncode, 0, cloned_harness.stdout + cloned_harness.stderr)
            working_diff = run("git", "diff", "--binary", "HEAD", cwd=ROOT).stdout
            if working_diff:
                applied = subprocess.run(
                    ("git", "apply"), cwd=harness, input=working_diff, text=True, capture_output=True, check=False
                )
                self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            harness_status_before = run("git", "status", "--porcelain=v1", cwd=harness).stdout
            environment = {
                **os.environ,
                "CODA_ANALYST_DOCUMENTS_URL": str(documents_remote),
                "CODA_ANALYST_CODA_URL": str(code_remote),
                "CODA_ANALYST_SOURCE_URL": str(source_remote),
            }
            bootstrap = run(sys.executable, "scripts/workspace.py", "bootstrap", cwd=harness, env=environment)
            self.assertEqual(bootstrap.returncode, 0, bootstrap.stdout + bootstrap.stderr)
            state = json.loads((harness / ".workspace-state/workspace.json").read_text(encoding="utf-8"))
            self.assertTrue(state["migration"]["required"])
            self.assertIsNone(state["local_entrypoint"])

            (documents_work / "context/remote-after-bootstrap.md").write_text("параллельный коммит\n", encoding="utf-8")
            self.commit_all(documents_work, "analytics change after bootstrap")
            self.assertEqual(run("git", "-C", str(documents_work), "push", "origin", "main").returncode, 0)

            analytics = harness / "documents"
            (analytics / old_name).rename(analytics / new_name)
            with (analytics / ".git/info/exclude").open("a", encoding="utf-8") as handle:
                handle.write(f"/{new_name}\n")
            before_sync = run("git", "status", "--porcelain=v1", "-z", cwd=analytics).stdout
            self.assertIn(f" D {old_name}", before_sync)
            self.assertNotIn("??", before_sync)

            synchronized = run(sys.executable, "scripts/repository-exchange.py", "sync", cwd=harness, env=environment)
            self.assertEqual(synchronized.returncode, 0, synchronized.stdout + synchronized.stderr)
            for relative in (
                "context/analytics-only.md",
                "context/remote-after-bootstrap.md",
                "context/source-only.md",
                new_name,
            ):
                self.assertTrue((analytics / relative).is_file(), relative)
            for forbidden in (".workflow", ".vscode"):
                self.assertFalse((analytics / forbidden).exists())
            entrypoint = analytics / "AGENTS.md"
            entrypoint_text = entrypoint.read_text(encoding="utf-8")
            self.assertIn("analyst-harness-local-entrypoint:v1", entrypoint_text)
            self.assertIn(f"HARNESS_ROOT = {harness}", entrypoint_text)
            self.assertIn(f"PROJECT_ROOT = {analytics}", entrypoint_text)
            self.assertIn(f"CODE_ROOT = {harness / 'coda'}", entrypoint_text)
            self.assertEqual(run("git", "check-ignore", "AGENTS.md", cwd=analytics).returncode, 0)
            self.assertEqual(run("git", "status", "--porcelain=v1", cwd=analytics).stdout, "")

            remote_check = root / "documents-check"
            self.assertEqual(run("git", "clone", str(documents_remote), str(remote_check)).returncode, 0)
            self.assertTrue((remote_check / "context/analytics-only.md").is_file())
            self.assertTrue((remote_check / "context/source-only.md").is_file())
            self.assertFalse((remote_check / ".workflow").exists())
            self.assertNotEqual(run("git", "-C", str(source_remote), "rev-parse", "main").stdout.strip(), "")
            self.assertEqual(run("git", "-C", str(source_remote), "rev-parse", "main").stdout.strip(), source_head)
            self.assertFalse((source_work / "context/analytics-only.md").exists())

            root_project = run(sys.executable, "scripts/workspace.py", "project-root", cwd=harness, env=environment)
            analytics_project = run(
                sys.executable,
                "../scripts/workspace.py",
                "--root",
                "..",
                "project-root",
                cwd=analytics,
                env=environment,
            )
            self.assertEqual(root_project.returncode, 0, root_project.stdout + root_project.stderr)
            self.assertEqual(analytics_project.returncode, 0, analytics_project.stdout + analytics_project.stderr)
            self.assertEqual(root_project.stdout.strip(), analytics_project.stdout.strip())

            root_code = run(sys.executable, "scripts/code-inspect.py", "doctor", "documents", cwd=harness, env=environment)
            analytics_code = run(sys.executable, "../scripts/code-inspect.py", "doctor", ".", cwd=analytics, env=environment)
            self.assertEqual(root_code.returncode, 0, root_code.stdout + root_code.stderr)
            self.assertEqual(analytics_code.returncode, 0, analytics_code.stdout + analytics_code.stderr)
            self.assertEqual(json.loads(root_code.stdout), json.loads(analytics_code.stdout))
            root_locate = run(
                sys.executable,
                "scripts/code-inspect.py",
                "locate",
                "documents",
                "productCode",
                "--contour",
                "backend",
                cwd=harness,
                env=environment,
            )
            analytics_locate = run(
                sys.executable,
                "../scripts/code-inspect.py",
                "locate",
                ".",
                "productCode",
                "--contour",
                "backend",
                cwd=analytics,
                env=environment,
            )
            self.assertEqual(root_locate.returncode, 0, root_locate.stdout + root_locate.stderr)
            self.assertEqual(analytics_locate.returncode, 0, analytics_locate.stdout + analytics_locate.stderr)
            self.assertEqual(json.loads(root_locate.stdout), json.loads(analytics_locate.stdout))
            inspection_environment = {**environment, "XDG_STATE_HOME": str(root / "inspection-state")}
            for cwd, tool, project in (
                (harness, "scripts/code-inspect.py", "documents"),
                (analytics, "../scripts/code-inspect.py", "."),
            ):
                begun = run(
                    sys.executable,
                    tool,
                    "begin",
                    project,
                    "--contour",
                    "backend",
                    "--query",
                    "productCode",
                    cwd=cwd,
                    env=inspection_environment,
                )
                self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
                state_path = begun.stdout.splitlines()[0]
                verified = run(sys.executable, tool, "verify", state_path, cwd=cwd, env=inspection_environment)
                self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
                self.assertEqual(json.loads(verified.stdout)["result"], "unchanged")

            root_doctor = run(sys.executable, "scripts/harnessctl.py", "doctor", "documents", cwd=harness, env=environment)
            analytics_doctor = run(sys.executable, "../scripts/harnessctl.py", "doctor", ".", cwd=analytics, env=environment)
            self.assertEqual(root_doctor.returncode, 0, root_doctor.stdout + root_doctor.stderr)
            self.assertEqual(analytics_doctor.returncode, 0, analytics_doctor.stdout + analytics_doctor.stderr)

            first_head = run("git", "rev-parse", "HEAD", cwd=analytics).stdout.strip()
            repeated = run(sys.executable, "scripts/repository-exchange.py", "sync", cwd=harness, env=environment)
            self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
            self.assertEqual(run("git", "rev-parse", "HEAD", cwd=analytics).stdout.strip(), first_head)
            repeated_bootstrap = run(sys.executable, "scripts/workspace.py", "bootstrap", cwd=harness, env=environment)
            self.assertEqual(repeated_bootstrap.returncode, 0, repeated_bootstrap.stdout + repeated_bootstrap.stderr)
            refreshed = json.loads((harness / ".workspace-state/workspace.json").read_text(encoding="utf-8"))
            self.assertFalse(refreshed["migration"]["required"])

            root_feature = run("bash", "scripts/scaffold-feature.sh", "documents", "from-root", cwd=harness)
            analytics_feature = run("bash", "../scripts/scaffold-feature.sh", ".", "from-analytics", cwd=analytics)
            self.assertEqual(root_feature.returncode, 0, root_feature.stdout + root_feature.stderr)
            self.assertEqual(analytics_feature.returncode, 0, analytics_feature.stdout + analytics_feature.stderr)
            self.assertTrue((analytics / "features/from-root/feature.md").is_file())
            self.assertTrue((analytics / "features/from-analytics/feature.md").is_file())
            self.assertFalse((harness / "features").exists())
            self.assertEqual(run("git", "rev-parse", "HEAD", cwd=harness / "coda").stdout.strip(), code_head)
            self.assertEqual(run("git", "status", "--porcelain=v1", cwd=harness / "coda").stdout, "")
            self.assertEqual(run("git", "status", "--porcelain=v1", cwd=harness).stdout, harness_status_before)


if __name__ == "__main__":
    unittest.main()
