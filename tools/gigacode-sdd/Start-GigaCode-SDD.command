#!/bin/bash
# Finder opens this file in Terminal; paths are independent of the current folder.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)" || exit 2
cd "$SCRIPT_DIR" || exit 2
PYTHON_PATH="$(command -v python3 2>/dev/null)"
SELECTED_PYTHON=""
for candidate in "$PYTHON_PATH" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [ -n "$candidate" ] && [ -x "$candidate" ] || continue
    # Avoid triggering Apple's Command Line Tools installation dialog.
    if [ "$candidate" = /usr/bin/python3 ] && ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
        continue
    fi
    if "$candidate" "$SCRIPT_DIR/bootstrap.py" --check-python >/dev/null 2>&1; then
        SELECTED_PYTHON="$candidate"
        break
    fi
done
if [ -z "$SELECTED_PYTHON" ]; then
    echo 'Не найден установленный Python 3.10+. Обратитесь к администратору за корпоративной установкой через Nexus.'
    echo 'Автоматическое скачивание не выполняется. Процессы стенда не остановлены.'
    read -r -p 'Нажмите Enter, чтобы закрыть окно. '
    exit 2
fi
"$SELECTED_PYTHON" "$SCRIPT_DIR/bootstrap.py" "$@"
RESULT=$?
read -r -p 'Нажмите Enter, чтобы закрыть окно. '
exit "$RESULT"
