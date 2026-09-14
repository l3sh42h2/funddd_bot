#!/usr/bin/env bash
set -euo pipefail
echo 'RETIRED: deploy switching is accepted only through deploy/deploy.sh (M5 supervised job)' >&2
exit 64
# Шаг 4 выката (на сервере): снимок → .prev, проверенная тестами .next → боевая папка, юниты, рестарт.
# .prev — последняя ПРОВЕРЕННАЯ версия: метку .verified ставит проверка после рестарта. Выкат, упавший на полпути,
# не затирает её непроверенным кодом (проверка исправлений 11.09). Что может упасть, не трогая боевую папку
# (sudo), — до rsync; установка пакета — без сети. Упало после rsync — deploy.sh делает откат.
#
# Трейдер (фаза 2): рестарт посреди исполнения оборвал бы пару ног (своп прошёл — хедж не поставлен). Поэтому
# выкат останавливается с «идёт исполнение», пока в trade.db есть намерение approved/running (trade_spec §10).
# Проверка — ДО замены кода (после снимка .prev: откат deploy.sh вернёт ту же версию) и ещё раз прямо перед
# рестартом трейдера (кнопку могли нажать, пока шёл rsync).
# Связка SOL×HL (M10): «тихо» — это ещё и ни одной неразрешённой попытки Solana/HL (trader_idle); версию без связки
# при сделке или попытке SOL/HL не ставим (sol_gate). Зависимости lock-файла — в боевой venv после ворот и до замены
# кода, из колёс шага тестов, без сети; после замены — импорт связки боевым venv (не прошёл — deploy.sh откатывает).
set -euo pipefail
source "$(dirname "$0")/remote_lib.sh"
need_lock "${1:-}"
[ -d "$NEXT/src" ] || { echo "нет $NEXT/src — переключение не начато"; exit 1; }
sudo -n true || { echo "sudo без пароля недоступен — переключение не начато"; exit 1; }

# число намерений approved/running в runtime/trade.db (нет БД — 0); не прочиталась — ошибка, выкат стоит
trader_busy() {
  local db="$DEST/runtime/trade.db"
  [ -e "$db" ] || { echo 0; return 0; }
  "$DEST/.venv/bin/python" - "$db" <<'EOF'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1], timeout=10)
try:
    print(con.execute("SELECT count(*) FROM intents WHERE status IN ('approved','running')").fetchone()[0])
except sqlite3.OperationalError as e:
    if "no such table" in str(e):
        print(0)
    else:
        raise
EOF
}

trader_gate() {
  local busy why
  busy=$(trader_busy) || { echo "!! trade.db не прочитана — трейдер не трогаю, выкат остановлен"; exit 1; }
  if [ "$busy" != "0" ]; then
    echo "!! идёт исполнение (намерений approved/running: $busy) — выкат остановлен, трейдер не перезапущен"
    exit 1
  fi
  if ! why=$(trader_idle); then
    echo "!! $why — выкат остановлен, трейдер не перезапущен"
    exit 1
  fi
}

if [ -e "$DEST/.verified" ] || [ ! -d "$PREV/src" ]; then
  rm -rf "$PREV"
  mkdir -p "$PREV"
  rsync -a --delete "${EXCL[@]}" "$DEST/" "$PREV/"
else
  echo "боевая версия не подтверждена проверками — .prev (последняя проверенная) не трогаю"
fi
mult_gate "$NEXT"          # версия до фазы 1 и открытая сделка с множителем контракта — стоп (ревью 13.09, F1)
sol_gate "$NEXT"           # версия без связки SOL×HL при сделке или попытке SOL/HL — стоп (M06, M10)
trader_gate
install_lock "$DEST/.venv" "$NEXT/$LOCKFILE"      # те же колёса, что прошли тесты в .next; без сети
trader_gate
rsync -a --delete "${EXCL[@]}" "$NEXT/" "$DEST/"
cd "$DEST"
mkdir -p runtime logs
# systemd открывает журнал (append:) от root ДО смены пользователя: без этого файлы root:root, и logrotate
# с «su admin admin» не может их обрезать — журналы росли бы молча (проверка исправлений 11.09)
for f in logs/collector.log logs/web.log logs/trader.log; do [ -e "$f" ] || install -m 644 /dev/null "$f"; done
sudo chown admin:admin logs/collector.log logs/web.log logs/trader.log
ensure_installed
.venv/bin/python -c 'import solders.keypair, base58, hyperliquid.utils.signing, funding_bot.trade.solana.sign, funding_bot.trade.hyperliquid_trade, funding_bot.trade.sol_flow' \
  || { echo "!! боевой venv не импортирует связку SOL×HL — переключение упало"; exit 1; }
sudo cp deploy/funding_bot-collector.service deploy/funding_bot-web.service deploy/funding_bot-trader.service \
  /etc/systemd/system/
sudo install -m 644 deploy/logrotate.conf /etc/logrotate.d/funding_bot
sudo systemctl daemon-reload
sudo systemctl enable funding_bot-collector funding_bot-web funding_bot-trader >/dev/null 2>&1
sudo systemctl restart funding_bot-collector funding_bot-web
trader_gate
# без TG_BOT_TOKEN/owner.toml трейдер выходит с кодом 78 и не перезапускается по кругу; коллектор это не откатывает
sudo systemctl restart funding_bot-trader || echo "!! трейдер не стартовал — см. logs/trader.log (коллектор и веб переключены)"
echo "переключено: $(date -u +%H:%M:%S) UTC"
