#!/usr/bin/env python3
"""Compatibility entry point for the separately versioned SDD Orchestrator."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdd-adapter"))
from adapter import main
if __name__ == "__main__":
    raise SystemExit(main('platform_support.py'))
