from __future__ import annotations

from pathlib import Path


MAPPING_SECTION_PREFIX = "## mapping"


def baseline_rows(path: Path) -> list[list[str]]:
    """Return approved-baseline rows from the ``## Mapping`` section only.

    The approved plan snapshot stores the ``Story ID``/``Baseline Start``/
    ``Baseline Duration`` triple for every mapping row. Later follow-up
    sections (for example ``## Q3 follow-up``) reuse the ``| STORY-`` marker
    with different column semantics, so they are not part of the approved
    baseline and must not be compared against the snapshot.
    """
    rows: list[list[str]] = []
    inside_mapping = False
    headers: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            inside_mapping = stripped.lower().startswith(MAPPING_SECTION_PREFIX)
            headers = []
            continue
        if inside_mapping and stripped.startswith("| Story ID"):
            headers = [cell.strip() for cell in stripped.strip("|").split("|")]
            continue
        if not inside_mapping or not stripped.startswith("| STORY-"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) >= 4:
            row = dict(zip(headers, cells))
            if row.get("Baseline State", "").strip("`").lower() == "absent" and not cells[2] and not cells[3]:
                continue
            rows.append([cells[0], cells[2], cells[3]])
    return rows
