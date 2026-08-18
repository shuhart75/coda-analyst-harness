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
        if not target.exists():
            missing.append((md, match))
if missing:
    print('Potential missing references:')
    for source, target in missing[:200]:
        print(f'- {source}: {target}')
    sys.exit(1)
print('Link scan OK')
