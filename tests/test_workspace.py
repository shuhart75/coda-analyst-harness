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
            "CODA_ANALYST_STATE_ROOT": str(workspace / ".workspace-state"),
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
            self.assertEqual(state["schema_version"], 3)
            self.assertEqual(
                {role: item["repository"] for role, item in state["roles"].items()},
                {"analytics": "documents", "code": "coda", "source": "changeswork-copy"},
            )
            self.assertEqual(state["repositories"]["changeswork-copy"]["storage"], "bare-mirror")
            self.assertEqual(state["write_policy"]["code"]["allowed_paths"], [])
            self.assertEqual(
                state["write_policy"]["code"]["allowed_operations"],
                ["initial-clone", "git-pull-ff-only-via-workspace"],
            )
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
            registry = json.loads((workspace / ".workspace-state/code-repos.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["schema_version"], 3)
            self.assertEqual(registry["repositories"][0]["write_policy"]["allowed_paths"], [])

            documents = workspace / "documents"
            entrypoint = documents / "AGENTS.md"
            self.assertTrue(entrypoint.is_file())
            self.assertIn("analyst-harness-local-entrypoint:v1", entrypoint.read_text(encoding="utf-8"))
            self.assertEqual(run("git", "-C", str(documents), "status", "--porcelain=v1").stdout, "")
            self.assertEqual(run("git", "-C", str(documents), "check-ignore", "AGENTS.md").returncode, 0)
            project_root = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "project-root",
                env=environment,
            )
            self.assertEqual(project_root.returncode, 0, project_root.stdout + project_root.stderr)
            self.assertEqual(Path(project_root.stdout.strip()), documents)

            doctor = run(
                sys.executable,
                str(ROOT / "scripts/code-inspect.py"),
                "doctor",
                str(documents),
                env=environment,
            )
            self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
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
                if item["role"] == "source"
            )
            self.assertEqual(source_status["repository"], "changeswork-copy")
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

    def test_protected_code_pull_and_full_sync_update_code_without_local_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, _, _, environment = self.prepare_workspace(root)
            code = workspace / "coda"
            config_before = (code / ".git/config").read_bytes()

            repeated = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "bootstrap",
                env=environment,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
            self.assertEqual((code / ".git/config").read_bytes(), config_before)

            upstream = root / "code-upstream"
            code_remote = Path(environment["CODA_ANALYST_CODA_URL"])
            self.assertEqual(run("git", "clone", str(code_remote), str(upstream)).returncode, 0)
            self.configure_identity(upstream)
            (upstream / "backend/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            self.assertEqual(run("git", "-C", str(upstream), "add", ".").returncode, 0)
            self.assertEqual(run("git", "-C", str(upstream), "commit", "-m", "code update").returncode, 0)
            self.assertEqual(run("git", "-C", str(upstream), "push", "origin", "main").returncode, 0)

            synchronized = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "sync",
                "--no-push",
                env=environment,
            )
            self.assertEqual(synchronized.returncode, 0, synchronized.stdout + synchronized.stderr)
            payload = json.loads(synchronized.stdout)
            self.assertEqual(payload["code_update"]["operation"], "git-pull-ff-only-via-workspace")
            self.assertEqual((code / "backend/app.py").read_text(encoding="utf-8"), "VALUE = 2\n")
            self.assertEqual(run("git", "-C", str(code), "status", "--porcelain=v1").stdout, "")
            self.assertEqual((code / ".git/config").read_bytes(), config_before)

            (code / "backend/app.py").write_text("LOCAL = 3\n", encoding="utf-8")
            blocked = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "update-code",
                env=environment,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("локальные изменения", blocked.stdout)

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
            self.assertIn("inspect-source-analytics-conflict", result.stdout)
            self.assertIn("skip-source-merge", result.stdout)
            self.assertEqual((documents / "shared.txt").read_text(encoding="utf-8"), "documents version\n")
            status = run("git", "-C", str(documents), "status", "--porcelain=v1")
            self.assertEqual(status.stdout, "")
            self.assertFalse((documents / ".git/MERGE_HEAD").exists())

            inspected = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "inspect-source-analytics-conflict",
                env=environment,
            )
            self.assertEqual(inspected.returncode, 0, inspected.stdout + inspected.stderr)
            inspection = json.loads(inspected.stdout)
            self.assertFalse(inspection["real_repositories_changed"])
            self.assertEqual(inspection["conflicts"][0]["path"], "shared.txt")
            self.assertEqual(inspection["conflicts"][0]["kind"], "both-modified")
            self.assertEqual(inspection["conflicts"][0]["recommended_resolution"], "analyst-decision-required")
            self.assertEqual(run("git", "-C", str(documents), "status", "--porcelain=v1").stdout, "")

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
                "CODA_ANALYST_STATE_ROOT": str(workspace / ".workspace-state"),
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

    def test_sync_repairs_verified_unicode_alias_left_by_filesystem_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_name = "context/Маршруты_согласовании\u0306.md"
            new_name = unicodedata.normalize("NFC", old_name)
            self.assertNotEqual(old_name, new_name)
            source_work, source_remote = self.create_seed(
                root,
                "changeswork-copy",
                {"README.md": "# Source\n", old_name: "same content\n"},
            )
            documents_remote = root / "documents.git"
            self.assertEqual(run("git", "clone", "--bare", str(source_remote), str(documents_remote)).returncode, 0)
            (source_work / old_name).rename(source_work / new_name)
            self.assertEqual(run("git", "-C", str(source_work), "add", "-A").returncode, 0)
            self.assertEqual(run("git", "-C", str(source_work), "commit", "-m", "normalize path").returncode, 0)
            self.assertEqual(run("git", "-C", str(source_work), "push", "origin", "main").returncode, 0)
            _, coda_remote = self.create_seed(root, "coda", {"backend/app.py": "VALUE = 1\n"})
            workspace = root / "workspace"
            workspace.mkdir()
            environment = {
                **os.environ,
                "CODA_ANALYST_STATE_ROOT": str(workspace / ".workspace-state"),
                "CODA_ANALYST_DOCUMENTS_URL": str(documents_remote),
                "CODA_ANALYST_CODA_URL": str(coda_remote),
                "CODA_ANALYST_SOURCE_URL": str(source_remote),
            }
            bootstrap = run(
                sys.executable, str(ROOT / "scripts/workspace.py"), "--root", str(workspace), "bootstrap", env=environment
            )
            self.assertEqual(bootstrap.returncode, 0, bootstrap.stdout + bootstrap.stderr)
            analytics = workspace / "documents"
            (analytics / old_name).rename(analytics / new_name)
            exclude = analytics / ".git/info/exclude"
            with exclude.open("a", encoding="utf-8") as handle:
                handle.write(f"/{new_name}\n")
            before = run("git", "-C", str(analytics), "status", "--porcelain=v1", "-z").stdout
            self.assertIn(f" D {old_name}", before)
            self.assertNotIn("??", before)

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
            tracked = run("git", "-C", str(analytics), "ls-files", "-z").stdout.split("\0")
            self.assertIn(new_name, tracked)
            self.assertNotIn(old_name, tracked)
            self.assertEqual(run("git", "-C", str(analytics), "status", "--porcelain=v1").stdout, "")

    def test_sync_migrates_legacy_embedded_harness_and_preserves_both_histories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_work, source_remote = self.create_seed(
                root,
                "changeswork-copy",
                {
                    "README.md": "# Source\n",
                    "AGENTS.md": "# Старая обвязка\n",
                    ".workflow/active-mode.md": "mode: requirements\n",
                    ".vscode/settings.json": "{}\n",
                    "shared.txt": "base\n",
                },
            )
            documents_remote = root / "documents.git"
            self.assertEqual(run("git", "clone", "--bare", str(source_remote), str(documents_remote)).returncode, 0)
            documents_work = root / "documents-work"
            self.assertEqual(run("git", "clone", str(documents_remote), str(documents_work)).returncode, 0)
            self.configure_identity(documents_work)
            (documents_work / "features/registry").mkdir(parents=True)
            (documents_work / "features/registry/requirements.md").write_text("# Новые требования\n", encoding="utf-8")
            (documents_work / ".workflow/active-mode.md").write_text("mode: execution-update\n", encoding="utf-8")
            self.assertEqual(run("git", "-C", str(documents_work), "add", ".").returncode, 0)
            self.assertEqual(run("git", "-C", str(documents_work), "commit", "-m", "documents requirements").returncode, 0)
            self.assertEqual(run("git", "-C", str(documents_work), "push", "origin", "main").returncode, 0)

            for relative in ("AGENTS.md", ".workflow/active-mode.md", ".vscode/settings.json"):
                (source_work / relative).unlink()
            (source_work / ".workflow").rmdir()
            (source_work / ".vscode").rmdir()
            (source_work / "planning").mkdir()
            (source_work / "planning/team.md").write_text("# Команда\n", encoding="utf-8")
            self.assertEqual(run("git", "-C", str(source_work), "add", "-A").returncode, 0)
            self.assertEqual(run("git", "-C", str(source_work), "commit", "-m", "remove embedded harness").returncode, 0)
            self.assertEqual(run("git", "-C", str(source_work), "push", "origin", "main").returncode, 0)

            _, coda_remote = self.create_seed(root, "coda", {"backend/app.py": "VALUE = 1\n"})
            workspace = root / "workspace"
            workspace.mkdir()
            environment = {
                **os.environ,
                "CODA_ANALYST_DOCUMENTS_URL": str(documents_remote),
                "CODA_ANALYST_CODA_URL": str(coda_remote),
                "CODA_ANALYST_SOURCE_URL": str(source_remote),
            }
            bootstrap = run(
                sys.executable, str(ROOT / "scripts/workspace.py"), "--root", str(workspace), "bootstrap", env=environment
            )
            self.assertEqual(bootstrap.returncode, 0, bootstrap.stdout + bootstrap.stderr)
            state = json.loads((workspace / ".workspace-state/workspace.json").read_text(encoding="utf-8"))
            self.assertTrue(state["migration"]["required"])
            self.assertIsNone(state["local_entrypoint"])

            sync = run(
                sys.executable,
                str(ROOT / "scripts/repository-exchange.py"),
                "--root",
                str(workspace),
                "sync",
                "--no-push",
                env=environment,
            )
            self.assertNotEqual(sync.returncode, 0)
            self.assertIn(".workflow/active-mode.md", sync.stdout)
            analytics = workspace / "documents"
            inspected = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "inspect-source-analytics-conflict",
                env=environment,
            )
            self.assertEqual(inspected.returncode, 0, inspected.stdout + inspected.stderr)
            inspection = json.loads(inspected.stdout)
            active_mode = next(item for item in inspection["conflicts"] if item["path"] == ".workflow/active-mode.md")
            self.assertEqual(active_mode["kind"], "source-deleted-analytics-modified")
            self.assertEqual(active_mode["recommended_resolution"], "accept-source-deletion")
            self.assertEqual(run("git", "-C", str(analytics), "status", "--porcelain=v1").stdout, "")

            self.assertEqual(run("git", "-C", str(analytics), "rm", ".workflow/active-mode.md").returncode, 0)
            self.assertEqual(run("git", "-C", str(analytics), "commit", "-m", "remove legacy active mode").returncode, 0)
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
            self.assertTrue((analytics / "features/registry/requirements.md").is_file())
            self.assertTrue((analytics / "planning/team.md").is_file())
            self.assertFalse((analytics / ".workflow").exists())
            self.assertFalse((analytics / ".vscode").exists())
            self.assertIn("analyst-harness-local-entrypoint:v1", (analytics / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertEqual(run("git", "-C", str(analytics), "status", "--porcelain=v1").stdout, "")

    def test_project_paths_in_harness_root_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, _, _, environment = self.prepare_workspace(root)
            (workspace / "features").mkdir()
            result = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "project-root",
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ошибочно созданы", result.stdout)

    def test_roles_change_only_after_explicit_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, documents_remote = self.create_seed(root, "documents", {"README.md": "# Documents\n"})
            _, coda_remote = self.create_seed(root, "coda", {"backend/app.py": "VALUE = 1\n"})
            _, source_remote = self.create_seed(root, "changeswork-copy", {"README.md": "# Source\n"})
            workspace = root / "workspace"
            workspace.mkdir()
            environment = {
                **os.environ,
                "CODA_ANALYST_STATE_ROOT": str(workspace / ".workspace-state"),
                "CODA_ANALYST_DOCUMENTS_URL": str(documents_remote),
                "CODA_ANALYST_CODA_URL": str(coda_remote),
                "CODA_ANALYST_SOURCE_URL": str(source_remote),
            }
            default_status = run(
                sys.executable, str(ROOT / "scripts/workspace.py"), "--root", str(workspace), "status", env=environment
            )
            default_payload = json.loads(default_status.stdout)
            self.assertEqual(
                [(item["role"], item["repository"]) for item in default_payload["repositories"]],
                [("analytics", "documents"), ("code", "coda"), ("source", "changeswork-copy")],
            )
            configured = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "configure-roles",
                "--analytics",
                "changeswork-copy",
                "--code",
                "coda",
                "--source",
                "documents",
                env=environment,
            )
            self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)
            bootstrap = run(
                sys.executable, str(ROOT / "scripts/workspace.py"), "--root", str(workspace), "bootstrap", env=environment
            )
            self.assertEqual(bootstrap.returncode, 0, bootstrap.stdout + bootstrap.stderr)
            state = json.loads((workspace / ".workspace-state/workspace.json").read_text(encoding="utf-8"))
            self.assertEqual(state["roles"]["analytics"]["repository"], "changeswork-copy")
            self.assertEqual(state["roles"]["source"]["repository"], "documents")
            self.assertTrue((workspace / "changeswork-copy/AGENTS.md").is_file())
            self.assertTrue((workspace / ".workspace-state/repositories/documents.git").is_dir())
            editor = json.loads((workspace / "coda-analyst.code-workspace").read_text(encoding="utf-8"))
            self.assertIn("changeswork-copy", {item["path"] for item in editor["folders"]})
            self.assertNotIn("changeswork-copy", editor["settings"]["files.exclude"])


if __name__ == "__main__":
    unittest.main()
