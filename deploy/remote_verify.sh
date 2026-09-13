#!/usr/bin/env bash
# Шаг 5 выката (на сервере). Аргументы: <токен> <strict|loose>.
# strict: оба сервиса active, /status отвечает (curl -fsS), таблицу пишет ИМЕННО текущий процесс коллектора
#   (pid в table.json = MainPID из systemd — раньше проверка проходила по таблице, которую успел записать ещё
#   старый процесс), тик после его старта, таблицы непустые — и так 10 проверок подряд (20 с) без смены pid и без
#   рестартов (падающий по кругу процесс успевает побыть active между рестартами).
# loose — после отката: прежняя версия может не писать pid — тик после старта процесса и те же 20 с без рестартов.
# Прошло — версия помечается проверенной (.verified): следующий выкат снимет с неё .prev.
set -euo pipefail
source "$(dirname "$0")/remote_lib.sh"
need_lock "${1:-}"
MODE="${2:-strict}"
good=0; last=""
for _ in $(seq 1 60); do
  sleep 2
  if ! systemctl is-active --quiet funding_bot-collector || ! systemctl is-active --quiet funding_bot-web; then
    good=0; continue
  fi
  pid=$(systemctl show -p MainPID --value funding_bot-collector)
  nr=$(systemctl show -p NRestarts --value funding_bot-collector)
  started=$(date -d "$(systemctl show -p ActiveEnterTimestamp --value funding_bot-collector)" +%s 2>/dev/null || echo 0)
  s=$(curl -fsS -m 5 http://127.0.0.1:8792/status 2>/dev/null) || { good=0; continue; }
  if python3 - "$s" "$pid" "$started" "$MODE" <<'EOF'
import json, sys
d, pid, started, mode = json.loads(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
ok = d.get("age_s") is not None and d["age_s"] < 30 and (d.get("tick_ts") or 0) >= started > 0
if mode == "strict":
    ok = ok and d.get("pid") == pid and (d.get("n_ff") or 0) > 0 and (d.get("n_sf") or 0) > 0
sys.exit(0 if ok else 1)
EOF
  then
    if [ "$pid:$nr" = "$last" ]; then good=$((good + 1)); else good=1; last="$pid:$nr"; fi
    if [ "$good" -ge 10 ]; then
      echo "ok ($MODE): pid $pid, рестартов $nr; status: $(echo "$s" | head -c 240)"
      echo "$1" > "$DEST/.verified"
      lr=$(sudo -n logrotate -d /etc/logrotate.d/funding_bot 2>&1 || true)
      if echo "$lr" | grep -i "error" >/dev/null; then
        echo "!! logrotate видит ошибки (журналы не ротируются): $(echo "$lr" | grep -i error | head -3)"
      fi
      # трейдер — не ворота выката (он может стоять по коду 78 до заполнения owner.toml), но молчать о круге рестартов
      # нельзя: два замера NRestarts через 12 с
      if systemctl is-enabled --quiet funding_bot-trader 2>/dev/null; then
        r1=$(systemctl show -p NRestarts --value funding_bot-trader); sleep 12
        r2=$(systemctl show -p NRestarts --value funding_bot-trader)
        ta=$(systemctl is-active funding_bot-trader || true)
        tc=$(systemctl show -p ExecMainStatus --value funding_bot-trader)
        tl=$(tail -n 2 "$DEST/logs/trader.log" 2>/dev/null | tr '\n' ' ' | head -c 300 || true)
        if [ "$r2" != "$r1" ]; then echo "!! трейдер перезапускается по кругу ($r1 → $r2 за 12 с, код $tc): $tl"
        elif [ "$ta" = "active" ]; then echo "трейдер: active, рестартов $r2"
        else echo "!! трейдер: $ta (код $tc; 78 = настройка, без перезапуска): $tl"; fi
      fi
      exit 0
    fi
  else
    good=0
  fi
done
echo "FAIL: collector=$(systemctl is-active funding_bot-collector || true) web=$(systemctl is-active funding_bot-web || true)" \
     "pid=$(systemctl show -p MainPID --value funding_bot-collector) рестартов=$(systemctl show -p NRestarts --value funding_bot-collector)"
echo "status: $(curl -s -m 5 http://127.0.0.1:8792/status | head -c 300 || true)"
echo "журнал: $(tail -n 5 "$DEST/logs/collector.log" 2>/dev/null || true)"
exit 1
