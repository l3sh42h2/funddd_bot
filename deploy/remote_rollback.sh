#!/usr/bin/env bash
# Откат выката (на сервере): последняя проверенная версия из .prev → боевая папка, её юниты, рестарт.
set -euo pipefail
source "$(dirname "$0")/remote_lib.sh"
need_lock "${1:-}"
[ -d "$PREV/src" ] || { echo "нет снимка прежней версии в $PREV"; exit 1; }
mult_gate "$PREV"          # .prev до фазы 1 и открытая сделка с множителем контракта — стоп (ревью 13.09, F1)
rsync -a --delete "${EXCL[@]}" "$PREV/" "$DEST/"
cd "$DEST"
ensure_installed
sudo cp deploy/funding_bot-collector.service deploy/funding_bot-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart funding_bot-collector funding_bot-web
echo "откат выполнен: $(date -u +%H:%M:%S) UTC"
