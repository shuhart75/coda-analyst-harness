#!/usr/bin/env python3
"""Install the pinned engine explicitly, without changing saved runs."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdd-adapter"))
from install import main
if __name__ == "__main__":
    raise SystemExit(main())
