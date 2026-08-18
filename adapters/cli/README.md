# CLI Adapter

Use these helpers from any terminal-first LLM workflow.

- `switch-mode.sh <mode>` updates `.workspace-state/active-mode.md`
- `start-session.sh <project-root>` prints the files the assistant should read first
- all executable tools remain in the harness `scripts/` directory

- `start-session.sh` also reminds the assistant to read `core/llm-contract.md` before the active mode file.
- `templates/workflow/command-catalog.template.md` defines the short natural-language workflow commands the assistant should interpret consistently.

- `start-session.sh` also surfaces `templates/requirements/README.md` so the active requirement template is visible at session start.
- `start-session.sh` surfaces `templates/workflow/command-cheatsheet.template.md` so the preferred natural-language prompts are visible at session start.
- `start-session.sh` also surfaces `templates/intake/README.md` so feature preflight rules are visible before a new feature is scaffolded.
