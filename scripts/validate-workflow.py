#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

from workspace_paths import active_mode_path, ensure_local_state, harness_root


MODE_RE = re.compile(r"^mode:\s*([a-z0-9-]+)\s*$", re.MULTILINE)
MODE_FILE_RE = re.compile(r"^modes/([a-z0-9-]+)\.md\s*$", re.MULTILINE)
VALID_MODES = {
    "planning",
    "requirements",
    "scope-prototype",
    "delivery-prototype",
    "execution-update",
    "release-finalization",
}


def main() -> int:
    project = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    ensure_local_state()
    errors: list[str] = []
    active = active_mode_path()
    text = active.read_text(encoding="utf-8", errors="ignore")
    mode_match = MODE_RE.search(text)
    file_match = MODE_FILE_RE.search(text)
    if not mode_match:
        errors.append("local active-mode.md has no valid 'mode:' field")
    else:
        mode = mode_match.group(1)
        if mode not in VALID_MODES:
            errors.append(f"unknown active mode: {mode}")
        if not (harness_root() / f"modes/{mode}.md").is_file():
            errors.append(f"missing harness mode contract: modes/{mode}.md")
        if not file_match:
            errors.append("local active-mode.md has no valid mode-file path")
        elif file_match.group(1) != mode:
            errors.append(f"active mode mismatch: mode={mode}, mode-file={file_match.group(1)}")
    if not (project / ".git").exists():
        errors.append(f"documents is not a repository root: {project}")
    if errors:
        print("Workflow errors:")
        for item in errors:
            print(f"- {item}")
        return 1
    print("Workflow OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
