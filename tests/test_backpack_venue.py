"""Backpack perps on a fake HTTP session and fake WS connections (no network). Response shapes are trimmed copies of the
12.09.2026 live captures (see the header of backpack.py)."""
import json, socket, time, types
from datetime import datetime, timezone
import pytest
import requests
from funding_bot import backpack, calc, config, identity, universe, venues
from funding_bot.backpack import BackpackFut
from funding_bot.client import BannedError, PermanentHTTPError

H = 3600_000
NOW = 1789248940.0                    # 12.09.2026 21:35:40 UTC — the live sample
T22 = 1789250400_000                  # 22:00 UTC that day = nextFundingTimestamp of the sample [L]
UTC = timezone.utc


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")


class _R:
    def __init__(self, body, code=200, headers=None, text=None):
        self.status_code, self._b, self.headers = code, body, headers or {}
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if isinstance(self._b, Exception):
            raise self._b
        return self._b

    def raise_for_status(self):
        if self.status_code >= 500:
            raise RuntimeError(str(self.status_code))


class _S:
    def __init__(self, handler):
        self.handler, self.calls, self.headers = handler, [], {}

    def get(self, url, params=None, timeout=None):
        path = url.split("/api/v1", 1)[1]
        self.calls.append((path, dict(params or {})))
        return self.handler(path, dict(params or {}))


@pytest.fixture(autouse=True)
def _fresh():
    backpack._HOSTS.clear(); backpack._WARNED.clear()
    yield
    backpack._HOSTS.clear(); backpack._WARNED.clear()


@pytest.fixture
def clock(monkeypatch):
    c = [NOW]

    def sleep(s):
        c[0] += max(0.0, s)
    monkeypatch.setattr(backpack, "time", types.SimpleNamespace(time=lambda: c[0], sleep=sleep, gmtime=time.gmtime,
                                                                strftime=time.strftime))
    return c


# --- recorded shapes ---------------------------------------------------------------------------------------------------
def mkt(sym, base, state="Open", visible=True, rwa=None, created="2025-08-07T05:45:29.683291", up="150", lo="-150",
        tick="0.01", step="0.01", minq="0.01", iv=3600000, mtype="PERP", quote="USDC"):
    return {"baseSymbol": base, "createdAt": created,
            "filters": {"price": {"borrowEntryFeeMaxMultiplier": None, "maxImpactMultiplier": "1.01", "maxMultiplier": "2",
                                  "maxPrice": "1000", "meanMarkPriceBand": {"maxMultiplier": "1.075", "minMultiplier": "0.925"},
                                  "minPrice": "0.01", "tickSize": tick},
                        "quantity": {"maxQuantity": None, "minQuantity": minq, "stepSize": step}},
            "fundingInterval": iv, "fundingRateLowerBound": lo, "fundingRateUpperBound": up,
            "imfFunction": {"base": "0.02", "factor": "0.000075", "type": "sqrt"}, "marketType": mtype,
            "mmfFunction": {"base": "0.0135", "factor": "0.0000375", "type": "sqrt"}, "openInterestLimit": "4000000",
            "orderBookState": state, "positionLimitWeight": "1", "quoteSymbol": quote, "rwaMarketType": rwa,
            "symbol": sym, "visible": visible}


MARKETS = [mkt("BTC_USDC_PERP", "BTC", up="100", lo="-100", tick="0.1", step="0.00001", minq="0.00001",
               created="2025-01-21T06:34:54.691858"),
           mkt("kBONK_USDC_PERP", "kBONK", tick="0.000001", step="100", minq="100", created="2025-06-13T01:50:00.013812"),
           mkt("KMNO_USDC_PERP", "KMNO"), mkt("FARTCOIN_USDC_PERP", "FARTCOIN"), mkt("APT_USDC_PERP", "APT"),
           mkt("PAXG_USDC_PERP", "PAXG"), mkt("NVDA.US_USDC_PERP", "NVDA.US", rwa="STOCK"),
           mkt("SKHY.US_USDC_PERP", "SKHY.US", rwa="STOCK"), mkt("SPCX.US_USDC_PERP", "SPCX.US", rwa="STOCK"),
           mkt("QQQ.US_USDC_PERP", "QQQ.US", rwa="INDEX"),
           mkt("TON_USDC_PERP", "TON", state="Closed"),                                   # delisted: no mark, depth 404
           mkt("AMZN.US_USDC_PERP", "AMZN.US", state="PostOnly", visible=False, rwa="STOCK"),   # no taker orders
           mkt("NEWBOND_USDC_PERP", "NEWBOND", rwa="BOND"),                               # unknown RWA kind
           mkt("SOON_USDC_PERP", "SOON", created="2026-09-20T00:00:00"),                  # listing not started
           mkt("SOL_USDC", "SOL", mtype="SPOT")]                                          # not a perp
LIVE = {"BTC_USDC_PERP", "kBONK_USDC_PERP", "KMNO_USDC_PERP", "FARTCOIN_USDC_PERP", "APT_USDC_PERP", "PAXG_USDC_PERP",
        "NVDA.US_USDC_PERP", "SKHY.US_USDC_PERP", "SPCX.US_USDC_PERP", "QQQ.US_USDC_PERP", "NEWBOND_USDC_PERP"}


def asset(sym, name, cg=None, tokens=()):
    return {"coingeckoId": cg, "displayName": name, "symbol": sym, "tokens": [
        {"blockchain": b, "contractAddress": a, "depositEnabled": True, "withdrawEnabled": True} for b, a in tokens]}


ASSETS = [asset("BTC", "Bitcoin", "bitcoin", [("Bitcoin", "bc1")]), asset("kBONK", "kBONK"),
          asset("BONK", "Bonk", "bonk", [("Solana", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")]),
          asset("FARTCOIN", "FARTCOIN", "fartcoin"), asset("APT", "APTOS", "aptos"), asset("KMNO", "Kamino", "kamino"),
          asset("PAXG", "PAX Gold", "pax-gold"), asset("NVDA.US", "NVIDIA Corporation"), asset("SKHY.US", "SK Hynix"),
          asset("SPCX.US", "SpaceX"), asset("QQQ.US", "Invesco QQQ Trust"), asset("USDC", "USD Coin", "usd-coin")]


def mark(sym, rate, mk, idx, nxt=T22):
    return {"fundingRate": rate, "indexPrice": idx, "markPrice": mk, "nextFundingTimestamp": nxt, "symbol": sym}


MARKS = [mark("BTC_USDC_PERP", "0.0000125", "77203.1", "77235.838"),
         mark("kBONK_USDC_PERP", "-0.0000492043511922378493112131", "0.002789", "0.00279133"),
         mark("NVDA.US_USDC_PERP", "0.00000625", "181.33", "181.4", nxt=T22 - H),      # a CDN copy from before hh:00
         mark("AMZN.US_USDC_PERP", "0.00000625", "230.1", "230.1")]                   # PostOnly: not in the universe


def routes(markets=MARKETS, assets=ASSETS, marks=MARKS, age=None, funding=None, depth=None):
    def h(path, p):
        if path == "/markets":
            assert p == {"marketType": "PERP"}
            return _R(list(markets))
        if path == "/assets":
            return _R(list(assets)) if assets is not None else _R({}, 503)
        if path == "/markPrices":
            return _R(list(marks), headers={"Age": str(age)} if age is not None else {})
        if path == "/fundingRates" and funding is not None:
            return funding(p)
        if path == "/depth" and depth is not None:
            return depth(p)
        raise AssertionError(path)
    return h


# --- universe --------------------------------------------------------------------------------------------------------------
def test_perp_instruments_fields_classes_bases_and_filters(clock):
    s = _S(routes())
    ins = {i["symbol"]: i for i in BackpackFut(session=s).perp_instruments()}
    assert set(ins) == LIVE                           # Closed, PostOnly/hidden, future listing and the spot row are out
    got = {k: (i["cls"], i["base"], i["factor"]) for k, i in ins.items()}
    assert got == {"BTC_USDC_PERP": ("crypto", "BTC", 1.0), "kBONK_USDC_PERP": ("crypto", "BONK", 1000.0),
                   "KMNO_USDC_PERP": ("crypto", "KMNO", 1.0),                  # upper-case K is not the ×1000 prefix
                   "FARTCOIN_USDC_PERP": ("crypto", "FARTCOIN", 1.0), "APT_USDC_PERP": ("crypto", "APT", 1.0),
                   "PAXG_USDC_PERP": ("crypto", "PAXG", 1.0),                  # gold token = coin (repo rule)
                   "NVDA.US_USDC_PERP": ("equity", "NVDA", 1.0),
                   "SKHY.US_USDC_PERP": ("equity", "SKHY", 1.0),               # the SKHY listing, not SKHYNIX
                   "SPCX.US_USDC_PERP": ("equity", "SPCX", 1.0),               # SpaceX — equity on 10 venues (review 13.09)
                   "QQQ.US_USDC_PERP": ("equity", "QQQ", 1.0),                 # tagged INDEX, but an ETF share
                   "NEWBOND_USDC_PERP": ("rwa", "NEWBOND", 1.0)}               # unknown RWA kind pairs with nothing
    b = ins["BTC_USDC_PERP"]
    created = int(datetime(2025, 1, 21, 6, 34, 54, tzinfo=UTC).timestamp()) * 1000 + 691      # naive ISO = UTC
    assert (b["onboard_ms"], b["interval_h"], b["quote"], b["contract"], b["min_notional"]) == \
        (created, 1, "USDC", "PERPETUAL", None)
    assert b["tick_size"] == 0.1 and b["step_size"] == pytest.approx(1e-5) and b["base_asset"] == "BTC"
    assert b["cap"] == pytest.approx(0.01) and b["floor"] == pytest.approx(-0.01)             # 100 bp per interval
    k = ins["kBONK_USDC_PERP"]
    assert k["cap"] == pytest.approx(0.015) and k["floor"] == pytest.approx(-0.015) and k["step_size"] == 100.0
    assert b["url"] == "https://backpack.exchange/trade/BTC_USD_PERP"
    assert ins["NVDA.US_USDC_PERP"]["url"] == "https://backpack.exchange/trade/NVDA.US_USD_PERP"
    names = {k: i["name_hint"] for k, i in ins.items()}
    assert names["BTC_USDC_PERP"] == "Bitcoin" and names["NVDA.US_USDC_PERP"] == "NVIDIA Corporation"
    # kBONK: own displayName «kBONK», the unprefixed asset's «Bonk» — both just the ticker [L] → no name; coingecko kept
    assert names["kBONK_USDC_PERP"] is None and k["coingecko_id"] == "bonk"
    assert names["FARTCOIN_USDC_PERP"] is None and names["APT_USDC_PERP"] == "APTOS"         # a ticker is no name
    assert names["NEWBOND_USDC_PERP"] is None and ins["FARTCOIN_USDC_PERP"]["coingecko_id"] == "fartcoin"
    assert [c[0] for c in s.calls] == ["/markets", "/assets"]
    backpack._HOSTS.clear()                                   # a k-market borrows a real name of its unprefixed asset
    named = [dict(a, displayName="Bonk Inu Token") if a["symbol"] == "BONK" else a for a in ASSETS]
    ins2 = {i["symbol"]: i for i in BackpackFut(session=_S(routes(assets=named))).perp_instruments()}
    assert ins2["kBONK_USDC_PERP"]["name_hint"] == "Bonk Inu Token"


def test_assets_outage_keeps_a_recent_copy_and_never_blocks_the_universe(clock):
    state = {"assets": ASSETS}

    def h(path, p):
        return routes(assets=state["assets"])(path, p)
    cl = BackpackFut(session=_S(h))
    assert {i["symbol"]: i["name_hint"] for i in cl.perp_instruments()}["BTC_USDC_PERP"] == "Bitcoin"
    state["assets"] = None                                                        # /assets answers 503
    clock[0] += backpack.ASSETS_TTL_S + 1
    assert {i["symbol"]: i["name_hint"] for i in cl.perp_instruments()}["BTC_USDC_PERP"] == "Bitcoin"
    clock[0] += backpack.ASSETS_STALE_OK_S
    ins = cl.perp_instruments()                                                   # too old: no names, still a universe
    assert len(ins) == len(LIVE) and all(i["name_hint"] is None for i in ins)


def test_units_parsers_and_classes():
    assert backpack.iso_ms("2026-09-12T22:00:00") == T22 and backpack.iso_ms("2026-09-12T22:00:00Z") == T22
    assert backpack.iso_ms("2026-09-12T22:00:00.5") == T22 + 500 and backpack.iso_ms("nope") is None
    assert backpack.perp_class("SPY.US", "INDEX") == "equity" and backpack.perp_class("US500", "INDEX") == "index"
    assert backpack.perp_class("X", "") == "crypto" and backpack.perp_class("X", "BOND") == "rwa"
    assert backpack.perp_base("kPEPE", "crypto") == ("PEPE", 1000.0) and backpack.perp_base("KAITO", "crypto") == ("KAITO", 1.0)
    assert backpack.perp_base("X.US", "crypto") == ("X.US", 1.0)                  # a «.US» coin is not a share
    assert backpack._roll(T22 - H, 1, T22 + 5000) == T22 + H and backpack._roll(None, 1, T22 - 1) == T22


# --- rates -----------------------------------------------------------------------------------------------------------------
def test_premium_rate_is_per_interval_hour_with_cdn_age_and_rolled_next(clock):
    fresh = BackpackFut(session=_S(routes(age=2)))
    assert set(fresh.premium()) == {m["symbol"] for m in MARKS}                   # before the universe: every *_PERP row
    cl = BackpackFut(session=_S(routes(age=2)))
    cl.perp_instruments()
    p = cl.premium()
    assert set(p) == {"BTC_USDC_PERP", "kBONK_USDC_PERP", "NVDA.US_USDC_PERP"}    # PostOnly AMZN.US dropped
    obs = NOW - 2                                                                 # arrival minus the CloudFront Age
    assert p["BTC_USDC_PERP"] == dict(rate=0.0000125, mark=77203.1, index=77235.838, next_ms=T22, ts_ms=int(obs * 1000),
                                      obs=obs, interval_h=1)
    assert p["NVDA.US_USDC_PERP"]["next_ms"] == T22                               # passed next rolled onto the grid
    # the published rate IS the rate per interval (1 h): no ÷8, no ÷(24·365). 0.0000125/h = 0.01 %/8 h = 0.03 %/day
    k = p["kBONK_USDC_PERP"]
    assert k["rate"] == pytest.approx(-0.0000492043511922) and calc.hourly(k["rate"], k["interval_h"]) == k["rate"]
    assert p["BTC_USDC_PERP"]["rate"] * 8 == pytest.approx(0.0001) and p["BTC_USDC_PERP"]["rate"] * 24 == pytest.approx(0.0003)
    implausible = BackpackFut(session=_S(routes(age=86400)))
    assert implausible.premium()["BTC_USDC_PERP"]["obs"] == NOW                   # a nonsense Age is ignored


# --- history ---------------------------------------------------------------------------------------------------------------
def fr(sym, ms, rate):
    return {"fundingRate": rate, "intervalEndTimestamp": iso(ms), "symbol": sym}


def _hist(rows_by_sym):
    """/fundingRates: newest first, limit/offset in rows, no time filter; unknown symbol → 200 [] [L]."""
    def h(p):
        rows = sorted(rows_by_sym.get(p["symbol"], []), key=lambda r: -backpack.iso_ms(r["intervalEndTimestamp"]))
        o, n = int(p.get("offset", 0)), int(p.get("limit", 100))
        return _R(rows[o:o + n])
    return h


def test_history_drops_the_in_progress_and_not_yet_final_rows(clock):
    sym = "kBONK_USDC_PERP"
    rows = {sym: [fr(sym, T22, "-0.000060053"),                                   # in progress (+24 min) [L]
                  fr(sym, T22 - H, "-0.000039497"), fr(sym, T22 - 2 * H, "-0.00004299"),
                  fr(sym, T22 - 3 * H, "0.0000125")]}
    s = _S(routes(funding=_hist(rows)))
    cl = BackpackFut(session=s, history_gap_s=0)
    cl.perp_instruments()
    out = cl.history_since(sym, T22 - 3 * H)
    assert [r["funding_ms"] for r in out] == [T22 - 3 * H, T22 - 2 * H, T22 - H]   # 22:00 is not settled yet
    assert out[-1] == dict(exchange="backpack", symbol=sym, funding_ms=T22 - H, rate=-0.000039497, mark=None)
    assert s.calls[-1] == ("/fundingRates", {"symbol": sym, "limit": 5, "offset": 0})   # 2.6 h back + 2 rows
    # 22:05: the 22:00 row exists but is inside the grace (final 1-3 min after hh:00, CDN copy up to 3 min old)
    clock[0] = T22 / 1000 + 300
    rows[sym][0] = fr(sym, T22, "-0.000059749")
    rows[sym].insert(0, fr(sym, T22 + H, "-0.0000438"))
    out = cl.history_since(sym, T22 - 2 * H)
    assert [r["funding_ms"] for r in out] == [T22 - 2 * H, T22 - H]
    clock[0] = T22 / 1000 + config.SETTLE_GRACE_S + 1
    assert cl.history_since(sym, T22 - H)[-1] == dict(exchange="backpack", symbol=sym, funding_ms=T22,
                                                       rate=-0.000059749, mark=None)
    assert [r["funding_ms"] for r in cl.history_since(sym, T22 - 3 * H, T22 - 2 * H)] == [T22 - 3 * H, T22 - 2 * H]


def test_history_pages_by_offset_and_a_settlement_between_pages_is_no_gap(clock):
    sym = "BTC_USDC_PERP"
    rows = [fr(sym, T22 - i * H, "0.0000125") for i in range(1500)]              # newest first, 22:00 in progress
    state = {"n": 0}

    def h(p):
        state["n"] += 1
        if state["n"] == 2:
            rows.insert(0, fr(sym, T22 + H, "0.0000125"))                        # hh:00 passed between the two calls
        o, n = p["offset"], p["limit"]
        return _R(rows[o:o + n])
    s = _S(routes(funding=h))
    cl = BackpackFut(session=s, history_gap_s=0)
    cl.perp_instruments()
    start = T22 - 1400 * H
    out = cl.history_since(sym, start)
    hi = int(NOW * 1000) - config.SETTLE_GRACE_S * 1000
    want = [ms for ms in range(start, hi + 1, H)]
    assert [r["funding_ms"] for r in out] == want                                 # contiguous, each hour once
    assert [c[1] for c in s.calls if c[0] == "/fundingRates"] == [{"symbol": sym, "limit": 1000, "offset": 0},
                                                                  {"symbol": sym, "limit": 1000, "offset": 1000}]


def test_history_never_truncates_silently(clock, monkeypatch):
    monkeypatch.setattr(backpack, "HISTORY_MAX_PAGES", 3)
    sym = "BTC_USDC_PERP"

    def h(p):
        return _R([fr(sym, T22 - (p["offset"] + i) * H, "0.0000125") for i in range(p["limit"])])   # endless full pages
    cl = BackpackFut(session=_S(routes(funding=h)), history_gap_s=0)
    cl.perp_instruments()
    with pytest.raises(RuntimeError, match="did not reach"):
        cl.history_since(sym, T22 - 10000 * H)


def test_empty_answer_for_a_live_market_is_not_a_confirmation(clock):
    new = mkt("NEW_USDC_PERP", "NEW", created=iso(int((NOW - 600) * 1000)))      # listed 10 min ago
    s = _S(routes(markets=MARKETS + [new], funding=_hist({})))
    cl = BackpackFut(session=s, history_gap_s=0)
    cl.perp_instruments()
    with pytest.raises(RuntimeError, match="not confirmed"):
        cl.history_since("BTC_USDC_PERP", T22 - 5 * H)                            # 200 [] for a market live since 2025
    assert cl.history_since("NEW_USDC_PERP", T22 - 5 * H) == []                   # nothing settled yet — honest
    n = len(s.calls)
    with pytest.raises(RuntimeError, match="no market NOPE"):
        cl.history_since("NOPE_USDC_PERP", 0)                                      # list is fresh: no refetch
    assert len(s.calls) == n
    clock[0] += backpack.MARKETS_TTL_S
    with pytest.raises(RuntimeError, match="no market TON"):
        cl.history_since("TON_USDC_PERP", 0)                                       # closed market: refreshed once
    assert [c[0] for c in s.calls[n:]] == ["/markets"]
    copy = BackpackFut(session=s, history_gap_s=0)                                # collector's background copy
    with pytest.raises(RuntimeError, match="not confirmed"):
        copy.history_since("BTC_USDC_PERP", T22 - 5 * H)
    assert [c[0] for c in s.calls[n + 1:]] == ["/fundingRates"]                   # learnt from the shared host


def test_history_is_paced_across_copies_and_config_overrides(clock, monkeypatch):
    sym = "BTC_USDC_PERP"
    rows = {sym: [fr(sym, T22 - i * H, "0.0000125") for i in range(10)]}
    s = _S(routes(funding=_hist(rows)))
    a, b = BackpackFut(session=s), BackpackFut(session=s)
    assert a.history_gap_s == backpack.HISTORY_GAP_S == 0.5
    a.perp_instruments()
    t = []
    for cl in (a, b, a):
        cl.history_since(sym, T22 - 3 * H)
        t.append(clock[0])
    assert t[1] - t[0] == pytest.approx(0.5) and t[2] - t[1] == pytest.approx(0.5)
    monkeypatch.setitem(config.FUNDING_HISTORY_MIN_GAP_S, "backpack", 1.25)
    assert BackpackFut(session=s).history_gap_s == 1.25
    assert a.recent_history() == [] and a.health()["used_weight"] == len(s.calls)


# --- transport ---------------------------------------------------------------------------------------------------------------
def test_429_pauses_the_host_for_every_client(clock):
    s = _S(lambda path, p: _R({"code": "TOO_MANY_REQUESTS"}, 429, {"Retry-After": "7"}))
    cl = BackpackFut(session=s)
    with pytest.raises(BannedError):
        cl.premium()
    assert cl.banned_until == pytest.approx(NOW + 7) and cl.n_429 == 1 and not cl.budget_ok()
    with pytest.raises(BannedError):
        BackpackFut(session=s).perp_instruments()                                  # same host: no request at all
    assert len(s.calls) == 1
    other = BackpackFut(session=_S(routes()), rest="https://other.example/api/v1")
    assert other.premium() and other.budget_ok()                                   # another host is not paused
    clock[0] += 8
    s2 = _S(lambda path, p: _R({}, 418))
    with pytest.raises(BannedError):
        BackpackFut(session=s2).premium()
    assert cl.banned_until == pytest.approx(clock[0] + backpack.BAN_DEFAULT_S)     # no Retry-After → 60 s
    assert cl.health()["banned_until"] == int(clock[0] + 60)


def test_4xx_is_permanent_5xx_retried_and_the_tick_tries_once(clock):
    s = _S(lambda path, p: _R(None, 400, text="Symbol not found"))                 # plain-text error body [L]
    with pytest.raises(PermanentHTTPError, match="400.*Symbol not found"):
        BackpackFut(session=s).perp_instruments()
    s2 = _S(lambda path, p: _R({}, 503))
    cl = BackpackFut(session=s2)
    with pytest.raises(RuntimeError, match="/markets"):
        cl.perp_instruments()
    assert len(s2.calls) == 3 and cl.n_err == 3
    with pytest.raises(RuntimeError):
        cl.premium()
    assert len(s2.calls) == 3 + config.TICK_RETRIES
    s3 = _S(lambda path, p: _R(ValueError("not json")))
    with pytest.raises(RuntimeError, match="ValueError"):
        BackpackFut(session=s3).premium()
    s4 = _S(lambda path, p: _R({"code": "INVALID_CLIENT_REQUEST", "message": "x"}))
    with pytest.raises(RuntimeError, match="not a list"):
        BackpackFut(session=s4).premium()


# --- books ---------------------------------------------------------------------------------------------------------------
def bt(sym, bid, ask, bq="1", aq="2", T=1789248950000000):
    return {"data": {"A": aq, "B": bq, "E": T + 300, "T": T, "a": ask, "b": bid, "e": "bookTicker", "s": sym, "u": 1},
            "stream": f"bookTicker.{sym}"}


def test_stream_cache_books_and_obs_across_reconnects():
    st = backpack._BookStream("wss://x", "backpack")
    st.conn_id = 1
    assert st.apply(bt("BTC_USDC_PERP", "77213.0", "77213.1", "0.2", "0.06"), 100.0)
    assert st.apply(bt("kBONK_USDC_PERP", "0.002790", "0.002792", "2800", "292800"), 101.0)
    assert st.apply(bt("WIDE_USDC_PERP", "1.0", "1.2"), 101.0) and st.apply(bt("CROSS_USDC_PERP", "2", "1"), 101.0)
    assert not st.apply({"id": None, "result": None}, 102.0)                     # subscribe ack
    assert not st.apply({"id": None, "error": {"code": 4005, "message": "Invalid stream"}}, 102.0)
    assert st.last_rx == 101.0
    b = st.books()
    assert set(b) == {"BTC_USDC_PERP", "kBONK_USDC_PERP"}                         # 18 % spread, crossed — not prices
    assert b["BTC_USDC_PERP"] == dict(bid=77213.0, ask=77213.1, bid_qty=0.2, ask_qty=0.06, obs=101.0)
    assert b["kBONK_USDC_PERP"]["ask_qty"] == 292800.0                            # in kBONK units, like the price
    st.apply(bt("BTC_USDC_PERP", "77000", "77001", T=1789248949000000), 103.0)    # an older engine time never wins
    assert st.books()["BTC_USDC_PERP"]["bid"] == 77213.0
    st.conn_id = 2                                            # reconnected: quiet markets are not re-sent
    assert st.books()["BTC_USDC_PERP"]["obs"] == 100.0        # its own last update — a missed change must not look fresh
    st.apply(bt("BTC_USDC_PERP", "77190", "77191", T=1789248960000000), 110.0)
    assert st.books({"BTC_USDC_PERP"}) == {"BTC_USDC_PERP": dict(bid=77190.0, ask=77191.0, bid_qty=1.0, ask_qty=2.0,
                                                                  obs=110.0)}
    assert st.books()["kBONK_USDC_PERP"]["obs"] == 101.0


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
        if callable(item):
            item()
            self.clock[0] += 1
            raise socket.timeout()
        if isinstance(item, (int, float)):
            self.clock[0] += item
            raise socket.timeout()
        if item == "close":
            return 8, b""
        return 1, json.dumps(item).encode()

    def close(self):
        self.closed = True


def _sub(*syms):
    return {"method": "SUBSCRIBE", "params": [f"bookTicker.{s}" for s in syms]}


def test_stream_subscribes_in_chunks_adds_markets_watchdog_and_reconnects(clock, monkeypatch):
    monkeypatch.setattr(backpack, "WS_SUB_CHUNK", 2)
    st = backpack._BookStream(backpack.WS_URL, "backpack")
    st.want(["ETH_USDC_PERP", "BTC_USDC_PERP"])
    B, E, K = "BTC_USDC_PERP", "ETH_USDC_PERP", "kBONK_USDC_PERP"
    conns = [_Conn([bt(B, "77213.0", "77213.1"), 3, bt(B, "77213.0", "77213.2", T=1789248951000000),
                    lambda: st.want([B, E, K]), 10, 31], clock),                   # then 31 s without data → reconnect
             _Conn([bt(E, "2525.3", "2525.4"), "close"], clock)]
    made = []

    def connect(url, timeout):
        made.append((url, timeout))
        return conns[len(made) - 1]
    st._connect = connect
    st.backoff0 = 0.0
    st._run(max_sessions=2)
    assert made == [(backpack.WS_URL, backpack.WS_RECV_TIMEOUT_S)] * 2 and st.n_conn == 2
    assert conns[0].sent == [_sub(B, E), _sub(K)]                                # a new market joins the live connection
    assert conns[1].sent == [_sub(B, E), _sub(K)]                                # all again after the reconnect, chunked
    assert all(c.closed for c in conns) and "closed by server" in st.err and not st.synced.is_set()
    b = st.books()
    assert set(b) == {B, E} and b[B]["ask"] == 77213.2                            # the cache survived the reconnect
    assert b[B]["obs"] == NOW + 3 and b[E]["obs"] > b[B]["obs"]                   # BTC from the first connection: own time


def test_depth_book_takes_prices_not_positions():
    body = {"asks": [["77211.1", "1.98704"], ["77211.3", "0.64757"], ["77213.1", "0.06473"]],     # ascending [L]
            "bids": [["77210.5", "0.00971"], ["77210.8", "1.36778"], ["77211", "2.95677"]],       # ascending [L]
            "lastUpdateId": "6520335337", "timestamp": 1789248951108224}                          # microseconds
    assert backpack.depth_book(body, 1789248951.5) == dict(bid=77211.0, ask=77211.1, bid_qty=2.95677, ask_qty=1.98704,
                                                           obs=pytest.approx(1789248951.108224))
    assert backpack.depth_book(dict(body, timestamp=1), NOW)["obs"] == NOW          # implausible time → arrival
    assert backpack.depth_book(dict(body, timestamp=int((NOW + 3) * 1e6)), NOW)["obs"] == NOW   # never after arrival
    assert backpack.depth_book(dict(body, bids=[]), 5.0) is None and backpack.depth_book([], 5.0) is None


def test_books_from_the_stream_and_rest_for_silent_markets(clock):
    depth_calls = []

    def depth(p):
        depth_calls.append(p["symbol"])
        if p["symbol"] == "APT_USDC_PERP":
            return _R({"code": "RESOURCE_NOT_FOUND", "message": "Market not found"}, 404)
        return _R({"asks": [["10.02", "5"], ["10.03", "1"]], "bids": [["9.98", "1"], ["10.0", "3"]],
                   "timestamp": int((clock[0] - 0.5) * 1e6)})
    cl = BackpackFut(session=_S(routes(depth=depth)))
    cl.perp_instruments()
    st = backpack._BookStream("wss://x", "backpack")
    st.start, st._waited, st.conn_id = (lambda: None), True, 1
    cl._stream = st
    st.apply(bt("BTC_USDC_PERP", "77213.0", "77213.1"), NOW)
    st.apply(bt("GONE_USDC_PERP", "1", "1.001"), NOW)                             # delisted since: not in the universe
    b = cl.books()
    assert st._want == LIVE and "GONE_USDC_PERP" not in b
    first = sorted(LIVE - {"BTC_USDC_PERP"})[:backpack.DEPTH_PER_CALL]
    assert depth_calls == first and "APT_USDC_PERP" in first
    assert set(b) == {"BTC_USDC_PERP"} | set(first) - {"APT_USDC_PERP"}           # a 404 market has no book
    assert b[first[1]] == dict(bid=10.0, ask=10.02, bid_qty=3.0, ask_qty=5.0, obs=NOW - 0.5)
    assert b["BTC_USDC_PERP"]["obs"] == NOW
    cl.books()                                                                    # the next five, never the same twice
    assert sorted(depth_calls) == sorted(LIVE - {"BTC_USDC_PERP"})
    n = len(depth_calls)
    st.apply(bt("KMNO_USDC_PERP", "0.0253", "0.0254"), NOW + 1)                   # the stream wins over REST
    b = cl.books()
    assert len(depth_calls) == n and b["KMNO_USDC_PERP"]["bid"] == 0.0253 and len(b) == len(LIVE) - 1
    clock[0] += backpack.REST_BOOK_TTL_S
    cl.books()
    assert len(depth_calls) == n + backpack.DEPTH_PER_CALL                        # re-read after the TTL

    def down(p):
        depth_calls.append(p["symbol"])
        raise ConnectionError("net down")
    cl2 = BackpackFut(session=_S(lambda path, p: down(p) if path == "/depth" else routes()(path, p)))
    cl2.perp_instruments()
    cl2._stream = st
    m = len(depth_calls)
    assert set(cl2.books()) == {"BTC_USDC_PERP", "KMNO_USDC_PERP"} and len(depth_calls) == m + 1   # stops at once


# --- interface, pairs, identity -------------------------------------------------------------------------------------------
def _no_net(*a, **k):
    raise AssertionError("network in a client constructor")


def test_interface_shape_and_pairs_with_other_venues(clock, monkeypatch):
    monkeypatch.setattr(requests.Session, "request", _no_net)
    cl = BackpackFut()
    assert venues.native(cl) and not venues.spot_native(cl) and cl.name == "backpack" and cl._stream is None
    assert not hasattr(cl, "index_legs") and not hasattr(cl, "coin_blob")
    assert backpack.PERP_CLIENTS == {"backpack": BackpackFut}
    c2 = BackpackFut(session=_S(routes()))
    assert venues.funding_intervals(c2) == {s: 1 for s in LIVE} and venues.recent_history(c2) == []
    monkeypatch.setattr(config, "PERP_VENUES", ("hyperliquid", "bitget", "backpack"))
    ins = c2.perp_instruments()
    hl = [dict(symbol=s, base=b, cls=c, factor=f) for s, b, c, f in (
        ("BTC", "BTC", "crypto", 1.0), ("kBONK", "BONK", "crypto", 1000.0), ("PAXG", "PAXG", "crypto", 1.0),
        ("xyz:NVDA", "NVDA", "equity", 1.0), ("xyz:SKHX", "SKHYNIX", "equity", 1.0), ("xyz:QQQ", "QQQ", "equity", 1.0),
        ("xyz:SPCX", "SPCX", "equity", 1.0))]
    bg = [dict(symbol="SPCXUSDT", base="SPCX", cls="preipo", factor=1.0), dict(symbol="NEWBONDUSDT", base="NEWBOND",
                                                                               cls="crypto", factor=1.0)]
    ff = universe.build_ff({"hyperliquid": hl, "bitget": bg, "backpack": ins})
    got = {r["key"]: (r["fa"], r["fb"]) for r in ff if "backpack:" in r["key"]}
    assert set(got) == {"hyperliquid:BTC|backpack:BTC_USDC_PERP", "hyperliquid:kBONK|backpack:kBONK_USDC_PERP",
                        "hyperliquid:PAXG|backpack:PAXG_USDC_PERP", "hyperliquid:xyz:NVDA|backpack:NVDA.US_USDC_PERP",
                        "hyperliquid:xyz:QQQ|backpack:QQQ.US_USDC_PERP",
                        # SKHY ≠ SKHYNIX; SPCX pairs as equity (review 13.09) — Bitget's pre-IPO SPCX stays apart (owner's call)
                        "hyperliquid:xyz:SPCX|backpack:SPCX.US_USDC_PERP"}
    assert got["hyperliquid:kBONK|backpack:kBONK_USDC_PERP"] == (1000.0, 1000.0)


def test_identity_is_oracle_only_and_the_declared_name_decides(monkeypatch):
    """Integrator's step (config_needed): «backpack» in identity.ORACLE_PERPS and in the collector's perp_names venues —
    the index composition is not public, so a pair is «не проверено: оракул площадки» unless the market's declared name
    (name_hint from /assets) matches the spot coin's; never by price."""
    monkeypatch.setattr(identity, "ORACLE_PERPS", identity.ORACLE_PERPS | {"backpack"})
    rec = lambda name, addr: identity.coin_record(name, [("SOL", addr, True, True)], "")
    # Backpack MET = «Meteora» (/assets displayName, coingecko «meteora») [L]; a spot «MET» may be another coin
    spots = {"gate_spot": {"coins": {"MET": rec("Metronome", "Met1" + "a" * 40), "METEORA": rec("Meteora", "Met2" + "b" * 40)},
                           "markets": {"MET_USDT": ["MET", True], "METEORA_USDT": ["METEORA", True]}}}
    sf = lambda spot: dict(key=f"gate_spot:{spot}|backpack:MET_USDC_PERP", base="MET", cls="crypto", spot_ex="gate_spot",
                           spot=spot, spot_factor=1.0, perp_ex="backpack", perp="MET_USDC_PERP", perp_factor=1.0)
    d = identity.decide_sf(identity.Resolver(spots, {}), sf("MET_USDT"))
    assert (d["ident"], d["ident_ev"]) == ("unknown", "oracle_only")             # no name → «не проверено», not «=»
    R = identity.Resolver(spots, {}, perp_names={"backpack": {"MET_USDC_PERP": "Meteora"}})
    d = identity.decide_sf(R, sf("MET_USDT"))
    assert (d["ident"], d["ident_ev"]) == ("unknown", "name_only")               # other name — but not «≠» by name alone
    d = identity.decide_sf(R, sf("METEORA_USDT"))
    assert (d["ident"], d["ident_ev"]) == ("same", "name")


@pytest.mark.skipif("backpack" not in config.PERP_VENUES, reason="the integrator has not wired backpack into config yet")
def test_wiring_once_integrated(monkeypatch):
    from funding_bot import dashboard
    from funding_bot.client import make_clients
    six = ("backpack", "variational", "edgex", "extended", "pacifica", "apex")
    order = [v for v in config.PERP_VENUES if v in six]
    assert order == [v for v in six if v in order]                                   # the owner's order among the six
    assert config.PERP_VENUES.index("backpack") == config.PERP_VENUES.index("lighter_rh") + 1
    assert config.FEES_TAKER["backpack"] == 0.0005 and config.FIXED_INTERVAL_H.get("backpack") == 1
    assert config.FUNDING_HISTORY_MIN_GAP_S.get("backpack") == 0.5
    assert "backpack" in identity.ORACLE_PERPS and "backpack" in dashboard.BRANDS
    monkeypatch.setattr(requests.Session, "request", _no_net)
    cl = make_clients()
    assert isinstance(cl["backpack"], BackpackFut) and cl["backpack"]._stream is None
