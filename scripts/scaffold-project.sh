#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <target-project-dir> [--merge|--force]"
  exit 1
fi

TARGET="$1"
INSTALL_MODE="${2:-create}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$INSTALL_MODE" == "--merge" ]]; then
  TMP_TARGET="$(mktemp -d /tmp/analyst-harness-scaffold.XXXXXX)"
  trap 'rm -rf "$TMP_TARGET"' EXIT
  bash "$0" "$TMP_TARGET/project"
  mkdir -p "$TARGET"
  mkdir -p "$TARGET/.workflow" "$TARGET/.vscode"
  cp -an "$TMP_TARGET/project/.workflow/." "$TARGET/.workflow/"
  cp -an "$TMP_TARGET/project/.vscode/." "$TARGET/.vscode/"
  [[ -e "$TARGET/AGENTS.md" ]] || cp "$TMP_TARGET/project/AGENTS.md" "$TARGET/AGENTS.md"
  [[ -e "$TARGET/README.md" ]] || cp "$TMP_TARGET/project/README.md" "$TARGET/README.md"
  echo "Harness runtime merged into $TARGET without touching project knowledge directories"
  exit 0
fi

if [[ "$INSTALL_MODE" != "create" && "$INSTALL_MODE" != "--force" ]]; then
  echo "Unknown install mode: $INSTALL_MODE"
  exit 1
fi

if [[ -d "$TARGET" && -n "$(find "$TARGET" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" && "$INSTALL_MODE" != "--force" ]]; then
  echo "Target is not empty: $TARGET"
  echo "Use --merge to add missing files or --force to overwrite managed scaffold files."
  exit 1
fi

mkdir -p "$TARGET/.workflow/modes" "$TARGET/.workflow/overrides" "$TARGET/.workflow/tools" "$TARGET/.workflow/templates" "$TARGET/.workflow/run-state" "$TARGET/.vscode"
mkdir -p "$TARGET/.workflow/skills" "$TARGET/.workflow/runs"
mkdir -p "$TARGET/.workflow/evals"
mkdir -p "$TARGET/context/source-materials/current-system/requirements" "$TARGET/context/source-materials/current-system/screenshots" "$TARGET/context/source-materials/current-system/prototypes" "$TARGET/context/source-materials/current-system/diagrams" "$TARGET/context/source-materials/change-requests"
mkdir -p "$TARGET/context/current-system" "$TARGET/context/change-requests"
mkdir -p "$TARGET/baseline/current/domain/state-machines" "$TARGET/baseline/current/requirements" "$TARGET/baseline/current/api" "$TARGET/baseline/current/ui" "$TARGET/baseline/current/data" "$TARGET/baseline/current/decisions" "$TARGET/baseline/versions"
mkdir -p "$TARGET/planning/intake" "$TARGET/features" "$TARGET/releases"

cp "$ROOT_DIR/templates/workflow/project-agents.template.md" "$TARGET/AGENTS.md"
cp "$ROOT_DIR/core/llm-contract.md" "$TARGET/.workflow/llm-contract.md"
cp "$ROOT_DIR/core/agent-delegation.md" "$TARGET/.workflow/agent-delegation.md"
cp "$ROOT_DIR/core/skills-policy.md" "$TARGET/.workflow/skills-policy.md"
cp "$ROOT_DIR/core/tooling-policy.md" "$TARGET/.workflow/tooling-policy.md"
cp "$ROOT_DIR/core/context-policy.md" "$TARGET/.workflow/context-policy.md"
cp "$ROOT_DIR/core/research-policy.md" "$TARGET/.workflow/research-policy.md"
cp "$ROOT_DIR/core/code-inspection.md" "$TARGET/.workflow/code-inspection.md"
cp "$ROOT_DIR/core/run-loop.md" "$TARGET/.workflow/run-loop.md"
cp "$ROOT_DIR/core/developer-handoff.md" "$TARGET/.workflow/developer-handoff.md"
cp "$ROOT_DIR/core/requirements-profile.md" "$TARGET/.workflow/requirements-profile.md"
cp "$ROOT_DIR/adapters/cli/switch-mode.sh" "$TARGET/.workflow/tools/switch-mode.sh"
cp "$ROOT_DIR/adapters/cli/start-session.sh" "$TARGET/.workflow/tools/start-session.sh"
cp "$ROOT_DIR/scripts/validate-structure.py" "$TARGET/.workflow/tools/validate-structure.py"
cp "$ROOT_DIR/scripts/validate-links.py" "$TARGET/.workflow/tools/validate-links.py"
cp "$ROOT_DIR/scripts/validate-context.py" "$TARGET/.workflow/tools/validate-context.py"
cp "$ROOT_DIR/scripts/validate-workflow.py" "$TARGET/.workflow/tools/validate-workflow.py"
cp "$ROOT_DIR/scripts/validate-planning.py" "$TARGET/.workflow/tools/validate-planning.py"
cp "$ROOT_DIR/scripts/validate-trace.py" "$TARGET/.workflow/tools/validate-trace.py"
cp "$ROOT_DIR/scripts/validate-language.py" "$TARGET/.workflow/tools/validate-language.py"
cp "$ROOT_DIR/scripts/validate-requirements-profile.py" "$TARGET/.workflow/tools/validate-requirements-profile.py"
cp "$ROOT_DIR/scripts/validate-handoff.py" "$TARGET/.workflow/tools/validate-handoff.py"
cp "$ROOT_DIR/scripts/handoffctl.py" "$TARGET/.workflow/tools/handoffctl.py"
cp "$ROOT_DIR/scripts/code-inspect.py" "$TARGET/.workflow/tools/code-inspect.py"
cp "$ROOT_DIR/scripts/harnessctl.py" "$TARGET/.workflow/tools/harnessctl.py"
cp "$ROOT_DIR/scripts/evaluate-harness.py" "$TARGET/.workflow/tools/evaluate-harness.py"
cp "$ROOT_DIR/scripts/sync-quarter-gantt.py" "$TARGET/.workflow/tools/sync-quarter-gantt.py"
cp "$ROOT_DIR/scripts/sync-planning-gantt.py" "$TARGET/.workflow/tools/sync-planning-gantt.py"
cp "$ROOT_DIR/scripts/calibrate-planning.py" "$TARGET/.workflow/tools/calibrate-planning.py"
cp "$ROOT_DIR/scripts/sync-actual-progress-overlay.py" "$TARGET/.workflow/tools/sync-actual-progress-overlay.py"
cp "$ROOT_DIR/scripts/find-stale-terms.py" "$TARGET/.workflow/tools/find-stale-terms.py"
cp "$ROOT_DIR/scripts/expand-plantuml-includes.py" "$TARGET/.workflow/tools/expand-plantuml-includes.py"
chmod +x "$TARGET/.workflow/tools/switch-mode.sh"
chmod +x "$TARGET/.workflow/tools/start-session.sh"
chmod +x "$TARGET/.workflow/tools/find-stale-terms.py"
chmod +x "$TARGET/.workflow/tools/expand-plantuml-includes.py"
chmod +x "$TARGET/.workflow/tools/validate-handoff.py"
chmod +x "$TARGET/.workflow/tools/validate-requirements-profile.py"
chmod +x "$TARGET/.workflow/tools/handoffctl.py"
chmod +x "$TARGET/.workflow/tools/code-inspect.py"
cp "$ROOT_DIR/adapters/vscodium/settings.json" "$TARGET/.vscode/settings.json"
cp "$ROOT_DIR/adapters/vscodium/tasks.json" "$TARGET/.vscode/tasks.json"
cp "$ROOT_DIR/adapters/vscodium/snippets.code-snippets" "$TARGET/.vscode/workflow.code-snippets"
for template_dir in context evals execution handoff intake planning prototypes requirements research runs testing workspace; do
  if [[ -d "$ROOT_DIR/templates/$template_dir" ]]; then
    mkdir -p "$TARGET/.workflow/templates/$template_dir"
    cp -R "$ROOT_DIR/templates/$template_dir/." "$TARGET/.workflow/templates/$template_dir/"
  fi
done

cp -R "$ROOT_DIR/skills/." "$TARGET/.workflow/skills/"

for mode in planning requirements scope-prototype delivery-prototype execution-update release-finalization; do
  cp "$ROOT_DIR/modes/${mode}.md" "$TARGET/.workflow/modes/${mode}.md"
done

cat > "$TARGET/.workflow/active-mode.md" <<EOF2
# Active Mode

mode: planning

## Mode File
.workflow/modes/planning.md
EOF2

cat > "$TARGET/.workflow/overrides/terminology.md" <<'EOF2'
# Terminology Override

- Требования и рабочие артефакты по умолчанию пишем на русском.
- Английские термины допускаются в скобках для однозначности.
- Пути, slug и технические идентификаторы оформляем латиницей.
EOF2

cat > "$TARGET/.workflow/overrides/requirements-rules.md" <<'EOF2'
# Requirements Rules Override

- Канонический шаблон requirements лежит в `.workflow/templates/requirements/`.
- Backend-требования должны включать OpenAPI-фрагмент и примеры запросов/ответов.
- Описание модели данных оформляется markdown-таблицей.
- Requirement packs живые и могут дополняться в ходе разработки до релизной фиксации.
EOF2

cat > "$TARGET/.workflow/overrides/design-system-rules.md" <<'EOF2'
# Design System Rules Override

- Прототипы по умолчанию делаем на React + MUI без build step.
- Для handoff предпочтительны узнаваемые MUI-компоненты, чтобы фронтендер сразу видел будущую реализацию.
EOF2

cat > "$TARGET/.workflow/overrides/prototyping-rules.md" <<'EOF2'
# Prototyping Rules Override

- Scope prototype и delivery prototype по умолчанию оформляются как single-file \`prototype.html\`.
- Прототип должен открываться локально без сборки и быть пригоден для отправки одним файлом.
EOF2

cat > "$TARGET/.workflow/overrides/baseline-rules.md" <<'EOF2'
# Baseline Rules Override

- `baseline/current/` — это каноническое описание текущей deployed-системы.
- Сырые материалы складываются в `context/source-materials/` и не считаются source of truth, пока не промоутированы в baseline.
- Все итоговые delivered-требования перед промоушеном в baseline сначала фиксируются в `releases/`.
EOF2

cat > "$TARGET/.workflow/overrides/gantt-rules.md" <<'EOF2'
# Gantt Rules Override

- Заголовок gantt генерируется скриптом, руками правим только include/preamble файлы и \`closed-days.txt\`.
- Праздники и нерабочие дни квартала хранятся в \`planning/<quarter>/gantt/closed-days.txt\`.
- Общие служебные блоки перед feature lanes кладём в \`planning/<quarter>/gantt/preamble/common.puml\`.
- View-специфичные блоки можно класть в \`planning/<quarter>/gantt/preamble/quarter-plan.puml\`, \`commander-plan.puml\`, \`actual-progress.puml\`.
- Feature lanes на общем gantt должны идти отдельными секциями \`-- Название фичи --\`.
- Actual-progress include-файлы генерируются из \`planning/actualization.md\` и \`slices/*/execution/tasks.md\`.
- Не начатые execution tasks (\`Progress % = 0\`, нет actual dates) не рисуются в прошлом: при каждой генерации генератор сдвигает их на today/следующий рабочий день только в PlantUML.
- Внутри feature не начатый frontend стартует не раньше чем через 3 рабочих дня после старта не начатого backend/API.
- Resource lanes канонические: \`A<N>\`, \`B<N>\`, \`F<N>\`, \`Q<N>\`; неизвестный ресурс с известной ролью — \`TBD_A\`, \`TBD_B\`, \`TBD_F\`, \`TBD_Q\`.
- Состав команды и допустимые lanes лежат в \`.workflow/team.md\`.
- Не начатые задачи раскладываются без перегруза ресурса выше 100% в один рабочий день; пустой/\`TBD_*\`/неростерный executor назначается автоматически по роли или префиксу задачи.
EOF2

cp "$ROOT_DIR/templates/workflow/command-catalog.template.md" "$TARGET/.workflow/command-catalog.md"
cp "$ROOT_DIR/templates/workflow/command-cheatsheet.template.md" "$TARGET/.workflow/command-cheatsheet.md"
cp "$ROOT_DIR/templates/workflow/consistency-backlog.template.md" "$TARGET/.workflow/consistency-backlog.md"
cp "$ROOT_DIR/templates/workflow/team.template.md" "$TARGET/.workflow/team.md"
cp "$ROOT_DIR/templates/workflow/code-repos.template.json" "$TARGET/.workflow/code-repos.json"
cp "$ROOT_DIR/templates/workflow/language-policy.template.json" "$TARGET/.workflow/language-policy.json"
cp "$ROOT_DIR/templates/evals/golden-scenarios.template.json" "$TARGET/.workflow/evals/golden-scenarios.json"
if [[ ! -e "$TARGET/README.md" ]]; then
  cp "$ROOT_DIR/templates/workflow/project-readme.template.md" "$TARGET/README.md"
fi

cat > "$TARGET/baseline/current/VERSION.md" <<EOF2
# Baseline Version

Version: initial
Date: $(date +%F)
Source release: initial
Previous baseline: none

## What this baseline represents

Initial scaffold baseline. Replace placeholders with the current deployed system description.
EOF2

cat > "$TARGET/baseline/current/domain/README.md" <<'EOF2'
# Domain Baseline

This directory is the canonical current-state domain model for the deployed system.

## Core files
- \`ubiquitous-language.md\`
- \`bounded-contexts.md\`
- \`aggregates.md\`
- \`business-rules.md\`
- \`state-machines/README.md\`
EOF2

cat > "$TARGET/baseline/current/domain/ubiquitous-language.md" <<'EOF2'
# Ubiquitous Language

Fill in the business terms used consistently across planning, requirements, prototypes and delivery.
EOF2

cat > "$TARGET/baseline/current/domain/bounded-contexts.md" <<'EOF2'
# Bounded Contexts

Describe the stable DDD decomposition of the current deployed system.
EOF2

cat > "$TARGET/baseline/current/domain/aggregates.md" <<'EOF2'
# Aggregates

List canonical aggregates, invariants and relationships of the current deployed system.
EOF2

cat > "$TARGET/baseline/current/domain/business-rules.md" <<'EOF2'
# Business Rules

Capture the stable cross-feature business rules of the current deployed system.
EOF2

cat > "$TARGET/baseline/current/domain/state-machines/README.md" <<'EOF2'
# State Machines

Describe canonical lifecycle states and transitions for deployed aggregates and versions.
EOF2

cat > "$TARGET/baseline/current/requirements/README.md" <<'EOF2'
# Canonical Requirements

Здесь хранятся требования текущего состояния, перенесённые из завершённых релизов.
EOF2

cat > "$TARGET/baseline/current/api/README.md" <<'EOF2'
# Canonical API

Store current-state API contracts and OpenAPI references here.
EOF2

cat > "$TARGET/baseline/current/ui/README.md" <<'EOF2'
# Canonical UI

Store current-state UI structure, navigation and component notes here.
EOF2

cat > "$TARGET/baseline/current/data/README.md" <<'EOF2'
# Canonical Data Model

Store current-state persistence and integration data model notes here.
EOF2

cat > "$TARGET/baseline/current/decisions/README.md" <<'EOF2'
# Decisions

Store ADR-style decisions that explain why the current baseline looks the way it does.
EOF2

cat > "$TARGET/releases/README.md" <<'EOF2'
# Releases

Each delivered release gets its own folder with final requirements and promotion notes before updating \`baseline/current\`.
EOF2

cat > "$TARGET/planning/intake/README.md" <<'EOF2'
# Feature Intake

Store feature preflight notes here before creating a new \`features/<slug>/\` structure.

- One markdown file per candidate feature.
- Use `.workflow/templates/intake/feature-intake.template.md`.
- Do not scaffold a new feature until the intake result is reviewed.
EOF2

python3 "$ROOT_DIR/scripts/harnessctl.py" manifest "$TARGET" --source "$ROOT_DIR" >/dev/null

echo "Project scaffold created at $TARGET"
