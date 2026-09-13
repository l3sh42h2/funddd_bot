# НЕ ТЕСТ: генератор фикстур, pytest его не собирает. Запуск только вручную (mempool-venv / публичный read-only доступ).
"""Сквозные эталоны /exchange для тестов ioc/setup: cloid считается здесь НЕЗАВИСИМО (sha256 по формуле ТЗ),
действие и подпись — официальным SDK 0.24.0 (float-путь SDK), тестовый ключ SDK. В сеть ничего не уходит."""
import hashlib, json
from eth_account import Account
from hyperliquid.utils import signing as S
from hyperliquid.utils.types import Cloid
KEY = "0x0123456789012345678901234567890123456789012345678901234567890123"
W = Account.from_key(KEY)
MASTER = "0x9a6f1bd2f1b7c1d2e3f4a5b6c7d8e9f0a1b2c3d4"
SUB = "0x1719884eb866cb12b2287399b15f7db5e7d775ea"
out = {"_about": "hyperliquid-python-sdk 0.24.0 + msgpack 1.2.1 (mempool-venv); cloid = 0x+sha256('mainnet|account|client_id')[:32]; "
                 "nonce = часы теста 1789305063.5 с → 1789305063500 мс, expiresAfter = +30000", "cases": []}
def order(is_buy, px, sz, ro, cloid):
    req = {"coin": "para:ANSEM", "is_buy": is_buy, "sz": float(sz), "limit_px": float(px),
           "order_type": {"limit": {"tif": "Ioc"}}, "reduce_only": ro, "cloid": Cloid.from_str(cloid)}
    return S.order_wires_to_order_action([S.order_request_to_order_wire(req, 180025)])
for name, account, cid, is_buy, px, sz, ro, vault in (
        ("sub_sell", SUB, "fb-D7K2-e03-c1-a1", False, "0.16608", "600", False, SUB),
        ("master_buy_ro", MASTER, "fb-D7K2-x01-c2-a1", True, "0.1583", "1000", True, None)):
    cloid = "0x" + hashlib.sha256(f"mainnet|{account.lower()}|{cid}".encode()).hexdigest()[:32]
    action = order(is_buy, px, sz, ro, cloid)
    nonce, exp = 1789305063500, 1789305093500
    sig = S.sign_l1_action(W, action, vault, nonce, exp, True)
    assert S.recover_agent_or_user_from_l1_action(action, sig, vault, nonce, exp, True) == W.address
    out["cases"].append({"name": name, "account": account, "master": MASTER, "client_id": cid, "cloid": cloid,
                         "side": "BUY" if is_buy else "SELL", "px": px, "sz": sz, "reduce_only": ro,
                         "payload": {"action": action, "nonce": nonce, "signature": sig, "vaultAddress": vault,
                                     "expiresAfter": exp}})
lev = {"type": "updateLeverage", "asset": 180025, "isCross": False, "leverage": 1}
sig = S.sign_l1_action(W, lev, SUB, 1789305063500, 1789305093500, True)
out["update_leverage_sub"] = {"payload": {"action": lev, "nonce": 1789305063500, "signature": sig, "vaultAddress": SUB,
                                          "expiresAfter": 1789305093500}}
json.dump(out, open("/private/tmp/claude-501/-Users-admin-Documents-vps/2b39936b-c4a4-4269-b3f0-5c3872b441ce/scratchpad/sol_work/tests/data/hl/golden_e2e_20260913.json", "w"), indent=1)
print(json.dumps(out)[:700])
