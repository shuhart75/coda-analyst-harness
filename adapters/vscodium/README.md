# VSCodium Adapter

Suggested files for projects that use this harness:

- `.vscode/settings.json`
- `.vscode/tasks.json`
- `.vscode/workflow.code-snippets`

`scaffold-project.sh` copies these files into the target project automatically.

- Tasks provide explicit mode switches, including `release-finalization` for baseline promotion work.
- Tasks also expose local structure/link validation inside the standalone project workspace.
- `workflow.code-snippets` provides two kinds of helpers:
  - prompt starters for the canonical natural-language workflow commands;
  - markdown skeletons for planning stories and implementation task registries.

## How to use in VSCodium

1. Open a markdown or plaintext file, chat draft, or scratch note.
2. Type a prefix such as `wf-plan`, `wf-req`, `wf-progress`, or `wf-release`.
3. Accept the snippet with Tab or Enter.
4. Fill the placeholders with your actual folder, feature, task ids, dates, and expected result.
5. Send the generated text to the LLM as a normal message.

The snippets do not execute anything by themselves. They only help you formulate stable, repeatable prompts for any CLI-neutral LLM workflow.
