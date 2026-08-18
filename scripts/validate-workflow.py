#!/usr/bin/env python3
from __future__ import annotations

import json
import hashlib
import re
import sys
from pathlib import Path


MODE_RE = re.compile(r"^mode:\s*([a-z0-9-]+)\s*$", re.MULTILINE)
MODE_FILE_RE = re.compile(r"^\.workflow/modes/([a-z0-9-]+)\.md\s*$", re.MULTILINE)
VALID_MODES = {
    "planning",
    "requirements",
    "scope-prototype",
    "delivery-prototype",
    "execution-update",
    "release-finalization",
}


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    errors: list[str] = []
    warnings: list[str] = []

    active = root / ".workflow/active-mode.md"
    if not active.exists():
        errors.append("missing .workflow/active-mode.md")
    else:
        text = active.read_text(encoding="utf-8", errors="ignore")
        mode_match = MODE_RE.search(text)
        file_match = MODE_FILE_RE.search(text)
        if not mode_match:
            errors.append("active-mode.md has no valid 'mode:' field")
        else:
            mode = mode_match.group(1)
            if mode not in VALID_MODES:
                errors.append(f"unknown active mode: {mode}")
            if not (root / f".workflow/modes/{mode}.md").is_file():
                errors.append(f"missing mode contract: .workflow/modes/{mode}.md")
            if not file_match:
                errors.append("active-mode.md has no valid mode-file path")
            elif file_match.group(1) != mode:
                errors.append(
                    "active mode mismatch: "
                    f"mode={mode}, mode-file={file_match.group(1)}"
                )

    manifest_path = root / ".workflow/harness.json"
    if not manifest_path.exists():
        errors.append("missing .workflow/harness.json")
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid .workflow/harness.json: {exc}")
        else:
            if manifest.get("schema_version") != 1:
                errors.append("unsupported harness manifest schema")
            if not manifest.get("harness_version"):
                errors.append("manifest has no harness_version")
            for rel, metadata in manifest.get("managed_files", {}).items():
                target = root / rel
                if not target.exists():
                    errors.append(f"managed file is missing: {rel}")
                    continue
                actual = hashlib.sha256(target.read_bytes()).hexdigest()
                if actual != metadata.get("installed_sha256"):
                    errors.append(f"managed file changed outside harness upgrade: {rel}")

    readme = root / "README.md"
    if readme.exists():
        first_line = readme.read_text(encoding="utf-8", errors="ignore").splitlines()[:1]
        if root.name != "analyst-harness" and first_line == ["# analyst-harness"]:
            warnings.append("project README still describes analyst-harness")

    if errors:
        print("Workflow errors:")
        for item in errors:
            print(f"- {item}")
    if warnings:
        print("Workflow warnings:")
        for item in warnings:
            print(f"- {item}")
    if not errors and not warnings:
        print("Workflow OK")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
