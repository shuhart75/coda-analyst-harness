#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <project-root> <feature-slug> <slice-slug> [--merge|--force]"
  exit 1
fi

PROJECT_ROOT="$1"
FEATURE="$2"
SLICE="$3"
INSTALL_MODE="${4:-create}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SLICE_DIR="$PROJECT_ROOT/features/$FEATURE/slices/$SLICE"

if [[ -d "$SLICE_DIR" && -n "$(find "$SLICE_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" && "$INSTALL_MODE" == "create" ]]; then
  echo "Slice already exists: $SLICE_DIR"
  echo "Use --merge to add missing scaffold files or --force to overwrite them."
  exit 1
fi
if [[ "$INSTALL_MODE" != "create" && "$INSTALL_MODE" != "--merge" && "$INSTALL_MODE" != "--force" ]]; then
  echo "Unknown install mode: $INSTALL_MODE"
  exit 1
fi

install_file() {
  local source="$1" target="$2"
  if [[ "$INSTALL_MODE" == "--merge" && -e "$target" ]]; then
    return
  fi
  cp "$source" "$target"
}

REQUIREMENTS_TEMPLATE_DIR="$ROOT_DIR/templates/requirements"

mkdir -p "$SLICE_DIR/requirements" "$SLICE_DIR/delivery-prototype" "$SLICE_DIR/execution/tasks" "$SLICE_DIR/.research" "$SLICE_DIR/testing"
install_file "$REQUIREMENTS_TEMPLATE_DIR/slice.template.md" "$SLICE_DIR/slice.md"
install_file "$REQUIREMENTS_TEMPLATE_DIR/frontend.template.md" "$SLICE_DIR/requirements/frontend.md"
install_file "$REQUIREMENTS_TEMPLATE_DIR/backend.template.md" "$SLICE_DIR/requirements/backend.md"
install_file "$ROOT_DIR/templates/context/slice-context-summary.template.md" "$SLICE_DIR/context-summary.md"
install_file "$ROOT_DIR/templates/handoff/slice-implementation-handoff.template.md" "$SLICE_DIR/implementation-handoff.md"
install_file "$ROOT_DIR/templates/execution/implementation-plan.template.md" "$SLICE_DIR/execution/implementation-plan.md"
install_file "$ROOT_DIR/templates/execution/task-candidates.template.md" "$SLICE_DIR/execution/task-candidates.md"
install_file "$ROOT_DIR/templates/research/research-summary.template.md" "$SLICE_DIR/.research/summary.md"
install_file "$ROOT_DIR/templates/testing/slice-test-plan.template.md" "$SLICE_DIR/testing/test-plan.md"
install_file "$ROOT_DIR/templates/prototypes/delivery-prototype-notes.template.md" "$SLICE_DIR/delivery-prototype/notes.md"
install_file "$ROOT_DIR/templates/prototypes/prototype.html.template" "$SLICE_DIR/delivery-prototype/prototype.html"
install_file "$ROOT_DIR/templates/execution/tasks.template.md" "$SLICE_DIR/execution/tasks.md"

echo "Slice scaffold created at $SLICE_DIR"
