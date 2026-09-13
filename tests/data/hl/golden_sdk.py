# НЕ ТЕСТ: генератор фикстур, pytest его не собирает. Запуск только вручную (mempool-venv / публичный read-only доступ).
"""Эталоны для тестов потока hl: настоящий msgpack 1.2.1 + официальный SDK hyperliquid 0.24.0 (utils.signing) из
mempool-venv. Ключ — тестовый (из тестов SDK), средств нет. Ничего не отправляется в сеть."""
import hashlib, json, msgpack
from decimal import Decimal
from eth_account import Account
from hyperliquid.utils import signing as S
from hyperliquid.utils.types import Cloid

KEY = "0x0123456789012345678901234567890123456789012345678901234567890123"
W = Account.from_key(KEY)
out = {"sdk": "hyperliquid-python-sdk 0.24.0", "msgpack": msgpack.version, "key": KEY, "agent": W.address}

# --- msgpack: границы кодировок ---
vals = {
    "ints": [0, 1, 127, 128, 255, 256, 65535, 65536, 2**32 - 1, 2**32, 2**64 - 1, -1, -31, -32, -33, -127, -128,
             -129, -32767, -32768, -32769, -2**31, -2**31 - 1, -2**63, 180025, 1789305063560],
    "strs": ["", "a", "x" * 31, "x" * 32, "y" * 255, "y" * 256, "z" * 65535, "z" * 65536, "Ioc", "para:ANSEM",
             "ф" * 20, "ф" * 16, "0x0123456789abcdef0123456789abcdef"],
    "misc": [True, False, None, [], [1, [2, [3]]], {}, {"a": 1, "b": [True, None]}, list(range(15)), list(range(16)),
             list(range(65536)), {f"k{i}": i for i in range(15)}, {f"k{i}": i for i in range(16)},
             {f"k{i:05d}": i for i in range(65536)}, b"", b"\x00\x01", b"\xff" * 256],
}
mp = []
for group, xs in vals.items():
    for v in xs:
        b = msgpack.packb(v)
        mp.append({"group": group, "value": v if not isinstance(v, bytes) else {"bytes_hex": v.hex()},
                   "hex": b.hex() if len(b) <= 128 else None, "sha256": hashlib.sha256(b).hexdigest(), "len": len(b)})
out["msgpack_vectors"] = mp

# --- wire чисел: SDK float_to_wire для набора цен/размеров ---
nums = ["0.16608", "0.1583", "600", "1000", "100000", "0.000123", "1234.5", "12345", "0.15839", "0.15838", "1",
        "0.00001", "99999", "123.46", "1.2345"]
out["float_to_wire"] = {n: S.float_to_wire(float(n)) for n in nums}

CL = "0x0123456789abcdef0123456789abcdef"
VAULT = "0x1719884eb866cb12b2287399b15f7db5e7d775ea"


def order(is_buy, px, sz, ro, cloid=CL):
    req = {"coin": "para:ANSEM", "is_buy": is_buy, "sz": float(sz), "limit_px": float(px),
           "order_type": {"limit": {"tif": "Ioc"}}, "reduce_only": ro, "cloid": Cloid.from_str(cloid)}
    return S.order_wires_to_order_action([S.order_request_to_order_wire(req, 180025)])


cases = [
    ("sell_ioc_mainnet", order(False, "0.16608", "600", False), None, 1789305063560, None, True),
    ("sell_ioc_expires", order(False, "0.16608", "600", False), None, 1789305063561, 1789305093561, True),
    ("buy_ro_vault_expires", order(True, "0.1583", "1000", True), VAULT, 1789305063562, 1789305093562, True),
    ("sell_ioc_testnet", order(False, "0.16608", "600", False), None, 1789305063563, None, False),
    ("update_leverage", {"type": "updateLeverage", "asset": 180025, "isCross": False, "leverage": 1}, None,
     1789305063564, 1789305093564, True),
    ("noop", {"type": "noop"}, None, 1789305063565, None, True),
    ("noop_vault_expires", {"type": "noop"}, VAULT, 1789305063566, 1789305093566, True),
]
sg = []
for name, action, vault, nonce, exp, main in cases:
    h = S.action_hash(action, vault, nonce, exp)
    sig = S.sign_l1_action(W, action, vault, nonce, exp, main)
    rec = S.recover_agent_or_user_from_l1_action(action, sig, vault, nonce, exp, main)
    sg.append({"name": name, "action": action, "vault": vault, "nonce": nonce, "expires_after": exp, "mainnet": main,
               "packed_hex": msgpack.packb(action).hex(), "action_hash": "0x" + bytes(h).hex(), "sig": sig,
               "recovered": rec})
out["sign_vectors"] = sg
json.dump(out, open("/private/tmp/claude-501/-Users-admin-Documents-vps/2b39936b-c4a4-4269-b3f0-5c3872b441ce/scratchpad/hl_stream/golden_sdk.json", "w"), indent=1)
print(W.address, len(mp), [(c["name"], c["recovered"] == W.address) for c in sg])
print(json.dumps(sg[0])[:900])
print(out["float_to_wire"])
