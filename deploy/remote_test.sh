#!/usr/bin/env bash
# Шаг 3 выката (на сервере): тесты новой версии в ~/hyper/funding_bot.next — в ЧИСТОМ venv .next/.venv (тот же Python,
# что у боевого) с полным замыканием deploy/requirements.lock (ТЗ SOL×HL §3, M09). Боевой venv до зелёных тестов не
# меняется (кроме setuptools): lock в него ставит переключение, из тех же колёс.
# Импорт обязан идти из .next: funding_bot — из .next/src (PYTHONPATH), solders/hyperliquid — из .next/.venv.
# Всё, что тянет сеть, — здесь, до переключения: колёса lock-файла (только бинарные, под Python и платформу сервера) —
# в $WHEELS с SHA256SUMS; setuptools — для установки без build isolation на шаге переключения.
set -euo pipefail
source "$(dirname "$0")/remote_lib.sh"
need_lock "${1:-}"
LOCKF="$NEXT/$LOCKFILE"
[ -f "$LOCKF" ] || { echo "нет $LOCKF — тесты не начаты"; exit 1; }
"$DEST/.venv/bin/pip" -q install 'setuptools>=68' >/dev/null
rm -rf "$WHEELS"
mkdir -p "$WHEELS"
"$DEST/.venv/bin/python" -m pip -q download --only-binary=:all: --no-deps -d "$WHEELS" -r "$LOCKF" >/dev/null \
  || { echo "!! колёса lock-файла не скачались (нет колеса под сервер?) — тесты не начаты"; exit 1; }
cp "$LOCKF" "$WHEELS/requirements.lock"
(cd "$WHEELS" && sha256sum -- *.whl > SHA256SUMS)
rm -rf "$NEXT/.venv"
"$DEST/.venv/bin/python" -m venv "$NEXT/.venv"
install_lock "$NEXT/.venv" "$LOCKF"
PY="$NEXT/.venv/bin/python"
cd "$NEXT"
PYTHONPATH="$NEXT/src" "$PY" - "$NEXT" <<'EOF'
import sys
from importlib.metadata import version
import base58, Crypto, eth_account, eth_utils, msgpack, websocket                     # noqa: F401
import funding_bot, hyperliquid, hyperliquid.utils.signing, solders, solders.keypair  # noqa: F401
root = sys.argv[1].rstrip("/") + "/"
bad = [f"{m.__name__}: {m.__file__}" for m in (funding_bot, solders, hyperliquid) if not m.__file__.startswith(root)]
if bad:
    sys.exit("импорт не из .next: " + "; ".join(bad))
print("зависимости .next:", ", ".join(f"{p} {version(p)}" for p in (
    "solders", "hyperliquid-python-sdk", "base58", "eth-account", "eth-utils", "pycryptodome")),
    f"· Python {sys.version.split()[0]}")
EOF
PYTHONPATH="$NEXT/src" "$PY" -m pytest tests -q -p no:cacheprovider
