#!/usr/bin/env python3
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CLOSED_DAY_RE = re.compile(r"^(\d{4}/\d{2}/\d{2})\s+is closed\s*$", re.MULTILINE)

FEATURE_SLICES = [
    {
        "feature": "approvals",
        "slice": "core-process",
        "backend": "final-spec/REQ_approval_core.md",
    },
    {
        "feature": "approvals",
        "slice": "page",
        "frontend": "final-spec/REQ_approvals_page_frontend.md",
        "backend": "final-spec/REQ_approvals_page_backend.md",
        "prototype": "prototypes/approvals_page.html",
    },
    {
        "feature": "artifacts",
        "slice": "core",
        "backend": "final-spec/REQ_artifacts_core.md",
        "prototype": "prototypes/artifacts_mockup.html",
    },
    {
        "feature": "deployments",
        "slice": "workspace",
        "frontend": "final-spec/REQ_deployments_frontend.md",
        "backend": "final-spec/REQ_deployments_backend.md",
        "prototype": "prototypes/deployments.html",
    },
    {
        "feature": "deployments",
        "slice": "list",
        "frontend": "final-spec/REQ_deployments_list_frontend.md",
    },
    {
        "feature": "deployments",
        "slice": "form-editing",
        "frontend": "final-spec/REQ_deployments_form_frontend.md",
    },
    {
        "feature": "deployments",
        "slice": "detail",
        "frontend": "final-spec/REQ_deployments_detail_frontend.md",
    },
    {
        "feature": "deployments",
        "slice": "db-api",
        "backend": "final-spec/REQ_deployments_backend_db_api.md",
    },
    {
        "feature": "deployments",
        "slice": "lifecycle",
        "backend": "final-spec/REQ_deployments_backend_lifecycle.md",
    },
    {
        "feature": "packages",
        "slice": "page",
        "frontend": "final-spec/REQ_packages_page_frontend.md",
        "backend": "final-spec/REQ_packages_page_backend.md",
        "prototype": "prototypes/packages_page.html",
    },
    {
        "feature": "pilots",
        "slice": "workspace",
        "frontend": "final-spec/REQ_pilots_frontend.md",
        "backend": "final-spec/REQ_pilots_backend.md",
    },
    {
        "feature": "roles",
        "slice": "rbac",
        "backend": "final-spec/REQ_roles_rbac.md",
    },
    {
        "feature": "scorecards",
        "slice": "workspace",
        "frontend": "final-spec/REQ_scorecards_frontend.md",
        "backend": "final-spec/REQ_scorecards_backend.md",
        "prototype": "prototypes/scorecard_create.html",
    },
]

PROTOTYPE_RAW = [
    "final-spec/prototype.html",
    "prototypes/approvals_page.html",
    "prototypes/artifacts_mockup.html",
    "prototypes/deployments.html",
    "prototypes/packages_page.html",
    "prototypes/scorecard_create.html",
    "prototypes/scorecards_detail.html",
]

DIAGRAM_RAW = [
    "spec/data_model.puml",
    "spec/state_machine.puml",
    "final-spec/spec_data_model.puml",
    "final-spec/spec_state_machine.puml",
    "final-spec/spec_domain_model.md",
    "final-spec/spec_data_model.puml",
    "final-spec/BPMN_methodologist_process.puml",
    "final-spec/BPMN_methodologist_process.bpmn",
    "final-spec/mvp_gantt.puml",
    "final-spec/mvp_gantt_chart_v6.15.puml",
]

REQ_RAW_GLOBS = [
    ("final-spec", "REQ_*.md"),
    ("final-spec", "README.md"),
    ("spec", "*.md"),
    ("docs", "*.md"),
]


FULL_SNAPSHOT_EXCLUDES = [
    ".git",
    ".claude/worktrees",
    "__pycache__",
]


def should_exclude_full_snapshot(rel: Path) -> bool:
    rel_str = rel.as_posix()
    return any(rel_str == item or rel_str.startswith(item + "/") for item in FULL_SNAPSHOT_EXCLUDES)


def write_full_snapshot(source: Path, target: Path) -> None:
    legacy_root = target / "context/source-materials/legacy"
    snapshot_dir = legacy_root / "changeswork-full"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    kept: list[Path] = []
    for item in source.rglob("*"):
        if not item.is_file():
            continue
        rel = item.relative_to(source)
        if should_exclude_full_snapshot(rel):
            continue
        kept.append(rel)
        copy_file(item, snapshot_dir / rel)

    counts: dict[str, int] = {}
    for rel in kept:
        bucket = rel.parts[0]
        counts[bucket] = counts.get(bucket, 0) + 1

    (legacy_root / "README.md").write_text(
        "# Legacy snapshots\n\n"
        "- `changeswork-full/` — full copied snapshot of the legacy `changesWork` repository, excluding `.git/`, `.claude/worktrees/`, and `__pycache__/`.\n"
        "- `changeswork-full.manifest.md` — import policy and file counts.\n\n"
        "Use this folder when the normalized harness structure is not enough and you need the raw historical project context.\n",
        encoding="utf-8",
    )

    (legacy_root / "changeswork-full.manifest.md").write_text(
        "# changesWork Full Snapshot Manifest\n\n"
        "Source: original `changesWork` repo snapshot\n"
        "Target: `context/source-materials/legacy/changeswork-full/`\n\n"
        "## Copy policy\n\n"
        "- Copied by value, never moved.\n"
        "- Excluded `.git/`, `.claude/worktrees/`, and `__pycache__/` as transient or duplicate artifacts.\n"
        "- Original source remains untouched.\n\n"
        f"## File counts\n\n- Copied files: {len(kept)}\n- Top-level buckets:\n"
        + "".join(f"  - `{key}`: {counts[key]}\n" for key in sorted(counts))
        + "\n## Why this snapshot exists\n\n"
        "- Keeps the previously curated harness structure intact.\n"
        "- Preserves the complete useful legacy repo context inside the project for future traceability.\n"
        "- Lets new features be compared against both normalized artifacts and raw historical materials.\n",
        encoding="utf-8",
    )

LEGACY_TASK_DOCS = {
    ("approvals", "page"): ["planning/mvp/current/tasks/legacy/mvp_tasks_approvals_page.md"],
    ("packages", "page"): ["planning/mvp/current/tasks/legacy/mvp_tasks_packages_page.md"],
    ("deployments", "workspace"): ["planning/mvp/current/tasks/legacy/mvp_tasks_deployments_page.md"],
    ("deployments", "form-editing"): ["planning/mvp/current/tasks/legacy/mvp_tasks_deployment_form.md"],
    ("deployments", "detail"): ["planning/mvp/current/tasks/legacy/mvp_tasks_deployment_detail_page.md"],
    ("scorecards", "workspace"): [
        "planning/mvp/current/tasks/legacy/mvp_tasks_scorecards_page.md",
        "planning/mvp/current/tasks/legacy/mvp_tasks_scorecard_form.md",
        "planning/mvp/current/tasks/legacy/mvp_tasks_scorecard_detail_page.md",
    ],
    ("pilots", "workspace"): [
        "planning/mvp/current/tasks/legacy/mvp_tasks_pilots_page.md",
        "planning/mvp/current/tasks/legacy/mvp_tasks_pilot_form.md",
        "planning/mvp/current/tasks/legacy/mvp_tasks_pilot_detail_page.md",
    ],
}


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def append_line(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(text)


def extract_closed_days(source: Path) -> list[str]:
    gantt_candidates = [
        source / "planning/mvp/current/gantt/mvp_gantt_chart_current_actualized.puml",
        source / "planning/mvp/current/gantt/mvp_gantt_chart_commander_frozen.puml",
        source / "planning/mvp/current/gantt/mvp_gantt_chart_current.puml",
    ]
    for path in gantt_candidates:
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        days = CLOSED_DAY_RE.findall(content)
        if days:
            deduped: list[str] = []
            for day in days:
                if day not in deduped:
                    deduped.append(day)
            return deduped
    return []


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: import_changeswork_snapshot.py <changeswork-source> <target-project>")
        return 1

    source = Path(sys.argv[1]).resolve()
    target = Path(sys.argv[2]).resolve()

    if target.exists():
        print(f"Target already exists: {target}")
        return 1

    run("bash", str(ROOT / "scripts/scaffold-project.sh"), str(target))
    run("bash", str(ROOT / "scripts/scaffold-quarter.sh"), str(target), "2026-Q2")

    quarter_dir = target / "planning/2026-Q2"
    imported_dir = quarter_dir / "imported-source"
    (imported_dir / "gantt").mkdir(parents=True, exist_ok=True)
    (imported_dir / "tasks").mkdir(parents=True, exist_ok=True)
    (imported_dir / "notes").mkdir(parents=True, exist_ok=True)

    # Raw requirements/docs snapshot
    raw_req_root = target / "context/current-system/requirements/raw"
    for folder, pattern in REQ_RAW_GLOBS:
        src_dir = source / folder
        if "*" in pattern:
            for item in src_dir.glob(pattern):
                if item.is_file():
                    copy_file(item, raw_req_root / folder / item.name)
        else:
            item = src_dir / pattern
            if item.exists():
                copy_file(item, raw_req_root / folder / item.name)

    # Raw diagrams and prototypes
    for rel in DIAGRAM_RAW:
        src = source / rel
        if src.exists():
            copy_file(src, target / "context/current-system/diagrams/raw" / Path(rel).name)
    for rel in PROTOTYPE_RAW:
        src = source / rel
        if src.exists():
            copy_file(src, target / "context/current-system/prototypes/raw" / Path(rel).name)

    # Legacy planning snapshot
    for item in (source / "planning/mvp/current/gantt").glob("*.puml"):
        copy_file(item, imported_dir / "gantt" / item.name)
    for item in (source / "planning/mvp/current/tasks").glob("*.md"):
        copy_file(item, imported_dir / "tasks" / item.name)
    for item in [
        source / "planning/mvp/README.md",
        source / "planning/mvp/versions/v6.37/README.md",
        source / "PLANNING_LAWS.md",
        source / "README.md",
    ]:
        if item.exists():
            copy_file(item, imported_dir / "notes" / item.name)

    quarter_readme = quarter_dir / "quarter/README.md"
    quarter_readme.write_text(
        "# 2026-Q2\n\n"
        "## Notes\n\n"
        "Этот квартал импортирован из legacy-проекта `changesWork`.\n"
        "Raw planning-артефакты лежат в `../imported-source/`.\n"
        "Новые view-файлы quarter/commander/actual-progress созданы как каркас и требуют постепенной нормализации.\n\n"
        "## Scope Decisions\n\n"
        "- legacy gantt и task docs скопированы без изменения оригинала\n"
        "- planning stories в новом формате пока не извлечены автоматически\n\n"
        "## Comparison Notes\n\n"
        "Сверку legacy-плана и новой структуры лучше делать по feature lanes.\n",
        encoding="utf-8",
    )

    closed_days = extract_closed_days(source)
    if closed_days:
        closed_days_path = quarter_dir / "gantt/closed-days.txt"
        closed_days_path.write_text(
            "# Imported from legacy gantt\n" + "\n".join(closed_days) + "\n",
            encoding="utf-8",
        )

    # Features and slices
    feature_refs = {}
    for mapping in FEATURE_SLICES:
        feature = mapping["feature"]
        if feature not in feature_refs:
            run("bash", str(ROOT / "scripts/scaffold-feature.sh"), str(target), feature)
            feature_refs[feature] = []
            notes_path = target / f"features/{feature}/planning/MIGRATION_NOTES.md"
            notes_path.write_text(
                "# Migration Notes\n\n"
                "Legacy project imported into the new harness structure.\n"
                "Planning stories for this feature still need manual extraction from legacy gantt and task docs.\n",
                encoding="utf-8",
            )

        run("bash", str(ROOT / "scripts/scaffold-slice.sh"), str(target), feature, mapping["slice"])
        slice_dir = target / f"features/{feature}/slices/{mapping['slice']}"

        if "frontend" in mapping:
            src = source / mapping["frontend"]
            if src.exists():
                copy_file(src, slice_dir / "requirements/frontend.md")
                feature_refs[feature].append(mapping["frontend"])
        if "backend" in mapping:
            src = source / mapping["backend"]
            if src.exists():
                copy_file(src, slice_dir / "requirements/backend.md")
                feature_refs[feature].append(mapping["backend"])
        if "prototype" in mapping:
            src = source / mapping["prototype"]
            if src.exists():
                copy_file(src, slice_dir / "delivery-prototype/prototype.html")
                feature_refs[feature].append(mapping["prototype"])

        tasks_md = slice_dir / "execution/tasks.md"
        legacy_docs = LEGACY_TASK_DOCS.get((feature, mapping["slice"]), [])
        if legacy_docs:
            append_line(tasks_md, "\n## Legacy references\n")
            for rel in legacy_docs:
                append_line(tasks_md, f"- `{rel}`\n")

    for feature, refs in feature_refs.items():
        refs_path = target / f"features/{feature}/references.md"
        uniq_refs = []
        for rel in refs:
            if rel not in uniq_refs:
                uniq_refs.append(rel)
        refs_text = "# References\n\n"
        refs_text += "## Legacy sources used for import\n"
        for rel in uniq_refs:
            refs_text += f"- `context/current-system/requirements/raw/{Path(rel).parent.name}/{Path(rel).name}`\n" if rel.endswith(".md") else f"- `context/current-system/prototypes/raw/{Path(rel).name}`\n"
        refs_text += "\n## Additional source folders\n"
        refs_text += "- `context/current-system/requirements/raw/`\n"
        refs_text += "- `context/current-system/prototypes/raw/`\n"
        refs_text += "- `context/current-system/diagrams/raw/`\n"
        refs_path.write_text(refs_text, encoding="utf-8")

    # Change request placeholder and root readme
    (target / "context/change-requests/README.md").write_text(
        "# Change Requests\n\nПоместите сюда новые change request документы для дальнейшей работы в режиме requirements.\n",
        encoding="utf-8",
    )
    (target / "README.md").write_text(
        "# changesWork copy under analyst-harness\n\n"
        "Это копия и частичная раскладка legacy-проекта `changesWork` под новый workflow.\n\n"
        "## Что уже сделано\n"
        "- raw requirements, diagrams и prototypes скопированы в `context/current-system/`\n"
        "- полный полезный snapshot legacy-репозитория сохранён в `context/source-materials/legacy/changeswork-full/`\n"
        "- основные feature и slice-контейнеры созданы в `features/`\n"
        "- legacy planning-артефакты скопированы в `planning/2026-Q2/imported-source/`\n"
        "- requirement docs разложены по feature/slice там, где mapping удалось определить автоматически\n\n"
        "## Что ещё требует ручной доработки\n"
        "- нормализация planning stories\n"
        "- раскладка implementation tasks в `execution/tasks.md` по каждому slice\n"
        "- перепаковка legacy gantt в новые quarter/commander/actual-progress includes\n",
        encoding="utf-8",
    )

    print(f"Imported snapshot created at {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
