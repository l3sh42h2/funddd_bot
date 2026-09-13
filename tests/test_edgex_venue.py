"""edgeX V2 (edgex.py) on a fake HTTP session and fake WS connections — no network. Response shapes are trimmed copies of
the 12-13.09 live captures (getMetaData, contract-labels, getLatestFundingRate, getFundingRatePage, ticker.all.1s)."""
from __future__ import annotations
import json, socket, time, types
import pytest
import requests
from funding_bot import calc, config, db, edgex, funding, universe, venues
from funding_bot.client import BannedError, BudgetExceeded, PermanentHTTPError
from funding_bot.edgex import EdgexPerp

H_MS = 3600_000
T0 = 1789249063.0                    # 12.09.2026 21:37:43 UTC (live capture)
FT = 1789243200000                   # last settlement 20:00 UTC
NEXT = FT + 4 * H_MS                 # 13.09 00:00 UTC = the ticker's nextFundingTime [L]
FTS = 1789249020000                  # fundingTimestamp of the per-minute batch: 21:37:00


class _R:
    def __init__(self, body, code=200, headers=None):
        self.status_code, self._b, self.headers = code, body, headers or {}
        self.text = json.dumps(body)

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 500:
            raise RuntimeError(str(self.status_code))


class _S:
    def __init__(self, handler):
        self.handler, self.calls, self.headers = handler, [], {}

    def get(self, url, params=None, timeout=None):
        assert url.startswith(edgex.REST)
        path = url[len(edgex.REST):]
        self.calls.append((path, dict(params or {})))
        return self.handler(path, dict(params or {}))

    def paths(self):
        return [c[0] for c in self.calls]


def env(data, code="SUCCESS"):
    return {"code": code, "data": data, "msg": None, "errorParam": None, "requestTime": "1789249063227",
            "responseTime": "1789249063227", "traceId": "a678e4874e768f2b"}


@pytest.fixture(autouse=True)
def _fresh_host(monkeypatch):
    monkeypatch.setattr(edgex, "_HOST", edgex._Host())


@pytest.fixture
def clock(monkeypatch):
    c = [T0]

    def sleep(s):
        c[0] += max(0.0, s)
    monkeypatch.setattr(edgex, "time", types.SimpleNamespace(time=lambda: c[0], sleep=sleep, gmtime=time.gmtime,
                                                              strftime=time.strftime))
    return c


# --- recorded shapes ---------------------------------------------------------------------------------------------------
def contract(cid, name, coin, stock=False, fx=False, display=True, can_open=True, iv="240", cap="0.002", tick="0.1",
             step="0.001", mos="0.001", feed=None, quote="1000"):
    return {"contractId": cid, "contractName": name, "baseCoinId": coin, "quoteCoinId": quote, "tickSize": tick,
            "stepSize": step, "minOrderSize": mos, "maxOrderSize": "100", "defaultTakerFeeRate": "0.00045",
            "defaultMakerFeeRate": "0.0004", "enableTrade": True, "enableDisplay": display,
            "enableOpenPosition": can_open, "fundingInterestRate": "0.0003", "fundingMaxRate": cap,
            "fundingMinRate": "-" + cap, "fundingRateIntervalMin": iv, "syntheticAssetId": "0x425443322d31300000000000000000",
            "oraclePriceQuorum": "1", "oraclePriceSignedAssetIds": [feed] * 5 if feed else [], "isStock": stock, "isFx": fx}


COINS = [{"coinId": i, "coinName": n, "stepSize": "0.001", "iconUrl": f"https://static.edgex.exchange/icons/coin/{n}.png",
          "assetId": None, "resolution": None}
         for i, n in (("1000", "USDC"), ("1001", "BTC"), ("1046", "1000PEPE"), ("1169", "HAJIMI"), ("1012", "AAPL"),
                      ("1010", "SPY"), ("1076", "SPCX"), ("1005", "XAU"), ("1090", "JPM"), ("1162", "EUR"),
                      ("1163", "JPY"), ("1158", "ZRO"), ("1200", "PAXG"), ("1201", "SLOW"), ("1202", "NOOPEN"),
                      ("1999", "USDT"))]
CONTRACTS = [contract("30000001", "BTCUSDC", "1001", feed="BTCUSD"),
             contract("30000046", "1000PEPEUSDC", "1046", cap="0.004", tick="0.0000001", step="100", mos="15000",
                      feed="1000PEPEUSD"),
             contract("30000169", "哈基米USDC", "1169", cap="0.02", feed="HAJIMIUSD"),       # base from coinList: HAJIMI
             contract("30000012", "AAPLUSDC", "1012", stock=True, cap="0.004", feed="AAPLUSD"),
             contract("30000010", "SPYUSDC", "1010", stock=True),                            # an ETF, flagged isStock
             contract("30000076", "SPCXUSDC", "1076", stock=True),                           # equity elsewhere → equity
             contract("30000005", "XAUUSDC", "1005", cap="0.004", feed="XAUUSD"),
             contract("30000090", "JPMUSDC", "1090", stock=True),                            # also in «Commodities TradeFi»
             contract("30000162", "EURUSDC", "1162", fx=True, display=False),                # hidden on 13.09 [L]
             contract("30000163", "JPYUSDC", "1163", fx=True, display=False),
             contract("30000158", "ZROUSDC", "1158", display=False),
             contract("30000200", "PAXGUSDC", "1200"),
             contract("30000201", "SLOWUSDC", "1201", iv="480"),
             contract("30000202", "NOOPENUSDC", "1202", can_open=False),
             contract("30000203", "BTCUSDT", "1001", quote="1999")]
UNIVERSE = {"BTCUSDC", "1000PEPEUSDC", "哈基米USDC", "AAPLUSDC", "SPYUSDC", "SPCXUSDC", "XAUUSDC", "JPMUSDC", "PAXGUSDC",
            "SLOWUSDC"}
CIDS = {c["contractName"]: c["contractId"] for c in CONTRACTS}


def group(name, names):
    return {"name": name, "multiLanguageKey": "tabs.x", "sort": 1, "productCategory": "PrepV2", "rewardRate": "",
            "contracts": [{"contractId": CIDS[n], "contractName": n, "sort": k, "rewardRate": ""} for k, n in enumerate(names)]}


LABELS = [group("Commodities TradeFi", ["XAUUSDC", "JPMUSDC"]), group("Commodities V2", ["XAUUSDC"]),
          group("Pre-IPO V2", []), group("Trending V2", ["BTCUSDC"])]


def latest(name, forecast="0.00005", fr="0.00005", ft=FT, fts=FTS, mark="1.0", index="1.0", iv="240", settle=False,
           ibid="0.99", iask="1.01"):
    return {"contractId": CIDS[name], "fundingTime": str(ft), "fundingTimestamp": str(fts), "oraclePrice": mark,
            "markPrice": mark, "indexPrice": index, "fundingRate": fr, "isSettlement": settle,
            "forecastFundingRate": forecast, "previousFundingRate": fr, "previousFundingTimestamp": str(fts - 60000),
            "premiumIndex": "0.00000000", "avgPremiumIndex": "-0.00001551", "premiumIndexTimestamp": str(fts),
            "impactMarginNotional": "100", "impactAskPrice": iask, "impactBidPrice": ibid, "interestRate": "0.0003",
            "predictedFundingRate": "0.00005000", "fundingRateIntervalMin": iv, "fundingIndex": mark}


LATEST = {r["contractId"]: r for r in [
    latest("BTCUSDC", "-0.00005463", "-0.00005532", mark="77210.58", index="77242.30", ibid="77080.7", iask="77080.8"),
    latest("1000PEPEUSDC", mark="0.0034128", index="0.0034134"),
    latest("哈基米USDC", "0.00016296", "0.00004455", ft=FT - 4 * H_MS),       # still shows the 16:00 settlement
    latest("AAPLUSDC", "", "-0.00004518", settle=True),                       # the settlement minute: forecast empty
    latest("SPYUSDC", ""),                                                    # no forecast outside it → no rate
    latest("SPCXUSDC", ft=0),                                                 # never settled yet
    latest("XAUUSDC"), latest("JPMUSDC"), latest("PAXGUSDC"),
    latest("SLOWUSDC", iv="480", ft=FT - 4 * H_MS),                           # 8 h grid: 16:00
    latest("EURUSDC")]}                                                       # hidden: never asked for


def routes(contracts=CONTRACTS, labels=LABELS, latest_rows=LATEST, hist=None):
    def h(path, p):
        if path == edgex.PATH_META:
            return _R(env({"global": {}, "coinList": COINS, "contractList": contracts, "multiChain": {}, "campaign": {}}))
        if path == edgex.PATH_LABELS:
            return labels(p) if callable(labels) else _R(env(labels))
        if path == edgex.PATH_LATEST:
            ids = [x for x in str(p.get("contractId") or "").split(",") if x]
            return _R(env([latest_rows[i] for i in ids if i in latest_rows]))   # unknown id → [] with SUCCESS [L]
        if path == edgex.PATH_HIST and hist is not None:
            return hist(p)
        raise AssertionError(path)
    return h


def tick_row(name, bid, ask, fr="0.00005", mark="1.0", index="1.0", ft=FT, nft=NEXT, **kw):
    return dict({"contractId": CIDS[name], "contractName": name, "priceChange": "-137.7", "trades": "55539",
                 "value": "140490146.5633", "lastPrice": mark, "indexPrice": index, "oraclePrice": mark, "markPrice": mark,
                 "openInterest": "3383.464", "fundingRate": fr, "fundingTime": str(ft), "nextFundingTime": str(nft),
                 "bestAskPrice": ask, "bestBidPrice": bid, "marketOpen": True}, **kw)


def frame(rows, kind="Snapshot"):
    return {"type": "quote-event", "channel": edgex.CHANNEL,
            "content": {"channel": edgex.CHANNEL, "dataType": kind, "data": rows}}


def stub_stream(rows=None, rx=None):
    st = edgex._Stream(edgex.WS_URL, "edgex")
    st.start, st._waited = (lambda: None), True                   # no thread: frames are applied by the test
    if rows is not None:
        st.apply(frame(rows), rx)
    return st


def client(s, st=None, gap=0.0):
    cl = EdgexPerp(session=s, history_gap_s=gap)
    cl._stream = st or stub_stream()
    return cl


def hrow(name, ms, rate, mark="77082.75", settle=True):
    return {"contractId": CIDS[name], "fundingTime": str(ms), "fundingTimestamp": str(ms), "oraclePrice": mark,
            "markPrice": mark, "indexPrice": mark, "fundingRate": rate, "isSettlement": settle, "forecastFundingRate": "",
            "previousFundingRate": rate, "premiumIndex": "-0.00012418", "avgPremiumIndex": "-0.00012418",
            "premiumIndexTimestamp": str(ms), "impactMarginNotional": "100", "impactAskPrice": mark,
            "impactBidPrice": mark, "interestRate": "0.0003", "predictedFundingRate": "0.00005000",
            "fundingRateIntervalMin": "240", "fundingIndex": mark}


def hist_server(rows):
    """getFundingRatePage: the server filters [begin, end), pages NEWEST first; the token is an opaque string [L]."""
    def h(p):
        assert p["filterSettlementFundingRate"] == "true"
        lo, hi = int(p["filterBeginTimeInclusive"]), int(p["filterEndTimeExclusive"])
        sel = sorted((r for r in rows if r["contractId"] == p["contractId"] and lo <= int(r["fundingTime"]) < hi),
                     key=lambda r: -int(r["fundingTime"]))
        off, size = int(p.get("offsetData") or 0), int(p["size"])
        nxt = str(off + size) if off + size < len(sel) else ""
        return _R(env({"dataList": sel[off:off + size], "nextPageOffsetData": nxt}))
    return h


# --- universe ------------------------------------------------------------------------------------------------------------
def test_instruments_classes_bases_filters_and_links(clock, tmp_path):
    s = _S(routes())
    ins = {i["symbol"]: i for i in client(s).perp_instruments()}
    # hidden (ZRO, EUR, JPY), not openable, and a non-USDC quote are not in the universe
    assert set(ins) == UNIVERSE
    got = {k: (i["cls"], i["base"], i["factor"]) for k, i in ins.items()}
    assert got == {"BTCUSDC": ("crypto", "BTC", 1.0), "1000PEPEUSDC": ("crypto", "PEPE", 1000.0),
                   "哈基米USDC": ("crypto", "HAJIMI", 1.0), "AAPLUSDC": ("equity", "AAPL", 1.0),
                   "SPYUSDC": ("equity", "SPY", 1.0), "SPCXUSDC": ("equity", "SPCX", 1.0),   # review 13.09
                   "XAUUSDC": ("commodity", "XAU", 1.0), "JPMUSDC": ("equity", "JPM", 1.0),   # isStock beats TradeFi
                   "PAXGUSDC": ("crypto", "PAXG", 1.0), "SLOWUSDC": ("crypto", "SLOW", 1.0)}
    b = ins["BTCUSDC"]
    assert (b["exchange"], b["base_asset"], b["interval_h"], b["cap"], b["floor"], b["quote"], b["contract"]) == \
        ("edgex", "BTC", 4, 0.002, -0.002, "USDC", "v2")                          # cap/floor per interval
    assert (b["contract_id"], b["tick_size"], b["step_size"], b["min_notional"], b["min_qty"], b["onboard_ms"]) == \
        ("30000001", 0.1, 0.001, None, 0.001, 0)                                  # minOrderSize is a base quantity
    assert b["name_hint"] is None and b["oracle_feed"] == "BTCUSD"                # a ticker is not a name
    assert ins["1000PEPEUSDC"]["oracle_feed"] == "1000PEPEUSD" and ins["1000PEPEUSDC"]["cap"] == 0.004
    assert ins["SLOWUSDC"]["interval_h"] == 8
    assert b["url"] == "https://pro.edgex.exchange/en-US/perpetuals/BTCUSDC"
    assert ins["哈基米USDC"]["url"] == "https://pro.edgex.exchange/en-US/perpetuals/%E5%93%88%E5%9F%BA%E7%B1%B3USDC"
    assert s.paths() == [edgex.PATH_META, edgex.PATH_LABELS]
    con = db.connect(tmp_path / "e.db")                                           # extra fields do not break the DB
    db.upsert_instruments(con, list(ins.values()))
    assert db.load_instruments(con, "edgex")["1000PEPEUSDC"]["factor"] == 1000.0


def test_fx_units_class_helpers_and_pairs_within_class(monkeypatch):
    # JPYUSDC is USD per JPY (0.00649 [L]) — the reciprocal of HL xyz:JPY / Lighter USDJPY (≈153): its own base
    assert edgex.perp_base("JPY", "fx") == ("JPYUSD", 1.0) and edgex.perp_base("EUR", "fx") == ("EUR", 1.0)
    assert edgex.perp_base("1000RATS", "crypto") == ("RATS", 1000.0)
    assert edgex.perp_base("GOLD", "commodity") == ("XAU", 1.0)                   # config.PERP_CANON applies
    assert edgex.asset_class("JPM", True, False, True) == "equity"
    assert edgex.asset_class("XAUT", False, False, True) == "crypto"
    assert edgex.asset_class("EUR", False, True, False) == "fx"
    assert edgex.interval_h("240") == 4 and edgex.interval_h("480") == 8 and edgex.interval_h("60") == 1
    assert edgex.interval_h(None) is None and edgex.interval_h("0") is None
    monkeypatch.setattr(config, "PERP_VENUES", ("hyperliquid", "edgex"))
    vis = [dict(c, enableDisplay=True) if c["contractName"] in ("EURUSDC", "JPYUSDC") else c for c in CONTRACTS]
    ins = client(_S(routes(contracts=vis))).perp_instruments()
    assert {i["symbol"]: (i["cls"], i["base"]) for i in ins if i["cls"] == "fx"} == \
        {"EURUSDC": ("fx", "EUR"), "JPYUSDC": ("fx", "JPYUSD")}
    hl = [dict(symbol=s, base=b, cls=c, factor=f) for s, b, c, f in
          (("BTC", "BTC", "crypto", 1.0), ("kPEPE", "PEPE", "crypto", 1000.0), ("xyz:GOLD", "XAU", "commodity", 1.0),
           ("xyz:AAPL", "AAPL", "equity", 1.0), ("xyz:SPCX", "SPCX", "equity", 1.0), ("xyz:EUR", "EUR", "fx", 1.0),
           ("xyz:JPY", "JPY", "fx", 1.0), ("PAXG", "PAXG", "crypto", 1.0))]
    ff = universe.build_ff({"hyperliquid": hl, "edgex": ins})
    assert {r["key"] for r in ff} == {"hyperliquid:BTC|edgex:BTCUSDC", "hyperliquid:kPEPE|edgex:1000PEPEUSDC",
                                      "hyperliquid:xyz:GOLD|edgex:XAUUSDC", "hyperliquid:xyz:AAPL|edgex:AAPLUSDC",
                                      "hyperliquid:xyz:EUR|edgex:EURUSDC", "hyperliquid:PAXG|edgex:PAXGUSDC",
                                      "hyperliquid:xyz:SPCX|edgex:SPCXUSDC"}      # review 13.09: SPCX is a share on both
    assert all(r["va"] == "hyperliquid" for r in ff)                              # JPY ≠ JPYUSD


def test_labels_outage_uses_a_recent_copy_then_the_known_list(clock):
    state = {"down": False}

    def labels(p):
        return _R({"code": "GATEWAY_INTERNAL_ERROR"}, 503) if state["down"] else _R(env(LABELS))
    cl = client(_S(routes(labels=labels)))
    cls = lambda: {i["symbol"]: i["cls"] for i in cl.perp_instruments()}
    assert cls()["XAUUSDC"] == "commodity"
    state["down"] = True
    clock[0] += 3600
    assert cls()["XAUUSDC"] == "commodity" and cls()["JPMUSDC"] == "equity"        # the cached group
    clock[0] += edgex.LABELS_STALE_OK_S
    c = cls()
    assert c["XAUUSDC"] == "commodity" and c["BTCUSDC"] == "crypto"               # the 13.09 list stands in


# --- tick ------------------------------------------------------------------------------------------------------------------
def test_premium_rest_fallback_is_per_interval_forecast_and_chunked(clock, monkeypatch):
    monkeypatch.setattr(edgex, "BATCH_IDS", 4)
    s = _S(routes())
    cl = client(s)                                                               # no stream data → the REST batch
    p = cl.premium()
    assert set(p) == UNIVERSE - {"SPYUSDC"}
    # the forecast — not predictedFundingRate (interestRate/6), not the settled fundingRate; per 4 h, no conversion
    assert p["BTCUSDC"] == dict(rate=-0.00005463, mark=77210.58, index=77242.3, next_ms=NEXT, ts_ms=FTS, obs=FTS / 1000,
                                interval_h=4)                                    # obs = the batch's minute, not arrival
    assert calc.hourly(p["BTCUSDC"]["rate"], p["BTCUSDC"]["interval_h"]) == pytest.approx(-0.00005463 / 4)
    assert p["AAPLUSDC"]["rate"] == -0.00004518                                  # settlement minute → finalized rate
    assert p["哈基米USDC"]["next_ms"] == NEXT and p["SPCXUSDC"]["next_ms"] == NEXT  # passed / missing → rolled on the grid
    assert (p["SLOWUSDC"]["interval_h"], p["SLOWUSDC"]["next_ms"]) == (8, FT - 4 * H_MS + 8 * H_MS)
    lat = [c[1]["contractId"].split(",") for c in s.calls if c[0] == edgex.PATH_LATEST]
    assert [len(x) for x in lat] == [4, 4, 2] and {i for x in lat for i in x} == {CIDS[n] for n in UNIVERSE}
    clock[0] = NEXT / 1000 + 30                                                  # 00:00:30, batch still at 20:00
    assert cl.premium()["BTCUSDC"]["next_ms"] == NEXT + 4 * H_MS
    with pytest.raises(RuntimeError, match="empty for 4 known"):
        client(_S(routes(latest_rows={}))).premium()                             # known ids answered [] = failure


def test_premium_and_books_from_the_stream_then_degraded(clock):
    st = stub_stream([tick_row("BTCUSDC", "77195.6", "77195.8", fr="-0.00005858", mark="77210.58", index="77242.30"),
                      tick_row("1000PEPEUSDC", "0.0034123", "0.0034130", mark="0.0034128", index="0.0034134"),
                      tick_row("哈基米USDC", "", "0.0123"),                           # empty side
                      tick_row("AAPLUSDC", "230.5", "230.1", marketOpen=False),       # crossed
                      tick_row("SPYUSDC", "600", "660"),                              # 9.5 % wide
                      tick_row("EURUSDC", "1.16", "1.17")], rx=T0 - 1)               # hidden: not in the universe
    s = _S(routes())
    cl = client(s, st)
    p = cl.premium()
    assert p["BTCUSDC"] == dict(rate=-0.00005858, mark=77210.58, index=77242.3, next_ms=NEXT, ts_ms=int((T0 - 1) * 1000),
                                obs=T0 - 1, interval_h=4)
    assert set(p) == {"BTCUSDC", "1000PEPEUSDC", "哈基米USDC", "AAPLUSDC", "SPYUSDC"}
    assert edgex.PATH_LATEST not in s.paths()                                    # a live stream: no REST in the tick
    assert cl.books() == {"BTCUSDC": dict(bid=77195.6, ask=77195.8, bid_qty=0.0, ask_qty=0.0, obs=T0 - 1),
                          "1000PEPEUSDC": dict(bid=0.0034123, ask=0.003413, bid_qty=0.0, ask_qty=0.0, obs=T0 - 1)}
    st.apply(frame([{"contractId": CIDS["BTCUSDC"], "bestBidPrice": "77195.7"}], "changed"), T0)   # a subset merges
    assert cl.books()["BTCUSDC"] == dict(bid=77195.7, ask=77195.8, bid_qty=0.0, ask_qty=0.0, obs=T0)
    clock[0] = T0 + edgex.WS_FRESH_S + 1                                         # the stream went quiet
    p = cl.premium()
    assert p["BTCUSDC"]["rate"] == -0.00005463 and p["BTCUSDC"]["obs"] == FTS / 1000
    n = len(s.calls)
    assert cl.books()["BTCUSDC"] == dict(bid=77080.7, ask=77080.8, bid_qty=0.0, ask_qty=0.0, obs=FTS / 1000)
    clock[0] += edgex.LATEST_TTL_S + 1
    assert cl.books() == {} and len(s.calls) == n                               # books never call on their own


def test_funding_intervals_cover_every_symbol_and_feed_the_tick(clock):
    rows = dict(LATEST)
    rows[CIDS["BTCUSDC"]] = latest("BTCUSDC", iv="60")                           # changed on the fly
    del rows[CIDS["PAXGUSDC"]]
    s = _S(routes(latest_rows=rows))
    cl = client(s, stub_stream([tick_row("BTCUSDC", "1", "1.001", nft=FT + 2 * H_MS)], rx=T0))
    iv = cl.funding_intervals()
    assert set(iv) == UNIVERSE and (iv["BTCUSDC"], iv["PAXGUSDC"], iv["SLOWUSDC"]) == (1, 4, 8)
    assert venues.funding_intervals(cl) == iv
    assert s.paths().count(edgex.PATH_LATEST) == 1                               # the batch is reused within a minute
    p = cl.premium()["BTCUSDC"]
    assert (p["interval_h"], p["next_ms"]) == (1, FT + 2 * H_MS)


# --- history ---------------------------------------------------------------------------------------------------------------
def test_history_window_pages_newest_first_and_keeps_settled_rows(clock):
    full = [FT - k * 4 * H_MS for k in range(250)][::-1]
    rows = [hrow("BTCUSDC", ms, "-0.00005532" if ms == FT else "0.00005") for ms in full]
    rows.append(hrow("BTCUSDC", FT - 60_000, "-0.0000522", settle=False))        # a per-minute row that slipped through
    s = _S(routes(hist=hist_server(rows)))
    cl = client(s)
    out = cl.history_since("BTCUSDC", full[0], FT)
    assert [r["funding_ms"] for r in out] == full
    assert out[-1] == dict(exchange="edgex", symbol="BTCUSDC", funding_ms=FT, rate=-0.00005532, mark=77082.75)
    hc = [c[1] for c in s.calls if c[0] == edgex.PATH_HIST]
    assert hc[0] == dict(contractId="30000001", size=200, filterSettlementFundingRate="true",
                         filterBeginTimeInclusive=full[0], filterEndTimeExclusive=FT + 1)
    assert len(hc) == 2 and hc[1]["offsetData"] == "200"
    out = cl.history_since("BTCUSDC", FT - 30 * 86400_000, FT)                  # 30 days: one call
    assert len(out) == 181 and len([c for c in s.calls if c[0] == edgex.PATH_HIST]) == 3
    assert cl.history_since("BTCUSDC", FT + 1, FT + H_MS) == []                  # repair before the next settlement
    n = len(s.calls)
    assert cl.history_since("BTCUSDC", FT, FT - 1) == [] and len(s.calls) == n


def test_history_learns_ids_once_per_host_and_refreshes_for_unknown(clock):
    s = _S(routes(hist=lambda p: _R(env({"dataList": [], "nextPageOffsetData": ""}))))
    client(s).history_since("BTCUSDC", 0, 1000)
    b = client(s)                                                               # the collector's background copy
    b.history_since("1000PEPEUSDC", 0, 1000)
    assert s.paths() == [edgex.PATH_META, edgex.PATH_LABELS, edgex.PATH_HIST, edgex.PATH_HIST]
    assert s.calls[3][1]["contractId"] == "30000046"
    with pytest.raises(RuntimeError, match="no market NOPE"):
        b.history_since("NOPE", 0, 1000)                                        # ids are fresh — no refetch
    assert len(s.calls) == 4
    clock[0] += edgex.IDS_TTL_S
    with pytest.raises(RuntimeError, match="no market NOPE"):
        b.history_since("NOPE", 0, 1000)
    assert s.paths()[4:] == [edgex.PATH_META, edgex.PATH_LABELS]


def test_history_raises_when_pages_never_reach_start(clock, monkeypatch):
    monkeypatch.setattr(edgex, "HISTORY_MAX_PAGES", 3)
    s = _S(routes(hist=lambda p: _R(env({"dataList": [hrow("BTCUSDC", FT, "0.00005")], "nextPageOffsetData": "more"}))))
    with pytest.raises(RuntimeError, match="did not reach"):
        client(s).history_since("BTCUSDC", FT - 86400_000, FT)
    assert s.paths().count(edgex.PATH_HIST) == 3


def test_recent_history_takes_only_the_newest_settlement_and_keeps_cursors_honest(clock, tmp_path):
    cl = client(_S(routes()))
    rh = cl.recent_history()
    # HAJIMI still shows 16:00, SLOW (8 h) settled at 16:00, SPCX never settled — left to the per-leg repair
    assert {r["symbol"] for r in rh} == UNIVERSE - {"哈基米USDC", "SLOWUSDC", "SPCXUSDC"}
    assert next(r for r in rh if r["symbol"] == "BTCUSDC") == dict(exchange="edgex", symbol="BTCUSDC", funding_ms=FT,
                                                                    rate=-0.00005532, mark=None, prev_ms=FT - 4 * H_MS)
    con = db.connect(tmp_path / "e.db")
    floor = FT - 30 * 86400_000
    db.set_leg_depth(con, "edgex", "BTCUSDC", floor, synced_to=FT + 60_000)               # already past 20:00
    db.set_leg_depth(con, "edgex", "1000PEPEUSDC", floor, synced_to=FT - 4 * H_MS + 60_000)
    db.set_leg_depth(con, "edgex", "哈基米USDC", floor, synced_to=FT - 4 * H_MS + 60_000)
    db.set_leg_depth(con, "edgex", "AAPLUSDC", floor, synced_to=FT - 8 * H_MS + 60_000)   # missed 16:00
    db.set_leg_depth(con, "edgex", "XAUUSDC", floor, synced_to=FT - 4 * H_MS + 60_000)
    ivs = {i["symbol"]: i["interval_h"] for i in cl.perp_instruments()}
    ivs["XAUUSDC"] = 1                                                          # the collector already saw 4 h → 1 h
    t0 = int(clock[0] * 1000)
    funding.apply_batch(con, rh, ivs, t0)
    sync = db.leg_sync(con)
    assert sync[("edgex", "BTCUSDC")] == t0 - config.SETTLE_GRACE_S * 1000
    # review 13.09: the cursor covered 16:00, nothing settles before 20:00, the 20:00 row is in the batch → through it
    assert sync[("edgex", "1000PEPEUSDC")] == t0 - config.SETTLE_GRACE_S * 1000
    assert sync[("edgex", "AAPLUSDC")] == FT - 8 * H_MS + 60_000               # 16:00 never stored → per-leg repair
    assert sync[("edgex", "XAUUSDC")] == FT - 4 * H_MS + 60_000                # a shorter interval is stricter → repair
    assert sync[("edgex", "哈基米USDC")] == FT - 4 * H_MS + 60_000             # not claimed past the missing 20:00
    assert db.funding_span(con, "edgex", "BTCUSDC") == (FT, FT)
    # why the filter: with HAJIMI's older row in the batch, apply_batch would open the window at 16:01 and claim 20:00
    stale = dict(exchange="edgex", symbol="哈基米USDC", funding_ms=FT - 4 * H_MS, rate=4.455e-05, mark=None)
    funding.apply_batch(con, rh + [stale], ivs, t0)
    assert db.leg_sync(con)[("edgex", "哈基米USDC")] > FT
    clock[0] = (NEXT / 1000) + config.SETTLE_GRACE_S + 1                        # 00:00 overdue, batch still at 20:00
    assert cl.recent_history() == []


# --- transport ---------------------------------------------------------------------------------------------------------------
def test_throttle_pauses_the_host_and_errors_are_classified(clock):
    s = _S(lambda path, p: _R({"code": "RATE_LIMIT_EXCEEDED", "data": None}, 429, {"Retry-After": "7"}))
    cl = client(s)
    with pytest.raises(BannedError):
        cl.perp_instruments()
    assert cl.banned_until == pytest.approx(T0 + 7) and cl.n_429 == 1 and cl.health()["banned_until"] == int(T0 + 7)
    other = client(_S(routes()))                                                # same host → same pause, no request
    with pytest.raises(BannedError):
        other.perp_instruments()
    assert other._s.calls == [] and len(s.calls) == 1
    clock[0] += 8
    with pytest.raises(BannedError):
        client(_S(lambda path, p: _R({}, 403))).perp_instruments()              # CDN refusal: default 60 s
    assert cl.banned_until == pytest.approx(clock[0] + 60)
    clock[0] += 61
    with pytest.raises(BannedError):
        client(_S(lambda path, p: _R({"code": "RATE_LIMIT_EXCEEDED", "data": None}))).perp_instruments()   # in a 200
    clock[0] += 61
    with pytest.raises(PermanentHTTPError, match="GATEWAY_PARAM_REQUIRED"):
        client(_S(lambda path, p: _R({"code": "GATEWAY_PARAM_REQUIRED", "msg": "contractId"}, 400))).perp_instruments()
    s5 = _S(lambda path, p: _R({"code": "GATEWAY_INTERNAL_ERROR"}, 500))
    c5 = client(s5)
    with pytest.raises(RuntimeError, match="500"):
        c5.perp_instruments()
    assert len(s5.calls) == 3 and c5.n_err == 3
    with pytest.raises(RuntimeError, match="without contracts"):
        client(_S(lambda path, p: _R(env({"coinList": COINS, "contractList": []})))).perp_instruments()


def test_history_is_paced_and_the_tick_refuses_a_full_window(clock):
    s = _S(routes(hist=lambda p: _R(env({"dataList": [], "nextPageOffsetData": ""}))))
    cl = client(s, gap=0.3)
    cl.perp_instruments()
    t = []
    for _ in range(3):
        cl.history_since("BTCUSDC", 0, 1000)
        t.append(clock[0])
    assert t[1] - t[0] == pytest.approx(0.3) and t[2] - t[1] == pytest.approx(0.3)
    h = edgex._host()
    h.calls.extend([clock[0]] * (edgex.TICK_CAP - len(h.calls)))
    n = len(s.calls)
    with pytest.raises(BudgetExceeded):
        cl.premium()                                                            # stream stale → batch → refused now
    assert len(s.calls) == n and cl.health()["used_weight"] == edgex.TICK_CAP
    t1 = clock[0]
    cl.history_since("BTCUSDC", 0, 1000)                                        # history waits for the window
    assert clock[0] - t1 >= 59 and len(s.calls) == n + 1


# --- stream -------------------------------------------------------------------------------------------------------------------
class _Conn:
    def __init__(self, script, clock):
        self.script, self.clock, self.sent, self.closed = list(script), clock, [], False

    def send_text(self, s):
        self.sent.append(json.loads(s))

    def recv(self):
        if not self.script:
            self.clock[0] += 5
            raise socket.timeout()
        item = self.script.pop(0)
        if isinstance(item, (int, float)):
            self.clock[0] += item
            raise socket.timeout()
        if item == "close":
            return 8, b""
        return 1, json.dumps(item).encode()

    def close(self):
        self.closed = True


def test_stream_answers_pings_merges_and_reconnects_after_silence_and_close(clock):
    conns = [_Conn([{"sid": "5bceabbe", "type": "connected"},
                    {"type": "subscribed", "channel": edgex.CHANNEL},
                    frame([tick_row("BTCUSDC", "77179.2", "77179.4"), tick_row("1000PEPEUSDC", "0.0034123", "0.0034130")]),
                    {"type": "ping", "time": "1789249100000"},
                    frame([{"contractId": CIDS["BTCUSDC"], "bestBidPrice": "77179.3"}], "changed"),
                    31], clock),                                                  # silence past WS_SILENT_S
             _Conn([frame([tick_row("1000PEPEUSDC", "0.0034", "0.0035")]), "close"], clock)]
    made = []

    def connect(url, timeout):
        made.append(url)
        return conns[len(made) - 1]
    st = edgex._Stream(edgex.WS_URL, "edgex", connect)
    st.backoff0 = 0.0
    st._run(max_sessions=1)
    data, _last = st.snapshot()
    assert (data[CIDS["BTCUSDC"]]["bestBidPrice"], data[CIDS["BTCUSDC"]]["bestAskPrice"]) == ("77179.3", "77179.4")
    assert conns[0].sent == [{"type": "subscribe", "channel": "ticker.all.1s"}, {"type": "pong", "time": "1789249100000"}]
    assert st.n_ping == 1 and "no ticker data" in st.err and conns[0].closed
    st._run(max_sessions=1)
    assert made == [edgex.WS_URL] * 2 and st.n_conn == 2 and conns[1].closed
    assert conns[1].sent == [{"type": "subscribe", "channel": "ticker.all.1s"}]
    data, _last = st.snapshot()
    assert set(data) == {CIDS["1000PEPEUSDC"]}                                  # a snapshot replaces the cache
    assert "closed by server" in st.err and not st.synced.is_set()


def test_interface_and_no_network_in_the_constructor(monkeypatch):
    def no_net(*a, **k):
        raise AssertionError("network in the constructor")
    monkeypatch.setattr(requests.Session, "request", no_net)
    cl = EdgexPerp()
    assert venues.native(cl) and not venues.spot_native(cl) and cl.name == "edgex" and cl._stream is None
    assert edgex.PERP_CLIENTS == {"edgex": EdgexPerp} and not hasattr(cl, "index_legs")
    assert cl.history_gap_s == edgex.HISTORY_GAP_S and cl.health()["exchange"] == "edgex"


# --- collector ------------------------------------------------------------------------------------------------------------------
def test_collector_pairs_edgex_per_interval_with_history_and_upkeep(tmp_path, monkeypatch):
    from fakes import make_world
    from funding_bot.collector import Collector
    if "edgex" not in config.PERP_VENUES:                                        # before the integrator wires it
        monkeypatch.setattr(config, "PERP_VENUES", config.PERP_VENUES + ("edgex",))
    monkeypatch.setitem(config.FEES_TAKER, "edgex", config.FEES_TAKER.get("edgex", 0.00045))
    now_ms = int(time.time() * 1000)
    step = 4 * H_MS
    last = now_ms // step * step
    rows = [hrow("1000PEPEUSDC", last - k * step, "0.00008", mark="0.01") for k in range(200)]
    lat = {CIDS[n]: latest(n, "0.00008", "0.00008", ft=last, fts=now_ms // 60000 * 60000, mark="0.01") for n in UNIVERSE}
    s = _S(routes(latest_rows=lat, hist=hist_server(rows)))
    st = stub_stream([tick_row("1000PEPEUSDC", "0.0099", "0.0101", fr="0.00008", mark="0.01", index="0.01", ft=last,
                               nft=last + step)], rx=time.time())
    w = make_world()
    w["edgex"] = client(s, st)
    t = [time.time()]
    col = Collector(clients=w, db_path=tmp_path / "c.db", table_path=tmp_path / "t.json", now=lambda: t[0],
                    sleep=lambda x: None, background=False)
    tbl = col.once()
    assert col.venues[-1] == "edgex"
    ff = {r["key"]: r for r in tbl["ff_rows"]}
    k = "aster:1000PEPEUSDT|edgex:1000PEPEUSDC"
    assert abs(ff[k]["spread_h"] - (0.0001 / 4 - 0.00008 / 4)) < 1e-15            # both per 4 h → per hour
    assert ff[k]["urls"][1] == "https://pro.edgex.exchange/en-US/perpetuals/1000PEPEUSDC"
    assert {"binance:1000PEPEUSDT|edgex:1000PEPEUSDC", "hyperliquid:kPEPE|edgex:1000PEPEUSDC"} <= set(ff)
    assert "binance_spot:PEPEUSDT|edgex:1000PEPEUSDC" in {r["key"] for r in tbl["sf_rows"]}
    assert ("edgex", "1000PEPEUSDC") in db.leg_depths(col.con)
    assert db.funding_span(col.con, "edgex", "1000PEPEUSDC")[1] == last
