"""Сеть для «того же актива» (identity.py — чистое решение). Всё публичное, без ключей; зовётся из вспомогательного
слота площадки в коллекторе (раз в час списки монет, раз в сутки состав индексов), результат хранится в БД.

Замерено 12.09 с ireland (все 200):
- Gate /spot/currencies (name, chains[].addr, deposit/withdraw_disabled) + /spot/currency_pairs (trade_status);
- KuCoin /api/v3/currencies (fullName, chains[].contractAddress, isDeposit/WithdrawEnabled) + /api/v2/symbols;
- Bitget /api/v2/spot/public/coins (имён нет) + /api/v2/spot/public/symbols;
- Binance getNetworkCoinAll (bapi, 188 КБ) + /api/v3/exchangeInfo + список Alpha (bapi, 173 КБ);
- состав индекса: Binance /fapi/v1/constituents, Aster /fapi/v3/indexreferences — по символу, 400 = индекса нет;
- DexScreener /latest/dex/search — только для перпов, у которых в индексе DEX-пулы и нет рынков наших площадок;
- множитель токена акции: xStocks — api.xstocks.fi …/multiplier (NFLXx → 10); Ondo — SyntheticSharesOracle.getSValue
  на BSC (адрес токена на BSC — из списка токенов Ondo по символу, как у Gate на Ethereum), eth_call через publicnode.
Ревью 12.09: сбой одного символа или записи не должен ни губить весь ответ, ни затирать хорошие данные «пустыми»;
у каждого долгого обхода — предел по времени (зависшая точка не держит слот площадки).
"""
from __future__ import annotations
import time, logging
from urllib.parse import quote
import requests
from . import config, identity
from .client import PermanentHTTPError, BannedError, BudgetExceeded

log = logging.getLogger(__name__)


def _t(x) -> bool:
    return str(x).lower() == "true" if isinstance(x, str) else bool(x)


def _get_json(url: str, timeout: float = config.HTTP_TIMEOUT):
    r = requests.get(url, headers={"user-agent": config.USER_AGENT, "accept-encoding": "gzip"}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _data(x):
    return x.get("data") if isinstance(x, dict) else x


def _records(rows, fn, what: str) -> dict:
    """Запись → (ключ, значение) через fn; кривая запись пропускается, а не губит весь список площадки."""
    out, bad = {}, 0
    for x in rows or []:
        try:
            k, v = fn(x)
            if k:
                out[k] = v
        except (KeyError, TypeError, AttributeError, ValueError):
            bad += 1
    if bad:
        log.warning("%s: пропущено кривых записей %d", what, bad)
    if not out:
        raise RuntimeError(f"{what}: пустой ответ — прежние данные остаются")
    return out


def _chains(c: dict, *fields) -> list[tuple]:
    net, addr, dep, wd = fields
    return [(x.get(net), x.get(addr), dep(x), wd(x)) for x in (c.get("chains") or c.get("networkList") or [])
            if isinstance(x, dict)]


# --- спот: монеты, рынки, множители акций --------------------------------------------------------------------
def coin_blob(cl) -> dict:
    """{"coins": {код: запись}, "markets": {символ: [код, торгуется]}, "alpha": [...] | None, "shares": {символ: {n, src}},
    "need_n": [символы, чей эмитент публикует множитель]}."""
    if hasattr(cl, "coin_blob"):
        return cl.coin_blob()
    return {"binance_spot": _binance, "gate_spot": _gate, "kucoin_spot": _kucoin, "bitget_spot": _bitget}[cl.name](cl)


def _binance(cl) -> dict:
    info = cl.get("/api/v3/exchangeInfo")
    markets = _records(info.get("symbols"), lambda s: (s["symbol"], [s["baseAsset"], s.get("status") == "TRADING"]),
                       "binance рынки")
    coins = _records(_data(_get_json(config.BINANCE_COINS_URL)), lambda c: (c["coin"], identity.coin_record(
        c.get("name"), _chains(c, "network", "contractAddress", lambda x: _t(x.get("depositEnable")),
                               lambda x: _t(x.get("withdrawEnable"))), c["coin"])), "binance монеты")
    alpha = None                                 # нет списка — пулы DEX не принимаются (identity), прежний список остаётся
    try:
        alpha = [{"s": t.get("symbol"), "n": t.get("name"), "a": t.get("contractAddress"), "c": str(t.get("chainId")),
                  "id": t.get("alphaId"), "off": bool(t.get("offline"))}
                 for t in (_data(_get_json(config.BINANCE_ALPHA_URL)) or []) if isinstance(t, dict)] or None
    except Exception as e:  # noqa
        log.warning("список Binance Alpha: %s: %s", type(e).__name__, e)
    return dict(coins=coins, markets=markets, alpha=alpha)


def _gate(cl) -> dict:
    cur = cl.get("/spot/currencies", retries=3, timeout=config.HTTP_TIMEOUT)
    pairs = cl.get("/spot/currency_pairs", retries=3, timeout=config.HTTP_TIMEOUT)
    markets = _records(pairs, lambda p: (p["id"], [p.get("base"), p.get("trade_status") != "untradable"]), "gate рынки")

    def one(c):
        chains = _chains(c, "name", "addr", lambda x: not x.get("deposit_disabled"), lambda x: not x.get("withdraw_disabled"))
        if not chains and c.get("chain"):
            chains = [(c["chain"], "", not c.get("deposit_disabled"), not c.get("withdraw_disabled"))]
        return c["currency"], identity.coin_record(c.get("name"), chains, c["currency"])
    coins = _records(cur, one, "gate монеты")
    cats = {c.get("currency"): set(c.get("category") or []) for c in cur if isinstance(c, dict)}
    eth = {c.get("currency"): next((str(x.get("addr") or "").lower() for x in (c.get("chains") or [])
                                    if isinstance(x, dict) and x.get("name") == "ETH"), "") for c in cur if isinstance(c, dict)}
    xs, on = {}, {}
    for p in pairs:
        if not isinstance(p, dict):
            continue
        b = p.get("base") or ""
        if p.get("quote") != config.QUOTE or p.get("trade_status") != "tradable":
            continue
        cat = cats.get(b, set())
        if "xstocks" in cat and b.upper().endswith("X"):
            xs[p["id"]] = b[:-1] + "x"                            # NFLXX → NFLXx (символ xStocks)
        elif "ondo-stocks" in cat and b.upper().endswith("ON") and eth.get(b):
            on[p["id"]] = eth[b]
    shares = _xstocks(xs)
    shares.update(_ondo(on))
    return dict(coins=coins, markets=markets, shares=shares, need_n=sorted(set(xs) | set(on)))


def _kucoin(cl) -> dict:
    cur = cl.get("/api/v3/currencies", retries=3, timeout=config.HTTP_TIMEOUT) or []
    syms = cl.get("/api/v2/symbols", retries=3, timeout=config.HTTP_TIMEOUT) or []
    markets = _records(syms, lambda s: (s["symbol"], [s.get("baseCurrency"), bool(s.get("enableTrading"))]), "kucoin рынки")
    coins = _records(cur, lambda c: (c["currency"], identity.coin_record(
        c.get("fullName") or c.get("name"),
        [(x.get("chainId") or x.get("chainName"), x.get("contractAddress"), _t(x.get("isDepositEnabled")),
          _t(x.get("isWithdrawEnabled"))) for x in (c.get("chains") or []) if isinstance(x, dict)],
        c.get("name") or c["currency"])), "kucoin монеты")
    xs = {}
    for s in syms:
        if not isinstance(s, dict):
            continue
        name = (s.get("name") or s.get("symbol") or "").split("-")[0]
        if s.get("market") == "Stocks" and s.get("quoteCurrency") == config.QUOTE and s.get("enableTrading") \
                and name.upper().endswith("X"):
            xs[s["symbol"]] = name[:-1] + "x"                     # KuCoin котирует токен как есть (как Gate)
    return dict(coins=coins, markets=markets, shares=_xstocks(xs), need_n=sorted(xs))


def _bitget(cl) -> dict:
    raw = cl.get("/api/v2/spot/public/coins", retries=3, timeout=config.HTTP_TIMEOUT) or []
    syms = cl.get("/api/v2/spot/public/symbols", retries=3, timeout=config.HTTP_TIMEOUT) or []
    markets = _records(syms, lambda s: (s["symbol"], [s.get("baseCoin"), s.get("status") == "online"]), "bitget рынки")
    coins = _records(raw, lambda c: (c["coin"], identity.coin_record(      # имя займётся через общий контракт
        None, [(x.get("chain"), x.get("contractAddress"), _t(x.get("rechargeable")), _t(x.get("withdrawable")))
               for x in (c.get("chains") or []) if isinstance(x, dict)], c["coin"])), "bitget монеты")
    return dict(coins=coins, markets=markets)                             # rToken Bitget: 1 токен = 1 акция (политика)


def _xstocks(syms: dict[str, str], deadline_s: float | None = None) -> dict[str, dict]:
    """Символ пары → множитель xStocks. Активация сплита — по времени из ответа (пока не наступила — текущий)."""
    out, now = {}, time.time()
    t_end = now + (config.SRC_DEADLINE_S if deadline_s is None else deadline_s)
    for pair, xs in syms.items():
        if time.time() > t_end:
            log.warning("xStocks: предел %d с — %d токенов в следующий раз", config.SRC_DEADLINE_S, len(syms) - len(out))
            break
        try:
            d = _get_json(config.XSTOCKS_MULTIPLIER_URL.format(symbol=quote(xs)))
            n = float(d.get("currentMultiplier") or 0)
        except Exception as e:  # noqa — нет множителя одного токена: берётся прежний (коллектор), прочие живут
            log.warning("xStocks %s: %s: %s", xs, type(e).__name__, e)
            continue
        try:
            new, at = float(d.get("newMultiplier") or 0), float(d.get("activationDateTime") or 0)
            if new > 0 and at > 0 and (at / 1000.0 if at > 1e12 else at) <= now:
                n = new
        except (TypeError, ValueError):
            pass                                                  # время активации не числом — остаётся текущий
        if n > 0:
            out[pair] = {"n": n, "src": "xStocks"}
        time.sleep(0.1)
    return out


def _eth_call(rpc: str, to: str, data: str):
    r = requests.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": to, "data": data}, "latest"]},
                      headers={"user-agent": config.USER_AGENT}, timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _ondo(eth_addrs: dict[str, str], deadline_s: float | None = None) -> dict[str, dict]:
    """Символ пары → sValue Ondo (акций в токене, ×1e18). Оракул на Ethereum знает 5 токенов, на BSC — остальные;
    где отвечают оба, значения равны (проверено 12.09: SPYON 1.0077209, TSLAON 1.0)."""
    if not eth_addrs:
        return {}
    try:
        toks = [t for t in (_get_json(config.ONDO_TOKENLIST_URL).get("tokens") or []) if isinstance(t, dict)]
    except Exception as e:  # noqa
        log.warning("список токенов Ondo: %s: %s", type(e).__name__, e)
        return {}
    sym_by_eth = {str(t.get("address") or "").lower(): t.get("symbol") for t in toks if t.get("chainId") == 1}
    by_chain = {(t.get("chainId"), t.get("symbol")): t.get("address") for t in toks}
    out, t_end = {}, time.time() + (config.SRC_DEADLINE_S if deadline_s is None else deadline_s)
    for pair, ea in eth_addrs.items():
        if time.time() > t_end:
            log.warning("Ondo: предел %d с", config.SRC_DEADLINE_S)
            break
        sym = sym_by_eth.get(ea)
        if not sym:
            continue
        for rpc, oracle, chain in config.ONDO_ORACLES:
            addr = by_chain.get((chain, sym))
            if not addr:
                continue
            try:
                res = _eth_call(rpc, oracle, "0x0a562827" + str(addr).lower().replace("0x", "").rjust(64, "0")).get("result")
                if res and len(res) >= 2 + 64:
                    n = int(res[2:66], 16) / 1e18
                    if n > 0:
                        out[pair] = {"n": n, "src": "Ondo"}
                        break
            except Exception as e:  # noqa — AssetNotFound на одном оракуле — пробуем другой
                log.debug("Ondo %s на %s: %s", sym, rpc, e)
        time.sleep(0.1)
    return out


# --- перпы: состав индекса ---------------------------------------------------------------------------------
def _dex_queries(legs: list[dict], sym: str) -> list[str]:
    """Пулы, по которым нужен поиск: в индексе есть DEX-ноги и нет ни одного рынка наших спот-площадок."""
    if any(str(x.get("exchange") or "").lower() in identity.OURX and not identity.dex_leg(x)
           and "uindex(" not in str(x.get("symbol") or "") for x in legs):
        return []
    base = identity.perp_base(sym)
    qs = {identity.dex_query(d, base) for d in (identity.dex_leg(x) for x in legs) if d}
    return sorted(q for q in qs if q)


def _no_index(e: Exception) -> bool:
    """400 «Invalid symbol» (-1121) — у символа индекс не публикуется. Прочие 4xx (403 экрана, 404 после смены пути) —
    сбой источника, а не «индекса нет»: записать их как «нет индекса» значит затереть хорошие данные на сутки."""
    s = str(e)
    return s.startswith("400") or "-1121" in s or "invalid symbol" in s.lower()


def index_legs(cl, symbols: list[str], deadline_s: float | None = None) -> dict[str, dict]:
    """Символ → {"legs": [...] | None (индекса нет), "dex": {запрос: [пулы]}}. Сбой одного символа — пропуск (досберётся
    следующим заданием); лимит, бан, чужой 4xx или предел по времени — отдаётся собранное."""
    if hasattr(cl, "index_legs"):
        return cl.index_legs(symbols)
    path, key = config.INDEX_LEGS_PATHS[cl.name]
    if hasattr(cl, "set_min_gap"):
        cl.set_min_gap(path, config.LEGS_GAP_S)
    out, errs = {}, 0
    t_end = time.time() + (config.LEGS_DEADLINE_S if deadline_s is None else deadline_s)
    for s in symbols:
        if time.time() > t_end:
            break
        try:
            r = cl.get(path, {"symbol": s}, retries=2)
            legs = r.get(key) if isinstance(r, dict) else None
        except PermanentHTTPError as ex:
            if not _no_index(ex):
                if out:
                    log.warning("%s состав индекса: %s — пачка оборвана", cl.name, ex)
                    break
                raise
            legs = None                                           # 400 «Invalid symbol»: индекс не публикуется
        except (BudgetExceeded, BannedError):
            if out:
                break
            raise
        except Exception as ex:  # noqa — сеть, 5xx, битый ответ одного символа: остальные идут
            errs += 1
            log.warning("%s состав индекса %s: %s: %s", cl.name, s, type(ex).__name__, ex)
            continue
        e = {"legs": legs if isinstance(legs, list) else None, "dex": {}}
        try:
            qs = _dex_queries(e["legs"] or [], s)
        except Exception as ex:  # noqa — нога странной формы: без поиска пулов
            log.warning("%s состав индекса %s: ноги не разобраны: %s", cl.name, s, ex)
            qs = []
        for q in qs:
            try:
                e["dex"][q] = identity.dex_pairs(_get_json(config.DEXSCREENER_SEARCH_URL + quote(q)))
            except Exception as ex:  # noqa — поиск пула не удался: у этой строки вердикт «не проверено»
                log.warning("DexScreener %r: %s: %s", q, type(ex).__name__, ex)
            time.sleep(0.25)
        out[s] = e
    if not out and errs:
        raise RuntimeError(f"{cl.name}: состав индекса — ошибка у всех {errs} символов пачки")
    return out


def hl_annotations(cl, coins: list[str]) -> dict[str, dict]:
    """HIP-3 крипто (flx:BTC, para:ANSEM): описание рынка → {"desc": текст}. Имя монеты берётся из него."""
    if hasattr(cl, "annotations"):
        return cl.annotations(coins)
    return {}
