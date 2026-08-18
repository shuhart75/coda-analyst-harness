#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import re
import sys


INCLUDE_RE = re.compile(r"^\s*!include\s+(.+?)\s*$")


def usage() -> None:
    print("Usage: expand-plantuml-includes.py <input.puml> <output.puml>")


def clean_include_target(value: str) -> str:
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def expand_file(path: Path, stack: list[Path]) -> list[str]:
    resolved = path.resolve()
    if resolved in stack:
        cycle = " -> ".join(str(item) for item in stack + [resolved])
        raise ValueError(f"PlantUML include cycle detected: {cycle}")

    lines: list[str] = []
    next_stack = stack + [resolved]
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        match = INCLUDE_RE.match(raw_line)
        if not match:
            lines.append(raw_line)
            continue

        include_target = clean_include_target(match.group(1))
        include_path = (path.parent / include_target).resolve()
        if not include_path.exists():
            raise FileNotFoundError(f"Included file not found: {include_target} from {path}")

        marker = include_path.relative_to(path.parent).as_posix() if include_path.is_relative_to(path.parent) else str(include_path)
        lines.append(f"' BEGIN INCLUDE: {marker}")
        lines.extend(expand_file(include_path, next_stack))
        lines.append(f"' END INCLUDE: {marker}")

    return lines


def main() -> int:
    if len(sys.argv) != 3:
        usage()
        return 1

    source = Path(sys.argv[1])
    target = Path(sys.argv[2])

    if not source.exists():
        print(f"Input file not found: {source}")
        return 1

    expanded = expand_file(source, [])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(expanded).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
