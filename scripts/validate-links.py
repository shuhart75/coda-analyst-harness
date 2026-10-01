#!/usr/bin/env python3
from pathlib import Path
import re
import sys

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
missing = []
pattern = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
for md in root.rglob("*.md"):
    if "imported-source" in md.parts:
        continue
    if "legacy" in md.parts and "source-materials" in md.parts:
        continue
    text = md.read_text(encoding="utf-8", errors="ignore")
    for match in pattern.findall(text):
        if match.startswith("http://") or match.startswith("https://") or match.startswith("#"):
            continue
        clean = match.split("#", 1)[0]
        target = (md.parent / clean).resolve() if not Path(clean).is_absolute() else Path(clean)
        # Immutable snapshots retain links relative to their former current root.
        # Resolve only references leaving the snapshot; missing internal files fail.
        relative = md.relative_to(root).parts
        if not target.exists() and relative[:2] == ("baseline", "versions"):
            depth = 4 if len(relative) > 2 and relative[2] == "documentation" else 3
            snapshot = root.joinpath(*relative[:depth]).resolve()
            if not Path(clean).is_absolute() and not target.is_relative_to(snapshot):
                original = root / "baseline" / "current" / Path(*relative[depth:])
                target = (original.parent / clean).resolve()
                # This report moved out of the exchange-forbidden project root.
                # Only immutable snapshots may retain its historical root path.
                if not target.exists() and target == (root / "baseline-review.md").resolve():
                    target = root / "releases/baseline-reconciliation/baseline-review.md"
        if not target.exists():
            missing.append((md, match))
if missing:
    print('Potential missing references:')
    for source, target in missing[:200]:
        print(f'- {source}: {target}')
    sys.exit(1)
print('Link scan OK')
