# НЕ ТЕСТ: генератор фикстур, pytest его не собирает. Запуск только вручную (mempool-venv / публичный read-only доступ).
"""Второй проход фикстур (публичный read-only mainnet-beta, темп ~0.7 запроса/с, пауза на 429).
1) дальше по подписям mint ANSEM: своп через OKX router (6m2C…) и неудачный своп ANSEM через Jupiter;
2) base64-копии выбранных транзакций (разбор провода, подписи, хеш сообщения);
3) getSignatureStatuses (история), высоты, isBlockhashValid старого hash, minimumLedgerSlot, rent по размерам;
4) token-счета владельцев из чеков (jsonParsed + base64) — сверка ATA и TLV."""
import json, sys, time, datetime
from pathlib import Path
import requests

RPC = "https://api.mainnet-beta.solana.com"
OUT = Path(sys.argv[1])
ANSEM = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
OKX = "6m2CDdhRgxpH4WjvdzxAYbGxwdGUz5MziiL5jek2kBma"
S = requests.Session()
_id = [0]


def call(method, params, tries=8):
    for k in range(tries):
        _id[0] += 1
        time.sleep(1.4)
        try:
            r = S.post(RPC, json={"jsonrpc": "2.0", "id": _id[0], "method": method, "params": params}, timeout=30)
        except requests.RequestException as e:
            print("net", method, type(e).__name__, flush=True); time.sleep(5); continue
        if r.status_code == 429 or r.status_code >= 500:
            print("http", r.status_code, method, flush=True); time.sleep(10 + 5 * k); continue
        return r.json()
    raise SystemExit(f"{method}: не ответил")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(name, method, params, body):
    (OUT / name).write_text(json.dumps({"captured_utc": now(), "endpoint": RPC, "method": method, "params": params,
                                        "response": body}, indent=1, ensure_ascii=False))


def keys_of(tx):
    la = tx["meta"].get("loadedAddresses") or {"writable": [], "readonly": []}
    return list(tx["transaction"]["message"]["accountKeys"]) + la["writable"] + la["readonly"]


def own_delta(tx):
    meta, ks = tx["meta"], keys_of(tx)
    payer = ks[0]
    pre = {b["accountIndex"]: b for b in meta.get("preTokenBalances") or []}
    post = {b["accountIndex"]: b for b in meta.get("postTokenBalances") or []}
    d = {}
    for i in set(pre) | set(post):
        b = post.get(i) or pre.get(i)
        if b.get("owner") != payer:
            continue
        a0 = int(pre[i]["uiTokenAmount"]["amount"]) if i in pre else 0
        a1 = int(post[i]["uiTokenAmount"]["amount"]) if i in post else 0
        d[b["mint"]] = d.get(b["mint"], 0) + a1 - a0
    return d, any(b.get("owner") == payer and b["mint"] == ANSEM for b in list(pre.values()) + list(post.values()))


def scan():
    summ = json.loads((OUT / "_scan_summary.json").read_text())
    before = summ["seen"][-1]["sig"]
    want = {"okx_swap": None, "failed_jup": None}
    progs = {}
    fetched = 0
    pages = 0
    while fetched < 200 and pages < 12 and any(v is None for v in want.values()):
        sl = call("getSignaturesForAddress", [ANSEM, {"limit": 1000, "before": before, "commitment": "confirmed"}])["result"]
        pages += 1
        if not sl:
            break
        before = sl[-1]["signature"]
        # сначала неудачные (дёшево: err есть в списке подписей), потом выборка успешных
        order = [s for s in sl if s.get("err") is not None] + [s for s in sl if s.get("err") is None][::4]
        for s in order:
            if fetched >= 200 or all(v is not None for v in want.values()):
                break
            if s.get("err") is not None and want["failed_jup"] is not None:
                continue
            if s.get("err") is None and want["okx_swap"] is not None:
                continue
            p = [s["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}]
            body = call("getTransaction", p)
            fetched += 1
            tx = body.get("result")
            if not tx:
                continue
            ks = keys_of(tx)
            top = {ks[ix["programIdIndex"]] for ix in tx["transaction"]["message"]["instructions"]}
            for pr in top:
                progs[pr] = progs.get(pr, 0) + 1
            d, has_ansem = own_delta(tx)
            if tx["meta"]["err"] is not None and JUP in top and has_ansem and want["failed_jup"] is None:
                want["failed_jup"] = s["signature"]
                save("tx_failed_jup.json", "getTransaction", p, body)
                print("got failed_jup", s["signature"], d, flush=True)
            if tx["meta"]["err"] is None and OKX in ks and d.get(ANSEM, 0) != 0 and want["okx_swap"] is None:
                want["okx_swap"] = s["signature"]
                save("tx_okx_swap.json", "getTransaction", p, body)
                print("got okx_swap", s["signature"], d, flush=True)
    (OUT / "_scan2_summary.json").write_text(json.dumps({"captured_utc": now(), "fetched": fetched, "pages": pages,
                                                         "want": want, "top_programs": progs}, indent=1))
    print("scan2", want, fetched, flush=True)


def extras():
    names = [p.stem[3:] for p in sorted(OUT.glob("tx_*.json")) if not p.stem.endswith("_b64")]
    sigs = {}
    for nm in names:
        doc = json.loads((OUT / f"tx_{nm}.json").read_text())
        sig = doc["params"][0]
        sigs[nm] = sig
        p = [sig, {"encoding": "base64", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}]
        save(f"tx_{nm}_b64.json", "getTransaction", p, call("getTransaction", p))
    bogus = "5" * 87 + "1"          # не существующая подпись (base58, 64 байта не гарантированы — проверим ответом)
    p = [list(sigs.values()), {"searchTransactionHistory": True}]
    save("statuses_history.json", "getSignatureStatuses", p, call("getSignatureStatuses", p))
    p = [list(sigs.values())]
    save("statuses_recent.json", "getSignatureStatuses", p, call("getSignatureStatuses", p))
    for c in ("finalized", "confirmed"):
        p = [{"commitment": c}]
        save(f"block_height_{c}.json", "getBlockHeight", p, call("getBlockHeight", p))
    p = [{"commitment": "finalized"}]
    save("latest_blockhash.json", "getLatestBlockhash", p, call("getLatestBlockhash", p))
    old_bh = json.loads((OUT / "tx_jup_buy.json").read_text())["response"]["result"]["transaction"]["message"]["recentBlockhash"]
    p = [old_bh, {"commitment": "processed"}]
    save("is_blockhash_valid_old.json", "isBlockhashValid", p, call("isBlockhashValid", p))
    save("minimum_ledger_slot.json", "minimumLedgerSlot", [], call("minimumLedgerSlot", []))
    for size in (0, 82, 165, 170, 401):
        save(f"rent_{size}.json", "getMinimumBalanceForRentExemption", [size],
             call("getMinimumBalanceForRentExemption", [size]))
    # token-счета плательщиков из чеков: и jsonParsed, и base64
    accs = []
    for nm in ("jup_buy", "jup_sell", "ata_create"):
        tx = json.loads((OUT / f"tx_{nm}.json").read_text())["response"]["result"]
        ks = keys_of(tx)
        for b in (tx["meta"].get("postTokenBalances") or []):
            if b.get("owner") == ks[0]:
                accs.append(ks[b["accountIndex"]])
    accs = list(dict.fromkeys(accs))
    for enc in ("jsonParsed", "base64"):
        p = [accs, {"encoding": enc, "commitment": "confirmed"}]
        save(f"token_accounts_{enc}.json", "getMultipleAccounts", p, call("getMultipleAccounts", p))
    print("extras done", names, flush=True)


if __name__ == "__main__":
    if "scan" in sys.argv[2:]:
        scan()
    extras()
