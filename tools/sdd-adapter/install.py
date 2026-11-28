#!/usr/bin/env python3
"""Explicit user-local installation of the harness-pinned orchestrator revision."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import sys

from adapter import AdapterError, cache_root, git, git_env, load_lock, validate_runtime

INSTALL_TIMEOUT = 300


def install(*, source=None, destination_root=None, lock_path=None):
    lock = load_lock(lock_path)
    base = Path(destination_root).expanduser().resolve() if destination_root else cache_root()
    versions = base / 'versions'
    if versions.is_symlink():
        raise AdapterError('Каталог версий не должен быть ссылкой: ' + str(versions))
    versions.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = versions / lock['commit']
    if destination.exists() or destination.is_symlink():
        return validate_runtime(destination, lock)
    origin = str(source) if source is not None else lock['repository']
    if not origin or origin.startswith('-'):
        raise AdapterError('Недопустимый источник Git')
    # The temporary clone is on the same filesystem: rename publishes a fully
    # validated version. A failed download leaves no apparently installed version.
    with tempfile.TemporaryDirectory(prefix='.install-', dir=versions) as temp:
        temp = Path(temp)
        checkout = temp / 'checkout'
        hooks = temp / 'empty-hooks'
        hooks.mkdir()
        command = ['git', '-c', 'core.hooksPath=' + str(hooks), 'clone', '--no-local',
                   '--no-checkout', '--template=' + str(hooks), '--', origin, str(checkout)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                    errors='replace', env=git_env(), timeout=INSTALL_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdapterError('Не удалось загрузить оркестратор: ' + str(exc)) from exc
        if result.returncode:
            raise AdapterError('Git не загрузил оркестратор. Проверьте доступ к приватному репозиторию. '
                               + result.stderr.strip()[:1000])
        git(checkout, 'config', 'core.autocrlf', 'false')
        git(checkout, '-c', 'core.hooksPath=' + str(hooks), 'checkout', '--detach', lock['commit'],
            timeout=INSTALL_TIMEOUT)
        validate_runtime(checkout, lock)
        try:
            # Never overwrite a version installed by a concurrent invocation.
            checkout.rename(destination)
        except OSError:
            if not destination.exists():
                raise
            validate_runtime(destination, lock)
        return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', help='Git URL or local repository containing the pinned commit')
    parser.add_argument('--cache-root', type=Path, help='User-owned installation directory')
    args = parser.parse_args(argv)
    try:
        root = install(source=args.source, destination_root=args.cache_root)
    except (AdapterError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print('Установлена закреплённая версия SDD Orchestrator: ' + str(root))
    if args.cache_root:
        print('Для запуска через адаптер задайте SDD_ORCHESTRATOR_CACHE=' + str(args.cache_root.expanduser().resolve()))
    print('Сохранённые прогоны, настройки и работающие процессы не изменены.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
