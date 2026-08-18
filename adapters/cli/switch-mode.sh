#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <project-root> <mode>"
  exit 1
fi

PROJECT_ROOT="$1"
MODE="$2"
MODE_FILE="$PROJECT_ROOT/.workflow/modes/${MODE}.md"
ACTIVE_FILE="$PROJECT_ROOT/.workflow/active-mode.md"

if [[ ! -f "$MODE_FILE" ]]; then
  echo "Mode file not found: $MODE_FILE"
  exit 1
fi

cat > "$ACTIVE_FILE" <<EOF2
# Active Mode

mode: $MODE

## Mode File
.workflow/modes/$MODE.md
EOF2

echo "Active mode set to '$MODE' in $PROJECT_ROOT"
