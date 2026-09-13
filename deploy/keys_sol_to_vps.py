#!/usr/bin/env python3
"""Переносит ключи связок SOL/Hyperliquid и Gate из botLP.rtf владельца в .env на VPS ireland. ЗАПУСКАЕТ ВЛАДЕЛЕЦ.

Значения нигде не печатаются и не попадают в аргументы команд: уходят на сервер через stdin ssh (сама программа для
сервера — base64 в аргументе, в ней секретов нет). На экран — только имена и длины. Перед записью на сервере делается
копия .env в ~/.config/funding_bot/ (0600); строки с теми же именами заменяются, остальные не трогаются, права .env
остаются 0600.

  SOLANA_SECRET_B58        ← sol          (base58, 64 байта: секрет + публичный ключ)
  HL_AGENT_PRIVATE_KEY     ← hyper2_private / api_private (ключ API-кошелька HL; адрес = hyper2_api_wallet / api_wallet_address,
                             и он должен быть одобренным агентом торгового счёта hyper2_public — иначе не отправляется)
  JUPITER_API_KEY          ← jupiter_api
  SOLANA_RPC_URL           ← https://mainnet.helius-rpc.com/?api-key=<helius_api>
  SOLANA_RPC_SECONDARY_URL ← https://api.mainnet-beta.solana.com (публичный, только для сверки)
  GATE_API_KEY / SECRET    ← gatekey / gatesecret (при настоящем запуске — подписанная проверка фьючерсов, только чтение)

--check     проверка botLP без записи на сервер (делает один публичный запрос к Hyperliquid: только адрес агента);
--selftest  проверка доставки на сервер фиктивными значениями во временный файл (botLP не читается).
"""
import base64, json, re, shlex, subprocess, sys

SRC = "/Users/admin/Documents/hyper/botLP.rtf"
ENV = "/home/admin/hyper/funding_bot/.env"
VPS = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", "-o", "ControlMaster=auto",
       "-o", "ControlPath=~/.ssh/cm-%r@%h:%p", "-o", "ControlPersist=600", "admin@34.65.234.12"]
SSH_TIMEOUT_S = 60
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Программа для сервера: путь файла — аргумент, значения — stdin (JSON). Копия прежнего файла — только если он был.
REMOTE = r'''
import json, os, sys, time
env = sys.argv[1]
new = json.loads(sys.stdin.read())
old = open(env).read().splitlines() if os.path.exists(env) else []
bak = None
if old:
    bak_dir = os.path.expanduser("~/.config/funding_bot"); os.makedirs(bak_dir, mode=0o700, exist_ok=True)
    os.chmod(bak_dir, 0o700)
    bak = f"{bak_dir}/env.bak-{time.time_ns()}"
    with open(os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        f.write("\n".join(old) + "\n")
out = [l for l in old if l.split("=", 1)[0].strip() not in new]
out += [f"{k}={v}" for k, v in new.items()]
tmp = env + ".tmp"
try:
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, env)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
os.chmod(env, 0o600)
for l in open(env).read().splitlines():
    if "=" in l:
        k, v = l.split("=", 1); print(f"  {k} len={len(v)}")
print("копия прежнего файла:", bak or "не было")
'''


def fail(msg: str) -> None:
    print("⛔ " + msg + " — ничего не записано")
    sys.exit(1)


def send(new: dict, target: str) -> str:
    code = base64.b64encode(REMOTE.encode()).decode()
    cmd = "python3 -c " + shlex.quote(f"import base64;exec(base64.b64decode('{code}').decode())") + " " + shlex.quote(target)
    try:
        r = subprocess.run(VPS + [cmd], input=json.dumps(new), text=True, capture_output=True, timeout=SSH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        fail(f"сервер не ответил за {SSH_TIMEOUT_S} с")
    if r.returncode != 0:
        fail("сервер ответил ошибкой: " + (r.stderr.strip().splitlines() or ["?"])[-1][:200])
    return r.stdout


if "--selftest" in sys.argv:                      # доставка без секретов: фиктивные значения во временный файл
    t = "/tmp/fb_keys_selftest.env"
    subprocess.run(VPS + ["rm -f " + t], capture_output=True, timeout=SSH_TIMEOUT_S)
    out = send({"FB_SELFTEST_A": "x" * 10, "FB_SELFTEST_URL": "https://example.invalid/?k=a&b=c'd\"e"}, t)
    subprocess.run(VPS + ["rm -f " + t], capture_output=True, timeout=SSH_TIMEOUT_S)
    print("✅ доставка работает (временный файл удалён):")
    print(out)
    sys.exit(0)

try:
    import requests
    from eth_account import Account
except ImportError as e:
    fail(f"в этом Python нет модуля {e.name} — запускайте через /Users/admin/Documents/vps/funding_bot/.venv/bin/python")


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + B58.index(c)
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(s) - len(s.lstrip("1"))) + b


raw = open(SRC, encoding="latin-1").read()
txt = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"), raw)
txt = re.sub(r"\\u-?\d+\??", " ", txt)            # юникод-экраны RTF (\u8203? и т.п.) — не часть значения
txt = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", txt)
txt = re.sub(r"[{}]", "", txt)
txt = txt.replace("\u00a0", " ").replace("\u200b", "")   # неразрывный пробел и нулевой ширины из вставки с веба
kv = {}
for line in txt.splitlines():
    m = re.match(r"^\s*([A-Za-z0-9_]+)\s*[:=]\s*(\S+?)\s*\\?\s*$", line)   # хвостовой пробел/NBSP перед «\» абзаца
    if m:
        kv[m.group(1)] = m.group(2).rstrip("\\")
# 13.09 владелец добавил в botLP счёт и API-кошелёк HL строками hyper2_*: они важнее старых api_* (старый агент не одобрен)
if bool(kv.get("hyper2_private")) != bool(kv.get("hyper2_api_wallet")):
    fail("в botLP есть только одна из строк hyper2_private / hyper2_api_wallet — нужны обе (ключ и адрес агента)")
if kv.get("hyper2_private"):
    kv["api_private"], kv["api_wallet_address"] = kv["hyper2_private"], kv["hyper2_api_wallet"]

for k in ("sol", "api_private", "api_wallet_address", "jupiter_api", "helius_api"):
    if not kv.get(k):
        fail(f"в botLP нет строки {k}")
try:
    sec = b58decode(kv["sol"])
except ValueError:
    fail("sol — не base58")
if len(sec) != 64:
    fail(f"sol: {len(sec)} байт вместо 64")
try:
    from solders.keypair import Keypair           # сверка: публичная половина соответствует секрету
    Keypair.from_bytes(sec)
except ImportError:
    pass
except Exception:                                 # noqa
    fail("sol: секрет и публичный ключ не сходятся")
if not re.fullmatch(r"(0x)?[0-9a-fA-F]{64}", kv["api_private"]):
    fail("ключ API-кошелька HL — не ключ secp256k1 (64 hex)")
try:
    agent = Account.from_key(kv["api_private"]).address
except Exception:                                 # noqa
    fail("ключ API-кошелька HL не читается как ключ secp256k1")
if agent.lower() != kv["api_wallet_address"].lower():
    fail("ключ API-кошелька HL не даёт адрес из botLP (hyper2_api_wallet / api_wallet_address)")
if not re.fullmatch(r"[0-9a-fA-F-]{36}", kv["helius_api"]):
    fail("helius_api — не похож на ключ Helius (36 знаков)")
if not re.fullmatch(r"[A-Za-z0-9_.\-]{16,128}", kv["jupiter_api"]):
    fail("jupiter_api — лишние символы (пробел/кавычка из вставки?) — поправьте строку в botLP")
gk, gs = kv.get("gatekey", ""), kv.get("gatesecret", "")   # Gate USDT-фьючерсы (сделка FATCOIN, 13.09)
if gk or gs:
    if not (re.fullmatch(r"[0-9a-fA-F]{32}", gk) and re.fullmatch(r"[0-9a-fA-F]{64}", gs)):
        fail("gatekey/gatesecret — не похожи на ключ Gate API v4 (32 и 64 hex)")

new = {
    "SOLANA_SECRET_B58": kv["sol"],
    "HL_AGENT_PRIVATE_KEY": kv["api_private"] if kv["api_private"].startswith("0x") else "0x" + kv["api_private"],
    "JUPITER_API_KEY": kv["jupiter_api"],
    "SOLANA_RPC_URL": "https://mainnet.helius-rpc.com/?api-key=" + kv["helius_api"],
    "SOLANA_RPC_SECONDARY_URL": "https://api.mainnet-beta.solana.com",
}
HL_ACCOUNT = kv.get("hyper2_public") or "0xE4Ebf0815d0980E5a03f7D675F86dc5079fB8919"   # торговый счёт HL (13.09)
try:                                              # одобрен ли агент именно у этого счёта — публичный /info userRole
    role = requests.post("https://api.hyperliquid.xyz/info", json={"type": "userRole", "user": agent},
                         timeout=15).json()
    approved = role.get("role") == "agent" and str((role.get("data") or {}).get("user", "")).lower() == HL_ACCOUNT.lower()
    why = f"роль: {role.get('role')}"
except Exception as e:                            # noqa
    approved, why = False, f"Hyperliquid не ответил ({type(e).__name__}) — проверить не удалось"
if approved:
    print(f"агент Hyperliquid …{agent[-4:]}: одобрен у счёта …{HL_ACCOUNT[-4:]}")
else:
    new.pop("HL_AGENT_PRIVATE_KEY")
    print(f"⚠️ агент Hyperliquid …{agent[-4:]} не подтверждён у счёта …{HL_ACCOUNT[-4:]} ({why}) — его ключ НЕ отправляю. "
          f"Если агент верный — просто запустите ещё раз; если нет — впишите в botLP ключ и адрес одобренного агента.")
if gk:
    new["GATE_API_KEY"], new["GATE_API_SECRET"] = gk, gs


def gate_check(key: str, secret: str) -> tuple[bool, str]:
    """Подписанный GET счёта фьючерсов Gate — только чтение; проверяет, что ключ живой и с правом фьючерсов."""
    import hashlib, hmac, time
    path, ts = "/api/v4/futures/usdt/accounts", str(int(time.time()))
    msg = "\n".join(["GET", path, "", hashlib.sha512(b"").hexdigest(), ts])
    sign = hmac.new(secret.encode(), msg.encode(), hashlib.sha512).hexdigest()
    try:
        r = requests.get("https://api.gateio.ws" + path, timeout=15,
                         headers={"KEY": key, "Timestamp": ts, "SIGN": sign, "Accept": "application/json"})
        body = r.json() if "json" in r.headers.get("content-type", "") else None
    except Exception as e:                        # noqa
        return False, f"нет ответа ({type(e).__name__})"
    if r.status_code == 200 and isinstance(body, dict) and "available" in body:
        return True, f"доступно {body.get('available')} {body.get('currency') or 'USDT'}"
    label = body.get("label", "") if isinstance(body, dict) else "ответ не от API Gate"
    return False, f"HTTP {r.status_code} {label}".strip()


if "--check" in sys.argv:                         # без записи на сервер
    if gk:
        print("Gate: ключ по виду верный; запрос к бирже — только при настоящем запуске")
    print("✅ ключи в botLP читаются и сходятся; на VPS будет записано (имена и длины):")
    for k, v in new.items():
        print(f"  {k} len={len(v)}")
    sys.exit(0)

if gk:
    ok, why = gate_check(gk, gs)
    if ok:
        print(f"Gate: фьючерсы доступны, {why}")
    else:
        new.pop("GATE_API_KEY"), new.pop("GATE_API_SECRET")
        print(f"⚠️ Gate: ключ не прошёл проверку фьючерсов ({why}) — ключи Gate НЕ отправляю; остальные отправляю")

out = send(new, ENV)
print("✅ ключи на VPS, в .env теперь (имена и длины):")
print(out)
