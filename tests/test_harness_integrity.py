from __future__ import annotations

import os
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HarnessIntegrityTests(unittest.TestCase):
    HARNESS_TEXT_SUFFIXES = {".md", ".json", ".yaml", ".yml", ".sh", ".py", ".txt", ".puml", ".template", ""}
    HARNESS_OWNED_PREFIXES = ("scripts/", "core/", "modes/", "templates/", "adapters/", "skills/", "examples/", "prompts/")

    def harness_text_corpus(self) -> dict[str, str]:
        corpus: dict[str, str] = {}
        candidates = [ROOT / "AGENTS.md", ROOT / "README.md"]
        for directory in (*self.HARNESS_OWNED_PREFIXES, "tests", ".github"):
            for parent, directories, filenames in os.walk(ROOT / directory):
                directories[:] = [name for name in directories if not name.startswith(".") and name not in {"__pycache__", "node_modules"}]
                candidates.extend(Path(parent) / name for name in filenames)
        for candidate in sorted(candidates):
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(ROOT)
            if candidate.suffix not in self.HARNESS_TEXT_SUFFIXES:
                continue
            try:
                corpus[str(relative)] = candidate.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
        return corpus

    def test_every_harness_script_is_referenced_somewhere(self) -> None:
        corpus = self.harness_text_corpus()
        unreferenced: list[str] = []
        for script in sorted((ROOT / "scripts").glob("*.py")):
            relative = str(script.relative_to(ROOT))
            pattern = re.compile(r"\b" + re.escape(script.name[:-3]) + r"(?:\.py)?\b")
            if not any(body for source, body in corpus.items() if source != relative and pattern.search(body)):
                unreferenced.append(relative)
        self.assertEqual(unreferenced, [], "scripts reachable from no document, test or other script")

    def test_rule_documents_reference_existing_harness_paths(self) -> None:
        corpus = self.harness_text_corpus()
        pattern = re.compile(
            r"(?<![\w/.-])(?:"
            + "|".join(re.escape(prefix) for prefix in self.HARNESS_OWNED_PREFIXES)
            + r")[A-Za-z0-9_.\-/]*[A-Za-z0-9_.\-]"
        )
        stale: dict[str, list[str]] = {}
        for source, body in corpus.items():
            if not source.endswith(".md"):
                continue
            for token in pattern.findall(body):
                token = token.rstrip(".,;:")
                if any(marker in token for marker in ("<", "*", "{", "$")):
                    continue
                if (ROOT / token).exists():
                    continue
                stale.setdefault(token, []).append(source)
        self.assertEqual(stale, {}, "harness-owned paths that no longer exist")

    def test_every_core_rule_document_is_reachable_from_the_contract(self) -> None:
        contract = (ROOT / "AGENTS.md").read_text(encoding="utf-8") + (ROOT / "core/llm-contract.md").read_text(encoding="utf-8")
        unreachable = [
            str(path.relative_to(ROOT))
            for path in sorted((ROOT / "core").glob("*.md"))
            if f"`core/{path.name}`" not in contract
        ]
        self.assertEqual(unreachable, [], "core rule documents that no reading list or contract section names")


if __name__ == "__main__":
    unittest.main()
