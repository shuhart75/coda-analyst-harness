import json
import os
from pathlib import Path
import tempfile
import unittest

import policy


class PermissionsTests(unittest.TestCase):
    def test_merge_preserves_constraints_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            directory = root / ".gigacode"
            directory.mkdir()
            path = directory / "settings.json"
            original = {"permissions": {"allow": ["Read"], "ask": ["Skill"], "deny": ["Bash"]},
                        "mcpServers": {"x": {"secret": "not-for-report"}}}
            before = json.dumps(original).encode()
            path.write_bytes(before)
            result = policy.prepare_permissions(root)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["permissions"]["ask"], ["Skill"])
            self.assertEqual(saved["permissions"]["deny"], ["Bash"])
            self.assertEqual(saved["mcpServers"], original["mcpServers"])
            self.assertNotIn("not-for-report", json.dumps(result))
            self.assertEqual(path.with_name("settings.json.before-sdd-runner").read_bytes(), before)
            if os.name != 'nt':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(result["requires_preflight"])
            self.assertEqual(policy.prepare_permissions(root)["added"], [])

    def test_contour_reports_parent_restrictions_without_modifying_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".gigacode").mkdir()
            parent = root / ".gigacode/settings.json"
            parent.write_text('{"permissions":{"deny":["Write"]}}', encoding="utf-8")
            before = parent.read_bytes()
            contour = root / "backend"
            contour.mkdir()
            report = policy.prepare_permissions(contour, "review")
            self.assertEqual(parent.read_bytes(), before)
            self.assertEqual(report["restrictions"][0]["deny_count"], 1)
            self.assertNotIn("Write", json.loads((contour / ".gigacode/settings.json").read_text(encoding="utf-8"))["permissions"]["allow"])

    def test_invalid_and_symlink_settings_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".gigacode").mkdir()
            path = root / ".gigacode/settings.json"
            for invalid in ("broken", "[]", '{"permissions":{"ask":"Bash"}}'):
                path.write_text(invalid, encoding="utf-8")
                with self.assertRaises(ValueError):
                    policy.prepare_permissions(root)
                self.assertEqual(path.read_text(encoding="utf-8"), invalid)
            path.unlink()
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            try:
                path.symlink_to(target)
            except OSError:
                if os.name != 'nt':
                    raise
                self.skipTest('Symlink creation requires Windows developer mode or privilege')
            with self.assertRaises(ValueError):
                policy.prepare_permissions(root)
            self.assertEqual(target.read_text(encoding="utf-8"), "{}")


class NexusTests(unittest.TestCase):
    def test_explicit_environment_and_no_fallback(self):
        env = policy.nexus_environment({"nexus_hosts": ["nexus.example"],
                                       "npm_registry": "https://nexus.example/npm/",
                                       "pip_index_url": "https://nexus.example/pypi/simple"},
                                      {"PIP_EXTRA_INDEX_URL": "https://pypi.org/simple", "OTHER": "kept"})
        self.assertEqual(env["PIP_EXTRA_INDEX_URL"], "")
        self.assertEqual(env["NPM_CONFIG_REGISTRY"], "https://nexus.example/npm/")
        self.assertEqual(env["OTHER"], "kept")

    def test_no_registry_disables_public_defaults_and_config_fallbacks(self):
        env = policy.nexus_environment({"nexus_hosts": ["nexus.example"]}, {
            "pip_extra_index_url": "https://pypi.org/simple",
            "pip_config_file": "/tmp/pip-public.conf",
            "pip_find_links": "https://public.example/wheels"})
        self.assertEqual(env["PIP_CONFIG_FILE"], os.devnull)
        self.assertEqual(env["PIP_FIND_LINKS"], "")
        self.assertEqual(env["PIP_NO_INDEX"], "1")
        self.assertEqual(env["NPM_CONFIG_OFFLINE"], "true")
        self.assertNotIn("pip_config_file", env)
        self.assertNotIn("pip_extra_index_url", env)

    def test_credentials_and_host_suffixes_rejected_without_echo(self):
        for url in ("https://nexus.example.evil/npm", "https://u:secret@nexus.example/npm",
                    "http://nexus.example/npm", "https://registry.npmjs.org"):
            with self.assertRaises(ValueError) as error:
                policy.nexus_environment({"nexus_hosts": ["nexus.example"], "npm_registry": url}, {})
            self.assertNotIn("secret", str(error.exception))

    def test_downloads_only_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package-lock.json").write_text(json.dumps({"packages": {
                "node_modules/a": {"resolved": "https://nexus.example/npm/a.tgz"},
                "node_modules/b": {"resolved": "https://user:SECRET@registry.npmjs.org/b.tgz"}}}), encoding="utf-8")
            (root / ".npmrc").write_text("registry=https://nexus.example/npm/\n@x:registry=https://evil.example/npm/\n//nexus.example/:_auth=SECRET\n", encoding="utf-8")
            (root / "requirements.txt").write_text("# docs https://example.org\n--index-url https://nexus.example/pypi/simple\na @ https://pypi.org/a.whl\n", encoding="utf-8")
            (root / "pom.xml").write_text('''<project xmlns="http://maven.apache.org/POM/4.0.0"><url>https://docs.example</url><repositories><repository><url>https://nexus.example/maven/</url></repository></repositories></project>''', encoding="utf-8")
            findings = policy.audit_dependency_sources(root, ["nexus.example"])
            self.assertEqual(len(findings), 3)
            self.assertNotIn("SECRET", json.dumps(findings))
            self.assertFalse(any(f["path"] == "pom.xml" for f in findings))

    def test_manifest_direct_downloads_and_git_shorthand_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({
                "homepage": "https://docs.example",
                "dependencies": {"a": "https://public.example/a.tgz",
                                 "b": "owner/repo#main", "c": "github:owner/repo",
                                 "d": "^1.2.3", "e": "workspace:*",
                                 "f": "https://nexus.example/npm/f.tgz"}}), encoding="utf-8")
            findings = policy.audit_dependency_sources(root, ["nexus.example"])
            self.assertEqual(len(findings), 3)
            self.assertTrue(all(f["code"] == "non-nexus-download" for f in findings))

    def test_maven_download_urls_and_unresolved_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "settings.xml").write_text('''<settings><mirrors><mirror><url>https://repo.maven.apache.org/maven2</url></mirror></mirrors></settings>''', encoding="utf-8")
            (root / ".npmrc").write_text("registry=${UNKNOWN_REGISTRY}\n", encoding="utf-8")
            findings = policy.audit_dependency_sources(root, ["nexus.example"])
            self.assertEqual({f["code"] for f in findings}, {"non-nexus-download", "unresolved-download-url"})

    def test_installed_dependencies_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "node_modules/a"
            nested.mkdir(parents=True)
            (nested / ".npmrc").write_text("registry=https://registry.npmjs.org", encoding="utf-8")
            self.assertEqual(policy.audit_dependency_sources(root, ["nexus.example"]), [])

    def test_prompt_states_real_limits(self):
        text = policy.make_policy_prompt(["nexus.example"], "review")
        self.assertIn("nexus.example", text)
        self.assertIn("не являются", text)
        self.assertIn("Не меняй исходники", text)


if __name__ == "__main__":
    unittest.main()
