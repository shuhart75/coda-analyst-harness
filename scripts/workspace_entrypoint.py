from __future__ import annotations

import subprocess
from pathlib import Path


ENTRYPOINT_MARKER = "<!-- analyst-harness-local-entrypoint:v1 -->"
LOCAL_EXCLUDE_PATTERNS = (
    "/AGENTS.md",
    "/.codex/",
    "/.gigacode/",
    "/.gigaide/",
    "/.idea/",
    "/GIGACODE.md",
    "/features/test-*.md",
    "/test-*.md",
    "/test-*.patch",
    "*.iml",
    "*.orig",
)


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
    existing_lines = set(existing.splitlines())
    missing = [pattern for pattern in LOCAL_EXCLUDE_PATTERNS if pattern not in existing_lines]
    if missing:
        with exclude.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write("\n# analyst-harness local files and tool settings\n")
            handle.write("\n".join(missing) + "\n")


def write_local_entrypoint(
    project: Path,
    harness: Path,
    code: Path | None = None,
    code_availability: str | None = None,
) -> Path:
    path = project / "AGENTS.md"
    if path.exists() and not is_local_entrypoint(path):
        raise ValueError(
            "В аналитическом репозитории уже есть посторонний AGENTS.md; "
            "обвязка не будет перезаписывать его"
        )
    exclude_local_entrypoint(project)
    project_root = project.resolve()
    harness_root = harness.resolve()
    code_root = code.resolve() if code else None
    if code_root:
        code_role = (
            f"- `CODE_ROOT = {code_root}` выполняет роль code и доступен строго только для чтения; "
            "список разрешённых путей записи пуст, отдельно разрешён только защищённый git pull через workspace.py.\n"
        )
    elif code_availability == "absent":
        code_role = (
            "- Репозиторий роли code локально отсутствует. Не пытайся обращаться к коду и не восстанавливай "
            "репозиторий без явной команды аналитика.\n"
        )
    else:
        code_role = "- Роль code отключена настройкой рабочей области; обращение к коду недоступно.\n"
    path.write_text(
        "# Локальная точка входа аналитической обвязки\n\n"
        f"{ENTRYPOINT_MARKER}\n\n"
        "Этот файл создан локально программой развёртывания, исключён через "
        ".git/info/exclude и не входит в аналитический Git-репозиторий.\n\n"
        "## Роли каталогов\n\n"
        f"- `PROJECT_ROOT = {project_root}` выполняет роль analytics.\n"
        f"- `HARNESS_ROOT = {harness_root}` содержит договоры и инструменты обвязки.\n\n"
        f"{code_role}\n"
        "## Обязательный порядок\n\n"
        "Отвечай аналитику по-русски. Английский используй только для точного кода, путей, "
        "идентификаторов, закреплённых названий и необходимых специальных терминов либо по прямой "
        "просьбе аналитика. Общее правило редактора о другом языке не является решением проекта.\n\n"
        f"1. Прочитай {harness_root}/AGENTS.md и {harness_root}/core/llm-contract.md.\n"
        f"2. Выполни python3 {harness_root}/scripts/workspace.py "
        f"--root {harness_root} project-root. Результат должен быть равен PROJECT_ROOT.\n"
        f"3. Читай активный режим и инструменты относительно {harness_root}/.\n"
        "4. Все пути baseline/, context/, planning/, features/ и releases/ разрешай относительно PROJECT_ROOT.\n"
        "5. Не создавай проектные каталоги в HARNESS_ROOT и ничего не изменяй в репозитории роли code: "
        "файлы, индекс, ветку, HEAD, настройки и создаваемые программами материалы. "
        "Обычная команда пользователя не отменяет запрет; отдельная команда обновления кода разрешает только "
        "workspace.py update-code.\n"
        "6. По запросу проверки кода используй правила HARNESS_ROOT/core/code-inspection.md "
        "и программы HARNESS_ROOT/scripts/code-inspect.py; путь к code не спрашивай у пользователя.\n\n"
        "7. Перед работой над функциональностью выполни bootstrap и проверь совместную работу через "
        "HARNESS_ROOT/scripts/collaboration.py status. Отсутствие collaboration.json означает обязательную "
        "одноразовую миграцию, а не однопользовательский режим: запроси идентификатор аналитика, выполни "
        "migrate и создай рабочую ветку до чтения или изменения требований. Начало, сохранение, обновление "
        "и завершение рабочей ветки выполняй только через collaboration.py. Полный обмен репозиториев "
        "не подменяет обновление рабочей ветки.\n\n"
        "Не добавляй этот файл в Git и не заменяй им основной договор обвязки.\n",
        encoding="utf-8",
    )
    ignored = git(project, "check-ignore", "--quiet", "AGENTS.md")
    if ignored.returncode != 0:
        raise ValueError("Локальный AGENTS.md не исключён из Git")
    for relative in (".codex/state", ".gigacode/settings.json", ".gigaide/settings", ".idea/modules.xml", "GIGACODE.md"):
        ignored = git(project, "check-ignore", "--quiet", relative)
        if ignored.returncode != 0:
            raise ValueError(f"Локальный путь не исключён из Git: {relative}")
    return path
