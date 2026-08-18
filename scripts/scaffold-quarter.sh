#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <project-root> <quarter-id> [--merge|--force]"
  exit 1
fi

PROJECT_ROOT="$1"
QUARTER="$2"
INSTALL_MODE="${3:-create}"
QUARTER_DIR="$PROJECT_ROOT/planning/$QUARTER"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -d "$QUARTER_DIR" && -n "$(find "$QUARTER_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" && "$INSTALL_MODE" == "create" ]]; then
  echo "Quarter already exists: $QUARTER_DIR"
  echo "Use --merge to add missing files or --force to overwrite scaffold files."
  exit 1
fi
if [[ "$INSTALL_MODE" != "create" && "$INSTALL_MODE" != "--merge" && "$INSTALL_MODE" != "--force" ]]; then
  echo "Unknown install mode: $INSTALL_MODE"
  exit 1
fi

should_write() {
  [[ "$INSTALL_MODE" != "--merge" || ! -e "$1" ]]
}

mkdir -p "$QUARTER_DIR/quarter" \
  "$QUARTER_DIR/gantt/preamble" \
  "$QUARTER_DIR/gantt/includes/quarter-plan" \
  "$QUARTER_DIR/gantt/includes/commander-plan" \
  "$QUARTER_DIR/gantt/includes/actual-progress"

if should_write "$QUARTER_DIR/quarter/README.md"; then
cat > "$QUARTER_DIR/quarter/README.md" <<EOF2
# $QUARTER

## Notes

## Scope Decisions

## Comparison Notes
EOF2
fi

if should_write "$QUARTER_DIR/gantt/closed-days.txt"; then
cat > "$QUARTER_DIR/gantt/closed-days.txt" <<EOF2
# One date per line. Format: YYYY/MM/DD
# Example:
# 2026/05/01
EOF2
fi

if should_write "$QUARTER_DIR/gantt/order.txt"; then
cat > "$QUARTER_DIR/gantt/order.txt" <<EOF2
# One feature slug per line, highest priority first.
EOF2
fi

if should_write "$QUARTER_DIR/gantt/preamble/common.puml"; then
cat > "$QUARTER_DIR/gantt/preamble/common.puml" <<EOF2
' Add team calendar blocks, shared milestones, or external dependency notes here.
EOF2
fi

if should_write "$QUARTER_DIR/plan-state.md"; then
  cp "$ROOT_DIR/templates/planning/quarter-plan-state.template.md" "$QUARTER_DIR/plan-state.md"
  sed -i "s/<YYYY-QN>/$QUARTER/g" "$QUARTER_DIR/plan-state.md"
fi
if should_write "$QUARTER_DIR/retrospective.md"; then
  cp "$ROOT_DIR/templates/planning/retrospective.template.md" "$QUARTER_DIR/retrospective.md"
  sed -i "s/<YYYY-QN>/$QUARTER/g" "$QUARTER_DIR/retrospective.md"
fi

python3 "$ROOT_DIR/scripts/sync-quarter-gantt.py" "$QUARTER_DIR/gantt"

echo "Quarter scaffold created at $QUARTER_DIR"
