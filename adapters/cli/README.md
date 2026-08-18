# CLI Adapter

Use these helpers from any terminal-first LLM workflow.

- `switch-mode.sh <project-root> <mode>` updates `.workflow/active-mode.md`
- `start-session.sh <project-root>` prints the files the assistant should read first
- standalone projects also contain `.workflow/tools/validate-structure.py`, `.workflow/tools/validate-links.py`, `.workflow/tools/sync-quarter-gantt.py`, `.workflow/tools/sync-actual-progress-overlay.py`, `.workflow/tools/find-stale-terms.py`, and `.workflow/tools/expand-plantuml-includes.py`

- `start-session.sh` also reminds the assistant to read `.workflow/llm-contract.md` before the active mode file.
- `.workflow/command-catalog.md` defines the short natural-language workflow commands the assistant should interpret consistently.

- `start-session.sh` also surfaces `.workflow/templates/requirements/README.md` so the active requirement template is visible at session start.
- `start-session.sh` surfaces `.workflow/command-cheatsheet.md` so the preferred natural-language prompts are visible at session start.
- `start-session.sh` also surfaces `.workflow/templates/intake/README.md` so feature preflight rules are visible before a new feature is scaffolded.
