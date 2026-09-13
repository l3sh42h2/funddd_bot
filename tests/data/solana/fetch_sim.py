"""Фикстуры simulateTransaction для tests/data/solana (публичный read-only mainnet-beta).
Симулируются чужие, уже исполненные свопы ANSEM через Jupiter (байты из tx_*_b64.json): sigVerify=false,
replaceRecentBlockhash=true (blockhash старый — иначе BlockhashNotFound), innerInstructions=true, accounts = кошелёк
и его token-счета сделки. Рядом — getMultipleAccounts(base64) тех же счетов прямо перед симуляцией (pre).
Это чтение: состояние сети не меняется. Запуск: python fetch_sim.py <tests/data/solana> <папка вывода>"""
import base64, datetime, json, sys, time
from pathlib import Path
import requests

RPC = "https://api.mainnet-beta.solana.com"
SRC, OUT = Path(sys.argv[1]), Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
S = requests.Session()
_id = [0]
CASES = {   # имя → (файл транзакции, счета: кошелёк, вход, выход)
    "sim_jup_buy": ("tx_jup_buy_b64.json", ["EH1KQLnYQoJUn4ofX1TKPiagtFbtEsrE3ChkY24eRXae",
                                            "68n6nUMcaSYEFxXJmvuC5K117WVKxKaCRvfoSgu1UBJA",
                                            "4WNw6fF5eGdep7nroX9oAqcZCDdSEiT4EnF7ZiNiy8k8"]),
    "sim_ata_create": ("tx_ata_create_b64.json", ["Ep4TUukQdi2X2Z1mKH54vYKHfoPtnSTi3TSArvGPuydR",
                                                  "FxuqW7exk6oxTJVvr652jY1mtyfwhRqdaASs4LsebgcH",
                                                  "EHWN9tUeAzWUjYPpkk69yeoLPRbtVh5hEFuX8o9xpigY"]),
}


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


for name, (f, accs) in CASES.items():
    tx_b64 = json.loads((SRC / f).read_text())["response"]["result"]["transaction"][0]
    pre_p = [accs, {"encoding": "base64", "commitment": "confirmed"}]
    pre = call("getMultipleAccounts", pre_p)
    sim_p = [tx_b64, {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
                      "commitment": "confirmed", "innerInstructions": True,
                      "accounts": {"encoding": "base64", "addresses": accs}}]
    sim = call("simulateTransaction", sim_p)
    (OUT / f"{name}.json").write_text(json.dumps({
        "captured_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "endpoint": RPC,
        "source_tx": f, "addresses": accs,
        "pre": {"method": "getMultipleAccounts", "params": pre_p, "response": pre},
        "sim": {"method": "simulateTransaction", "params": ["<tx_b64 из source_tx>", sim_p[1]], "response": sim}},
        indent=1))
    v = (sim.get("result") or {}).get("value") or {}
    print(name, "err", json.dumps(v.get("err"))[:200], "units", v.get("unitsConsumed"),
          "inner", len(v.get("innerInstructions") or []), "error" in sim and sim["error"], flush=True)
