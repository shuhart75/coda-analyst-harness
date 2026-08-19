from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False, env=env)


class CodaWorkspaceTests(unittest.TestCase):
    def configure_identity(self, repository: Path) -> None:
        for key, value in (("user.name", "Harness Test"), ("user.email", "harness@example.test")):
            result = run("git", "-C", str(repository), "config", key, value)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def create_seed(self, root: Path, name: str, files: dict[str, str]) -> tuple[Path, Path]:
        work = root / f"seed-{name}"
        remote = root / f"{name}.git"
        self.assertEqual(run("git", "init", "-b", "main", str(work)).returncode, 0)
        self.configure_identity(work)
        for relative, content in files.items():
            path = work / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.assertEqual(run("git", "-C", str(work), "add", ".").returncode, 0)
        self.assertEqual(run("git", "-C", str(work), "commit", "-m", "initial").returncode, 0)
        self.assertEqual(run("git", "clone", "--bare", str(work), str(remote)).returncode, 0)
        self.assertEqual(run("git", "-C", str(work), "remote", "add", "origin", str(remote)).returncode, 0)
        return work, remote

    def prepare_workspace(self, root: Path) -> tuple[Path, Path, Path, dict[str, str]]:
        source_work, source_remote = self.create_seed(
            root,
            "changeswork-copy",
            {
                "README.md": "# Source\n",
                "planning/team.md": "# Команда\n",
                "context/project-rules/README.md": "# Правила проекта\n",
                "shared.txt": "base\n",
            },
        )
        documents_remote = root / "documents.git"
        self.assertEqual(run("git", "clone", "--bare", str(source_remote), str(documents_remote)).returncode, 0)
        _, coda_remote = self.create_seed(
            root,
            "coda",
            {"backend/AGENTS.md": "# Backend\n", "backend/app.py": "VALUE = 1\n"},
        )
        workspace = root / "workspace"
        workspace.mkdir()
        environment = {
            **os.environ,
            "CODA_ANALYST_DOCUMENTS_URL": str(documents_remote),
            "CODA_ANALYST_CODA_URL": str(coda_remote),
            "CODA_ANALYST_SOURCE_URL": str(source_remote),
        }
        result = run(
            sys.executable,
            str(ROOT / "scripts/workspace.py"),
            "--root",
            str(workspace),
            "bootstrap",
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.configure_identity(workspace / "documents")
        return workspace, source_work, documents_remote, environment

    def test_bootstrap_and_exchange_create_verified_reverse_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, source_work, documents_remote, environment = self.prepare_workspace(root)
            state = json.loads((workspace / ".workspace-state/workspace.json").read_text(encoding="utf-8"))
            self.assertEqual(set(state["repositories"]), {"documents", "coda", "changeswork-copy"})
            self.assertEqual(state["schema_version"], 2)
            self.assertEqual(state["repositories"]["changeswork-copy"]["storage"], "bare-mirror")
            self.assertTrue((workspace / "coda-analyst.code-workspace").is_file())
            workspace_config = json.loads(
                (workspace / "coda-analyst.code-workspace").read_text(encoding="utf-8")
            )
            self.assertNotIn(
                "changeswork-copy",
                {folder["path"] for folder in workspace_config["folders"]},
            )
            self.assertFalse((workspace / "changeswork-copy").exists())
            source_mirror = workspace / ".workspace-state/repositories/changeswork-copy.git"
            bare = run("git", "-C", str(source_mirror), "rev-parse", "--is-bare-repository")
            self.assertEqual(bare.stdout.strip(), "true")
            for repository in (workspace / "coda", source_mirror):
                push_url = run("git", "-C", str(repository), "remote", "get-url", "--push", "origin")
                self.assertEqual(push_url.stdout.strip(), "DISABLED_BY_CODA_ANALYST_HARNESS")

            documents = workspace / "documents"
            (documents / "documents-only.txt").write_text("analyst result\n", encoding="utf-8")
            self.assertEqual(run("git", "-C", str(documents), "add", ".").returncode, 0)
            self.assertEqual(run("git", "-C", str(documents), "commit", "-m", "documents change").returncode, 0)
            self.assertEqual(run("git", "-C", str(documents), "push", "origin", "main").returncode, 0)

            (source_work / "source-only.txt").write_text("upstream result\n", encoding="utf-8")
            self.assertEqual(run("git", "-C", str(source_work), "add", ".").returncode, 0)
            self.assertEqual(run("git", "-C", str(source_work), "commit", "-m", "source change").returncode, 0)
            self.assertEqual(run("git", "-C", str(source_work), "push", "origin", "main").returncode, 0)

            result = run(
                sys.executable,
                str(ROOT / "scripts/repository-exchange.py"),
                "--root",
                str(workspace),
                "sync",
                "--no-push",
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((documents / "source-only.txt").is_file())
            patch = workspace / "reverse-diffs/reverse-diff-latest.patch"
            metadata = json.loads((workspace / "reverse-diffs/reverse-diff-latest.json").read_text(encoding="utf-8"))
            self.assertTrue(patch.is_file())
            self.assertTrue(metadata["verified"])
            self.assertFalse(metadata["repositories_identical"])
            applied = root / "applied-source"
            self.assertEqual(run("git", "clone", str(source_mirror), str(applied)).returncode, 0)
            check = run("git", "-C", str(applied), "apply", "--check", str(patch))
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            self.assertEqual(run("git", "-C", str(applied), "apply", str(patch)).returncode, 0)
            self.assertEqual(run("git", "-C", str(applied), "add", ".").returncode, 0)
            applied_tree = run("git", "-C", str(applied), "write-tree").stdout.strip()
            documents_tree = run("git", "-C", str(documents), "rev-parse", "HEAD^{tree}").stdout.strip()
            self.assertEqual(applied_tree, documents_tree)

            status = run(
                sys.executable,
                str(ROOT / "scripts/repository-exchange.py"),
                "--root",
                str(workspace),
                "status",
                env=environment,
            )
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            status_payload = json.loads(status.stdout)
            source_status = next(
                item for item in status_payload["repositories"]
                if item["id"] == "changeswork-copy"
            )
            self.assertEqual(source_status["storage"], "bare-mirror")
            self.assertIsNone(source_status["worktree"])

            remote_check = root / "documents-remote-check"
            self.assertEqual(run("git", "clone", str(documents_remote), str(remote_check)).returncode, 0)
            self.assertFalse((remote_check / "source-only.txt").exists(), "--no-push must not update documents origin")

            result = run(
                sys.executable,
                str(ROOT / "scripts/repository-exchange.py"),
                "--root",
                str(workspace),
                "sync",
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(run("git", "-C", str(remote_check), "pull", "--ff-only").returncode, 0)
            self.assertTrue((remote_check / "source-only.txt").is_file())
            self.assertTrue((remote_check / "documents-only.txt").is_file())

    def test_exchange_aborts_conflicting_merge_without_overwriting_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, source_work, _, environment = self.prepare_workspace(root)
            documents = workspace / "documents"
            (documents / "shared.txt").write_text("documents version\n", encoding="utf-8")
            self.assertEqual(run("git", "-C", str(documents), "add", "shared.txt").returncode, 0)
            self.assertEqual(run("git", "-C", str(documents), "commit", "-m", "documents conflict").returncode, 0)
            self.assertEqual(run("git", "-C", str(documents), "push", "origin", "main").returncode, 0)

            (source_work / "shared.txt").write_text("source version\n", encoding="utf-8")
            self.assertEqual(run("git", "-C", str(source_work), "add", "shared.txt").returncode, 0)
            self.assertEqual(run("git", "-C", str(source_work), "commit", "-m", "source conflict").returncode, 0)
            self.assertEqual(run("git", "-C", str(source_work), "push", "origin", "main").returncode, 0)

            result = run(
                sys.executable,
                str(ROOT / "scripts/repository-exchange.py"),
                "--root",
                str(workspace),
                "sync",
                "--no-push",
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("слияние отменено", result.stdout)
            self.assertEqual((documents / "shared.txt").read_text(encoding="utf-8"), "documents version\n")
            status = run("git", "-C", str(documents), "status", "--porcelain=v1")
            self.assertEqual(status.stdout, "")
            self.assertFalse((documents / ".git/MERGE_HEAD").exists())

    def test_bootstrap_retires_dirty_legacy_source_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_work, source_remote = self.create_seed(
                root,
                "changeswork-copy",
                {"README.md": "# Source\n", "shared.txt": "base\n"},
            )
            documents_remote = root / "documents.git"
            self.assertEqual(run("git", "clone", "--bare", str(source_remote), str(documents_remote)).returncode, 0)
            _, coda_remote = self.create_seed(root, "coda", {"backend/app.py": "VALUE = 1\n"})
            workspace = root / "workspace"
            workspace.mkdir()
            legacy = workspace / "changeswork-copy"
            self.assertEqual(run("git", "clone", str(source_remote), str(legacy)).returncode, 0)
            (legacy / "accidental.txt").write_text("must be preserved\n", encoding="utf-8")
            environment = {
                **os.environ,
                "CODA_ANALYST_DOCUMENTS_URL": str(documents_remote),
                "CODA_ANALYST_CODA_URL": str(coda_remote),
                "CODA_ANALYST_SOURCE_URL": str(source_remote),
            }

            result = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "bootstrap",
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(legacy.exists())
            state = json.loads((workspace / ".workspace-state/workspace.json").read_text(encoding="utf-8"))
            retired = Path(state["retired_legacy_source"])
            self.assertTrue((retired / "accidental.txt").is_file())
            self.assertTrue((workspace / ".workspace-state/repositories/changeswork-copy.git").is_dir())

            sync = run(
                sys.executable,
                str(ROOT / "scripts/repository-exchange.py"),
                "--root",
                str(workspace),
                "sync",
                "--no-push",
                env=environment,
            )
            self.assertEqual(sync.returncode, 0, sync.stdout + sync.stderr)

    def test_exchange_rejects_non_nfc_source_path_without_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, source_work, _, environment = self.prepare_workspace(root)
            decomposed = "Маршруты_согласовании\u0306.md"
            (source_work / decomposed).write_text("source\n", encoding="utf-8")
            self.assertEqual(run("git", "-C", str(source_work), "add", ".").returncode, 0)
            self.assertEqual(run("git", "-C", str(source_work), "commit", "-m", "non nfc").returncode, 0)
            self.assertEqual(run("git", "-C", str(source_work), "push", "origin", "main").returncode, 0)

            result = run(
                sys.executable,
                str(ROOT / "scripts/repository-exchange.py"),
                "--root",
                str(workspace),
                "sync",
                "--no-push",
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unicode NFC", result.stdout)
            self.assertNotIn(decomposed, run("git", "-C", str(workspace / "documents"), "ls-files").stdout)

    def test_exchange_rejects_source_mirror_with_push_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, _, _, environment = self.prepare_workspace(root)
            source_mirror = workspace / ".workspace-state/repositories/changeswork-copy.git"
            self.assertEqual(
                run(
                    "git",
                    "-C",
                    str(source_mirror),
                    "config",
                    "--unset-all",
                    "remote.origin.pushurl",
                ).returncode,
                0,
            )

            result = run(
                sys.executable,
                str(ROOT / "scripts/repository-exchange.py"),
                "--root",
                str(workspace),
                "sync",
                "--no-push",
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("запрет отправки", result.stdout)


if __name__ == "__main__":
    unittest.main()
