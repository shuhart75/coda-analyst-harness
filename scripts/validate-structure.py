#!/usr/bin/env python3
from pathlib import Path
import sys


REQUIRED = [
    "README.md",
    "baseline/current/VERSION.md",
    "baseline/current/domain/aggregates.md",
    "baseline/current/domain",
    "baseline/current/requirements",
    "baseline/current/api",
    "baseline/current/ui",
    "baseline/current/data",
    "baseline/versions",
    "planning/intake",
    "planning/team.md",
    "planning/consistency-backlog.md",
    "planning/approved-plans",
    "context/source-materials/current-system/requirements",
    "context/source-materials/current-system/screenshots",
    "context/source-materials/change-requests",
    "context/project-rules",
    "context/evals/golden-scenarios.json",
    "features",
    "releases",
]
FORBIDDEN = [
    ".workflow",
    ".vscode",
    "AGENTS.md",
]


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    missing = [path for path in REQUIRED if not (root / path).exists()]
    embedded = [path for path in FORBIDDEN if (root / path).exists()]
    if missing:
        print("Missing required content paths:")
        for item in missing:
            print(f"- {item}")
    if embedded:
        print("Embedded harness paths are forbidden in the content repository:")
        for item in embedded:
            print(f"- {item}")
    if missing or embedded:
        return 1
    print("Content structure OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
