"""Фикстуры ALT для tests/data/solana (публичный read-only mainnet-beta, getAccountInfo base64).
Таблицы — из v0-транзакций свопов ANSEM, уже сохранённых в tests/data/solana (tx_*_b64.json).
Запуск: python fetch_alt.py <папка tests/data/solana> <папка вывода>"""
import base64, datetime, json, sys, time
from pathlib import Path
import requests

RPC = "https://api.mainnet-beta.solana.com"
SRC, OUT = Path(sys.argv[1]), Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
S = requests.Session()
_id = [0]
ALPH = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(b):
    n = int.from_bytes(b, "big"); o = ""
    while n:
        n, r = divmod(n, 58); o = ALPH[r] + o
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + o


def cu16(b, off):
    v = 0
    for i in range(3):
        x = b[off + i]; v |= (x & 0x7F) << (7 * i)
        if not x & 0x80:
            return v, off + i + 1


def tables(raw):
    n, off = cu16(raw, 0)
    m = raw[off + 64 * n:]
    assert m[0] & 0x80
    o = 4
    nk, o = cu16(m, o); o += 32 * nk + 32
    ni, o = cu16(m, o)
    for _ in range(ni):
        o += 1
        na, o = cu16(m, o); o += na
        nd, o = cu16(m, o); o += nd
    nl, o = cu16(m, o)
    out = []
    for _ in range(nl):
        out.append(b58(m[o:o + 32])); o += 32
        w, o = cu16(m, o); o += w
        r, o = cu16(m, o); o += r
    return out


def call(method, params, tries=8):
    for k in range(tries):
        _id[0] += 1
        time.sleep(1.4)
        try:
            r = S.post(RPC, json={"jsonrpc": "2.0", "id": _id[0], "method": method, "params": params}, timeout=30)
        except requests.RequestException as e:
            print("net", type(e).__name__, flush=True); time.sleep(5); continue
        if r.status_code == 429 or r.status_code >= 500:
            print("http", r.status_code, flush=True); time.sleep(10 + 5 * k); continue
        return r.json()
    raise SystemExit(f"{method}: не ответил")


want = []
for f in ("tx_jup_buy_b64.json", "tx_jup_sell_b64.json", "tx_failed_jup_b64.json", "tx_ata_create_b64.json"):
    res = json.loads((SRC / f).read_text())["response"]["result"]
    for t in tables(base64.b64decode(res["transaction"][0])):
        if t not in want:
            want.append(t)
print("таблиц:", len(want), flush=True)
for t in want:
    params = [t, {"encoding": "base64", "commitment": "finalized"}]
    body = call("getAccountInfo", params)
    (OUT / f"alt_{t}.json").write_text(json.dumps({
        "captured_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "endpoint": RPC,
        "method": "getAccountInfo", "params": params, "response": body}, indent=1))
    v = (body.get("result") or {}).get("value")
    print(t, "owner", v and v.get("owner"), "len", v and len(base64.b64decode(v["data"][0])), flush=True)
