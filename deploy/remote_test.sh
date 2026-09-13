#!/usr/bin/env bash
# Шаг 3 выката (на сервере): тесты новой версии в ~/hyper/funding_bot.next интерпретатором боевого venv.
# Импорт обязан идти из .next (PYTHONPATH + проверка пути), а не из установленной в venv боевой копии.
# Всё, что тянет сеть, — здесь, до переключения: setuptools нужен установке без build isolation на шаге переключения.
set -euo pipefail
source "$(dirname "$0")/remote_lib.sh"
need_lock "${1:-}"
PY="$DEST/.venv/bin/python"
"$DEST/.venv/bin/pip" -q install 'requests>=2.28' 'pytest>=8' 'setuptools>=68' >/dev/null
cd "$NEXT"
PYTHONPATH="$NEXT/src" "$PY" -c "import funding_bot, sys; p = funding_bot.__file__; sys.exit(0 if p.startswith('$NEXT/') else 'импорт не из .next: ' + p)"
PYTHONPATH="$NEXT/src" "$PY" -m pytest tests -q -p no:cacheprovider
