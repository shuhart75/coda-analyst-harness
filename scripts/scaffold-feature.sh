#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <project-root> <feature-slug> [--merge|--force]"
  exit 1
fi

PROJECT_ROOT="$1"
FEATURE="$2"
INSTALL_MODE="${3:-create}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FEATURE_DIR="$PROJECT_ROOT/features/$FEATURE"

if [[ -d "$FEATURE_DIR" && -n "$(find "$FEATURE_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" && "$INSTALL_MODE" == "create" ]]; then
  echo "Feature already exists: $FEATURE_DIR"
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

mkdir -p "$FEATURE_DIR/planning/stories" "$FEATURE_DIR/planning/scope-prototype" "$FEATURE_DIR/slices"
install_file "$ROOT_DIR/templates/planning/feature.template.md" "$FEATURE_DIR/feature.md"
install_file "$ROOT_DIR/templates/planning/estimates.template.md" "$FEATURE_DIR/planning/estimates.md"
install_file "$ROOT_DIR/templates/planning/actualization.template.md" "$FEATURE_DIR/planning/actualization.md"
install_file "$ROOT_DIR/templates/planning/planning-context.template.md" "$FEATURE_DIR/planning/planning-context.md"
install_file "$ROOT_DIR/templates/planning/assumptions.template.md" "$FEATURE_DIR/planning/assumptions.md"
install_file "$ROOT_DIR/templates/planning/risk-register.template.md" "$FEATURE_DIR/planning/risk-register.md"
install_file "$ROOT_DIR/templates/planning/story-map.template.md" "$FEATURE_DIR/planning/story-map.md"
install_file "$ROOT_DIR/templates/context/feature-context-summary.template.md" "$FEATURE_DIR/context-summary.md"
install_file "$ROOT_DIR/templates/context/artifact-map.template.md" "$FEATURE_DIR/artifact-map.md"
install_file "$ROOT_DIR/templates/domain/domain-impact.template.md" "$FEATURE_DIR/domain-impact.md"
install_file "$ROOT_DIR/templates/prototypes/scope-prototype-notes.template.md" "$FEATURE_DIR/planning/scope-prototype/notes.md"
install_file "$ROOT_DIR/templates/prototypes/prototype.html.template" "$FEATURE_DIR/planning/scope-prototype/prototype.html"
if [[ "$INSTALL_MODE" != "--merge" || ! -e "$FEATURE_DIR/references.md" ]]; then
cat > "$FEATURE_DIR/references.md" <<EOF2
# References

- baseline/current/
- context/source-materials/current-system/requirements/
- context/source-materials/current-system/screenshots/
- context/source-materials/change-requests/
EOF2
fi

python3 "$ROOT_DIR/scripts/requirementsctl.py" init "$PROJECT_ROOT" "$FEATURE" >/dev/null

echo "Feature scaffold created at $FEATURE_DIR"
