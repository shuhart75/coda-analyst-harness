#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <target-content-repository> [--merge]"
  exit 1
fi

TARGET="$1"
MODE="${2:-create}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$MODE" != "create" && "$MODE" != "--merge" ]]; then
  echo "Unknown mode: $MODE"
  exit 1
fi
if [[ "$MODE" == "create" && -d "$TARGET" && -n "$(find "$TARGET" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  echo "Target is not empty: $TARGET"
  exit 1
fi

mkdir -p \
  "$TARGET/context/source-materials/current-system/requirements" \
  "$TARGET/context/source-materials/current-system/screenshots" \
  "$TARGET/context/source-materials/current-system/prototypes" \
  "$TARGET/context/source-materials/current-system/diagrams" \
  "$TARGET/context/source-materials/change-requests" \
  "$TARGET/context/current-system" \
  "$TARGET/context/change-requests" \
  "$TARGET/context/project-rules" \
  "$TARGET/context/evals" \
  "$TARGET/baseline/current/domain/state-machines" \
  "$TARGET/baseline/current/requirements" \
  "$TARGET/baseline/current/api" \
  "$TARGET/baseline/current/ui" \
  "$TARGET/baseline/current/data" \
  "$TARGET/baseline/current/decisions" \
  "$TARGET/baseline/versions" \
  "$TARGET/planning/intake" \
  "$TARGET/planning/approved-plans" \
  "$TARGET/features" \
  "$TARGET/releases"

copy_if_missing() {
  local source="$1"
  local destination="$2"
  [[ -e "$destination" ]] || cp "$source" "$destination"
}

copy_if_missing "$ROOT_DIR/templates/workflow/project-readme.template.md" "$TARGET/README.md"
copy_if_missing "$ROOT_DIR/templates/workflow/team.template.md" "$TARGET/planning/team.md"
copy_if_missing "$ROOT_DIR/templates/workflow/consistency-backlog.template.md" "$TARGET/planning/consistency-backlog.md"
copy_if_missing "$ROOT_DIR/templates/evals/golden-scenarios.template.json" "$TARGET/context/evals/golden-scenarios.json"

if [[ ! -e "$TARGET/context/project-rules/README.md" ]]; then
  cat > "$TARGET/context/project-rules/README.md" <<'EOF2'
# Правила проекта

Здесь хранятся только особенности конкретного продукта, которые дополняют внешнюю аналитическую обвязку. Исполняемые инструменты, режимы и шаблоны в этот репозиторий не копируются.
EOF2
fi

if [[ ! -e "$TARGET/baseline/current/VERSION.md" ]]; then
  cat > "$TARGET/baseline/current/VERSION.md" <<EOF2
# Baseline Version

Version: initial
Date: $(date +%F)
Source release: initial
Previous baseline: none
EOF2
fi
if [[ ! -e "$TARGET/baseline/current/domain/README.md" ]]; then
  printf '# Domain Baseline\n' > "$TARGET/baseline/current/domain/README.md"
fi
if [[ ! -e "$TARGET/baseline/current/domain/aggregates.md" ]]; then
  printf '# Aggregates\n' > "$TARGET/baseline/current/domain/aggregates.md"
fi
if [[ ! -e "$TARGET/baseline/current/requirements/README.md" ]]; then
  printf '# Canonical Requirements\n' > "$TARGET/baseline/current/requirements/README.md"
fi
if [[ ! -e "$TARGET/baseline/current/api/README.md" ]]; then
  printf '# Canonical API\n' > "$TARGET/baseline/current/api/README.md"
fi
if [[ ! -e "$TARGET/baseline/current/ui/README.md" ]]; then
  printf '# Canonical UI\n' > "$TARGET/baseline/current/ui/README.md"
fi
if [[ ! -e "$TARGET/baseline/current/data/README.md" ]]; then
  printf '# Canonical Data Model\n' > "$TARGET/baseline/current/data/README.md"
fi
if [[ ! -e "$TARGET/baseline/current/decisions/README.md" ]]; then
  printf '# Decisions\n' > "$TARGET/baseline/current/decisions/README.md"
fi
if [[ ! -e "$TARGET/releases/README.md" ]]; then
  printf '# Releases\n' > "$TARGET/releases/README.md"
fi
if [[ ! -e "$TARGET/planning/intake/README.md" ]]; then
  printf '# Входящие инициативы\n' > "$TARGET/planning/intake/README.md"
fi

for directory in \
  context/source-materials/current-system/requirements \
  context/source-materials/current-system/screenshots \
  context/source-materials/current-system/prototypes \
  context/source-materials/current-system/diagrams \
  context/source-materials/change-requests \
  context/current-system \
  context/change-requests \
  baseline/current/domain/state-machines \
  baseline/versions \
  planning/approved-plans \
  features; do
  if [[ -z "$(find "$TARGET/$directory" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    : > "$TARGET/$directory/.gitkeep"
  fi
done

for forbidden in .workflow .vscode AGENTS.md; do
  if [[ -e "$TARGET/$forbidden" ]]; then
    if [[ "$forbidden" == "AGENTS.md" ]] && grep -q "analyst-harness-local-entrypoint:v1" "$TARGET/$forbidden"; then
      continue
    fi
    echo "Embedded harness path is forbidden: $TARGET/$forbidden"
    exit 1
  fi
done

echo "Content repository scaffold created at $TARGET"
