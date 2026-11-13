"""Local support bundle. No agent calls and no changes to run inputs."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
import zipfile


def collect(config, destination=None, error=None):
    config = Path(config).absolute()
    root = config.parent
    home = root.parent.parent if root.parent.name == 'runs' else root
    destination = Path(destination) if destination else home / 'diagnostics/latest.zip'
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    summary = {'run': root.name, 'config': str(config),
               'created_at': datetime.now(timezone.utc).isoformat(),
               'error': str(error) if error is not None else None,
               'mode': 'local raw diagnostic bundle', 'omitted': [], 'files': {}}
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
    fd, temp = tempfile.mkstemp(prefix='.latest-', suffix='.zip', dir=destination.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(set(files)):
                if path.is_symlink() or not path.is_file():
                    summary['omitted'].append(str(path))
                    continue
                name = 'run/' + path.relative_to(root).as_posix()
                try:
                    data = path.read_bytes()
                except OSError as exc:
                    summary['omitted'].append({'path': str(path), 'error': str(exc)})
                    continue
                bundle.writestr(name, data)
                summary['files'][name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            for path in sorted(Path(__file__).parent.glob('*.py')):
                if not path.is_symlink():
                    bundle.writestr('runner/' + path.name, path.read_bytes())
            bundle.writestr('summary.json', json.dumps(summary, ensure_ascii=False, indent=2))
            bundle.writestr('README.txt',
                'Начните с summary.json: запуск, время сбора, ошибка и список файлов.\n'
                'run/: исходные отчёты, планы, закреплённый вход и журналы.\n'
                'runner/: версия Python-обвязки на момент сбора.\n'
                'Пакет содержит исходные данные без гарантированного удаления секретов. '
                'Передавайте его только доверенному получателю.\n'
                'Рабочие копии кода и настройки учётных записей не собираются.\n')
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
    print('Диагностический пакет: ' + str(output))
    return output
