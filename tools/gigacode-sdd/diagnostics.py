"""Local support bundle. No agent calls and no changes to run inputs."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
import shutil


def write_section(stream, name, text):
    stream.write('=== FILE ' + json.dumps(name, ensure_ascii=False)
                 + ' CHARS ' + str(len(text)) + ' ===\n')
    stream.write(text)
    stream.write('\n=== END FILE ===\n\n')


def collect(config, destination=None, error=None):
    config = Path(config).absolute()
    root = config.parent
    home = root.parent.parent if root.parent.name == 'runs' else root
    destination = Path(destination) if destination else home / 'diagnostics/latest-diagnostics.txt'
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    summary = {'run': root.name, 'config': str(config),
               'created_at': datetime.now(timezone.utc).isoformat(),
               'error': str(error) if error is not None else None,
               'mode': 'local UTF-8 diagnostic report', 'omitted': [], 'files': {}}
    # Never traverse workspaces, credentials, .git or links supplied by agents.
    files = list(root.glob('*.json')) + list(root.glob('*.md'))
    for name in ('logs', 'input', 'plan-imports', 'preserved-services'):
        directory = root / name
        if directory.is_symlink():
            summary['omitted'].append(str(directory))
            continue
        for folder, dirs, names in os.walk(directory, followlinks=False):
            base = Path(folder)
            dirs[:] = sorted(d for d in dirs if not (base / d).is_symlink())
            files.extend(base / n for n in sorted(names))
    files = [(path, 'run/' + path.relative_to(root).as_posix()) for path in sorted(set(files))]
    files.extend((path, 'runner/' + path.name) for path in sorted(Path(__file__).parent.glob('*.py')))
    fd, temp = tempfile.mkstemp(prefix='.latest-', suffix='.txt', dir=destination.parent)
    os.close(fd)
    try:
        # Spool sections to disk so the summary comes first without retaining
        # every journal in memory. Source bytes and line endings are untouched.
        with tempfile.TemporaryFile(mode='w+', encoding='utf-8', newline='') as sections:
            for path, name in files:
                if path.is_symlink() or not path.is_file():
                    summary['omitted'].append(str(path))
                    continue
                try:
                    data = path.read_bytes()
                except OSError as exc:
                    summary['omitted'].append({'path': str(path), 'error': str(exc)})
                    continue
                metadata = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                summary['files'][name] = metadata
                try:
                    text = data.decode('utf-8')
                    if any(ord(c) < 32 and c not in '\t\r\n' for c in text) or '\x7f' in text:
                        raise ValueError('Non-text control characters')
                except (UnicodeError, ValueError):
                    metadata['included'] = False
                    summary['omitted'].append({'path': name, 'reason': 'Not UTF-8 text; content not embedded'})
                    continue
                metadata['included'] = True
                write_section(sections, name, text)
            with open(temp, 'w', encoding='utf-8', newline='') as output:
                output.write('GigaCode SDD diagnostics (UTF-8)\n'
                    'Разделы содержат имя файла и длину содержимого в символах.\n'
                    'Исходные текстовые данные не обезличены. Бинарные файлы не включены.\n'
                    'Рабочие копии кода и настройки учётных записей не собираются.\n\n')
                write_section(output, 'summary.json', json.dumps(summary, ensure_ascii=False, indent=2))
                sections.seek(0)
                shutil.copyfileobj(sections, output)
        os.replace(temp, destination)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

    return destination


def report(config, error=None):
    """A diagnostic failure must not hide the original runner result."""
    try:
        output = collect(config, error=error)
    except Exception as exc:
        print('Не удалось собрать диагностику: ' + str(exc))
        return None
    print('Диагностический файл: ' + str(output))
    return output
