# НЕ ТЕСТ: генератор фикстур, pytest его не собирает. Запуск только вручную (mempool-venv / публичный read-only доступ).
"""Сбор фикстур для tests/test_solana_*.py: ТОЛЬКО публичный read-only mainnet-beta RPC.
Методы: getGenesisHash, getAccountInfo, getSignaturesForAddress, getTransaction, getSignatureStatuses,
getBlockHeight, getLatestBlockhash, isBlockhashValid, minimumLedgerSlot, getMinimumBalanceForRentExemption.
Ни подписей, ни отправок. Темп ~2.5 запроса/с."""
import base64, datetime, json, sys, time
from pathlib import Path
import requests

RPC = "https://api.mainnet-beta.solana.com"
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
ANSEM = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
OKX = "6m2CDdhRgxpH4WjvdzxAYbGxwdGUz5MziiL5jek2kBma"
S = requests.Session()
_id = [0]


def call(method, params, tries=6):
    for k in range(tries):
        _id[0] += 1
        time.sleep(0.4)
        try:
            r = S.post(RPC, json={"jsonrpc": "2.0", "id": _id[0], "method": method, "params": params}, timeout=30)
        except requests.RequestException as e:
            print("net", method, type(e).__name__); time.sleep(2 + k * 2); continue
        if r.status_code == 429 or r.status_code >= 500:
            print("http", r.status_code, method); time.sleep(3 + k * 3); continue
        body = r.json()
        return body
    raise SystemExit(f"{method}: не ответил")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(name, method, params, body):
    doc = {"captured_utc": now(), "endpoint": RPC, "method": method, "params": params, "response": body}
    (OUT / name).write_text(json.dumps(doc, indent=1, ensure_ascii=False))


def keys_of(tx):
    m = tx["transaction"]["message"]
    la = tx["meta"].get("loadedAddresses") or {"writable": [], "readonly": []}
    return list(m["accountKeys"]) + list(la["writable"]) + list(la["readonly"])


def classify(tx):
    meta = tx["meta"]
    ks = keys_of(tx)
    payer = ks[0]
    pre = {(b["accountIndex"]): b for b in meta.get("preTokenBalances") or []}
    post = {(b["accountIndex"]): b for b in meta.get("postTokenBalances") or []}
    d = {}
    for idx in set(pre) | set(post):
        b = post.get(idx) or pre.get(idx)
        if b.get("owner") != payer:
            continue
        a0 = int(pre[idx]["uiTokenAmount"]["amount"]) if idx in pre else 0
        a1 = int(post[idx]["uiTokenAmount"]["amount"]) if idx in post else 0
        d[b["mint"]] = d.get(b["mint"], 0) + a1 - a0
    created_ansem = any(idx in post and idx not in pre and post[idx]["mint"] == ANSEM and post[idx].get("owner") == payer
                        for idx in post)
    progs = {ks[ix["programIdIndex"]] for ix in tx["transaction"]["message"]["instructions"]}
    return {"payer": payer, "delta": d, "jup": JUP in progs, "okx": OKX in progs, "err": meta.get("err"),
            "created_ansem": created_ansem, "version": tx.get("version"), "progs": sorted(progs)}


def main():
    save("genesis.json", "getGenesisHash", [], call("getGenesisHash", []))
    for nm, mint in (("ansem", ANSEM), ("usdc", USDC)):
        for enc in ("jsonParsed", "base64"):
            p = [mint, {"encoding": enc, "commitment": "finalized"}]
            save(f"mint_{nm}_{enc}.json", "getAccountInfo", p, call("getAccountInfo", p))
    want = {"jup_buy": None, "jup_sell": None, "okx_buy": None, "okx_sell": None, "failed": None,
            "ata_create": None}
    before = None
    fetched = 0
    seen_summary = []
    while fetched < 260 and any(v is None for v in want.values()):
        p = [ANSEM, {"limit": 100, **({"before": before} if before else {}), "commitment": "confirmed"}]
        sl = call("getSignaturesForAddress", p)["result"]
        if not sl:
            break
        before = sl[-1]["signature"]
        for s in sl:
            if fetched >= 260 or all(v is not None for v in want.values()):
                break
            sig = s["signature"]
            # сначала дешёвый фильтр: неудачные нам нужны одна, успешные — любые
            if s.get("err") is not None and want["failed"] is not None:
                continue
            p2 = [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}]
            body = call("getTransaction", p2)
            fetched += 1
            tx = body.get("result")
            if not tx:
                continue
            c = classify(tx)
            seen_summary.append({"sig": sig, **{k: c[k] for k in ("jup", "okx", "err", "created_ansem", "version")},
                                 "delta": c["delta"]})
            dA, dU = c["delta"].get(ANSEM, 0), c["delta"].get(USDC, 0)
            slot = None
            if c["err"] is not None and want["failed"] is None:
                slot = "failed"
            elif c["err"] is None and dU < 0 < dA:
                slot = "jup_buy" if c["jup"] else ("okx_buy" if c["okx"] else None)
            elif c["err"] is None and dA < 0 < dU:
                slot = "jup_sell" if c["jup"] else ("okx_sell" if c["okx"] else None)
            if slot and want.get(slot) is None:
                want[slot] = sig
                save(f"tx_{slot}.json", "getTransaction", p2, body)
                print("got", slot, sig, c["delta"], c["version"])
            if c["err"] is None and c["created_ansem"] and want["ata_create"] is None and slot is None:
                want["ata_create"] = sig
                save("tx_ata_create.json", "getTransaction", p2, body)
                print("got ata_create", sig, c["delta"])
    (OUT / "_scan_summary.json").write_text(json.dumps({"captured_utc": now(), "fetched": fetched, "want": want,
                                                         "seen": seen_summary}, indent=1))
    print("want", want, "fetched", fetched)


if __name__ == "__main__":
    main()
