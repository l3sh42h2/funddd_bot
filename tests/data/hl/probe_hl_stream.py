# НЕ ТЕСТ: генератор фикстур, pytest его не собирает. Запуск только вручную (mempool-venv / публичный read-only доступ).
"""Публичные read-only пробы HL /info для фикстур потока hl (13.09). Ничего не подписывается и не отправляется."""
import json, time, datetime, requests
URL = "https://api.hyperliquid.xyz/info"
S = requests.Session()
out = {}
def q(name, body):
    t0 = time.time()
    r = S.post(URL, json=body, timeout=15)
    out[name] = {"payload": body, "http": r.status_code,
                 "utc": datetime.datetime.utcfromtimestamp(t0).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "ms_local": int(t0 * 1000), "data": r.json() if r.status_code == 200 else r.text[:300]}
    time.sleep(0.4)
    return out[name]["data"]
now = int(time.time() * 1000)
q("perpDexs", {"type": "perpDexs"})
q("meta_para", {"type": "meta", "dex": "para"})
q("metaAndAssetCtxs_para", {"type": "metaAndAssetCtxs", "dex": "para"})
q("l2Book", {"type": "l2Book", "coin": "para:ANSEM"})
tr = q("recentTrades", {"type": "recentTrades", "coin": "para:ANSEM"})
users = []
for t in tr or []:
    for u in t.get("users") or []:
        if u not in users:
            users.append(u)
out["_users"] = users[:6]
picked = 0
for u in users[:6]:
    fills = q(f"userFillsByTime_{picked}", {"type": "userFillsByTime", "user": u, "startTime": now - 2 * 86400_000,
                                              "aggregateByTime": False})
    ans = [f for f in fills or [] if f.get("coin") == "para:ANSEM"]
    if not ans:
        continue
    q(f"userFunding_{picked}", {"type": "userFunding", "user": u, "startTime": now - 2 * 86400_000})
    q(f"clearinghouseState_para_{picked}", {"type": "clearinghouseState", "user": u, "dex": "para"})
    q(f"activeAssetData_{picked}", {"type": "activeAssetData", "user": u, "coin": "para:ANSEM"})
    q(f"userAbstraction_{picked}", {"type": "userAbstraction", "user": u})
    q(f"userRole_{picked}", {"type": "userRole", "user": u})
    q(f"userFees_{picked}", {"type": "userFees", "user": u})
    q(f"extraAgents_{picked}", {"type": "extraAgents", "user": u})
    q(f"spotClearinghouseState_{picked}", {"type": "spotClearinghouseState", "user": u})
    q(f"orderStatus_oid_{picked}", {"type": "orderStatus", "user": u, "oid": ans[-1]["oid"]})
    q(f"frontendOpenOrders_para_{picked}", {"type": "frontendOpenOrders", "user": u, "dex": "para"})
    out[f"_user_{picked}"] = u
    picked += 1
    if picked >= 2:
        break
json.dump(out, open("/private/tmp/claude-501/-Users-admin-Documents-vps/2b39936b-c4a4-4269-b3f0-5c3872b441ce/scratchpad/hl_stream/probe_hl_stream.json", "w"), indent=1)
print("picked", picked, "users", len(users), {k: (v["http"] if isinstance(v, dict) and "http" in v else v) for k, v in out.items() if not k.startswith("_")})
