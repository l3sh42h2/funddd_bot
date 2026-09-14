#!/usr/bin/env bash
set -euo pipefail
echo 'RETIRED: code-only rollback is accepted only through deploy/deploy.sh (M5 supervised job)' >&2
exit 64
# Откат выката (на сервере): последняя проверенная версия из .prev → боевая папка, её юниты, рестарт.
# runtime/trade.db не трогается: откат кода не откатывает сделки. Версию, которая не поймёт открытую сделку или
# неразрешённую попытку, не ставим (mult_gate, sol_gate). Трейдер — явным шагом: иначе он дорабатывал бы в памяти
# откатываемой версией, подгружая файлы .prev; рестарт — только когда тихо (trader_idle), иначе громко и без рестарта.
set -euo pipefail
source "$(dirname "$0")/remote_lib.sh"
need_lock "${1:-}"
[ -d "$PREV/src" ] || { echo "нет снимка прежней версии в $PREV"; exit 1; }
mult_gate "$PREV"          # .prev до фазы 1 и открытая сделка с множителем контракта — стоп (ревью 13.09, F1)
sol_gate "$PREV"           # .prev без связки SOL×HL при сделке или попытке SOL/HL — стоп (M06, M10)
rsync -a --delete "${EXCL[@]}" "$PREV/" "$DEST/"
cd "$DEST"
ensure_installed
sudo cp deploy/funding_bot-collector.service deploy/funding_bot-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart funding_bot-collector funding_bot-web
if systemctl is-enabled --quiet funding_bot-trader 2>/dev/null; then
  if why=$(trader_idle); then
    sudo cp deploy/funding_bot-trader.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl restart funding_bot-trader || echo "!! трейдер не стартовал — см. logs/trader.log"
  else
    echo "!! трейдер не перезапущен: $why — после разбора: sudo systemctl restart funding_bot-trader"
  fi
fi
echo "откат выполнен: $(date -u +%H:%M:%S) UTC"
