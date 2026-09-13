#!/usr/bin/env bash
# Общее для шагов выката на сервере (подключается через source): пути, исключения rsync, замок, установка пакета.
set -euo pipefail
DEST=/home/admin/hyper/funding_bot
NEXT=/home/admin/hyper/funding_bot.next
PREV=/home/admin/hyper/funding_bot.prev
LOCK=/home/admin/hyper/.funding_bot.deploy.lock
EXCL=(--exclude .venv --exclude runtime --exclude logs --exclude __pycache__ --exclude '*.egg-info' --exclude .env --exclude .pytest_cache)

# Каждый шаг выполняется только выкатом, который держит замок: два выката одновременно — второй ждёт человека,
# а не подменяет первому .next между его тестами и переключением (проверка исправлений 11.09).
need_lock() {
  if [ -z "${1:-}" ] || [ "$(cat "$LOCK/owner" 2>/dev/null)" != "$1" ]; then
    echo "!! замок выката держит не этот выкат (${1:-без токена}) — шаг не выполнен"
    exit 1
  fi
}

# Ревью 13.09 (F1): версия до фазы 1 (её store.py не знает inst_json) читает контракты сделки как токены — открытую
# сделку с множителем контракта (m ≠ 1) она не закроет, а её «дохедж» отправит заявку ×m. Такую версию на место боевой
# не ставим (переключение и откат), пока в runtime/trade.db есть активная сделка с m ≠ 1 или с нечитаемым инструментом
# (m не известен). trade.db не прочиталась — тоже стоп. Сделки m = 1 (и открытые до фазы 1, без inst_json) не мешают.
mult_deals() {
  "$DEST/.venv/bin/python" - "$DEST/runtime/trade.db" <<'EOF'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1], timeout=10)
try:
    print(con.execute(
        "SELECT count(*) FROM deals WHERE state NOT IN ('DRAFT','CLOSED','ABORTED') AND COALESCE(inst_json, '') != '' "
        "AND (CASE WHEN json_valid(inst_json) THEN CAST(json_extract(inst_json, '$.units_per_contract') AS REAL) END) "
        "IS NOT 1").fetchone()[0])
except sqlite3.OperationalError as e:
    if "no such table" in str(e) or "no such column" in str(e):
        print(0)                               # БД до фазы 1: сделок со спецификацией инструмента нет
    else:
        raise
EOF
}

mult_gate() {
  local target="$1" n
  if grep -q inst_json "$target/src/funding_bot/trade/store.py" 2>/dev/null || [ ! -e "$DEST/runtime/trade.db" ]; then
    return 0
  fi
  n=$(mult_deals) || { echo "!! trade.db не прочитана — версию до фазы 1 не ставлю, выкат остановлен"; exit 1; }
  if [ "$n" != "0" ]; then
    echo "!! открыта сделка с множителем контракта ($n) — версия до фазы 1 её не понимает, сначала закрыть"
    exit 1
  fi
}

# Установка пакета без сети (build isolation тянет setuptools с PyPI) и только если venv смотрит не в боевую папку:
# editable-установка уже указывает на $DEST/src, новый код подхватывается без переустановки.
ensure_installed() {
  if "$DEST/.venv/bin/python" -c "import funding_bot, sys; sys.exit(0 if funding_bot.__file__.startswith('$DEST/src/') else 1)" 2>/dev/null \
     && [ -x "$DEST/.venv/bin/funding_bot" ]; then
    return 0
  fi
  (cd "$DEST" && .venv/bin/pip -q install -e . --no-deps --no-build-isolation >/dev/null)
}
