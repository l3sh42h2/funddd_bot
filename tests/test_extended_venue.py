"""Extended on a fake HTTP session (no network). Response shapes are trimmed copies of the 12-13.09 live captures of
/info/markets and /info/{market}/funding (see the header of extended.py)."""
import json, logging, time, types
import pytest
from funding_bot import calc, config, extended, identity, universe, venues
from funding_bot.client import BannedError, BudgetExceeded, PermanentHTTPError
from funding_bot.extended import ExtendedPerp
from funding_bot.identity import Resolver, coin_record, decide_ff, decide_sf

H = 3600_000
NOW = 1789249652.0                     # 12.09.2026 21:47:32 UTC
NEXT = 1789250400000                   # 22:00Z — nextFundingRate of every perp at that moment [L]


class _R:
    def __init__(self, body, code=200, headers=None):
        self.status_code, self._b, self.headers = code, body, headers or {}
        self.text = json.dumps(body)

    def json(self): return self._b

    def raise_for_status(self):
        if self.status_code >= 500:
            raise RuntimeError(str(self.status_code))


class _S:
    def __init__(self, handler):
        self.handler, self.calls, self.headers = handler, [], {}

    def get(self, url, params=None, timeout=None):
        host, path = url.split("/api/v1", 1)
        self.calls.append((host, path, dict(params or {})))
        return self.handler(path, dict(params or {}))


@pytest.fixture(autouse=True)
def _fresh_hosts():
    extended._HOSTS.clear()
    yield
    extended._HOSTS.clear()


@pytest.fixture
def clock(monkeypatch):
    c = [NOW]

    def sleep(s):
        c[0] += max(0.0, s)
    monkeypatch.setattr(extended, "time", types.SimpleNamespace(time=lambda: c[0], sleep=sleep, gmtime=time.gmtime,
                                                                strftime=time.strftime))
    return c


# --- recorded shapes ---------------------------------------------------------------------------------------------------
def mk(name, ui=None, cat="Crypto", sub="L1", asset=None, desc=None, status="ACTIVE", typ="PERPETUAL", active=True,
       off=False, ref="null", hours="CONTINUOUS", rate="0.000013", mark="1.0", index="1.0", bid="0.999", ask="1.001",
       min_sz="1", step="1", tick="0.001", cap="1", created=1752829532673, vol="1000", visible=True, nxt=NEXT):
    return {"name": name, "type": typ, "uiName": ui or name, "category": cat, "subCategory": sub,
            "assetName": asset or name.rsplit("-", 1)[0], "assetPrecision": 2, "collateralAssetName": "USD",
            "collateralAssetPrecision": 6, "description": desc or name.rsplit("-", 1)[0], "active": active,
            "isRfq": True, "isOffHours": off, "status": status, "tradingHours": hours, "referenceMarket": ref,
            "visibleOnUi": visible, "createdAt": created,
            "marketStats": {"dailyVolume": vol, "lastPrice": mark, "askPrice": ask, "bidPrice": bid, "markPrice": mark,
                            "indexPrice": index, "fundingRate": rate, "nextFundingRate": nxt if typ == "PERPETUAL" else 0,
                            "openInterest": "1"},
            "tradingConfig": {"minOrderSize": min_sz, "minOrderSizeChange": step, "minPriceChange": tick,
                              "maxLeverage": "50.00", "hourlyFundingRateCap": cap},
            "l2Config": {"type": "STARKX"}}


MARKETS = [
    mk("BTC-USD", desc="Bitcoin", rate="0.000004", mark="77196.04", index="77231.31", bid="77191", ask="77192",
       min_sz="0.0001", step="0.00001", tick="1", cap="0.25", vol="103696017.14"),
    mk("ETH-USD", desc="Ethereum", rate="0.000013", mark="2523.61", index="2524.71", bid="2523.7", ask="2523.8",
       min_sz="0.01", step="0.001", tick="0.1"),
    mk("1000PEPE-USD", ui="kPEPE-USD", sub="Meme", desc="Pepe", rate="0.000001", mark="0.00340597", index="0.00340679",
       bid="0.003404", ask="0.003406", min_sz="1000", step="100", tick="0.000001", cap="0.75"),
    mk("kNOT-USD", sub="Infra", desc="Notcoin", mark="0.5"),
    mk("ONG-USD", desc="Ontology Gas", rate="-0.000497", cap="0.006"),                   # cap units are mixed [L]
    mk("SPX-USD", ui="SPX6900-USD", sub="Meme", desc="SPX6900"),                         # the coin
    mk("SPX500m-USD", ui="SPX-USD", cat="RWA", sub="ETF/Index", desc="S&P 500", ref="us_index_fut", off=True,
       rate="0.000004", mark="7682.978", index="7682.978", bid="7677.6", ask="7681.4"),  # off-hours: rate yes, book no
    mk("TECH100m-USD", ui="NDX-USD", cat="RWA", sub="ETF/Index", desc="Nasdaq-100", ref="us_index_fut"),
    mk("JP225-USD", cat="RWA", sub="ETF/Index", desc="Nikkei 225", ref="jp_equity"),
    mk("DRAM-USD", cat="RWA", sub="ETF/Index", desc="Roundhill Memory ETF", ref="us_equity"),
    mk("NVDA_24_5-USD", ui="NVDA-USD", cat="RWA", sub="Equity", desc="NVIDIA", ref="us_equity", off=True,
       mark="180.0", index="180.0", bid="174.6", ask="185.4"),                            # synthetic ±3 % band
    mk("PURR-USD", cat="RWA", sub="Equity", desc="Hyperliquid Strategies Inc.", ref="us_equity"),
    mk("STXX-USD", cat="RWA", sub="Equity", desc="Seagate Technology", ref="us_equity"),
    mk("SKHYNIX-USD", cat="RWA", sub="Equity", desc="SK Hynix", ref="kr_equity"),
    mk("SKHY-USD", cat="RWA", sub="Equity", desc="SK Hynix ADR", ref="us_equity"),
    mk("XNG-USD", ui="NATGAS-USD", cat="RWA", sub="Commodity", desc="Natural Gas", ref="commodity_cme"),
    mk("WTI-USD", cat="RWA", sub="Commodity", desc="WTI Crude Oil", ref="commodity_cme", rate="0.000134"),
    mk("XBR-USD", cat="RWA", sub="Commodity", desc="Brent Crude Oil", ref="commodity_cme"),
    mk("XCU-USD", cat="RWA", sub="Commodity", desc="Copper", ref="commodity_cme"),
    mk("XAU-USD", cat="RWA", sub="Commodity", desc="Gold", ref="commodity_cme"),
    mk("PAXG-USD", sub="Commodity", desc="PAX Gold"),                                      # gold token = coin
    mk("EUR-USD", cat="RWA", sub="FX", desc="Euro", ref="fx"),
    mk("USDJPY-USD", cat="RWA", sub="FX", desc="Japanese Yen", ref="fx"),
    mk("OPENAI-USD", cat="RWA", sub="Pre-market", desc="OpenAI (pre-IPO)", ref="always_open"),
    mk("BOND10-USD", cat="RWA", sub="TradFi", desc="Some bond"),                          # unmapped → «rwa»
    mk("WIDE-USD", bid="1.0", ask="1.2"),                                                  # 18 % spread: no book
    mk("NOBID-USD", bid="0", ask="1.0"),
    # not tradable
    mk("BTCSPOT-USD", ui="wBTC/USDC", typ="SPOT", desc="Wrapped Bitcoin"),
    mk("NFLX_24_5-USD", ui="NFLX-USD", cat="RWA", sub="Equity", status="PRELISTED", visible=False, bid="97", ask="103"),
    mk("FTM-USD", ui="NA16-USD", status="DELISTED", visible=False),
    mk("MKR-USD", sub="DeFi", desc="MKR", status="REDUCE_ONLY", visible=False, bid="0"),
    mk("GONE-USD", status="DELISTED", active=False, visible=False),
]


def routes(markets=MARKETS, funding=None, headers=None):
    def h(path, p):
        if path == "/info/markets":
            return _R({"status": "OK", "data": list(markets)}, headers=headers)
        if path.startswith("/info/") and path.endswith("/funding") and funding is not None:
            if "startTime" not in p:
                return _R({"status": "ERROR", "error": {"code": 1006, "message": "Request parameter startTime is missing"}}, 400)
            return funding(path.split("/")[2], p)
        raise AssertionError(path)
    return h


# --- universe --------------------------------------------------------------------------------------------------------------
def test_instruments_filters_classes_bases_and_links():
    ins = {i["symbol"]: i for i in ExtendedPerp(session=_S(routes())).perp_instruments()}
    # SPOT, PRELISTED, DELISTED (active or not) and REDUCE_ONLY markets cannot be opened — not in the universe
    assert not {"BTCSPOT-USD", "NFLX_24_5-USD", "FTM-USD", "MKR-USD", "GONE-USD"} & set(ins)
    got = {s: (i["cls"], i["base"], i["factor"]) for s, i in ins.items()}
    assert got == {"BTC-USD": ("crypto", "BTC", 1.0), "ETH-USD": ("crypto", "ETH", 1.0),
                   "1000PEPE-USD": ("crypto", "PEPE", 1000.0), "kNOT-USD": ("crypto", "NOT", 1000.0),
                   "ONG-USD": ("crypto", "ONG", 1.0), "SPX-USD": ("crypto", "SPX", 1.0),
                   "SPX500m-USD": ("index", "SP500", 1.0),                 # S&P 500, as Lighter US500 / HL xyz:SP500
                   "TECH100m-USD": ("index", "TECH100M", 1.0), "JP225-USD": ("index", "JP225", 1.0),   # not mapped
                   "DRAM-USD": ("equity", "DRAM", 1.0),                   # ETF share (referenceMarket us_equity)
                   "NVDA_24_5-USD": ("equity", "NVDA", 1.0),              # the 24/5 twin is the NVDA share
                   "PURR-USD": ("equity", "PURR", 1.0),                   # Hyperliquid Strategies Inc., not the coin
                   "STXX-USD": ("equity", "STX", 1.0),                    # config.PERP_CANON
                   "SKHYNIX-USD": ("equity", "SKHYNIX", 1.0), "SKHY-USD": ("equity", "SKHY", 1.0),   # ADR kept apart
                   "XNG-USD": ("commodity", "NATGAS", 1.0), "WTI-USD": ("commodity", "CL", 1.0),
                   "XBR-USD": ("commodity", "BZ", 1.0), "XCU-USD": ("commodity", "COPPER", 1.0),
                   "XAU-USD": ("commodity", "XAU", 1.0), "PAXG-USD": ("crypto", "PAXG", 1.0),
                   "EUR-USD": ("fx", "EUR", 1.0), "USDJPY-USD": ("fx", "JPY", 1.0),
                   "OPENAI-USD": ("preipo", "OPENAI", 1.0), "BOND10-USD": ("rwa", "BOND10", 1.0),
                   "WIDE-USD": ("crypto", "WIDE", 1.0), "NOBID-USD": ("crypto", "NOBID", 1.0)}
    b = ins["BTC-USD"]
    assert (b["exchange"], b["interval_h"], b["quote"], b["contract"], b["cap"], b["floor"]) == \
        ("extended", 1, "USD", "PERPETUAL", None, None)
    assert (b["tick_size"], b["step_size"], b["onboard_ms"], b["base_asset"]) == (1.0, 1e-5, 1752829532673, "BTC")
    assert b["min_notional"] == pytest.approx(0.0001 * 77196.04)            # contract units × mark
    assert ins["1000PEPE-USD"]["min_notional"] == pytest.approx(1000 * 0.00340597)
    assert ins["ONG-USD"]["cap"] is None                                      # «0.006» is not a cap in any unit
    # the app routes by uiName: /trade/1000PEPE-USD or /trade/NVDA_24_5-USD would silently show BTC-USD [L]
    assert ins["1000PEPE-USD"]["url"] == "https://app.extended.exchange/trade/kPEPE-USD"
    assert ins["NVDA_24_5-USD"]["url"] == "https://app.extended.exchange/trade/NVDA-USD"
    assert ins["SPX500m-USD"]["url"] == "https://app.extended.exchange/trade/SPX-USD"
    assert ins["SPX-USD"]["url"] == "https://app.extended.exchange/trade/SPX6900-USD"
    assert ins["XNG-USD"]["url"] == "https://app.extended.exchange/trade/NATGAS-USD"
    assert (ins["BTC-USD"]["name_hint"], ins["PURR-USD"]["name_hint"], ins["SPX-USD"]["name_hint"]) == \
        ("Bitcoin", "Hyperliquid Strategies Inc.", "SPX6900")
    assert "volume" not in b


def test_stock_and_its_24_5_twin_keep_the_busier_market(caplog):
    mkts = [mk("AMD-USD", cat="RWA", sub="Equity", ref="us_equity", vol="1200"),
            mk("AMD_24_5-USD", ui="AMD-USD", cat="RWA", sub="Equity", ref="us_equity", vol="98000"),
            mk("BTC-USD", vol="5")]
    with caplog.at_level(logging.WARNING, logger="funding_bot.extended"):
        ins = ExtendedPerp(session=_S(routes(mkts))).perp_instruments()
    assert [i["symbol"] for i in ins] == ["AMD_24_5-USD", "BTC-USD"]
    assert "AMD-USD" in caplog.text and "kept AMD_24_5-USD" in caplog.text


def test_empty_or_all_untradable_list_raises():
    with pytest.raises(RuntimeError, match="without tradable"):
        ExtendedPerp(session=_S(routes([mk("BTCSPOT-USD", typ="SPOT")]))).perp_instruments()
    with pytest.raises(RuntimeError, match="empty"):
        ExtendedPerp(session=_S(routes([]))).perp_instruments()


# --- tick: premium and books from one snapshot -------------------------------------------------------------------------
def test_premium_passes_the_hourly_rate_through_and_shares_one_download(clock):
    s = _S(routes())
    cl = ExtendedPerp(session=s)
    p = cl.premium()
    assert "BTCSPOT-USD" not in p and "NFLX_24_5-USD" not in p and "MKR-USD" not in p and "FTM-USD" not in p
    btc = p["BTC-USD"]
    # fundingRate is a fraction PER 1 HOUR [L/D]: passed through, interval 1 h — no ÷8, no annualisation
    assert btc == dict(rate=0.000004, mark=77196.04, index=77231.31, next_ms=NEXT, ts_ms=int(NOW * 1000), obs=NOW,
                       interval_h=1)
    assert calc.hourly(btc["rate"], btc["interval_h"]) == 0.000004
    assert p["ETH-USD"]["rate"] * 8 == pytest.approx(0.0001, rel=0.05)      # baseline 0.01 %/8 h ÷ 8 = 1.25e-5 ≈ 1.3e-5
    assert p["ONG-USD"]["rate"] == -0.000497 and p["1000PEPE-USD"]["rate"] == 0.000001
    assert p["SPX500m-USD"]["rate"] == 0.000004                               # off-hours: rate kept (it still settles)
    b = cl.books()
    assert [c[1] for c in s.calls] == ["/info/markets"]                       # one ≈1 MB download per tick
    clock[0] += cl.ctx_ttl_s
    cl.premium()
    assert len(s.calls) == 2                                                  # stale snapshot → a new download
    assert s.calls[0][2] == {}                                                # the whole list, unfiltered


def test_books_top_of_book_and_what_is_not_a_price(clock):
    b = ExtendedPerp(session=_S(routes())).books()
    assert b["BTC-USD"] == dict(bid=77191.0, ask=77192.0, bid_qty=0.0, ask_qty=0.0, obs=NOW)
    assert b["1000PEPE-USD"]["bid"] == 0.003404                               # per contract unit (1000 PEPE)
    assert "SPX500m-USD" not in b and "NVDA_24_5-USD" not in b                # off-hours: synthetic band, no trading
    assert "WIDE-USD" not in b and "NOBID-USD" not in b                       # 18 % spread / empty side
    assert "NFLX_24_5-USD" not in b and "MKR-USD" not in b and "BTCSPOT-USD" not in b
    assert extended._book("2", "1") is None and extended._book("1", "1.04") is not None


def test_next_settlement_falls_back_to_the_hour_grid(clock):
    past = [mk("BTC-USD", nxt=NEXT - H), mk("ETH-USD", nxt="garbage")]
    p = ExtendedPerp(session=_S(routes(past))).premium()
    assert p["BTC-USD"]["next_ms"] == NEXT and p["ETH-USD"]["next_ms"] == NEXT


def test_a_cached_copy_shows_its_real_age_by_the_date_header(clock):
    stale = {"Date": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(NOW - 45))}
    p = ExtendedPerp(session=_S(routes(headers=stale))).premium()
    assert p["BTC-USD"]["obs"] == NOW - 45 and p["BTC-USD"]["ts_ms"] == int((NOW - 45) * 1000)
    fresh = {"Date": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(NOW - 1))}   # 1 s resolution: ignored
    assert ExtendedPerp(session=_S(routes(headers=fresh))).books()["BTC-USD"]["obs"] == NOW
    assert ExtendedPerp(session=_S(routes(headers={"Date": "nonsense"}))).books()["BTC-USD"]["obs"] == NOW


# --- history -----------------------------------------------------------------------------------------------------------------
def _row(m, f, t):
    return {"m": m, "f": f, "T": t}


def test_history_floors_late_rows_to_the_hour_and_reads_both_ends(clock):
    h0 = 1789171200000                                                        # 12.09.2026 00:00Z
    rows = [_row("BTC-USD", "0.000008", h0 + 3 * H + 772),                    # newest first [L]
            _row("BTC-USD", "-0.00001", h0 + 2 * H + 559_000),                # +559 s outlier [L]
            _row("BTC-USD", "-0.000013", h0 + H + 1018),
            _row("BTC-USD", "0.000009", h0 + 1031)]

    def funding(market, p):
        assert market == "BTC-USD"
        return _R({"status": "OK", "data": [r for r in rows if p["startTime"] <= r["T"] <= p["endTime"]]})
    s = _S(routes(funding=funding))
    out = ExtendedPerp(session=s, history_gap_s=0).history_since("BTC-USD", h0, h0 + 3 * H)
    assert [r["funding_ms"] for r in out] == [h0, h0 + H, h0 + 2 * H, h0 + 3 * H]   # on the hour, ascending
    assert [r["rate"] for r in out] == [0.000009, -0.000013, -0.00001, 0.000008]   # fraction per 1 h, as settled
    assert out[0] == dict(exchange="extended", symbol="BTC-USD", funding_ms=h0, rate=0.000009, mark=None)
    _h, path, p = s.calls[-1]
    # end exactly on the hour: the endTime slack still brings that hour's row (T lands after the hour)
    assert path == "/info/BTC-USD/funding" and p == {"startTime": h0, "endTime": h0 + 3 * H + extended.LATE_MS}
    # a start just after a settlement drops that settlement (it is already stored), even though its raw T is later
    out2 = ExtendedPerp(session=s, history_gap_s=0).history_since("BTC-USD", h0 + 1, h0 + 3 * H - 1)
    assert [r["funding_ms"] for r in out2] == [h0 + H, h0 + 2 * H]


def test_history_pages_backwards_past_1000_rows(clock):
    h0 = 1783728000000
    grid = [h0 + i * H + 800 for i in range(1440)]                            # 60 days, like 1000PEPE [L]

    def funding(market, p):
        sel = [t for t in grid if p["startTime"] <= t <= p["endTime"]][-1000:][::-1]   # the newest 1000, newest first
        return _R({"status": "OK", "data": [_row(market, "0.000013", t) for t in sel]})
    s = _S(routes(funding=funding))
    out = ExtendedPerp(session=s, history_gap_s=0).history_since("1000PEPE-USD", h0, h0 + 1439 * H)
    assert [r["funding_ms"] for r in out] == [h0 + i * H for i in range(1440)]
    calls = [c[2] for c in s.calls]
    assert len(calls) == 2 and calls[1]["endTime"] == grid[440] - 1 and calls[1]["startTime"] == h0
    # 30 days = 720 rows = one call
    s.calls.clear()
    assert len(ExtendedPerp(session=s, history_gap_s=0).history_since("1000PEPE-USD", h0, h0 + 719 * H)) == 720
    assert len(s.calls) == 1


def test_history_never_cuts_silently_and_empty_window(clock, monkeypatch):
    def funding(market, p):                                                   # always a full page, never older
        return _R({"status": "OK", "data": [_row(market, "0", p["endTime"] - k) for k in range(1000)]})
    monkeypatch.setattr(extended, "HISTORY_MAX_PAGES", 2)
    with pytest.raises(RuntimeError, match="did not reach"):
        ExtendedPerp(session=_S(routes(funding=funding)), history_gap_s=0).history_since("BTC-USD", 0, 10 * H)
    cl = ExtendedPerp(session=_S(routes(funding=lambda m, p: _R({"status": "OK", "data": []}))), history_gap_s=0)
    assert cl.history_since("BTC-USD", 5 * H, 4 * H) == [] and cl.history_since("BTC-USD", 0, H) == []
    assert cl.recent_history() == []


def test_unknown_market_is_permanent(clock):
    s = _S(lambda path, p: _R({"status": "ERROR", "error": {"code": 1001, "message": "Market not found"}}, 400))
    with pytest.raises(PermanentHTTPError, match="1001"):
        ExtendedPerp(session=s, history_gap_s=0).history_since("NOPE-USD", 0, H)
    assert len(s.calls) == 1                                                  # a 4xx is not retried


# --- transport: 429, errors, pacing ------------------------------------------------------------------------------------
def test_429_pauses_the_host_for_every_client_and_honours_retry_after(clock):
    s = _S(lambda path, p: _R({"status": "ERROR"}, 429, {"Retry-After": "7"}))
    cl = ExtendedPerp(session=s)
    with pytest.raises(BannedError):
        cl.premium()
    assert cl.banned_until == pytest.approx(NOW + 7) and cl.n_429 == 1 and not cl.budget_ok()
    other = ExtendedPerp(session=s, history_gap_s=0)                          # the collector's background copy
    with pytest.raises(BannedError):
        other.history_since("BTC-USD", 0, H)
    assert len(s.calls) == 1                                                  # same host → no request at all
    clock[0] += 8
    s2 = _S(lambda path, p: _R({}, 429))                                      # no Retry-After → 60 s
    with pytest.raises(BannedError):
        ExtendedPerp(session=s2).perp_instruments()
    assert cl.banned_until == pytest.approx(clock[0] + 60) and cl.health()["banned_until"] == int(clock[0] + 60)
    assert ExtendedPerp(session=_S(routes()), rest="https://other.fake/api/v1").premium()   # another host: not paused


def test_error_status_on_200_and_5xx_are_retried(clock):
    s = _S(lambda path, p: _R({"status": "ERROR", "error": {"code": 1, "message": "x"}}))
    cl = ExtendedPerp(session=s)
    with pytest.raises(RuntimeError, match="status ERROR"):
        cl.perp_instruments()
    assert len(s.calls) == 3 and cl.n_err == 3
    n = [0]

    def flaky(path, p):
        n[0] += 1
        return _R({}, 503) if n[0] == 1 else routes()(path, p)
    assert ExtendedPerp(session=_S(flaky)).perp_instruments()               # universe: 3 tries
    s3 = _S(lambda path, p: _R({}, 503))
    with pytest.raises(RuntimeError):
        ExtendedPerp(session=s3).premium()
    assert len(s3.calls) == config.TICK_RETRIES                               # the tick: one try, the next tick retries
    with pytest.raises(PermanentHTTPError, match="403"):
        ExtendedPerp(session=_S(lambda path, p: _R("forbidden", 403))).premium()


def test_history_is_paced_and_the_tick_refuses_a_full_window(clock):
    s = _S(routes(funding=lambda m, p: _R({"status": "OK", "data": []})))
    cl = ExtendedPerp(session=s, history_gap_s=0.25)
    t = []
    for _ in range(3):
        cl.history_since("BTC-USD", 0, H)
        t.append(clock[0])
    assert t[1] - t[0] == pytest.approx(0.25) and t[2] - t[1] == pytest.approx(0.25)
    h = extended._host(cl.rest)
    h.calls.extend([clock[0]] * (extended.TICK_CAP - len(h.calls)))
    n = len(s.calls)
    with pytest.raises(BudgetExceeded):
        cl.premium()                                                          # the tick never waits
    assert len(s.calls) == n and cl.health()["used_weight"] == extended.TICK_CAP
    t1 = clock[0]
    cl.history_since("BTC-USD", 0, H)                                         # history waits for the window to drain
    assert clock[0] - t1 >= 59 and len(s.calls) == n + 1


def test_user_agent_is_always_sent():
    s = _S(routes())
    ExtendedPerp(session=s)
    assert s.headers["user-agent"] == config.USER_AGENT                       # an empty UA gets 403 [L]


# --- wiring: interface, pairs, identity ----------------------------------------------------------------------------------
def test_interface_shape_and_ff_alignment(monkeypatch):
    cl = ExtendedPerp(session=_S(routes()))
    assert venues.native(cl) and not venues.spot_native(cl) and venues.funding_intervals(cl) is None   # fixed 1 h
    assert extended.PERP_CLIENTS == {"extended": ExtendedPerp} and cl.name == "extended"
    assert cl.history_gap_s == config.FUNDING_HISTORY_MIN_GAP_S.get("extended", 0.25)
    monkeypatch.setattr(config, "PERP_VENUES", ("hyperliquid", "extended"))
    ins = cl.perp_instruments()
    hl = [dict(symbol=s, base=b, cls=c, factor=f) for s, b, c, f in
          (("BTC", "BTC", "crypto", 1.0), ("kPEPE", "PEPE", "crypto", 1000.0), ("SPX", "SPX", "crypto", 1.0),
           ("PURR", "PURR", "crypto", 1.0),                                   # HL's PURR coin
           ("xyz:SP500", "SP500", "index", 1.0), ("xyz:CL", "CL", "commodity", 1.0),
           ("xyz:NATGAS", "NATGAS", "commodity", 1.0), ("xyz:BRENTOIL", "BZ", "commodity", 1.0),
           ("xyz:JPY", "JPY", "fx", 1.0), ("xyz:NVDA", "NVDA", "equity", 1.0), ("xyz:SKHX", "SKHYNIX", "equity", 1.0))]
    keys = {r["key"] for r in universe.build_ff({"hyperliquid": hl, "extended": ins})}
    assert keys == {"hyperliquid:BTC|extended:BTC-USD", "hyperliquid:kPEPE|extended:1000PEPE-USD",
                    "hyperliquid:SPX|extended:SPX-USD",                        # SPX6900 coin, not the S&P index
                    "hyperliquid:xyz:SP500|extended:SPX500m-USD", "hyperliquid:xyz:CL|extended:WTI-USD",
                    "hyperliquid:xyz:NATGAS|extended:XNG-USD", "hyperliquid:xyz:BRENTOIL|extended:XBR-USD",
                    "hyperliquid:xyz:JPY|extended:USDJPY-USD", "hyperliquid:xyz:NVDA|extended:NVDA_24_5-USD",
                    "hyperliquid:xyz:SKHX|extended:SKHYNIX-USD"}               # PURR coin ≠ PURR share


def _sf(spot_ex, spot, perp_ex, perp, base):
    return dict(key=f"{spot_ex}:{spot}|{perp_ex}:{perp}", base=base, cls="crypto", spot_ex=spot_ex, spot=spot,
                spot_factor=1.0, perp_ex=perp_ex, perp=perp, perp_factor=1.0)


def test_identity_uses_the_declared_name_as_for_lighter(monkeypatch):
    """Oracle venue: no index composition, no contracts — the declared full name (name_hint) is the only evidence."""
    monkeypatch.setattr(identity, "ORACLE_PERPS", identity.ORACLE_PERPS | {"extended"})   # the integrator's wiring
    ins = {i["symbol"]: i for i in ExtendedPerp(session=_S(routes())).perp_instruments()}
    names = {"extended": {s: i["name_hint"] for s, i in ins.items() if i.get("name_hint")}}
    rec = lambda name, addr: coin_record(name, [("ETH", addr, True, True)], "")
    spots = {"gate_spot": {"coins": {"SPX": rec("SPX6900", "0x" + "e0" * 20), "ONG": rec("Ontology Gas", "0x" + "01" * 20)},
                           "markets": {"SPX_USDT": ["SPX", True], "ONG_USDT": ["ONG", True]}}}
    R = Resolver(spots, {}, perp_names=names)
    d = decide_sf(R, _sf("gate_spot", "SPX_USDT", "extended", "SPX-USD", "SPX"))
    assert (d["ident"], d["ident_ev"]) == ("same", "name")
    d = decide_sf(Resolver(spots, {}), _sf("gate_spot", "ONG_USDT", "extended", "ONG-USD", "ONG"))
    assert (d["ident"], d["ident_ev"]) == ("unknown", "oracle_only")          # no name passed → not checked, not «≠»
    ff = dict(key="extended:BTC-USD|x:BTC", base="BTC", cls="crypto", va="extended", sa="BTC-USD", vb="lighter",
              sb="BTC", fa=1.0, fb=1.0)
    assert decide_ff(R, ff)["ident"] == "unknown"                             # two oracle venues prove nothing
