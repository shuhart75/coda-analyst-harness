from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from pathlib import Path, PurePosixPath


BLOCK_START = "<!-- coda-gigacode-workspace:v1:start -->"
BLOCK_END = "<!-- coda-gigacode-workspace:v1:end -->"
MANIFEST = ".gigacode/coda-harness-projection.json"
BUNDLE_ROOT = Path(__file__).resolve().parents[1]


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def managed_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        return False
    return relative == "GIGACODE.md" or (
        len(path.parts) == 3
        and path.parts[:2] == (".gigacode", "commands")
        and path.suffix == ".md"
    ) or (
        len(path.parts) == 4
        and path.parts[:2] == (".gigacode", "skills")
        and path.name == "SKILL.md"
    )


def local_path(project: Path, relative: str) -> Path:
    path = project / relative
    for item in (path, *path.parents):
        if item == project:
            break
        if item.is_symlink():
            raise ValueError(f"Проекция GigaCode не может проходить через symlink: {item}")
    if path.exists() and not path.is_file():
        raise ValueError(f"Путь проекции GigaCode не является файлом: {path}")
    return path


def split_block(content: str) -> tuple[str, str, str]:
    if BLOCK_START not in content and BLOCK_END not in content:
        return content, "", ""
    if content.count(BLOCK_START) != 1 or content.count(BLOCK_END) != 1:
        raise ValueError("Повреждён управляемый блок GIGACODE.md")
    start = content.index(BLOCK_START)
    end_marker = content.index(BLOCK_END)
    if end_marker < start:
        raise ValueError("Неверный порядок маркеров GIGACODE.md")
    end = end_marker + len(BLOCK_END)
    return content[:start], content[start:end], content[end:]


def install_gigacode_workspace(project: Path, harness: Path) -> Path:
    """Preflight the entire projection before updating any of its files."""
    manifest_path = local_path(project, MANIFEST)
    previous = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), dict):
            raise ValueError("Неподдерживаемый манифест проекции GigaCode")
        previous = manifest["files"]
        if any(not managed_path(name) or not isinstance(value, str) for name, value in previous.items()):
            raise ValueError("Недопустимые пути в манифесте проекции GigaCode")

    harness_root = harness.resolve()
    project_root = project.resolve()
    block = (
        f"{BLOCK_START}\n"
        "# Аналитическая рабочая область АС КОДА\n\n"
        f"**RULE:** `HARNESS_ROOT = {harness_root}`; `PROJECT_ROOT = {project_root}`.\n"
        f"**DOCS:** Сначала прочитай `{harness_root}/GIGACODE.md`, затем договоры\n"
        "его процедуры. Относительные core/modes/scripts/templates/skills/.gigacode\n"
        "в поставленных инструкциях относятся к HARNESS_ROOT.\n"
        f"Разрешай проект командой `python3 {shlex.quote(str(harness_root / 'scripts/workspace.py'))} "
        f"--root {shlex.quote(str(harness_root))} project-root`.\n"
        "**RULE:** Общайся по-русски; роль code доступна только через зарегистрированные операции.\n"
        "**RULE:** Команды и skills выбирают процедуру; stop gates, режим и проверки обязательны.\n"
        "**PROHIBITED:** Не добавляй эту локальную проекцию в Git.\n"
        f"{BLOCK_END}"
    )
    desired = {"GIGACODE.md": block}
    for pattern in (".gigacode/commands/*.md", ".gigacode/skills/*/SKILL.md"):
        for source in sorted(BUNDLE_ROOT.glob(pattern)):
            relative = source.relative_to(BUNDLE_ROOT).as_posix()
            desired[relative] = source.read_text(encoding="utf-8")
    if len(desired) == 1:
        raise ValueError("Нативные команды и skills GigaCode отсутствуют в поставке")

    paths = set(previous) | set(desired) | {MANIFEST}
    tracked = subprocess.run(
        ("git", "-C", str(project), "ls-files", "-z", "--", *sorted(paths)),
        text=True, capture_output=True, check=False,
    )
    if tracked.returncode != 0 or tracked.stdout:
        raise ValueError("Проекция GigaCode разрешена только в неотслеживаемых локальных файлах")

    updates = {}
    removals = []
    for relative in sorted(set(previous) | set(desired)):
        target = local_path(project, relative)
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if relative == "GIGACODE.md":
            prefix, managed, suffix = split_block(current)
        else:
            prefix, managed, suffix = "", current, ""
        if target.exists() and relative in previous and digest(managed) != previous[relative]:
            raise ValueError(f"Локально изменена проекция GigaCode: {target}")
        if relative not in previous and (managed or (relative != "GIGACODE.md" and target.exists())):
            raise ValueError(f"Конфликт с существующим файлом GigaCode: {target}")
        if relative in desired:
            if relative == "GIGACODE.md" and not managed and prefix and not prefix.endswith("\n\n"):
                prefix += "\n" if prefix.endswith("\n") else "\n\n"
            updates[target] = prefix + desired[relative] + suffix
            if relative == "GIGACODE.md" and not suffix:
                updates[target] += "\n"
        elif target.exists():
            removals.append(target)

    for target, content in updates.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for target in removals:
        target.unlink()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"schema_version": 1, "files": {name: digest(value) for name, value in desired.items()}},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return project / "GIGACODE.md"
