from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from gigacode_workspace import BLOCK_START, MANIFEST, install_gigacode_workspace
from workspace_entrypoint import exclude_local_entrypoint


class GigaCodeWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "analytics"
        self.project.mkdir()
        subprocess.run(("git", "init", "-q", str(self.project)), check=True)
        exclude_local_entrypoint(self.project)

    def snapshot(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.project)): path.read_bytes()
            for path in self.project.rglob("*") if path.is_file() and ".git" not in path.parts
        }

    def test_install_preserves_memory_and_settings_and_is_idempotent(self) -> None:
        memory = self.project / "GIGACODE.md"
        memory.write_text("# GigaCode Added Memories\nДоменный факт.\n", encoding="utf-8")
        settings = self.project / ".gigacode/settings.json"
        settings.parent.mkdir()
        settings.write_text('{"permissions":{"allow":[]}}\n', encoding="utf-8")
        install_gigacode_workspace(self.project, ROOT)
        memory.write_text(memory.read_text(encoding="utf-8") + "\nНовая память.\n", encoding="utf-8")
        installed = self.snapshot()
        install_gigacode_workspace(self.project, ROOT)
        self.assertEqual(self.snapshot(), installed)
        self.assertIn("Доменный факт.", memory.read_text(encoding="utf-8"))
        self.assertIn("Новая память.", memory.read_text(encoding="utf-8"))
        self.assertEqual(memory.read_text(encoding="utf-8").count(BLOCK_START), 1)
        self.assertIn(str(ROOT), memory.read_text(encoding="utf-8"))
        self.assertEqual(subprocess.run(
            ("git", "-C", str(self.project), "status", "--porcelain"),
            text=True, capture_output=True, check=True,
        ).stdout, "")

    def test_documented_routes_resolve_to_shipped_commands_skills_and_contracts(self) -> None:
        routes = []
        for line in (ROOT / "GIGACODE.md").read_text().splitlines():
            if line.startswith("| `/"):
                cells = [cell.strip() for cell in line.strip("|").split("|")]
                command = cells[0].strip("`").removeprefix("/")
                skill = re.fullmatch(r"`(coda-[a-z-]+)`", cells[1])
                contract = cells[2].strip("`")
                self.assertTrue((ROOT / ".gigacode/commands" / f"{command}.md").is_file())
                self.assertTrue((ROOT / contract).is_file())
                if skill:
                    name = skill.group(1)
                    text = (ROOT / ".gigacode/skills" / name / "SKILL.md").read_text()
                    self.assertRegex(text, rf"(?m)^name: {re.escape(name)}$")
                routes.append(command)
        shipped = {p.stem for p in (ROOT / ".gigacode/commands").glob("*.md")}
        self.assertEqual(set(routes), shipped)
        self.assertEqual(len(routes), len(shipped))

    def test_upgrade_adds_repository_skill_and_preserves_user_files(self) -> None:
        distribution = Path(self.temp.name) / "old-distribution"
        shutil.copytree(ROOT / ".gigacode", distribution / ".gigacode")
        relative = ".gigacode/skills/coda-repositories/SKILL.md"
        shutil.rmtree(distribution / ".gigacode/skills/coda-repositories")
        with patch("gigacode_workspace.BUNDLE_ROOT", distribution):
            install_gigacode_workspace(self.project, ROOT)
        self.assertFalse((self.project / relative).exists())
        user = self.project / ".gigacode/skills/custom/SKILL.md"
        user.parent.mkdir(parents=True)
        user.write_text("user-owned\n")
        install_gigacode_workspace(self.project, ROOT)
        self.assertEqual((self.project / relative).read_bytes(), (ROOT / relative).read_bytes())
        manifest = json.loads((self.project / MANIFEST).read_text())
        self.assertIn(relative, manifest["files"])
        self.assertEqual(user.read_text(), "user-owned\n")
        installed = self.snapshot()
        install_gigacode_workspace(self.project, ROOT)
        self.assertEqual(installed, self.snapshot())
        self.assertEqual(subprocess.run(
            ("git", "-C", str(self.project), "status", "--porcelain"),
            text=True, capture_output=True, check=True,
        ).stdout, "")

    def test_existing_command_conflict_has_no_partial_writes(self) -> None:
        command = self.project / ".gigacode/commands/tracker.md"
        command.parent.mkdir(parents=True)
        command.write_text("user command\n", encoding="utf-8")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "Конфликт"):
            install_gigacode_workspace(self.project, ROOT)
        self.assertEqual(self.snapshot(), before)

    def test_modified_projection_blocks_upgrade_without_partial_writes(self) -> None:
        install_gigacode_workspace(self.project, ROOT)
        command = self.project / ".gigacode/commands/tracker.md"
        command.write_text("modified locally\n", encoding="utf-8")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "Локально изменена"):
            install_gigacode_workspace(self.project, ROOT)
        self.assertEqual(self.snapshot(), before)

    def test_existing_empty_user_file_is_also_a_conflict(self) -> None:
        command = self.project / ".gigacode/commands/tracker.md"
        command.parent.mkdir(parents=True)
        command.touch()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "Конфликт"):
            install_gigacode_workspace(self.project, ROOT)
        self.assertEqual(self.snapshot(), before)

    def test_edited_managed_rule_block_is_preserved_on_failure(self) -> None:
        install_gigacode_workspace(self.project, ROOT)
        memory = self.project / "GIGACODE.md"
        memory.write_text(memory.read_text(encoding="utf-8").replace("Общайся по-русски", "Custom rule"), encoding="utf-8")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "Локально изменена"):
            install_gigacode_workspace(self.project, ROOT)
        self.assertEqual(self.snapshot(), before)

    def test_tracked_target_is_never_overwritten(self) -> None:
        memory = self.project / "GIGACODE.md"
        memory.write_text("tracked rule\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(self.project), "add", "-f", "--", "GIGACODE.md"), check=True)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "неотслеживаемых"):
            install_gigacode_workspace(self.project, ROOT)
        self.assertEqual(self.snapshot(), before)

    def test_symlink_cannot_write_outside_analytics(self) -> None:
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.project / ".gigacode").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            install_gigacode_workspace(self.project, ROOT)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.project / "GIGACODE.md").exists())

    def test_upgrade_replaces_owned_files_and_removes_only_retired_owned_files(self) -> None:
        install_gigacode_workspace(self.project, ROOT)
        distribution = Path(self.temp.name) / "distribution"
        command = distribution / ".gigacode/commands/tracker.md"
        command.parent.mkdir(parents=True)
        command.write_text("new command\n", encoding="utf-8")
        user_command = self.project / ".gigacode/commands/custom.md"
        user_command.write_text("user-owned\n", encoding="utf-8")
        with patch("gigacode_workspace.BUNDLE_ROOT", distribution):
            install_gigacode_workspace(self.project, ROOT)
        self.assertEqual((self.project / ".gigacode/commands/tracker.md").read_text(), "new command\n")
        self.assertFalse((self.project / ".gigacode/commands/planning.md").exists())
        self.assertEqual(user_command.read_text(), "user-owned\n")
        manifest = json.loads((self.project / MANIFEST).read_text())
        self.assertEqual(set(manifest["files"]), {"GIGACODE.md", ".gigacode/commands/tracker.md"})

    def test_manifest_cannot_claim_unrelated_project_file(self) -> None:
        manifest = self.project / MANIFEST
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({"schema_version": 1, "files": {"requirements.md": "abc"}}))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "Недопустимые пути"):
            install_gigacode_workspace(self.project, ROOT)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
