#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <mode>"
  exit 1
fi

HARNESS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODE="$1"
MODE_FILE="$HARNESS_ROOT/modes/${MODE}.md"
ACTIVE_FILE="$HARNESS_ROOT/.workspace-state/active-mode.md"

if [[ ! -f "$MODE_FILE" ]]; then
  echo "Mode file not found: $MODE_FILE"
  exit 1
fi

mkdir -p "$(dirname "$ACTIVE_FILE")"
cat > "$ACTIVE_FILE" <<EOF2
# Active Mode

mode: $MODE

## Mode File
modes/$MODE.md
EOF2

echo "Active mode set to '$MODE'"
