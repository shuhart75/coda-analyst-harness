from __future__ import annotations

import os
import subprocess
from pathlib import Path


ENTRYPOINT_MARKER = "<!-- analyst-harness-local-entrypoint:v1 -->"


def is_local_entrypoint(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        return ENTRYPOINT_MARKER in path.read_text(encoding="utf-8", errors="ignore")[:4096]
    except OSError:
        return False


def embedded_harness_paths(project: Path) -> list[str]:
    embedded = [name for name in (".workflow", ".vscode") if (project / name).exists()]
    agents = project / "AGENTS.md"
    if agents.exists() and not is_local_entrypoint(agents):
        embedded.append("AGENTS.md")
    return embedded


def git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", "-C", str(project), *args),
        text=True,
        capture_output=True,
        check=False,
    )


def exclude_local_entrypoint(project: Path) -> None:
    tracked = git(project, "ls-files", "--error-unmatch", "AGENTS.md")
    if tracked.returncode == 0:
        raise ValueError("Локальный AGENTS.md аналитического проекта не должен отслеживаться Git")
    git_path = git(project, "rev-parse", "--git-path", "info/exclude")
    if git_path.returncode != 0:
        raise ValueError(f"Не удалось определить .git/info/exclude: {git_path.stderr.strip()}")
    exclude = Path(git_path.stdout.strip())
    if not exclude.is_absolute():
        exclude = project / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if "/AGENTS.md" not in existing.splitlines():
        with exclude.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write("\n# analyst-harness local LLM entry point\n/AGENTS.md\n")


def write_local_entrypoint(project: Path, harness: Path, code: Path | None = None) -> Path:
    path = project / "AGENTS.md"
    if path.exists() and not is_local_entrypoint(path):
        raise ValueError(
            "В аналитическом репозитории уже есть посторонний AGENTS.md; "
            "обвязка не будет перезаписывать его"
        )
    exclude_local_entrypoint(project)
    relative_harness = os.path.relpath(harness.resolve(), project.resolve())
    relative_code = os.path.relpath(code.resolve(), project.resolve()) if code else None
    code_role = (
        f"- Каталог {relative_code}/ выполняет роль code и доступен только для чтения.\n"
        if relative_code
        else "- Роль code отключена.\n"
    )
    path.write_text(
        "# Локальная точка входа аналитической обвязки\n\n"
        f"{ENTRYPOINT_MARKER}\n\n"
        "Этот файл создан локально программой развёртывания, исключён через "
        ".git/info/exclude и не входит в аналитический Git-репозиторий.\n\n"
        "## Роли каталогов\n\n"
        "- Текущий каталог выполняет роль analytics и является PROJECT_ROOT.\n"
        f"- Каталог {relative_harness}/ выполняет роль harness и является HARNESS_ROOT.\n\n"
        f"{code_role}\n"
        "## Обязательный порядок\n\n"
        f"1. Прочитай {relative_harness}/AGENTS.md и {relative_harness}/core/llm-contract.md.\n"
        f"2. Выполни python3 {relative_harness}/scripts/workspace.py "
        f"--root {relative_harness} project-root. Результат должен совпасть с текущим каталогом.\n"
        f"3. Читай активный режим и инструменты относительно {relative_harness}/.\n"
        "4. Все пути baseline/, context/, planning/, features/ и releases/ "
        "разрешай относительно текущего каталога.\n"
        "5. Не создавай проектные каталоги в HARNESS_ROOT и не изменяй репозиторий роли code.\n"
        "6. По запросу проверки кода используй правила HARNESS_ROOT/core/code-inspection.md "
        "и программы HARNESS_ROOT/scripts/code-inspect.py; путь к code не спрашивай у пользователя.\n\n"
        "Не добавляй этот файл в Git и не заменяй им основной договор обвязки.\n",
        encoding="utf-8",
    )
    ignored = git(project, "check-ignore", "--quiet", "AGENTS.md")
    if ignored.returncode != 0:
        raise ValueError("Локальный AGENTS.md не исключён из Git")
    return path
