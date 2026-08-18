#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <project-root>"
  exit 1
fi

PROJECT_ROOT="$1"
CONTRACT_FILE="$PROJECT_ROOT/.workflow/llm-contract.md"
AGENT_DELEGATION_FILE="$PROJECT_ROOT/.workflow/agent-delegation.md"
SKILLS_POLICY_FILE="$PROJECT_ROOT/.workflow/skills-policy.md"
TOOLING_POLICY_FILE="$PROJECT_ROOT/.workflow/tooling-policy.md"
CONSISTENCY_BACKLOG_FILE="$PROJECT_ROOT/.workflow/consistency-backlog.md"
COMMAND_CATALOG_FILE="$PROJECT_ROOT/.workflow/command-catalog.md"
COMMAND_CHEATSHEET_FILE="$PROJECT_ROOT/.workflow/command-cheatsheet.md"
REQUIREMENTS_TEMPLATE_README="$PROJECT_ROOT/.workflow/templates/requirements/README.md"
INTAKE_TEMPLATE_README="$PROJECT_ROOT/.workflow/templates/intake/README.md"
PROTOTYPES_TEMPLATE_README="$PROJECT_ROOT/.workflow/templates/prototypes/README.md"
FIND_STALE_TERMS_TOOL="$PROJECT_ROOT/.workflow/tools/find-stale-terms.py"
EXPAND_PLANTUML_INCLUDES_TOOL="$PROJECT_ROOT/.workflow/tools/expand-plantuml-includes.py"
BASELINE_DIR="$PROJECT_ROOT/baseline/current"
ACTIVE_FILE="$PROJECT_ROOT/.workflow/active-mode.md"
HARNESSCTL="$PROJECT_ROOT/.workflow/tools/harnessctl.py"

if [[ ! -f "$ACTIVE_FILE" ]]; then
  echo "Missing $ACTIVE_FILE"
  exit 1
fi

if [[ -f "$HARNESSCTL" ]]; then
  python3 "$HARNESSCTL" session-brief "$PROJECT_ROOT" >/dev/null
  echo "Generated progressive session brief:"
  echo "- $PROJECT_ROOT/.workflow/run-state/session-brief.md"
fi

echo "Read these files first:"
echo "- $PROJECT_ROOT/AGENTS.md"
if [[ -f "$CONTRACT_FILE" ]]; then
  echo "- $CONTRACT_FILE"
fi
if [[ -f "$AGENT_DELEGATION_FILE" ]]; then
  echo "- $AGENT_DELEGATION_FILE"
fi
if [[ -f "$SKILLS_POLICY_FILE" ]]; then
  echo "- $SKILLS_POLICY_FILE"
fi
if [[ -f "$TOOLING_POLICY_FILE" ]]; then
  echo "- $TOOLING_POLICY_FILE"
fi
if [[ -f "$CONSISTENCY_BACKLOG_FILE" ]]; then
  echo "- $CONSISTENCY_BACKLOG_FILE"
fi
if [[ -f "$COMMAND_CATALOG_FILE" ]]; then
  echo "- $COMMAND_CATALOG_FILE"
fi
if [[ -f "$COMMAND_CHEATSHEET_FILE" ]]; then
  echo "- $COMMAND_CHEATSHEET_FILE"
fi
if [[ -f "$REQUIREMENTS_TEMPLATE_README" ]]; then
  echo "- $REQUIREMENTS_TEMPLATE_README"
fi
if [[ -f "$INTAKE_TEMPLATE_README" ]]; then
  echo "- $INTAKE_TEMPLATE_README"
fi
if [[ -f "$PROTOTYPES_TEMPLATE_README" ]]; then
  echo "- $PROTOTYPES_TEMPLATE_README"
fi
if [[ -f "$FIND_STALE_TERMS_TOOL" ]]; then
  echo "- $FIND_STALE_TERMS_TOOL"
fi
if [[ -f "$EXPAND_PLANTUML_INCLUDES_TOOL" ]]; then
  echo "- $EXPAND_PLANTUML_INCLUDES_TOOL"
fi
echo "- $ACTIVE_FILE"
if [[ -d "$BASELINE_DIR" ]]; then
  echo "- $BASELINE_DIR"
fi
MODE=$(awk '/^mode:/ {print $2}' "$ACTIVE_FILE")
if [[ -n "$MODE" ]]; then
  echo "- $PROJECT_ROOT/.workflow/modes/${MODE}.md"
fi
if [[ -d "$PROJECT_ROOT/.workflow/overrides" ]]; then
  find "$PROJECT_ROOT/.workflow/overrides" -maxdepth 1 -type f | sort | sed 's/^/- /'
fi
