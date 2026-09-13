"""Bitget USDT-M perps (bitget_fut.BitgetFut) on a fake HTTP session — shapes recorded 12.09 from api.bitget.com, no
network. Covered: universe filters, asset classes, unit factor from the name, tickers + current-fund-rate join, one
tickers call per tick, next settlement roll-over, dynamic interval 8 h → 1 h, newest-first history paging (pageSize cap,
empty page / 40808 end, 40034, shifted pages), weekend zero rates, 429 pause, index legs in the Resolver's shapes."""
import json, time, types
import pytest
from funding_bot import config, identity, identity_src, universe, venues
from funding_bot import bitget_fut
from funding_bot.bitget_fut import BitgetFut, asset_class
from funding_bot.client import BannedError, PermanentHTTPError

H = 3600_000
NOW = 1789225200.0                       # 12.09.2026 15:00 UTC
NOW_MS = int(NOW * 1000)
T16 = NOW_MS + H                         # next 8 h / 4 h settlement (16:00 UTC)


class _R:
    def __init__(self, body, code=200, headers=None):
        self.status_code, self._b = code, body
        self.headers = {"x-mbx-used-remain-limit": "19", **(headers or {})}
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self): return self._b

    def raise_for_status(self):
        if self.status_code >= 500:
            raise RuntimeError(str(self.status_code))


def ok(data):
    return _R({"code": "00000", "msg": "success", "requestTime": NOW_MS, "data": data})


def err(code, status=400):
    return _R({"code": code, "msg": "error", "requestTime": NOW_MS, "data": None}, status)


class _S:
    def __init__(self, routes):
        self.routes, self.calls, self.headers = routes, [], {}

    def get(self, url, params=None, timeout=None):
        p = dict(params or {})
        self.calls.append((url, p))
        for k, v in self.routes.items():
            if url.endswith(k):
                return v(p) if callable(v) else v
        raise AssertionError(url)

    def n(self, path):
        return sum(1 for u, _ in self.calls if u.endswith(path))


def ins(sym, coin=None, st="crypto", rwa="NO", iv="8", off="-1", status="online", tick="0.001", step="1", mn="5",
        launch="0", quote="USDT"):
    return dict(symbol=sym, baseCoin=coin or sym[:-len(quote)], quoteCoin=quote, symbolType=st, isRwa=rwa,
                fundInterval=iv, offTime=off, limitOpenTime=off, status=status, type="perpetual", priceMultiplier=tick,
                quantityMultiplier=step, minOrderAmount=mn, launchTime=launch, takerFeeRate="0.0006")


def tk(sym, rate="0.0001", mark="1", index="1", bid="0.99", ask="1.01", bsz="5", asz="6"):
    return dict(symbol=sym, fundingRate=rate, markPrice=mark, indexPrice=index, bidPr=bid, askPr=ask, bidSz=bsz,
                askSz=asz, ts=str(NOW_MS - 100))


def fr(sym, rate="0.0001", iv="8", nxt=T16, cap="0.003", floor="-0.003"):
    return dict(symbol=sym, fundingRate=rate, fundingRateInterval=iv, nextUpdate=str(nxt), minFundingRate=floor,
                maxFundingRate=cap)


def _clock(monkeypatch, start=NOW):
    clock = [start]

    def sleep(s):
        clock[0] += s
    monkeypatch.setattr("funding_bot.bitget_fut.time",
                        types.SimpleNamespace(time=lambda: clock[0], sleep=sleep, gmtime=time.gmtime, strftime=time.strftime))
    return clock


# --- universe -------------------------------------------------------------------------------------------------------
UNI = [ins("BTCUSDT", tick="0.1", step="0.0001"), ins("1000BONKUSDT", iv="4"), ins("PEPEUSDT", step="1000"),
       ins("1MBABYDOGEUSDT"), ins("IOSTUSDT", iv="8"),
       ins("PKXUSDT", st="stock", rwa="YES", off="1789725600000"),          # delisting scheduled, still «online»
       ins("HALTUSDT", status="maintain"), ins("FUTUSDT", launch="4102444800000"), ins("ETHUSDC", quote="USDC"),
       ins("NVDAUSDT", st="stock", rwa="YES"), ins("WENUSDT", st="stock", rwa="YES"),       # Wendy's, not the coin WEN
       ins("SP500USDT", st="stock", rwa="YES"), ins("EURUSDUSDT", st="crypto", rwa="YES"),
       ins("B200USDT", st="crypto", rwa="YES"), ins("BHPUSDT", st="crypto", rwa="YES"),
       ins("XAUUSDT", st="metal", rwa="YES"), ins("PAXGUSDT", st="metal", rwa="YES"), ins("CLUSDT", st="commodity", rwa="YES"),
       ins("OPENAIUSDT", st="crypto", rwa="YES"), ins("QNTSTOCKUSDT", st="stock", rwa="YES"),
       ins("TENCENTHKDUSDT", st="stock", rwa="YES"), ins("龙虾USDT"), ins("NEWRWAUSDT", st="crypto", rwa="YES")]
FUND = [fr(i["symbol"], iv=i["fundInterval"]) for i in UNI if i["symbol"] not in ("PEPEUSDT", "IOSTUSDT")] + \
       [fr("IOSTUSDT", iv="1", nxt=NOW_MS + H), fr("RWATESTMEUSDT", cap=None, floor=None)]   # test symbol: not listed


def _client(routes=None, **kw):
    r = {"/api/v3/market/instruments": ok(UNI), "/api/v2/mix/market/current-fund-rate": ok(FUND),
         "/api/v2/mix/market/tickers": ok([tk(i["symbol"]) for i in UNI])}
    r.update(routes or {})
    s = _S(r)
    kw.setdefault("history_gap_s", 0)
    kw.setdefault("legs_gap_s", 0)
    return BitgetFut(session=s, **kw), s


def test_universe_filters_classes_units_and_intervals():
    cl, _s = _client()
    got = {i["symbol"]: i for i in cl.perp_instruments()}
    assert "PKXUSDT" not in got and "HALTUSDT" not in got and "FUTUSDT" not in got and "ETHUSDC" not in got
    assert {s: i["cls"] for s, i in got.items() if i["cls"] != "crypto"} == {
        "NVDAUSDT": "equity", "WENUSDT": "equity", "SP500USDT": "index", "EURUSDUSDT": "fx", "B200USDT": "index",
        "BHPUSDT": "equity", "XAUUSDT": "commodity", "CLUSDT": "commodity", "OPENAIUSDT": "preipo",
        "QNTSTOCKUSDT": "equity", "TENCENTHKDUSDT": "equity", "NEWRWAUSDT": "rwa"}      # PAXG is a coin (repo rule)
    # the unit comes from the NAME: PEPE's qty step 1000 is not a factor; 1000BONK / 1MBABYDOGE are
    unit = {s: (got[s]["base"], got[s]["factor"]) for s in ("PEPEUSDT", "1000BONKUSDT", "1MBABYDOGEUSDT", "BTCUSDT")}
    assert unit == {"PEPEUSDT": ("PEPE", 1.0), "1000BONKUSDT": ("BONK", 1000.0), "1MBABYDOGEUSDT": ("BABYDOGE", 1e6),
                    "BTCUSDT": ("BTC", 1.0)} and got["PEPEUSDT"]["step_size"] == 1000.0
    # Bitget's share names → the exchange ticker; HKD lines stay a different unit
    assert got["QNTSTOCKUSDT"]["base"] == "QNT" and got["TENCENTHKDUSDT"]["base"] == "TENCENTHKD"
    b = got["BTCUSDT"]
    assert (b["tick_size"], b["step_size"], b["min_notional"], b["cap"], b["floor"], b["quote"], b["interval_h"]) == \
        (0.1, 0.0001, 5.0, 0.003, -0.003, "USDT", 8)
    assert got["1000BONKUSDT"]["interval_h"] == 4
    assert got["IOSTUSDT"]["interval_h"] == 1                    # the funding endpoint's live interval beats fundInterval
    assert got["PEPEUSDT"]["interval_h"] == 8 and got["PEPEUSDT"]["cap"] is None      # absent from it: v3, no cap
    assert got["龙虾USDT"]["base"] == "龙虾" and got["龙虾USDT"]["url"].endswith("/%E9%BE%99%E8%99%BEUSDT")


def test_asset_class_needs_rwa_for_overrides():
    assert asset_class("OPENAI", "crypto", "NO") == "crypto"     # a meme coin called OPENAI stays a coin
    assert asset_class("XAUT", "metal", "YES") == "crypto" and asset_class("COPPER", "metal", "YES") == "commodity"
    assert asset_class("KO", "stock", "YES") == "equity" and asset_class("USDJPY", "crypto", "YES") == "fx"


def test_universe_pairs_inside_class_only(monkeypatch):
    monkeypatch.setattr(config, "PERP_VENUES", ("aster", "binance", "hyperliquid", "bitget"))
    cl, _s = _client()
    bg = cl.perp_instruments()
    bn = [dict(exchange="binance", symbol=s, base=b, factor=f, cls="crypto") for s, b, f in
          (("BTCUSDT", "BTC", 1.0), ("1000BONKUSDT", "BONK", 1000.0), ("WENUSDT", "WEN", 1.0), ("QNTUSDT", "QNT", 1.0))]
    ff = {r["key"]: r for r in universe.build_ff({"binance": bn, "bitget": bg})}
    assert set(ff) == {"binance:BTCUSDT|bitget:BTCUSDT", "binance:1000BONKUSDT|bitget:1000BONKUSDT"}   # WEN, QNT: other class
    assert (ff["binance:1000BONKUSDT|bitget:1000BONKUSDT"]["fa"], ff["binance:1000BONKUSDT|bitget:1000BONKUSDT"]["fb"]) == (1000.0, 1000.0)
    spots = {"gate_spot": [dict(exchange="gate_spot", symbol="QNTX_USDT", base="~QNTX", factor=1.0, alt_base="QNT", base_asset="QNTX"),
                           dict(exchange="gate_spot", symbol="QNT_USDT", base="QNT", factor=1.0, base_asset="QNT")]}
    sf = {r["key"] for r in universe.build_sf({"binance": bn, "bitget": bg}, spots)}
    assert {"gate_spot:QNTX_USDT|bitget:QNTSTOCKUSDT", "gate_spot:QNT_USDT|binance:QNTUSDT"} <= sf
    assert "gate_spot:QNT_USDT|bitget:QNTSTOCKUSDT" not in sf and "gate_spot:QNTX_USDT|binance:QNTUSDT" not in sf


# --- tick -------------------------------------------------------------------------------------------------------------
def test_premium_and_books_share_one_tickers_call(monkeypatch):
    clock = _clock(monkeypatch)
    ticks = [tk("BTCUSDT", rate="0.000074", mark="77390", index="77422.5", bid="77389.9", ask="77390"),
             tk("IOSTUSDT", rate="-0.001431", bid="0.0009", ask="0.0008"),                 # crossed book
             tk("PKXUSDT", rate="0.0005")]                                                 # outside the universe
    fund = [fr("BTCUSDT", rate="0.000999"), fr("IOSTUSDT", iv="1", nxt=NOW_MS + H), fr("RWATESTMEUSDT")]
    cl, s = _client({"/api/v2/mix/market/tickers": ok(ticks), "/api/v2/mix/market/current-fund-rate": ok(fund),
                     "/api/v3/market/instruments": ok([ins("BTCUSDT"), ins("IOSTUSDT")])})
    cl.perp_instruments()
    p = cl.premium()
    b = cl.books()
    assert set(p) == {"BTCUSDT", "IOSTUSDT"} and set(b) == {"BTCUSDT"}         # PKX not listed; crossed IOST book dropped
    assert p["BTCUSDT"] == dict(rate=0.000074, mark=77390.0, index=77422.5, next_ms=T16, ts_ms=NOW_MS - 100, obs=NOW,
                                interval_h=8)
    assert p["IOSTUSDT"]["next_ms"] == NOW_MS + H                              # the rate from tickers, time from funding
    assert p["IOSTUSDT"]["interval_h"] == 1                                    # live interval rides with every tick
    assert b["BTCUSDT"] == dict(bid=77389.9, ask=77390.0, bid_qty=5.0, ask_qty=6.0, obs=NOW)
    assert s.n("/tickers") == 1                                                # books() reused the snapshot
    clock[0] += config.TICK_S
    assert cl.premium()["BTCUSDT"]["obs"] == NOW + config.TICK_S and s.n("/tickers") == 2


def test_funding_failure_keeps_rates_and_rolls_next_settlement(monkeypatch):
    clock = _clock(monkeypatch)
    state = {"fund_ok": True}
    cl, s = _client({"/api/v2/mix/market/current-fund-rate":
                     lambda p: ok([fr("BTCUSDT")]) if state["fund_ok"] else _R({"code": "50001"}, 502),
                     "/api/v3/market/instruments": ok([ins("BTCUSDT")]),
                     "/api/v2/mix/market/tickers": ok([tk("BTCUSDT")])})
    cl.perp_instruments()
    assert cl.premium()["BTCUSDT"]["next_ms"] == T16
    state["fund_ok"] = False
    clock[0] += 2 * 3600                                                       # 17:00, past the 16:00 settlement
    p = cl.premium()
    assert p["BTCUSDT"]["rate"] == 0.0001 and p["BTCUSDT"]["next_ms"] == T16 + 8 * H     # rolled on the 8 h grid
    # no funding snapshot at all and no universe yet: next settlement from the UTC grid, rows for every USDT ticker
    cl2, _ = _client({"/api/v2/mix/market/current-fund-rate": _R({"code": "50001"}, 502),
                      "/api/v2/mix/market/tickers": ok([tk("BTCUSDT"), tk("ETHUSDC")])})
    p2 = cl2.premium()
    assert set(p2) == {"BTCUSDT"} and p2["BTCUSDT"]["next_ms"] == (int(clock[0] * 1000) // (8 * H) + 1) * 8 * H


def test_interval_switch_8h_to_1h_is_seen_without_a_universe_rebuild(monkeypatch):
    _clock(monkeypatch)
    fund = {"rows": [fr("IOSTUSDT", iv="8"), fr("ARBUSDT", iv="4")]}
    cl, _s = _client({"/api/v2/mix/market/current-fund-rate": lambda p: ok(fund["rows"]),
                      "/api/v3/market/instruments": ok([ins("IOSTUSDT"), ins("ARBUSDT", iv="4")]),
                      "/api/v2/mix/market/tickers": ok([tk("IOSTUSDT"), tk("ARBUSDT")])})
    assert {i["symbol"]: i["interval_h"] for i in cl.perp_instruments()} == {"IOSTUSDT": 8, "ARBUSDT": 4}
    fund["rows"] = [fr("IOSTUSDT", iv="1", nxt=NOW_MS + H)]                    # IOST switched (09-10.09); ARB missing
    # a symbol missing from the answer keeps its interval (the collector falls back to 8 for absent keys)
    assert cl.funding_intervals() == {"IOSTUSDT": 1, "ARBUSDT": 4}
    assert cl.premium()["IOSTUSDT"]["next_ms"] == NOW_MS + H
    assert {i["symbol"]: i["interval_h"] for i in cl.perp_instruments()}["IOSTUSDT"] == 1


def test_429_pauses_a_minute_and_sends_nothing_meanwhile():
    cl, s = _client({"/api/v2/mix/market/tickers": _R({"code": "429", "msg": "Too Many Requests"}, 429)})
    with pytest.raises(BannedError):
        cl.premium()
    assert cl.banned_until - time.time() > 55 and cl.health()["n_429"] == 1
    with pytest.raises(BannedError):
        cl.books()
    assert s.n("/tickers") == 1


def test_venue_dispatch_sees_a_native_perp_not_a_spot():
    cl, _s = _client()
    assert venues.native(cl) and not venues.spot_native(cl)
    assert "BTCUSDT" in venues.premium(cl) and "BTCUSDT" in venues.books(cl) and venues.recent_history(cl) == []


# --- history ----------------------------------------------------------------------------------------------------------
def _server(rows_desc: list[tuple[int, str]], calls: list | None = None, clock=None):
    """history-fund-rate: newest first, pageSize capped at 100, startTime/endTime ignored, past the end → []."""
    def h(p):
        if calls is not None:
            calls.append((dict(p), clock[0] if clock else None))
        size, n = min(100, int(p["pageSize"])), int(p["pageNo"])
        return ok([{"symbol": p["symbol"], "fundingRate": r, "fundingTime": str(ms)}
                   for ms, r in rows_desc[(n - 1) * size:n * size]])
    return h


def test_history_pages_backwards_to_start_and_returns_ascending(monkeypatch):
    clock = _clock(monkeypatch)
    end = NOW_MS
    rows = [(end - k * H, "0.0001") for k in range(40 * 24)]                  # 1 h symbol, 40 days deep
    calls = []
    cl, _s = _client({"/api/v2/mix/market/history-fund-rate": _server(rows, calls, clock)}, history_gap_s=0.2)
    got = cl.history_since("IOSTUSDT", end - 30 * 86400_000, end)
    ms = [r["funding_ms"] for r in got]
    assert len(got) == 30 * 24 + 1 and ms == sorted(ms) and ms[0] == end - 30 * 86400_000 and ms[-1] == end
    assert got[0] == dict(exchange="bitget", symbol="IOSTUSDT", funding_ms=ms[0], rate=0.0001, mark=None)
    assert [c["pageNo"] for c, _t in calls] == list(range(1, 9)) and {c["pageSize"] for c, _t in calls} == {100}
    assert all("startTime" not in c for c, _t in calls)                       # ignored by the venue — not sent
    assert all(b - a >= 0.2 - 1e-9 for (_c, a), (_d, b) in zip(calls, calls[1:]))   # paced
    # 8 h symbol, 30 days: one page of 100 covers 33 days
    calls.clear()
    rows8 = [(end - k * 8 * H, "0.0001") for k in range(200)]
    cl8, _ = _client({"/api/v2/mix/market/history-fund-rate": _server(rows8, calls)})
    assert len(cl8.history_since("BTCUSDT", end - 30 * 86400_000, end)) == 90 + 1 and len(calls) == 1
    # a short repair asks for a few rows, not a page of 100
    calls.clear()
    got = cl.history_since("IOSTUSDT", end - 2 * H, end)
    assert [r["funding_ms"] for r in got] == [end - 2 * H, end - H, end] and calls[-1][0]["pageSize"] <= 4


def test_history_settlement_between_pages_gives_no_gap_and_no_duplicate():
    end = NOW_MS
    rows = [(end - k * H, "0.0002") for k in range(300)]
    base = _server(rows)
    n = {"calls": 0}

    def h(p):
        n["calls"] += 1
        if n["calls"] == 2:
            rows.insert(0, (end + H, "0.0003"))                               # settled while paging: pages shift by one
        return base(p)
    cl, _s = _client({"/api/v2/mix/market/history-fund-rate": h})
    got = cl.history_since("X1USDT", end - 250 * H, end + 90 * 60_000)
    ms = [r["funding_ms"] for r in got]
    # page 2 repeats page 1's last row (dropped); the fresh row slid into page 1 after it was read — the next top-up
    # takes it (sync_leg keeps the cursor at now − grace, before that settlement)
    assert len(ms) == len(set(ms)) == 251 and all(b - a == H for a, b in zip(ms, ms[1:])) and ms[-1] == end


def test_history_end_of_data_empty_page_or_40808_and_unknown_symbol():
    end = NOW_MS
    rows = [(end - k * 8 * H, "0.0001") for k in range(200)]                 # exactly two full pages
    calls = []
    cl, _s = _client({"/api/v2/mix/market/history-fund-rate": _server(rows, calls)})
    assert len(cl.history_since("OLDUSDT", end - 90 * 86400_000, end)) == 200 and len(calls) == 3   # 3rd page: []

    def h40808(p):
        return err("40808") if int(p["pageNo"]) > 2 else _server(rows)(p)
    cl2, _ = _client({"/api/v2/mix/market/history-fund-rate": h40808})
    assert len(cl2.history_since("OLDUSDT", end - 90 * 86400_000, end)) == 200
    cl3, _ = _client({"/api/v2/mix/market/history-fund-rate": err("40034")})
    with pytest.raises(PermanentHTTPError, match="40034"):
        cl3.history_since("NOSUCHUSDT", end - H, end)


def test_history_that_cannot_reach_start_raises_instead_of_truncating(monkeypatch):
    monkeypatch.setattr(bitget_fut, "HISTORY_MAX_PAGES", 3)
    rows = [(NOW_MS - k * H, "0.0001") for k in range(40 * 24)]
    cl, _s = _client({"/api/v2/mix/market/history-fund-rate": _server(rows)})
    with pytest.raises(RuntimeError, match="did not reach"):
        cl.history_since("IOSTUSDT", NOW_MS - 30 * 86400_000, NOW_MS)


def test_weekend_zero_rates_are_rows_not_holes():
    """Stocks / metals: rate 0 while the market is closed (NVDA 179 of 200 rows, every XAU weekend row) — real rows."""
    rows = [(NOW_MS - k * 8 * H, "0" if k % 3 else "0.00005") for k in range(30)]
    cl, _s = _client({"/api/v2/mix/market/history-fund-rate": _server(rows)})
    got = cl.history_since("NVDAUSDT", NOW_MS - 29 * 8 * H, NOW_MS)
    assert len(got) == 30 and sum(r["rate"] == 0.0 for r in got) == 20


# --- index composition ---------------------------------------------------------------------------------------------------
def _comp(ex, pair, w="0.2"):
    return {"exchange": ex, "spotPair": pair, "equivalentPrice": "1", "weight": w}


INDEX = {"BTCUSDT": [_comp("BINANCE", "BTC/USDT", "0.3"), _comp("BITGET", "BTC/USDT", "0.3"), _comp("GATEIO", "BTC/USDT"),
                     _comp("KUCOIN", "BTC/USDT"), _comp("OKX", "BTC/USDT"), _comp("MASSIVE", "BTC/USD")],
         "1000BONKUSDT": [_comp("BINANCE", "1000BONK/USDT", "0.5"), _comp("KUCOIN", "1000BONK/USDT", "0.5")],
         "AIOUSDT": [_comp("BINANCE_ALPHA", "ALPHA_64/USDT", "0.6"), _comp("BITGET_CROSS", "AIO/USDT", "0.4")],
         "EMPTYUSDT": []}


def _index_route(p):
    if p["symbol"] not in INDEX:
        return err("40034")
    return ok({"symbol": p["symbol"], "componentList": INDEX[p["symbol"]]})


def test_index_legs_in_the_resolvers_shapes():
    alpha = _R({"code": "000000", "data": [{"alphaId": "ALPHA_64", "symbol": "AIO", "name": "AIO Token"}]})
    cl, s = _client({"/api/v3/market/index-components": _index_route, "/alpha/all/token/list": alpha})
    out = identity_src.index_legs(cl, ["BTCUSDT", "1000BONKUSDT", "AIOUSDT", "EMPTYUSDT", "NOSUCHUSDT"])   # hasattr dispatch
    sym = lambda k: [(x["exchange"], x["symbol"]) for x in out[k]["legs"]]
    assert sym("BTCUSDT") == [("binance", "BTCUSDT"), ("bitget", "BTCUSDT"), ("gateio", "BTC_USDT"), ("kucoin", "BTC-USDT"),
                              ("okx", "BTC_USDT"), ("massive", "BTC_USD")]
    assert sym("1000BONKUSDT") == [("binance", "BONKUSDT*1000"), ("kucoin", "BONK-USDT*1000")]   # the perp's unit → «*N»
    assert sym("AIOUSDT") == [("binance_alpha", "AIOUSDT"), ("bitget_cross", "AIO_USDT")]
    assert out["EMPTYUSDT"]["legs"] is None and out["NOSUCHUSDT"]["legs"] is None and out["BTCUSDT"]["dex"] == {}
    assert s.n("/alpha/all/token/list") == 1
    rec = lambda code: identity.coin_record(code, [("ETH", "0x" + code.encode().hex().ljust(40, "0")[:40], True, True)], code)
    spots = {"binance_spot": {"coins": {"BTC": rec("BTC"), "BONK": rec("BONK")}, "markets": {"BTCUSDT": ["BTC", True], "BONKUSDT": ["BONK", True]}},
             "gate_spot": {"coins": {"BTC": rec("BTC")}, "markets": {"BTC_USDT": ["BTC", True]}},
             "kucoin_spot": {"coins": {"BTC": rec("BTC"), "BONK": rec("BONK")}, "markets": {"BTC-USDT": ["BTC", True], "BONK-USDT": ["BONK", True]}},
             "bitget_spot": {"coins": {"BTC": rec("BTC")}, "markets": {"BTCUSDT": ["BTC", True]}}}
    R = identity.Resolver(spots, {"bitget": out})
    parsed = [R._parse(x) for x in out["BTCUSDT"]["legs"]]
    assert [(L["kind"], L.get("venue"), L.get("code")) for L in parsed] == [
        ("our", "binance_spot", "BTC"), ("our", "bitget_spot", "BTC"), ("our", "gate_spot", "BTC"),
        ("our", "kucoin_spot", "BTC"), ("cex", "okx", None), ("vendor", None, None)]
    bonk = [R._parse(x) for x in out["1000BONKUSDT"]["legs"]]
    assert [(L["kind"], L["venue"], L["code"], L["mult"]) for L in bonk] == [("our", "binance_spot", "BONK", 1000.0),
                                                                            ("our", "kucoin_spot", "BONK", 1000.0)]
    assert R._parse(out["AIOUSDT"]["legs"][0]) == dict(ex="binance_alpha", sym="AIOUSDT", w=0.6, kind="alpha", target="AIO")
    assert {it["venue"] for it in R.identity("bitget", "BTCUSDT")["items"]} == {"binance_spot", "bitget_spot", "gate_spot", "kucoin_spot"}


def test_index_legs_unknown_alpha_id_is_no_evidence_and_batch_survives_a_bad_symbol():
    def route(p):
        if p["symbol"] == "BADUSDT":
            return _R({"code": "50000"}, 502)
        return _index_route(p)
    cl, _s = _client({"/api/v3/market/index-components": route, "/alpha/all/token/list": _R({}, 503)})
    out = cl.index_legs(["BADUSDT", "AIOUSDT", "BTCUSDT"])
    assert "BADUSDT" not in out and set(out) == {"AIOUSDT", "BTCUSDT"}             # skipped, re-collected next job
    leg = out["AIOUSDT"]["legs"][0]
    assert (leg["exchange"], leg["symbol"]) == ("binance_alpha_id", "ALPHA_64")       # not a guess by the word «ALPHA»
    assert identity.Resolver({}, {})._parse(leg)["kind"] == "unknown_ex"
    cl2, _ = _client({"/api/v3/market/index-components": _R({"code": "429"}, 429)})
    with pytest.raises(BannedError):
        cl2.index_legs(["BTCUSDT"])
