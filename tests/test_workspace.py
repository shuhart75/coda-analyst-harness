from __future__ import annotations

import hashlib
import json
import os
import shutil
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
                "context/source-materials/legacy/snapshot/.codex": "historical marker\n",
                "context/shared.txt": "base\n",
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
            self.assertEqual(state["schema_version"], 4)
            self.assertEqual(state["status"], "ready")
            self.assertEqual(
                {role: item["repository"] for role, item in state["roles"].items()},
                {"analytics": "documents", "code": "coda", "source": "changeswork-copy"},
            )
            self.assertEqual(state["repositories"]["changeswork-copy"]["storage"], "bare-mirror")
            self.assertEqual(
                {role: item["availability"] for role, item in state["roles"].items()},
                {"analytics": "ready", "code": "ready", "source": "ready"},
            )
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
            for local_path in (
                ".codex/state",
                ".gigacode/settings.json",
                ".gigaide/settings",
                ".idea/modules.xml",
                "GIGACODE.md",
                "local.iml",
                "settings.json.orig",
                "test-sync.md",
                "test-reverse.patch",
                "features/test-patch.md",
            ):
                self.assertEqual(
                    run("git", "-C", str(documents), "check-ignore", local_path).returncode,
                    0,
                    local_path,
                )
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
            (documents / "context/documents-only.txt").write_text("analyst result\n", encoding="utf-8")
            self.assertEqual(run("git", "-C", str(documents), "add", ".").returncode, 0)
            self.assertEqual(run("git", "-C", str(documents), "commit", "-m", "documents change").returncode, 0)
            self.assertEqual(run("git", "-C", str(documents), "push", "origin", "main").returncode, 0)

            (source_work / "context/source-only.txt").write_text("upstream result\n", encoding="utf-8")
            self.assertEqual(run("git", "-C", str(source_work), "add", ".").returncode, 0)
            self.assertEqual(run("git", "-C", str(source_work), "commit", "-m", "source change").returncode, 0)
            self.assertEqual(run("git", "-C", str(source_work), "push", "origin", "main").returncode, 0)
            source_head = run("git", "-C", str(source_work), "rev-parse", "HEAD").stdout.strip()

            result = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "sync",
                "--no-push",
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            sync_payload = json.loads(result.stdout)
            self.assertEqual(sync_payload["status"], "analytics-synchronized-reverse-diff-pending")
            self.assertEqual(sync_payload["source_analytics_state"], "reverse-diff-pending")
            self.assertFalse(sync_payload["repositories_identical"])
            self.assertFalse(sync_payload["all_repositories_synchronized"])
            self.assertIn("source не изменён", sync_payload["report_message"])
            self.assertNotIn("Все доступные репозитории синхронизированы", sync_payload["report_message"])
            self.assertIn("all-repositories-synchronized", sync_payload["forbidden_claims"])
            self.assertIn("обратную заплату", sync_payload["next_action"])
            self.assertTrue((documents / "context/source-only.txt").is_file())
            patch = workspace / "reverse-diffs/reverse-diff-latest.patch"
            metadata = json.loads((workspace / "reverse-diffs/reverse-diff-latest.json").read_text(encoding="utf-8"))
            self.assertTrue(patch.is_file())
            self.assertTrue(metadata["verified"])
            self.assertTrue(metadata["tree_verified"])
            self.assertTrue(metadata["content_policy_verified"])
            self.assertEqual(metadata["schema_version"], 2)
            self.assertEqual(metadata["patch_sha256"], hashlib.sha256(patch.read_bytes()).hexdigest())
            archived_patch = Path(metadata["patch"])
            archived_metadata = Path(metadata["metadata"])
            self.assertTrue(archived_patch.is_file())
            self.assertTrue(archived_metadata.is_file())
            self.assertIn(metadata["artifact_id"], archived_patch.name)
            self.assertIn(metadata["artifact_id"], archived_metadata.name)
            archived_patch_bytes = archived_patch.read_bytes()
            archived_metadata_bytes = archived_metadata.read_bytes()
            self.assertEqual(metadata["changed_path_count"], len(metadata["changed_paths"]))
            self.assertEqual(metadata["approved_source_deletions"], [])
            self.assertTrue((documents / "context/source-materials/legacy/snapshot/.codex").is_file())
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
            self.assertFalse((remote_check / "context/source-only.txt").exists(), "--no-push must not update documents origin")

            result = run(
                sys.executable,
                str(ROOT / "scripts/repository-exchange.py"),
                "--root",
                str(workspace),
                "sync",
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            completed_payload = json.loads(result.stdout)
            self.assertEqual(completed_payload["source_analytics_state"], "reverse-diff-pending")
            self.assertFalse(completed_payload["reverse_diff"]["repositories_identical"])
            self.assertFalse(completed_payload["all_repositories_synchronized"])
            self.assertEqual(archived_patch.read_bytes(), archived_patch_bytes)
            self.assertEqual(archived_metadata.read_bytes(), archived_metadata_bytes)
            self.assertEqual(run("git", "-C", str(source_mirror), "rev-parse", "main").stdout.strip(), source_head)
            self.assertEqual(run("git", "-C", str(remote_check), "pull", "--ff-only").returncode, 0)
            self.assertTrue((remote_check / "context/source-only.txt").is_file())
            self.assertTrue((remote_check / "context/documents-only.txt").is_file())

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
            self.assertEqual(payload["status"], "fully-synchronized")
            self.assertEqual(payload["source_analytics_state"], "identical")
            self.assertTrue(payload["repositories_identical"])
            self.assertTrue(payload["all_repositories_synchronized"])
            self.assertIn("Все доступные репозитории синхронизированы", payload["report_message"])
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

    def test_removed_source_stays_absent_and_sync_updates_code_and_pushes_analytics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, _, documents_remote, environment = self.prepare_workspace(root)
            source_mirror = workspace / ".workspace-state/repositories/changeswork-copy.git"
            shutil.rmtree(source_mirror)
            documents = workspace / "documents"
            relative = "context/analytics-without-source.md"
            (documents / relative).write_text("локальное изменение\n", encoding="utf-8")
            self.assertEqual(run("git", "-C", str(documents), "add", "--", relative).returncode, 0)
            self.assertEqual(run("git", "-C", str(documents), "commit", "-m", "analytics without source").returncode, 0)

            bootstrap = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "bootstrap",
                env=environment,
            )
            self.assertEqual(bootstrap.returncode, 0, bootstrap.stdout + bootstrap.stderr)
            self.assertFalse(source_mirror.exists())
            state = json.loads((workspace / ".workspace-state/workspace.json").read_text(encoding="utf-8"))
            self.assertEqual(state["roles"]["source"]["availability"], "absent")
            self.assertEqual(state["roles"]["code"]["availability"], "ready")

            synchronized = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "sync",
                env=environment,
            )
            self.assertEqual(synchronized.returncode, 0, synchronized.stdout + synchronized.stderr)
            payload = json.loads(synchronized.stdout)
            self.assertEqual(payload["status"], "analytics-synchronized-source-unavailable")
            self.assertEqual(payload["source_analytics_state"], "source-unavailable")
            self.assertIsNone(payload["repositories_identical"])
            self.assertFalse(payload["all_repositories_synchronized"])
            self.assertIn("source отсутствует", payload["report_message"])
            self.assertIn("all-repositories-synchronized", payload["forbidden_claims"])
            self.assertEqual(payload["sync_mode"], "analytics-only")
            self.assertIn(payload["code_update"]["status"], {"current", "updated"})
            self.assertEqual(payload["analytics_exchange"]["reverse_diff"]["reason"], "source-role-absent")
            self.assertFalse(payload["analytics_exchange"]["reverse_diff"]["verified"])
            self.assertFalse((workspace / "reverse-diffs/reverse-diff-latest.patch").exists())
            unavailable_metadata = payload["analytics_exchange"]["reverse_diff"]
            self.assertTrue(Path(unavailable_metadata["metadata"]).is_file())
            self.assertIn(unavailable_metadata["artifact_id"], Path(unavailable_metadata["metadata"]).name)
            self.assertIn(
                f"CODE_ROOT = {workspace / 'coda'}",
                (documents / "AGENTS.md").read_text(encoding="utf-8"),
            )

            reverse_diff = run(
                sys.executable,
                str(ROOT / "scripts/repository-exchange.py"),
                "--root",
                str(workspace),
                "reverse-diff",
                env=environment,
            )
            self.assertNotEqual(reverse_diff.returncode, 0)
            self.assertIn("Репозиторий роли source отсутствует", reverse_diff.stdout)

            remote_check = root / "documents-without-source-check"
            self.assertEqual(run("git", "clone", str(documents_remote), str(remote_check)).returncode, 0)
            self.assertTrue((remote_check / relative).is_file())

    def test_analytics_only_sync_does_not_invent_a_commit_for_dirty_analytics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, _, _, environment = self.prepare_workspace(root)
            shutil.rmtree(workspace / ".workspace-state/repositories/changeswork-copy.git")
            bootstrap = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "bootstrap",
                env=environment,
            )
            self.assertEqual(bootstrap.returncode, 0, bootstrap.stdout + bootstrap.stderr)
            documents = workspace / "documents"
            dirty_path = documents / "context/not-reviewed.md"
            dirty_path.write_text("непроверенное изменение\n", encoding="utf-8")
            head_before = run("git", "-C", str(documents), "rev-parse", "HEAD").stdout.strip()

            synchronized = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "sync",
                "--no-push",
                env=environment,
            )
            self.assertNotEqual(synchronized.returncode, 0)
            payload = json.loads(synchronized.stdout)
            self.assertEqual(payload["sync_mode"], "analytics-only")
            self.assertIn("незакоммиченные изменения", payload["analytics_exchange"])
            self.assertEqual(run("git", "-C", str(documents), "rev-parse", "HEAD").stdout.strip(), head_before)
            self.assertTrue(dirty_path.is_file())

    def test_removed_code_stays_absent_and_full_source_exchange_continues(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, source_work, _, environment = self.prepare_workspace(root)
            shutil.rmtree(workspace / "coda")
            documents = workspace / "documents"
            analytics_relative = "context/analytics-without-code.md"
            (documents / analytics_relative).write_text("аналитическое изменение\n", encoding="utf-8")
            self.assertEqual(run("git", "-C", str(documents), "add", "--", analytics_relative).returncode, 0)
            self.assertEqual(run("git", "-C", str(documents), "commit", "-m", "analytics without code").returncode, 0)
            source_relative = "context/source-without-code.md"
            (source_work / source_relative).write_text("изменение источника\n", encoding="utf-8")
            self.assertEqual(run("git", "-C", str(source_work), "add", "--", source_relative).returncode, 0)
            self.assertEqual(run("git", "-C", str(source_work), "commit", "-m", "source without code").returncode, 0)
            self.assertEqual(run("git", "-C", str(source_work), "push", "origin", "main").returncode, 0)

            bootstrap = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "bootstrap",
                env=environment,
            )
            self.assertEqual(bootstrap.returncode, 0, bootstrap.stdout + bootstrap.stderr)
            self.assertFalse((workspace / "coda").exists())
            state = json.loads((workspace / ".workspace-state/workspace.json").read_text(encoding="utf-8"))
            self.assertEqual(state["roles"]["code"]["availability"], "absent")
            self.assertEqual(state["roles"]["source"]["availability"], "ready")
            registry = json.loads((workspace / ".workspace-state/code-repos.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["repositories"], [])

            synchronized = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "sync",
                env=environment,
            )
            self.assertEqual(synchronized.returncode, 0, synchronized.stdout + synchronized.stderr)
            payload = json.loads(synchronized.stdout)
            self.assertEqual(payload["status"], "analytics-synchronized-reverse-diff-pending")
            self.assertEqual(payload["source_analytics_state"], "reverse-diff-pending")
            self.assertFalse(payload["repositories_identical"])
            self.assertFalse(payload["all_repositories_synchronized"])
            self.assertIn("source не изменён", payload["report_message"])
            self.assertEqual(payload["sync_mode"], "source-analytics")
            self.assertEqual(payload["code_update"]["status"], "skipped")
            self.assertEqual(payload["code_update"]["reason"], "repository-absent")
            self.assertTrue(payload["analytics_exchange"]["reverse_diff"]["verified"])
            self.assertTrue((documents / source_relative).is_file())
            self.assertTrue((documents / analytics_relative).is_file())
            entrypoint = (documents / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("Репозиторий роли code локально отсутствует", entrypoint)
            self.assertNotIn("CODE_ROOT =", entrypoint)
            editor = json.loads((workspace / "coda-analyst.code-workspace").read_text(encoding="utf-8"))
            self.assertNotIn("code-read-only", {item["name"] for item in editor["folders"]})
            code_doctor = run(
                sys.executable,
                str(ROOT / "scripts/code-inspect.py"),
                "doctor",
                str(documents),
                env=environment,
            )
            self.assertEqual(code_doctor.returncode, 0, code_doctor.stdout + code_doctor.stderr)
            self.assertEqual(json.loads(code_doctor.stdout)["repositories"], [])

    def test_removed_source_and_code_stay_absent_while_analytics_syncs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, _, documents_remote, environment = self.prepare_workspace(root)
            shutil.rmtree(workspace / "coda")
            shutil.rmtree(workspace / ".workspace-state/repositories/changeswork-copy.git")
            documents = workspace / "documents"
            relative = "context/analytics-only-workspace.md"
            (documents / relative).write_text("изменение без дополнительных ролей\n", encoding="utf-8")
            self.assertEqual(run("git", "-C", str(documents), "add", "--", relative).returncode, 0)
            self.assertEqual(run("git", "-C", str(documents), "commit", "-m", "analytics only workspace").returncode, 0)

            bootstrap = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "bootstrap",
                env=environment,
            )
            self.assertEqual(bootstrap.returncode, 0, bootstrap.stdout + bootstrap.stderr)
            self.assertFalse((workspace / "coda").exists())
            self.assertFalse((workspace / ".workspace-state/repositories/changeswork-copy.git").exists())
            state = json.loads((workspace / ".workspace-state/workspace.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "degraded")
            self.assertEqual(state["roles"]["code"]["availability"], "absent")
            self.assertEqual(state["roles"]["source"]["availability"], "absent")

            synchronized = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "sync",
                env=environment,
            )
            self.assertEqual(synchronized.returncode, 0, synchronized.stdout + synchronized.stderr)
            payload = json.loads(synchronized.stdout)
            self.assertEqual(payload["status"], "analytics-synchronized-source-unavailable")
            self.assertEqual(payload["source_analytics_state"], "source-unavailable")
            self.assertIsNone(payload["repositories_identical"])
            self.assertFalse(payload["all_repositories_synchronized"])
            self.assertEqual(payload["sync_mode"], "analytics-only")
            self.assertEqual(payload["code_update"]["status"], "skipped")
            entrypoint = (documents / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("Репозиторий роли code локально отсутствует", entrypoint)
            registry = json.loads((workspace / ".workspace-state/code-repos.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["repositories"], [])
            editor = json.loads((workspace / "coda-analyst.code-workspace").read_text(encoding="utf-8"))
            self.assertNotIn("code-read-only", {item["name"] for item in editor["folders"]})
            status = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "status",
                env=environment,
            )
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            self.assertEqual(json.loads(status.stdout)["status"], "degraded")
            remote_check = root / "documents-analytics-only-check"
            self.assertEqual(run("git", "clone", str(documents_remote), str(remote_check)).returncode, 0)
            self.assertTrue((remote_check / relative).is_file())

    def test_schema_3_state_migrates_without_recreating_removed_optional_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, _, _, environment = self.prepare_workspace(root)
            state_path = workspace / ".workspace-state/workspace.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["schema_version"] = 3
            state.pop("status", None)
            for item in state["roles"].values():
                item.pop("availability", None)
            for item in state["repositories"].values():
                item.pop("availability", None)
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            shutil.rmtree(workspace / "coda")
            shutil.rmtree(workspace / ".workspace-state/repositories/changeswork-copy.git")

            bootstrap = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "bootstrap",
                env=environment,
            )
            self.assertEqual(bootstrap.returncode, 0, bootstrap.stdout + bootstrap.stderr)
            migrated = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(migrated["schema_version"], 4)
            self.assertEqual(migrated["status"], "degraded")
            self.assertEqual(migrated["roles"]["analytics"]["availability"], "ready")
            self.assertEqual(migrated["roles"]["code"]["availability"], "absent")
            self.assertEqual(migrated["roles"]["source"]["availability"], "absent")
            self.assertFalse((workspace / "coda").exists())
            self.assertFalse((workspace / ".workspace-state/repositories/changeswork-copy.git").exists())

    def test_existing_invalid_optional_role_paths_block_reduced_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, _, _, environment = self.prepare_workspace(root)
            code = workspace / "coda"
            shutil.rmtree(code)
            code.mkdir()

            status = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "status",
                env=environment,
            )
            self.assertNotEqual(status.returncode, 0)
            payload = json.loads(status.stdout)
            self.assertEqual(payload["status"], "invalid")
            code_item = next(item for item in payload["repositories"] if item["role"] == "code")
            self.assertEqual(code_item["state"], "invalid")

            update = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "update-code",
                env=environment,
            )
            self.assertNotEqual(update.returncode, 0)
            self.assertIn("не является отдельным Git-репозиторием", update.stdout)

            shutil.rmtree(code)
            source = workspace / ".workspace-state/repositories/changeswork-copy.git"
            shutil.rmtree(source)
            source.mkdir()
            synchronized = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "sync",
                env=environment,
            )
            self.assertNotEqual(synchronized.returncode, 0)
            self.assertIn("не является bare-репозиторием", synchronized.stdout)

    def test_removed_analytics_role_is_never_recloned_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, _, _, environment = self.prepare_workspace(root)
            shutil.rmtree(workspace / "documents")

            status = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "status",
                env=environment,
            )
            self.assertNotEqual(status.returncode, 0)
            payload = json.loads(status.stdout)
            self.assertEqual(payload["status"], "incomplete")
            analytics_item = next(item for item in payload["repositories"] if item["role"] == "analytics")
            self.assertEqual(analytics_item["state"], "missing")

            bootstrap = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "bootstrap",
                env=environment,
            )
            self.assertNotEqual(bootstrap.returncode, 0)
            self.assertIn("автоматическое повторное клонирование запрещено", bootstrap.stdout)
            self.assertFalse((workspace / "documents").exists())

    def test_sync_blocks_committed_local_settings_and_test_artifacts_from_documents_origin(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, _, documents_remote, environment = self.prepare_workspace(root)
            author = root / "documents-author"
            self.assertEqual(run("git", "clone", str(documents_remote), str(author)).returncode, 0)
            self.configure_identity(author)
            files = {
                ".gigacode/settings.json": "{}\n",
                ".gigaide/gigaide.properties": "local=true\n",
                ".idea/modules.xml": "<project/>\n",
                "GIGACODE.md": "# Local memory\n",
                "features/test-patch.md": "test\n",
                "test-sync.md": "test\n",
            }
            for relative, content in files.items():
                path = author / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                self.assertEqual(run("git", "-C", str(author), "add", "--", relative).returncode, 0)
            self.assertEqual(run("git", "-C", str(author), "commit", "-m", "polluted analytics").returncode, 0)
            self.assertEqual(run("git", "-C", str(author), "push", "origin", "main").returncode, 0)

            result = run(
                sys.executable,
                str(ROOT / "scripts/workspace.py"),
                "--root",
                str(workspace),
                "sync",
                "--no-push",
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("analytics-content-policy", result.stdout)
            for relative in files:
                self.assertIn(relative, result.stdout)
            self.assertIn("git add -A", result.stdout)
            self.assertFalse((workspace / "reverse-diffs/reverse-diff-latest.json").exists())
            documents = workspace / "documents"
            self.assertEqual(run("git", "-C", str(documents), "status", "--porcelain=v1").stdout, "")

            status = run(
                sys.executable,
                str(ROOT / "scripts/repository-exchange.py"),
                "--root",
                str(workspace),
                "status",
                env=environment,
            )
            self.assertNotEqual(status.returncode, 0)
            payload = json.loads(status.stdout)
            analytics = next(item for item in payload["repositories"] if item["role"] == "analytics")
            self.assertEqual(
                {item["path"] for item in analytics["content_policy_violations"]},
                set(files),
            )

    def test_reverse_diff_requires_explicit_approval_for_source_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, _, _, environment = self.prepare_workspace(root)
            documents = workspace / "documents"
            self.assertEqual(run("git", "-C", str(documents), "rm", "context/shared.txt").returncode, 0)
            self.assertEqual(run("git", "-C", str(documents), "commit", "-m", "delete source material").returncode, 0)

            blocked = run(
                sys.executable,
                str(ROOT / "scripts/repository-exchange.py"),
                "--root",
                str(workspace),
                "reverse-diff",
                env=environment,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("analytics-content-policy", blocked.stdout)
            self.assertIn("context/shared.txt", blocked.stdout)
            self.assertFalse((workspace / "reverse-diffs/reverse-diff-latest.json").exists())

            approved = run(
                sys.executable,
                str(ROOT / "scripts/repository-exchange.py"),
                "--root",
                str(workspace),
                "approve-deletion",
                "--path",
                "context/shared.txt",
                env=environment,
            )
            self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
            self.assertIn("explicit-analyst-approval", (
                workspace / ".workspace-state/exchange-deletion-approvals.json"
            ).read_text(encoding="utf-8"))

            created = run(
                sys.executable,
                str(ROOT / "scripts/repository-exchange.py"),
                "--root",
                str(workspace),
                "reverse-diff",
                env=environment,
            )
            self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
            metadata = json.loads((workspace / "reverse-diffs/reverse-diff-latest.json").read_text(encoding="utf-8"))
            self.assertTrue(metadata["verified"])
            self.assertEqual(metadata["changed_paths"], ["context/shared.txt"])
            self.assertEqual(metadata["approved_source_deletions"], ["context/shared.txt"])

    def test_sync_blocks_invalid_paths_introduced_by_source_before_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, source_work, _, environment = self.prepare_workspace(root)
            invalid = source_work / ".idea/modules.xml"
            invalid.parent.mkdir(parents=True)
            invalid.write_text("<project/>\n", encoding="utf-8")
            self.assertEqual(run("git", "-C", str(source_work), "add", "--", ".idea/modules.xml").returncode, 0)
            self.assertEqual(run("git", "-C", str(source_work), "commit", "-m", "invalid source path").returncode, 0)
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
            self.assertIn("source-content-policy", result.stdout)
            self.assertIn(".idea/modules.xml", result.stdout)
            self.assertFalse((workspace / "documents/.idea").exists())

    def test_exchange_aborts_conflicting_merge_without_overwriting_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace, source_work, _, environment = self.prepare_workspace(root)
            documents = workspace / "documents"
            (documents / "context/shared.txt").write_text("documents version\n", encoding="utf-8")
            self.assertEqual(run("git", "-C", str(documents), "add", "context/shared.txt").returncode, 0)
            self.assertEqual(run("git", "-C", str(documents), "commit", "-m", "documents conflict").returncode, 0)
            self.assertEqual(run("git", "-C", str(documents), "push", "origin", "main").returncode, 0)

            (source_work / "context/shared.txt").write_text("source version\n", encoding="utf-8")
            self.assertEqual(run("git", "-C", str(source_work), "add", "context/shared.txt").returncode, 0)
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
            self.assertEqual((documents / "context/shared.txt").read_text(encoding="utf-8"), "documents version\n")
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
            self.assertEqual(inspection["conflicts"][0]["path"], "context/shared.txt")
            self.assertEqual(inspection["conflicts"][0]["kind"], "both-modified")
            self.assertEqual(inspection["conflicts"][0]["recommended_resolution"], "analyst-decision-required")
            self.assertEqual(run("git", "-C", str(documents), "status", "--porcelain=v1").stdout, "")

    def test_bootstrap_retires_dirty_legacy_source_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_work, source_remote = self.create_seed(
                root,
                "changeswork-copy",
                {"README.md": "# Source\n", "context/shared.txt": "base\n"},
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

            self.assertEqual(run("git", "-C", str(analytics), "rm", "--", new_name).returncode, 0)
            self.assertEqual(run("git", "-C", str(analytics), "commit", "-m", "accidental normalized deletion").returncode, 0)
            blocked = run(
                sys.executable,
                str(ROOT / "scripts/repository-exchange.py"),
                "--root",
                str(workspace),
                "reverse-diff",
                env=environment,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("analytics-content-policy", blocked.stdout)
            self.assertIn(new_name, blocked.stdout)

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
                    "context/shared.txt": "base\n",
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
