#!/usr/bin/env bash
# Общее для шагов выката на сервере (подключается через source): пути, исключения rsync, замок, установка пакета.
set -euo pipefail
DEST=/home/admin/hyper/funding_bot
NEXT=/home/admin/hyper/funding_bot.next
PREV=/home/admin/hyper/funding_bot.prev
LOCK=/home/admin/hyper/.funding_bot.deploy.lock
WHEELS=/home/admin/hyper/funding_bot.wheels     # колёса lock-файла: качает remote_test.sh (сеть), ставит remote_switch.sh
LOCKFILE=deploy/requirements.lock
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

# Состояние исполнения в runtime/trade.db одной строкой «намерений попыток_Solana попыток_HL сделок_SOL/HL min_reader»
# (M10: «нет approved/running» ещё не значит «тихо»). Попытка Solana не разрешена, пока подписана без доказанного исхода
# или её чек не стал finalized (как trade/solana/journal.unresolved); VALIDATED без подписи в сеть уйти не могла.
# Hyperliquid: PREPARED/SIGNED/UNKNOWN в hl_order_attempts (как hyperliquid_trade.OPEN_STATES) и SENT/UNKNOWN заявки HL
# в perp_orders. Сделка SOL/HL — незакрытая со scope перпа (schema 2), сетью solana или площадкой hyperliquid; сделки
# BSC/Aster (DQA9Q) сюда не входят. Нет таблицы (БД до схемы 2) — 0; нет БД — все нули.
trade_state() {
  local db="$DEST/runtime/trade.db"
  [ -e "$db" ] || { echo "0 0 0 0 0"; return 0; }
  "$DEST/.venv/bin/python" - "$db" <<'EOF'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1], timeout=10)
Q = (
    "SELECT count(*) FROM intents WHERE status IN ('approved','running')",
    "SELECT count(*) FROM sol_tx_attempts a WHERE a.state IN ('SIGNED_DURABLE','BROADCAST_ATTEMPTED','UNKNOWN',"
    "'CONFIRMED_OK','CONFIRMED_ERR') OR (a.state IN ('FINALIZED_OK','FINALIZED_ERR') AND (NOT EXISTS (SELECT 1 FROM "
    "sol_receipts r WHERE r.network=a.network AND r.signature=a.signature) OR EXISTS (SELECT 1 FROM sol_receipts r "
    "WHERE r.network=a.network AND r.signature=a.signature AND r.commitment<>'finalized')))",
    "SELECT count(*) FROM hl_order_attempts WHERE state IN ('PREPARED','SIGNED','UNKNOWN')",
    "SELECT count(*) FROM perp_orders WHERE venue='hyperliquid' AND state IN ('SENT','UNKNOWN')",
    "SELECT count(*) FROM deals WHERE state NOT IN ('DRAFT','CLOSED','ABORTED') AND (perp_scope IS NOT NULL "
    "OR chain='solana' OR perp_venue='hyperliquid')",
    "SELECT min_reader FROM schema_version WHERE id=1",
)
out = []
for q in Q:
    try:
        row = con.execute(q).fetchone()
        out.append(int(row[0]) if row and row[0] is not None else 0)
    except sqlite3.OperationalError as e:
        if "no such table" in str(e) or "no such column" in str(e):
            out.append(0)
        else:
            raise
intents, sol, hl_att, hl_ord, deals, min_reader = out
print(intents, sol, hl_att + hl_ord, deals, min_reader)
EOF
}

# 0 — трейдер можно перезапускать; иначе печатает причину. Рестарт посреди разбора попытки оставил бы голую ногу на
# время подъёма, а неразрешённая попытка в сделке на паузе — не «тихо» (M10). Разбор — сверка трейдера («позиции»).
trader_idle() {
  local st intents sol hl deals mr
  st=$(trade_state) || { echo "trade.db не прочитана"; return 1; }
  read -r intents sol hl deals mr <<<"$st"
  if [ "$intents" != "0" ]; then echo "идёт исполнение (намерений approved/running: $intents)"; return 1; fi
  if [ "$sol" != "0" ] || [ "$hl" != "0" ]; then
    echo "неразрешённые попытки: Solana $sol, Hyperliquid $hl (разбор — «позиции» в боте)"
    return 1
  fi
}

# Связка SOL×HL (M06, M10): версия без неё (store.py без SCHEMA_VERSION — ворот схемы нет) выбрала бы для сделки
# Solana ноги BSC/Aster и не знает резолверов попыток Solana/HL. Такую версию на место боевой не ставим (переключение и
# откат), пока в trade.db есть незакрытая сделка SOL/HL или неразрешённая попытка Solana/HL. Версия с воротами сама
# откажется от слишком новой БД — но тогда её не ставим и здесь (схема версии ниже min_reader БД), чтобы служба не
# падала по кругу. trade.db не прочиталась — стоп. БД не подменяется никогда: откат кода не откатывает сделки.
sol_gate() {
  local target="$1" st tv intents sol hl deals mr
  [ -e "$DEST/runtime/trade.db" ] || return 0
  st=$(trade_state) || { echo "!! trade.db не прочитана — версию не ставлю, выкат остановлен"; exit 1; }
  read -r intents sol hl deals mr <<<"$st"
  tv=$(grep -m 1 -oE '^SCHEMA_VERSION = [0-9]+' "$target/src/funding_bot/trade/store.py" 2>/dev/null \
       | grep -oE '[0-9]+$' || true)
  if [ -n "$tv" ]; then
    if [ "$tv" -lt "$mr" ]; then
      echo "!! версия со схемой $tv не откроет trade.db (min_reader $mr) — не ставлю, выкат остановлен"
      exit 1
    fi
    return 0
  fi
  if [ "$deals" != "0" ] || [ "$sol" != "0" ] || [ "$hl" != "0" ]; then
    echo "!! версия без связки SOL×HL, а в trade.db сделок SOL/HL: $deals, неразрешённых попыток Solana: $sol," \
         "Hyperliquid: $hl — не ставлю, сначала закрыть и разобрать"
    exit 1
  fi
}

# Зависимости (ТЗ SOL×HL §3, M09): полное замыкание lock-файла из колёс, скачанных шагом тестов, с --no-deps и без
# сети, затем pip check. Ставятся ровно те файлы, на которых прошли тесты: lock тот же, SHA256SUMS сходится.
install_lock() {
  local venv="$1" lockf="$2"
  cmp -s "$lockf" "$WHEELS/requirements.lock" || { echo "!! $lockf не тот, с которым скачаны колёса — стоп"; exit 1; }
  (cd "$WHEELS" && sha256sum -c --quiet --strict SHA256SUMS) || { echo "!! колёса в $WHEELS изменились — стоп"; exit 1; }
  "$venv/bin/python" -m pip -q install --no-index --find-links "$WHEELS" --no-deps -r "$lockf" >/dev/null \
    || { echo "!! зависимости из lock не встали в $venv — стоп"; exit 1; }
  "$venv/bin/python" -m pip check || { echo "!! pip check в $venv не прошёл — стоп"; exit 1; }
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
