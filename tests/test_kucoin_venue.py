"""KuCoin futures («kucoin») on a fake HTTP session — no network. Response shapes are trimmed copies of the 12.09 live
captures (contracts/active, allTickers, contract/funding-rates; see the header of kucoin_fut.py)."""
import json, time, types
import pytest
from funding_bot import config, db, funding, kucoin_fut, universe, venues
from funding_bot.client import BannedError, BudgetExceeded, PermanentHTTPError
from funding_bot.kucoin_fut import KucoinFutures, ACTIVE, TICKERS, HISTORY

H = 3600_000
D = 24 * H
T0000 = 1789171200000                                    # 12.09.2026 00:00 UTC
NOW = T0000 + 14 * H + 30 * 60_000                       # 14:30


class _R:
    def __init__(self, body, code=200, headers=None):
        self.status_code, self._b = code, body
        self.headers = dict(HDR) if headers is None else headers
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self): return self._b

    def raise_for_status(self):
        if self.status_code >= 500:
            raise RuntimeError(str(self.status_code))


class _S:
    def __init__(self, handler):
        self.handler, self.calls, self.headers = handler, [], {}

    def get(self, url, params=None, timeout=None):
        assert url.startswith(KucoinFutures.base), url
        path = url[len(KucoinFutures.base):]
        self.calls.append((path, dict(params or {})))
        return self.handler(path, dict(params or {}))


HDR = {"gw-ratelimit-limit": "2000", "gw-ratelimit-remaining": "1990", "gw-ratelimit-reset": "29000"}


def ok(data, headers=None):
    return _R({"code": "200000", "data": data}, headers=headers)


def contract(symbol, base, quote="USDT", **kw):
    """Shape of a contracts/active row (XBTUSDTM, 12.09 14:35 UTC); kw overrides fields."""
    c = {"symbol": symbol, "rootSymbol": quote, "type": "FFWCSX", "firstOpenDate": 1585555200000, "baseCurrency": base,
         "displayBaseCurrency": base, "quoteCurrency": quote, "settleCurrency": quote, "lotSize": 1, "tickSize": 0.1,
         "multiplier": 0.001, "makerFeeRate": 0.0002, "takerFeeRate": 0.0006, "isQuanto": True, "isInverse": False,
         "indexSymbol": f".K{base}{quote}", "status": "Open", "fundingFeeRate": -1.1e-05, "predictedFundingFeeRate": None,
         "fundingRateGranularity": 8 * H, "fundingRateCap": 0.003, "fundingRateFloor": -0.003,
         "effectiveFundingRateCycleStartTime": 1750147200000, "currentFundingRateGranularity": 8 * H,
         "markPrice": 77393.82, "indexPrice": 77422.07, "nextFundingRateTime": 5356787,
         "nextFundingRateDateTime": T0000 + 16 * H, "sourceExchanges": ["okex", "binance", "kucoin", "bybit", "gateio"],
         "marketStage": "NORMAL", "marketType": "CRYPTO", "assetClass": "CRYPTO", "subMarketType": None,
         "lastTimeFundingRate": 1.7e-05}
    c.update(kw)
    return c


def four_h(symbol, base, **kw):
    kw.setdefault("fundingRateGranularity", 4 * H); kw.setdefault("currentFundingRateGranularity", 4 * H)
    kw.setdefault("fundingRateCap", 0.02); kw.setdefault("fundingRateFloor", -0.02)
    return contract(symbol, base, **kw)


STOCK = dict(assetClass="STOCK", marketType="NASDAQ", subMarketType="US.STOCK", sourceExchanges=["binance_index"])
UNI = [
    contract("XBTUSDTM", "XBT"),
    four_h("10000CATUSDTM", "10000CAT", multiplier=10.0, tickSize=1e-05, markPrice=0.02086, indexPrice=0.02086),
    four_h("1MBABYDOGEUSDTM", "1MBABYDOGE", multiplier=1.0),
    contract("DOGEUSDTM", "DOGE", multiplier=100.0, currentFundingRateGranularity=None, effectiveFundingRateCycleStartTime=None,
             fundingRateCap=0.00525, fundingRateFloor=-0.00525),
    four_h("TREEUSDTM", "TREE", fundingFeeRate=-0.005233),
    four_h("TRUSTUSDTM", "TRUST", nextFundingRateDateTime=T0000 + 15 * H, effectiveFundingRateCycleStartTime=1766372400000),
    contract("ETHUSDTM", "ETH"),
    contract("ETHUSDCM", "ETH", quote="USDC"),                                    # USDC twin: dropped by keep_best_quote
    contract("XBTMU26", "XBT", quote="USD", type="FFICSX", isInverse=True, multiplier=-1.0, fundingFeeRate=None,
             lastTimeFundingRate=None, nextFundingRateDateTime=None, currentFundingRateGranularity=None,
             fundingRateGranularity=None),                                       # dated, inverse
    contract("XBTUSDM", "XBT", quote="USD", isInverse=True, multiplier=-1.0),     # coin-margined
    contract("ANTHROPICUSDTM", "ANTHROPIC", marketStage="PRE_MARKET", **STOCK),
    four_h("PAXGUSDTM", "PAXG", assetClass="METAL"),
    four_h("CLUSDTM", "CL", assetClass="COMMODITY", sourceExchanges=["okx_index"]),
    contract("AAPLUSDTM", "AAPL", **STOCK),
    contract("QNTXUSDTM", "QNTX", **STOCK),
    four_h("NIULAIUSDTM", "NIULAI", displayBaseCurrency="牛来"),
    four_h("NEIROCTOUSDTM", "NEIROCTO"),
    contract("ODDUSDTM", "ODD", assetClass="FOREX"),
    contract("PAUSEDUSDTM", "PAUSED", status="Paused"),
]


def client(contracts=None, tickers=None, series=None, headers=None, gap=0.0):
    series = series or {}

    def handler(path, p):
        if path == ACTIVE:
            return ok(contracts if contracts is not None else UNI, headers)
        if path == TICKERS:
            return ok(tickers or [], headers)
        if path == HISTORY:
            if "from" not in p or "to" not in p:                               # both bounds are required [L]
                return _R({"code": "400000", "msg": "Parameter [from] is required"}, code=400)
            if p["symbol"] not in series:                                     # 404000 comes with HTTP 200 [L]
                return _R({"code": "404000", "msg": "This contract does not exist."})
            rows = sorted((x for x in series[p["symbol"]] if p["from"] <= x[0] <= p["to"]), reverse=True)[:100]
            return ok([{"symbol": p["symbol"], "fundingRate": r, "timepoint": ms} for ms, r in rows], headers)
        raise AssertionError(path)
    s = _S(handler)
    return KucoinFutures(session=s, history_gap_s=gap), s


@pytest.fixture
def clock(monkeypatch):
    c = [NOW / 1000.0]
    slept = []

    def sleep(s):
        slept.append(s); c[0] += max(0.0, s)
    monkeypatch.setattr(kucoin_fut, "time", types.SimpleNamespace(time=lambda: c[0], sleep=sleep))
    return types.SimpleNamespace(now=c, slept=slept)


# --- universe --------------------------------------------------------------------------------------------------------
def test_instruments_universe_bases_factors_classes_intervals():
    cl, s = client()
    ins = {i["symbol"]: i for i in cl.perp_instruments()}
    assert set(ins) == {"XBTUSDTM", "10000CATUSDTM", "1MBABYDOGEUSDTM", "DOGEUSDTM", "TREEUSDTM", "TRUSTUSDTM", "ETHUSDTM",
                        "ANTHROPICUSDTM", "PAXGUSDTM", "CLUSDTM", "AAPLUSDTM", "QNTXUSDTM", "NIULAIUSDTM", "NEIROCTOUSDTM",
                        "ODDUSDTM"}         # USDC twin, dated FFICSX, coin-margined inverse and a paused contract are out
    x = ins["XBTUSDTM"]
    assert (x["base"], x["factor"], x["base_asset"], x["cls"], x["quote"], x["contract"]) == ("BTC", 1.0, "XBT", "crypto", "USDT", "PERPETUAL")
    assert (x["interval_h"], x["cap"], x["floor"], x["tick_size"], x["step_size"], x["contract_size"]) == (8, 0.003, -0.003, 0.1, 0.001, 0.001)
    assert x["min_notional"] is None and x["onboard_ms"] == 1585555200000 and x["exchange"] == "kucoin"
    # factor from the NAME (mark is per one unit of the named base); multiplier is the contract size, not the factor
    assert (ins["10000CATUSDTM"]["base"], ins["10000CATUSDTM"]["factor"], ins["10000CATUSDTM"]["step_size"]) == ("CAT", 10000.0, 10.0)
    assert (ins["1MBABYDOGEUSDTM"]["base"], ins["1MBABYDOGEUSDTM"]["factor"]) == ("BABYDOGE", 1_000_000.0)
    assert ins["DOGEUSDTM"]["interval_h"] == 8                  # currentFundingRateGranularity null → fundingRateGranularity
    assert ins["TREEUSDTM"]["interval_h"] == 4 and ins["TREEUSDTM"]["cap"] == 0.02
    assert {s: ins[s]["cls"] for s in ("ANTHROPICUSDTM", "PAXGUSDTM", "CLUSDTM", "AAPLUSDTM", "ODDUSDTM")} == \
        {"ANTHROPICUSDTM": "preipo", "PAXGUSDTM": "crypto", "CLUSDTM": "commodity", "AAPLUSDTM": "equity", "ODDUSDTM": "rwa"}
    assert (ins["QNTXUSDTM"]["base"], ins["QNTXUSDTM"]["cls"]) == ("QNT", "equity")     # config.PERP_CANON (Quantinuum)
    assert (ins["NIULAIUSDTM"]["base"], ins["NIULAIUSDTM"]["base_asset"]) == ("牛来", "NIULAI")   # Binance, Aster: 牛来
    assert ins["NEIROCTOUSDTM"]["base"] == "NEIRO"              # SPOT_ALIASES verified for kucoin_spot
    assert s.calls == [(ACTIVE, {})]
    assert cl.index_sources()["CLUSDTM"] == ["okx_index"]


def test_alias_not_applied_when_kucoin_lists_the_canonical_ticker_too():
    cl, _ = client([contract("LUNAUSDTM", "LUNA"), contract("LUNA2USDTM", "LUNA2"), contract("RAYUSDTM", "RAY")])
    assert {i["symbol"]: i["base"] for i in cl.perp_instruments()} == {"LUNAUSDTM": "LUNA", "LUNA2USDTM": "LUNA2",
                                                                       "RAYUSDTM": "RAYSOL"}


def test_venues_dispatch_treats_it_as_a_native_perp_not_a_spot():
    cl, _ = client()
    assert venues.native(cl) and not venues.spot_native(cl)
    assert venues.funding_intervals(cl) is None
    assert "XBTUSDTM" in venues.premium(cl)


def test_pairs_with_other_venues_by_base_class_and_factor(monkeypatch):
    monkeypatch.setattr(config, "PERP_VENUES", ("aster", "binance", "hyperliquid", "kucoin"))
    cl, _ = client()
    kc = cl.perp_instruments()
    bn = [dict(symbol="BTCUSDT", base="BTC", factor=1.0, cls="crypto"), dict(symbol="1000CATUSDT", base="CAT", factor=1000.0, cls="crypto"),
          dict(symbol="CATUSDT", base="CAT", factor=1.0, cls="equity"), dict(symbol="牛来USDT", base="牛来", factor=1.0, cls="crypto")]
    ff = {r["key"]: r for r in universe.build_ff({"binance": bn, "kucoin": kc})}
    assert set(ff) == {"binance:BTCUSDT|kucoin:XBTUSDTM", "binance:1000CATUSDT|kucoin:10000CATUSDTM", "binance:牛来USDT|kucoin:NIULAIUSDTM"}
    assert (ff["binance:1000CATUSDT|kucoin:10000CATUSDTM"]["fa"], ff["binance:1000CATUSDT|kucoin:10000CATUSDTM"]["fb"]) == (1000.0, 10000.0)
    spots = {"kucoin_spot": [dict(symbol="NIULAI-USDT", base="牛来", base_asset="牛来", factor=1.0),
                             dict(symbol="NEIROCTO-USDT", base="NEIROCTO", base_asset="NEIROCTO", factor=1.0)]}
    sf = {r["key"] for r in universe.build_sf({"kucoin": kc}, spots)}
    assert {"kucoin_spot:NIULAI-USDT|kucoin:NIULAIUSDTM", "kucoin_spot:NEIROCTO-USDT|kucoin:NEIROCTOUSDTM"} <= sf


# --- tick -------------------------------------------------------------------------------------------------------------
def test_premium_rate_mark_index_next_and_receipt_time():
    cl, s = client()
    t0 = time.time()
    p = cl.premium()
    x = p["XBTUSDTM"]
    assert (x["rate"], x["mark"], x["index"], x["next_ms"]) == (-1.1e-05, 77393.82, 77422.07, T0000 + 16 * H)
    assert x["obs"] >= t0 and abs(x["ts_ms"] - x["obs"] * 1000) < 1
    assert p["TRUSTUSDTM"]["next_ms"] == T0000 + 15 * H          # offset grid: the venue's own next time, not ours
    assert p["TREEUSDTM"]["rate"] == -0.005233
    assert "XBTMU26" not in p and "XBTUSDM" not in p and "PAUSEDUSDTM" not in p
    assert s.calls == [(ACTIVE, {})]


def test_books_sizes_are_contracts_times_multiplier_and_obs_is_receipt_not_trade_ts():
    ticks = [{"symbol": "XBTUSDTM", "bestBidPrice": "77390.5", "bestBidSize": 671, "bestAskPrice": "77390.6", "bestAskSize": 660,
              "ts": 1789151358837000000},                                    # last trade 20 h ago — the book is current
             {"symbol": "10000CATUSDTM", "bestBidPrice": "0.02085", "bestBidSize": 3, "bestAskPrice": "0.02087", "bestAskSize": 4,
              "ts": 1789223358837000000},
             {"symbol": "TREEUSDTM", "bestBidPrice": "", "bestBidSize": 0, "bestAskPrice": "0.5", "bestAskSize": 1, "ts": 0},
             {"symbol": "ETHUSDTM", "bestBidPrice": "2541", "bestBidSize": 1, "bestAskPrice": "2540", "bestAskSize": 1, "ts": 0}]
    cold, _ = client(tickers=ticks)
    assert cold.books()["XBTUSDTM"]["bid_qty"] == 0.0            # before the first contracts/active: size unknown, book kept
    cl, s = client(tickers=ticks)
    cl.premium()
    t0 = time.time()
    b = cl.books()
    assert set(b) == {"XBTUSDTM", "10000CATUSDTM"}               # one-sided and crossed books are not prices
    assert (b["XBTUSDTM"]["bid"], b["XBTUSDTM"]["ask"]) == (77390.5, 77390.6)
    assert b["XBTUSDTM"]["bid_qty"] == pytest.approx(0.671) and b["XBTUSDTM"]["ask_qty"] == pytest.approx(0.66)
    assert b["10000CATUSDTM"]["bid_qty"] == pytest.approx(30.0)
    assert b["XBTUSDTM"]["obs"] >= t0
    assert [c[0] for c in s.calls] == [ACTIVE, TICKERS]


# --- history ----------------------------------------------------------------------------------------------------------
def grid(iv_h, days, last):
    return [(last - k * iv_h * H, 1e-4 + k * 1e-7) for k in range(int(days * 24 / iv_h))]


def test_history_pages_backwards_180_rows_for_4h_over_30_days(clock):
    series = {"TREEUSDTM": grid(4, 40, T0000 + 12 * H)}
    cl, s = client(series=series, gap=0.25)
    rows = cl.history_since("TREEUSDTM", NOW - 30 * D, NOW)
    ms = [r["funding_ms"] for r in rows]
    assert len(rows) == 180 and ms == sorted(ms) and len(set(ms)) == 180
    assert ms[0] > NOW - 30 * D and ms[-1] == T0000 + 12 * H
    assert {b - a for a, b in zip(ms, ms[1:])} == {4 * H}
    assert rows[-1] == dict(exchange="kucoin", symbol="TREEUSDTM", funding_ms=T0000 + 12 * H, rate=1e-4, mark=None)
    hist = [p for path, p in s.calls if path == HISTORY]
    assert len(hist) == 2 and hist[0]["from"] == hist[1]["from"] == NOW - 30 * D
    assert hist[0]["to"] == NOW and hist[1]["to"] == T0000 + 12 * H - 99 * 4 * H - 1     # oldest of page 1, minus 1 ms
    assert clock.slept and max(clock.slept) == pytest.approx(0.25)                          # pacing between pages


def test_history_one_call_for_8h_inclusive_bounds_and_empty_window():
    cl, s = client(series={"XBTUSDTM": grid(8, 40, T0000 + 8 * H)})
    rows = cl.history_since("XBTUSDTM", NOW - 30 * D, NOW)
    assert len(rows) == 90 and len(s.calls) == 1
    assert [r["funding_ms"] for r in cl.history_since("XBTUSDTM", T0000 + 8 * H, T0000 + 8 * H)] == [T0000 + 8 * H]
    n = len(s.calls)
    assert cl.history_since("XBTUSDTM", NOW, NOW - H) == [] and len(s.calls) == n


def test_history_that_would_need_more_pages_than_the_cap_fails_loudly(monkeypatch):
    monkeypatch.setattr(kucoin_fut, "HISTORY_MAX_PAGES", 1)
    cl, _ = client(series={"TREEUSDTM": grid(4, 40, T0000 + 12 * H)})
    with pytest.raises(RuntimeError, match="pages"):
        cl.history_since("TREEUSDTM", NOW - 30 * D, NOW)


def test_unknown_contract_404000_over_http_200_and_missing_bounds_are_permanent():
    cl, s = client(series={})
    with pytest.raises(PermanentHTTPError, match="404000"):
        cl.history_since("NOPEUSDTM", NOW - D, NOW)
    assert len(s.calls) == 1                                     # not retried
    with pytest.raises(PermanentHTTPError):
        cl.get(HISTORY, {"symbol": "TREEUSDTM"})


def test_429000_pauses_the_venue_until_the_reset_and_makes_no_calls_meanwhile():
    lim = {"gw-ratelimit-limit": "2000", "gw-ratelimit-remaining": "0", "gw-ratelimit-reset": "7000"}
    s = _S(lambda path, p: _R({"code": "429000", "msg": "Too Many Requests"}, headers=lim))
    cl = KucoinFutures(session=s)
    with pytest.raises(BannedError):
        cl.premium()
    assert 5.0 < cl.banned_until - time.time() <= 7.5 and not cl.budget_ok()
    with pytest.raises(BannedError):
        cl.books()
    assert len(s.calls) == 1 and cl.health()["n_429"] == 1
    s2 = _S(lambda path, p: _R({"code": "429000"}, code=429, headers={}))
    cl2 = KucoinFutures(session=s2)
    with pytest.raises(BannedError):
        cl2.books()
    assert cl2.banned_until - time.time() > 25                   # gateway overload without limit headers: 30 s


def test_budget_from_shared_pool_headers_gates_history_and_expires_with_its_window():
    busy = {"gw-ratelimit-limit": "2000", "gw-ratelimit-remaining": "400", "gw-ratelimit-reset": "29000"}
    cl, s = client(headers=busy, series={"TREEUSDTM": grid(4, 2, T0000 + 12 * H)})
    cl.premium()
    assert cl.budget_used() == pytest.approx(0.8) and cl.used_weight == 1600 and not cl.budget_ok()
    assert cl.health()["budget"] == 0.8
    with pytest.raises(BudgetExceeded):
        cl.history_since("TREEUSDTM", NOW - D, NOW)
    assert [c[0] for c in s.calls] == [ACTIVE]                   # history not sent
    cl.budget_ts -= 30.0                                         # the window of that reading has rolled
    assert cl.budget_used() == 0.0 and cl.budget_ok()


# --- recent_history: batch without a batch endpoint ----------------------------------------------------------------------
RECENT = [contract("XBTUSDTM", "XBT"),                                            # 8 h epoch grid
          four_h("TREEUSDTM", "TREE", lastTimeFundingRate=-0.004),              # 4 h epoch grid
          four_h("TRUSTUSDTM", "TRUST", lastTimeFundingRate=5e-05),             # 4 h grid offset by 3 h
          contract("DOGEUSDTM", "DOGE", currentFundingRateGranularity=None, effectiveFundingRateCycleStartTime=None),
          contract("ETHUSDTM", "ETH"), contract("ETHUSDCM", "ETH", quote="USDC"),
          contract("XBTMU26", "XBT", quote="USD", type="FFICSX", isInverse=True, fundingFeeRate=None, lastTimeFundingRate=None,
                   nextFundingRateDateTime=None, currentFundingRateGranularity=None, fundingRateGranularity=None)]


def at(contracts, t_ms, offsets):
    """Next settlement of each contract as the venue shows it at t_ms (epoch grid, or offset by `offsets[symbol]` h)."""
    out = []
    for c in contracts:
        c = dict(c)
        iv = c.get("currentFundingRateGranularity") or c.get("fundingRateGranularity")
        if iv:
            off = offsets.get(c["symbol"], 0) * H
            c["nextFundingRateDateTime"] = ((t_ms - off) // iv + 1) * iv + off
        out.append(c)
    return out


def split(batch):
    """Строки пакета {символ: расчёт} и метки hold — контракты, в пакет намеренно не вошедшие (их ведёт добор)."""
    assert all(("funding_ms" in r) != bool(r.get("hold")) for r in batch)
    return ({r["symbol"]: r["funding_ms"] for r in batch if not r.get("hold")},
            {r["symbol"] for r in batch if r.get("hold")})


def test_recent_history_rows_are_last_settlements_of_the_universe(clock):
    cl, s = client(at(RECENT, NOW, {"TRUSTUSDTM": 3}))
    rows = {r["symbol"]: r for r in cl.recent_history()}
    assert set(rows) == {"XBTUSDTM", "TREEUSDTM", "TRUSTUSDTM", "DOGEUSDTM", "ETHUSDTM"}     # no USDC twin, no dated
    assert rows["XBTUSDTM"] == dict(exchange="kucoin", symbol="XBTUSDTM", funding_ms=T0000 + 8 * H, rate=1.7e-05, mark=None)
    assert rows["TREEUSDTM"]["funding_ms"] == T0000 + 12 * H and rows["TREEUSDTM"]["rate"] == -0.004
    assert rows["TRUSTUSDTM"]["funding_ms"] == T0000 + 11 * H    # 14:30: its 07:00 is before cover 08:01 — safe
    assert s.calls == [(ACTIVE, {})]


def test_recent_history_drops_an_offset_grid_contract_whose_previous_settlement_is_inside_the_batch_window(clock, tmp_path):
    t_ms = T0000 + 7 * H + 30 * 60_000                           # 07:30: cover 00:01, TRUST settled 03:00 and 07:00
    clock.now[0] = t_ms / 1000.0
    cl, _ = client(at(RECENT, t_ms, {"TRUSTUSDTM": 3}))
    batch = cl.recent_history()
    got, held = split(batch)
    rows = [r for r in batch if not r.get("hold")]
    assert "TRUSTUSDTM" not in got and "TREEUSDTM" in got and held == {"TRUSTUSDTM"}
    # end to end through funding.apply_batch: TRUST's cursor (all settled up to 00:00) must not jump over 03:00
    ivs = {"XBTUSDTM": 8, "TREEUSDTM": 4, "TRUSTUSDTM": 4, "DOGEUSDTM": 8, "ETHUSDTM": 8}
    floor = t_ms - 30 * D

    def cursors(batch, name, iv=ivs):
        con = db.connect(tmp_path / name)
        for sym in iv:
            db.set_leg_depth(con, "kucoin", sym, floor, synced_to=T0000 + 30 * 60_000)
        funding.apply_batch(con, batch, iv, t_ms)
        return db.leg_sync(con)
    good = cursors(batch, "guarded.db")
    assert good[("kucoin", "TRUSTUSDTM")] == T0000 + 30 * 60_000
    assert good[("kucoin", "TREEUSDTM")] == t_ms - config.SETTLE_GRACE_S * 1000
    # у коллектора устаревший интервал 8 ч (> окна 00:01…07:30): без метки hold курсор TRUST ушёл бы правилом «длинный
    # интервал — нечего было рассчитывать» за его 03:00 и 07:00; с меткой — стоит, его ведёт посимвольный добор
    stale = dict(ivs, TRUSTUSDTM=8)
    assert cursors(rows, "stale_nohold.db", stale)[("kucoin", "TRUSTUSDTM")] == t_ms - config.SETTLE_GRACE_S * 1000
    assert cursors(batch, "stale_hold.db", stale)[("kucoin", "TRUSTUSDTM")] == T0000 + 30 * 60_000
    # without the guard the batch would have claimed TRUST complete through 07:20 — its 03:00 settlement lost silently
    raw = at(RECENT, t_ms, {"TRUSTUSDTM": 3})
    trust = next(c for c in raw if c["symbol"] == "TRUSTUSDTM")
    naive = rows + [dict(exchange="kucoin", symbol="TRUSTUSDTM", funding_ms=trust["nextFundingRateDateTime"] - 4 * H,
                         rate=5e-05, mark=None)]
    assert cursors(naive, "naive.db")[("kucoin", "TRUSTUSDTM")] > T0000 + 3 * H


def test_recent_history_skips_fresh_rolls_and_settlements_before_the_current_cycle(clock):
    t_ms = T0000 + 16 * H + 30_000                               # 16:00:30 — XBT and TREE rolled 30 s ago
    clock.now[0] = t_ms / 1000.0
    lagging = contract("ETHUSDTM", "ETH", nextFundingRateDateTime=T0000 + 16 * H)   # not rolled yet: last = 08:00
    changed = four_h("TENCENTUSDTM", "TENCENT", nextFundingRateDateTime=T0000 + 20 * H,
                     effectiveFundingRateCycleStartTime=T0000 + 16 * H)             # cycle start = a real settlement [L]
    announced = four_h("BYDUSDTM", "BYD", nextFundingRateDateTime=T0000 + 20 * H,
                       effectiveFundingRateCycleStartTime=T0000 + 20 * H)           # next − interval predates the cycle
    cl, _ = client(at(RECENT[:2], t_ms, {}) + [lagging, changed, announced])
    rows, held = split(cl.recent_history())
    assert rows == {"ETHUSDTM": T0000 + 8 * H}                   # 16:00 of TENCENT is 30 s old too
    assert held == {"XBTUSDTM", "TREEUSDTM", "TENCENTUSDTM", "BYDUSDTM"}     # every skipped contract comes as a marker
    clock.now[0] = (t_ms + 5 * 60_000) / 1000.0                  # 16:05:30
    rows, held = split(cl.recent_history())
    # the laggard's 08:00 keeps cover at 08:01: the 4 h contracts' 12:00 lies inside the window and is not in the batch
    assert rows == {"ETHUSDTM": T0000 + 8 * H, "XBTUSDTM": T0000 + 16 * H}
    assert held == {"TREEUSDTM", "TENCENTUSDTM", "BYDUSDTM"}
    cl, _ = client(at(RECENT[:2], t_ms, {}) + [changed, announced])
    rows, held = split(cl.recent_history())
    assert rows == {"XBTUSDTM": T0000 + 16 * H, "TREEUSDTM": T0000 + 16 * H, "TENCENTUSDTM": T0000 + 16 * H}
    assert held == {"BYDUSDTM"}


# --- 13.09: READYUSDTM 4 ч → 1 ч, расчёт 00:00 потерян под курсором ------------------------------------------------------
D13 = T0000 + D                                               # 13.09.2026 00:00 UTC


def ready(nxt, last, cur_h=1):
    """READYUSDTM 13.09 [L]: 4 ч по 00:00 включительно (−0.02 = пол), с 01:00 — 1 ч (effectiveFundingRateCycleStartTime
    01:00). cur_h — что биржа показывала в currentFundingRateGranularity между 00:00 и 01:00 (не видели: 1 ч или ещё 4 ч)."""
    return four_h("READYUSDTM", "READY", fundingRateGranularity=H, currentFundingRateGranularity=cur_h * H,
                  nextFundingRateDateTime=nxt, effectiveFundingRateCycleStartTime=D13 + H, lastTimeFundingRate=last)


READY_HIST = [(D13 - 4 * H, -0.010163), (D13, -0.02), (D13 + H, -0.01282)]      # funding-rates биржи [L]


@pytest.mark.parametrize("cur_h", [1, 4])
def test_interval_change_skipped_by_the_batch_keeps_its_cursor_and_repair_collects_the_last_old_grid_settlement(
        clock, tmp_path, monkeypatch, cur_h):
    """Прод 13.09: курсор READY 01:00:46, а расчёта 00:00 в БД нет (у биржи −2 %). 00:10:46 — пакет: READY пропущен
    проверкой цикла (00:00 раньше начала цикла 01:00), окно пакета открыл 23:00 TRUST (23:01…00:10 = 70 мин), у коллектора
    интервал READY ещё 4 ч (вселенная раз в час) — правило «длинный интервал» ставило курсор на 00:00:46. Полнота
    сравнивает курсор с последним расчётом по слову биржи, дыру под курсором не видит — посимвольный добор не шёл."""
    monkeypatch.setattr(funding, "time", types.SimpleNamespace(time=lambda: clock.now[0]))
    grace = config.SETTLE_GRACE_S * 1000
    cur0 = D13 - 14 * 60_000 - 14_000                           # 23:45:46 — курсор после досбора 23:55:46
    t1 = D13 + 10 * 60_000 + 46_000                             # 00:10:46
    t2 = t1 + H                                                  # 01:10:46

    def leg(con):
        return db.leg_sync(con)[("kucoin", "READYUSDTM")]

    def run(name, hold):
        con = db.connect(tmp_path / name)
        db.insert_funding_events(con, [dict(exchange="kucoin", symbol="READYUSDTM", funding_ms=D13 - 4 * H,
                                            rate=-0.010163, mark=None)], {"READYUSDTM": 4})
        for sym in ("XBTUSDTM", "TREEUSDTM", "TRUSTUSDTM", "READYUSDTM"):
            db.set_leg_depth(con, "kucoin", sym, cur0 - 30 * D, synced_to=cur0)
        clock.now[0] = t1 / 1000.0
        cl, _ = client(at(RECENT[:3], t1, {"TRUSTUSDTM": 3}) + [ready(D13 + H, -0.02, cur_h)])
        batch = cl.recent_history()
        assert split(batch) == ({"XBTUSDTM": D13, "TREEUSDTM": D13, "TRUSTUSDTM": D13 - H}, {"READYUSDTM"})
        funding.apply_batch(con, batch if hold else [r for r in batch if not r.get("hold")],
                            {"XBTUSDTM": 8, "TREEUSDTM": 4, "TRUSTUSDTM": 4, "READYUSDTM": 4}, t1)
        after1 = leg(con)
        # 01:10:46: вселенная 01:01 уже знает 1 ч; READY 01:00 не в пакете (его 00:00 позже окна 23:01 TRUST) — добор
        clock.now[0] = t2 / 1000.0
        cl, _ = client(at(RECENT[:3], t2, {"TRUSTUSDTM": 3}) + [ready(D13 + 2 * H, -0.01282)],
                       series={"READYUSDTM": READY_HIST})
        ivs = {"XBTUSDTM": 8, "TREEUSDTM": 4, "TRUSTUSDTM": 4, "READYUSDTM": 1}
        funding.apply_batch(con, cl.recent_history(), ivs, t2)
        comp = funding.completeness(db.funding_events_since(con, t2 - 31 * D), [("kucoin", "READYUSDTM")],
                                    {"kucoin": ivs}, t2, 720, {"kucoin": {"READYUSDTM": D13 + 2 * H}},
                                    depth=db.leg_depths(con), sync=db.leg_sync(con))
        g = comp[("kucoin", "READYUSDTM")]
        assert g["missing"] == [D13 + H]
        funding.repair(con, {"kucoin": cl}, {("kucoin", "READYUSDTM"): g}, {"kucoin": ivs})
        got = {ms: r for ms, r in con.execute("SELECT funding_ms, rate FROM funding_events WHERE symbol='READYUSDTM'")}
        return after1, g["since"], got, leg(con)

    after1, since, got, cur = run("hold.db", hold=True)
    assert after1 == cur0 and since == cur0 + 1                  # курсор не перешёл 00:00 — добор идёт от 23:45:47
    assert got == dict(READY_HIST) and cur == t2 - grace         # 00:00 (−2 %) и 01:00 собраны, курсор 01:00:46
    # как было в проде: без метки курсор ушёл на 00:00:46, добор 01:10 взял только 01:00 — 00:00 потерян под курсором
    after1, since, got, cur = run("nohold.db", hold=False)
    assert after1 == t1 - grace and since == t1 - grace + 1
    assert D13 not in got and got[D13 + H] == -0.01282 and cur == t2 - grace
