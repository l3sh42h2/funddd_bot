"""Разбор ответов бирж в единый вид. Пути и поля у Aster и Binance совпадают (Aster — клон API),
поэтому один набор функций обслуживает обе; спот Binance — те же тикеры под /api/v3.

Знак фандинга как у бирж: плюс — лонги платят шортам.
"""
from __future__ import annotations
import re, time, logging
from .client import BinanceLike
from .symbols import norm_symbol_factor
from . import config

log = logging.getLogger(__name__)


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def perp_instruments(cl: BinanceLike) -> list[dict]:
    """exchangeInfo -> перпы в USDT со статусом TRADING."""
    info = cl.get("/fapi/v1/exchangeInfo")
    out = []
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING" or s.get("quoteAsset") not in config.PERP_QUOTES:
            continue
        ct = s.get("contractType", "PERPETUAL")
        if ct not in config.PERP_CONTRACTS:
            continue
        filt = {f["filterType"]: f for f in s.get("filters", [])}
        base, factor = norm_symbol_factor(s["baseAsset"])
        cls = asset_class(s)
        # Aster: tags[0] — заявленное имя монеты (Unitas, Simons Cat); свободный текст, только для сверки имён
        tags = [t for t in (s.get("tags") or []) if isinstance(t, str) and t.isascii() and t.strip()]
        out.append(dict(
            name_hint=re.sub(r"^\d+\*", "", tags[0]).strip() if tags else None,
            exchange=cl.name, symbol=s["symbol"], base_asset=s["baseAsset"],
            base=config.PERP_CANON.get(cls, {}).get(base, base), factor=factor,
            tick_size=_f(filt.get("PRICE_FILTER", {}).get("tickSize")),
            step_size=_f(filt.get("LOT_SIZE", {}).get("stepSize")),
            min_notional=_f(filt.get("MIN_NOTIONAL", {}).get("notional")),
            onboard_ms=int(s.get("onboardDate") or 0), quote=s.get("quoteAsset"), contract=ct or "PERPETUAL", cls=cls,
        ))
    return out


def asset_class(s: dict) -> str:
    """Класс актива перпа Binance-подобной биржи (исследование 12.09): Binance — contractType TRADIFI_PERPETUAL и
    underlyingType EQUITY*/COMMODITY/PREMARKET, индексы — underlyingType INDEX (ALL, BTCDOM, DEFI); Aster — underlyingType
    всегда COIN, класс в underlyingSubType (STOCK, ETF, Commodities, pre-launch). PAXG/XAUT — токены золота, это монеты
    (у Aster помечены Commodities, а спот PAXG — та же монета)."""
    ut = (s.get("underlyingType") or "").upper()
    sub = {str(x).upper() for x in (s.get("underlyingSubType") or [])}
    base = (s.get("baseAsset") or "").upper()
    if ut == "INDEX" or "INDEX" in sub or base == "BTCDOM":
        return "index"
    if ut == "PREMARKET" or sub & {"PRE-IPO", "PRE-LAUNCH"}:
        return "preipo"
    if base in ("PAXG", "XAUT"):
        return "crypto"
    if ut == "COMMODITY" or "COMMODITIES" in sub:
        return "commodity"
    if (s.get("contractType") or "") == "TRADIFI_PERPETUAL" or ut.endswith("EQUITY") or sub & {"STOCK", "ETF", "TRADFI"}:
        return "equity"
    return "crypto"


def keep_best_quote(ins: list[dict]) -> list[dict]:
    """На площадке одна монета (с одним множителем) — один рынок: USDT, иначе USDC, USD1, U (config.PERP_QUOTES).
    Ключ — с классом актива (12.09): Quant QNTUSDT (монета) и Quantinuum QNTXUSDT (акция, канон QNT) — не дубли."""
    rank = {q: n for n, q in enumerate(config.PERP_QUOTES)}
    best: dict[tuple[str, str, float], dict] = {}
    for i in ins:
        k = (i.get("cls") or "crypto", i["base"], i["factor"])
        if k not in best or rank.get(i.get("quote"), 99) < rank.get(best[k].get("quote"), 99):
            best[k] = i
    keep = {id(v) for v in best.values()}
    return [i for i in ins if id(i) in keep]


def spot_instruments(cl: BinanceLike) -> list[dict]:
    info = cl.get("/api/v3/exchangeInfo")
    # bStocks — токенизированные акции Binance (CRCLB, NVDAB…, 74 шт. на 12.09): в exchangeInfo их не отличить от монет
    # (NVDAB выглядит как BNB), признак — тег в публичном списке продуктов. Нет списка — акций нет, а не «всё с B — акции»
    # (ARB → AR, BNB → BN дали бы чужие пары).
    try:
        bst = cl.bstocks() if hasattr(cl, "bstocks") else set()
    except Exception as e:  # noqa
        log.warning("binance bStocks: %s: %s — токенизированные акции Binance в этот раз без пар", type(e).__name__, e)
        bst = set()
    out = []
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING" or s.get("quoteAsset") != config.QUOTE:
            continue
        base, factor = norm_symbol_factor(s["baseAsset"])
        filt = {f["filterType"]: f for f in s.get("filters", [])}
        stock = s["symbol"] in bst
        ba = s["baseAsset"].upper()
        out.append(dict(exchange=cl.name, symbol=s["symbol"], base_asset=s["baseAsset"],
                        base=("~" + ba) if stock else base, factor=1.0 if stock else factor,
                        tick_size=_f(filt.get("PRICE_FILTER", {}).get("tickSize")),
                        step_size=_f(filt.get("LOT_SIZE", {}).get("stepSize")),
                        min_notional=_f(filt.get("NOTIONAL", {}).get("minNotional")), onboard_ms=0,
                        # акция: своя база «~CRCLB» с монетами не совпадёт, тикер акции — в alt_base (пара — перпу на акцию)
                        alt_base=ba[:-1] if stock and ba.endswith("B") else None))
    return out


def funding_info(cl: BinanceLike) -> dict[str, dict]:
    """symbol -> интервал (ч), кап, флор. Символа нет в ответе — интервал 8 ч (умолчание обеих бирж)."""
    rows = cl.get("/fapi/v1/fundingInfo")
    out = {}
    for r in rows:
        out[r["symbol"]] = dict(
            interval_h=int(r.get("fundingIntervalHours") or 8),
            cap=_f(r.get("adjustedFundingRateCap", r.get("fundingFeeCap"))),
            floor=_f(r.get("adjustedFundingRateFloor", r.get("fundingFeeFloor"))),
        )
    return out


def premium_index(cl: BinanceLike) -> dict[str, dict]:
    """symbol -> марк, индекс, ПРЕДСКАЗАННАЯ ставка ближайшего расчёта, время расчёта."""
    rows = cl.get("/fapi/v1/premiumIndex", retries=config.TICK_RETRIES, timeout=config.TICK_HTTP_TIMEOUT)
    out = {}
    for r in rows:
        out[r["symbol"]] = dict(mark=_f(r.get("markPrice")), index=_f(r.get("indexPrice")),
                                rate=_f(r.get("lastFundingRate")), next_ms=int(r.get("nextFundingTime") or 0),
                                ts_ms=int(r.get("time") or 0))
    return out


def book_tickers(cl: BinanceLike) -> dict[str, dict]:
    path = "/api/v3/ticker/bookTicker" if config.EXCHANGES[cl.name]["kind"] == "spot" else "/fapi/v1/ticker/bookTicker"
    rows = cl.get(path, retries=config.TICK_RETRIES, timeout=config.TICK_HTTP_TIMEOUT)
    out = {}
    for r in rows:
        bid, ask = _f(r.get("bidPrice"), 0.0), _f(r.get("askPrice"), 0.0)
        out[r["symbol"]] = dict(bid=bid, ask=ask, bid_qty=_f(r.get("bidQty"), 0.0), ask_qty=_f(r.get("askQty"), 0.0))
    return out


def funding_history(cl: BinanceLike, symbol: str | None = None, start_ms: int | None = None,
                    end_ms: int | None = None, limit: int = 1000) -> list[dict]:
    """Расчитанные ставки. Без символа — последние `limit` по ВСЕМ монетам (Aster: ~2 ч, Binance: ~6 ч)."""
    params: dict = {"limit": limit}
    if symbol:
        params["symbol"] = symbol
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    rows = cl.get("/fapi/v1/fundingRate", params)
    out = []
    for r in rows:
        rate = _f(r.get("fundingRate"))
        if rate is None:
            continue
        out.append(dict(exchange=cl.name, symbol=r["symbol"], funding_ms=int(r["fundingTime"]), rate=rate,
                        mark=_f(r.get("markPrice"))))
    return out


def funding_history_since(cl: BinanceLike, symbol: str, start_ms: int, end_ms: int | None = None,
                          page: int = 1000) -> list[dict]:
    """Вся история символа с start_ms постранично. Страницы идут по возрастанию времени."""
    end_ms = end_ms or int(time.time() * 1000)
    out, cursor = [], start_ms
    for _ in range(50):
        rows = funding_history(cl, symbol, cursor, end_ms, page)
        out.extend(rows)
        if len(rows) < page:
            break
        cursor = rows[-1]["funding_ms"] + 1
    return out
