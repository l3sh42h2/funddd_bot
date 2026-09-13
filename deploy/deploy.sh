#!/usr/bin/env bash
# Выкат funding_bot на VPS ireland. Внешнее ревью 11.09 (п.8) и проверка исправлений:
#   1. тесты на Маке — здесь есть JavaScriptCore, и тест страницы не пропускается;
#   2. замок выката на сервере: второй одновременный выкат останавливается, а не подменяет .next первому;
#   3. код → ~/hyper/funding_bot.next, тесты там на Python 3.11 сервера; боевая папка не тронута;
#   4. .prev = последняя ПРОВЕРЕННАЯ версия, .next → боевая, рестарт; упало переключение — откат;
#   5. проверки: оба сервиса active, /status отвечает, таблицу пишет новый процесс (pid = MainPID) и живёт 20 с без
#      рестартов, таблицы непустые — версия помечается проверенной; иначе откат и ненулевой код выхода.
# Миграции БД только добавляют колонки и таблицы — прежняя версия работает с новой схемой, откат безопасен.
# runtime/ и .env на сервере не трогаются никогда.
set -euo pipefail
VPS="admin@34.65.234.12"
SSH_OPTS=(-i "$HOME/.ssh/google_compute_engine" -o IdentitiesOnly=yes -o UserKnownHostsFile="$HOME/.ssh/google_compute_known_hosts")
NEXT=/home/admin/hyper/funding_bot.next
LOCK=/home/admin/hyper/.funding_bot.deploy.lock
TOKEN="$(hostname -s)-$$-$(date +%s)"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
EXCL=(--exclude .venv --exclude runtime --exclude logs --exclude __pycache__ --exclude '*.egg-info' --exclude .env --exclude .pytest_cache)
on_vps() { ssh "${SSH_OPTS[@]}" "$VPS" "$@"; }
remote() { local s=$1; shift; on_vps "bash $NEXT/deploy/$s $*"; }
rollback() {
  echo '!! возвращаю последнюю проверенную версию'
  if remote remote_rollback.sh "$TOKEN" && remote remote_verify.sh "$TOKEN" loose; then
    echo '==> прежняя версия поднята'
  else
    echo '!! И ПРЕЖНЯЯ ВЕРСИЯ НЕ ПОДНЯЛАСЬ — нужен человек'
  fi
}

echo "==> 1/5 тесты на Маке (с тестом страницы в JavaScriptCore)"
( cd "$SRC" && .venv/bin/python -m pytest tests -q -p no:cacheprovider ) \
  || { echo '!! ТЕСТЫ НА МАКЕ КРАСНЫЕ — выкат остановлен, сервер не тронут'; exit 1; }

echo "==> 2/5 замок выката"
if ! on_vps "mkdir $LOCK && echo $TOKEN > $LOCK/owner"; then
  echo "!! на сервере идёт другой выкат: $(on_vps "cat $LOCK/owner; stat -c %y $LOCK" 2>/dev/null || true)"
  echo "   если он мёртв — снять замок руками: ssh $VPS rm -rf $LOCK"
  exit 1
fi
trap 'on_vps "[ \"\$(cat $LOCK/owner 2>/dev/null)\" = $TOKEN ] && rm -rf $LOCK" || true' EXIT

echo "==> 3/5 код -> $NEXT, тесты на сервере (боевая папка не тронута)"
rsync -az --delete "${EXCL[@]}" -e "ssh ${SSH_OPTS[*]}" "$SRC/" "$VPS:$NEXT/"
remote remote_test.sh "$TOKEN" || { echo '!! ТЕСТЫ НА СЕРВЕРЕ КРАСНЫЕ — выкат остановлен, боевая версия не тронута'; exit 1; }

echo "==> 4/5 снимок проверенной версии, переключение, рестарт"
remote remote_switch.sh "$TOKEN" || { echo '!! ПЕРЕКЛЮЧЕНИЕ УПАЛО НА ПОЛПУТИ'; rollback; exit 1; }

echo "==> 5/5 проверки после рестарта"
if remote remote_verify.sh "$TOKEN" strict; then
  echo "==> done"
else
  echo '!! ПРОВЕРКИ НЕ ПРОШЛИ'
  rollback
  exit 1
fi
