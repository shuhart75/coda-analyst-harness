#!/usr/bin/env bash
set -euo pipefail

HARNESS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT_ROOT="${1:-$HARNESS_ROOT/documents}"

python3 "$HARNESS_ROOT/scripts/harnessctl.py" session-brief "$PROJECT_ROOT" >/dev/null

echo "Read these files first:"
echo "- $HARNESS_ROOT/AGENTS.md"
echo "- $HARNESS_ROOT/core/llm-contract.md"
echo "- $HARNESS_ROOT/.workspace-state/active-mode.md"
MODE="$(awk '/^mode:/ {print $2}' "$HARNESS_ROOT/.workspace-state/active-mode.md")"
echo "- $HARNESS_ROOT/modes/${MODE}.md"
echo "- $PROJECT_ROOT/README.md"
echo "- $PROJECT_ROOT/planning/team.md"
echo "- $PROJECT_ROOT/planning/consistency-backlog.md"
if [[ -d "$PROJECT_ROOT/context/project-rules" ]]; then
  find "$PROJECT_ROOT/context/project-rules" -maxdepth 1 -type f | sort | sed 's/^/- /'
fi
echo "Generated session brief:"
echo "- $HARNESS_ROOT/.workspace-state/run-state/session-brief.md"
