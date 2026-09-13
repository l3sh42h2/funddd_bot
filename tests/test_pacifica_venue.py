"""Pacifica on a fake HTTP session and fake WS connections (no network). Response shapes are trimmed copies of the
12-13.09 live captures (see the header of pacifica.py)."""
import json, logging, math, socket, time, types
import pytest
import requests
from funding_bot import calc, config, identity, pacifica, universe, venues
from funding_bot.client import BannedError, BudgetExceeded, PermanentHTTPError
from funding_bot.pacifica import PacificaPerp

H = 3600_000
NOW = 1789249652.0                    # 12.09.2026 21:47:32 UTC — the live capture
HOUR = 1789246800_000                 # 21:00 of that day, ms

# the 76 perpetuals of /info on 12.09.2026 21:47Z [L] (+ the spot SOL-USDC, not a perp)
LIVE_PERPS = ["2Z", "AAVE", "ADA", "ARB", "ASTER", "AVAX", "BCH", "BNB", "BP", "BTC", "CHIP", "CL", "COPPER", "CRCL", "CRV",
              "DOGE", "DRAM", "ENA", "ETH", "EURUSD", "FARTCOIN", "GOOGL", "HOOD", "HYPE", "ICP", "JUP", "KAITO", "LDO",
              "LINK", "LIT", "LTC", "MEGA", "MON", "MSTR", "MU", "NATGAS", "NEAR", "NVDA", "PAXG", "PENGU", "PIPPIN",
              "PLATINUM", "PLTR", "PONS", "PUMP", "SAMSUNG", "SKHYNIX", "SNDK", "SOL", "SP500", "SPCX", "STRK", "SUI", "TAO",
              "TRUMP", "TSLA", "UNI", "URNM", "USDJPY", "USELESS", "VIRTUAL", "VVV", "WIF", "WLD", "WLFI", "XAG", "XAU",
              "XMR", "XPL", "XRP", "ZEC", "ZK", "ZRO", "kBONK", "kPEPE", "kSHIB"]


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
        path = url.split("/api/v1", 1)[1]
        self.calls.append((path, dict(params or {})))
        return self.handler(path, dict(params or {}))


def ok(data, **kw):
    return {"success": True, "data": data, "error": None, "code": None, **kw}


@pytest.fixture(autouse=True)
def _fresh():
    pacifica._HOSTS.clear(); pacifica._WARNED.clear()
    yield
    pacifica._HOSTS.clear(); pacifica._WARNED.clear()


@pytest.fixture
def clock(monkeypatch):
    c = [NOW]

    def sleep(s):
        c[0] += max(0.0, s)
    monkeypatch.setattr(pacifica, "time", types.SimpleNamespace(time=lambda: c[0], sleep=sleep, gmtime=time.gmtime,
                                                                strftime=time.strftime))
    return c


# --- recorded shapes ---------------------------------------------------------------------------------------------------
BTC_INFO = {"symbol": "BTC", "tick_size": "1", "min_tick": "0", "max_tick": "1000000", "lot_size": "0.00001",
            "max_leverage": 50, "isolated_only": False, "min_order_size": "10", "max_order_size": "5000000",
            "funding_rate": "0.00001085", "next_funding_rate": "0.00000193", "created_at": 1748881333944,
            "instrument_type": "perpetual", "base_asset": "BTC", "execution_modes": ["orderbook"]}


def info(sym, created=1765000000000, itype="perpetual", tick="0.001", lot="0.01", lev=10):
    return dict(BTC_INFO, symbol=sym, tick_size=tick, lot_size=lot, max_leverage=lev, created_at=created,
                instrument_type=itype, base_asset=sym.split("-")[0], funding_rate="0.0000125", next_funding_rate="0.0000125")


INFO = [BTC_INFO, info("ETH", tick="0.1", lot="0.0001", lev=50), info("kBONK", tick="0.000001", lot="1"), info("kSHIB"),
        info("KAITO"), info("EURUSD"), info("USDJPY"), info("PLATINUM"), info("CL"), info("XAU"), info("PAXG"),
        info("SP500"), info("NVDA"), info("SKHYNIX"), info("URNM"), info("BP"), info("2Z"),
        info("SOL-USDC", itype="spot", lev=1), info("NEWTHING"), info("SOON", created=int(NOW * 1000) + 86400_000)]

BTC_PRICE = {"funding": "0.00001085", "mark": "77212.98", "mid": "77197.5", "next_funding": "0.00000188",
             "open_interest": "417.15943", "oracle": "77248.87592", "symbol": "BTC", "timestamp": 1789249651537,
             "volume_24h": "149455677.41006", "yesterday_price": "77282"}


def price(sym, nxt, funding="0.0000125", mark="1.0", oracle="1.0", ts=1789249651537):
    return dict(BTC_PRICE, symbol=sym, next_funding=nxt, funding=funding, mark=mark, mid=mark, oracle=oracle, timestamp=ts)


PRICES = [BTC_PRICE, price("ETH", "0.0000125", mark="2524.6", oracle="2524.93"),
          price("kBONK", "-0.00000054", funding="-0.00002032", mark="0.002788", oracle="0.00279"),
          price("SOL-USDC", "0", funding="0"), price("GONE", "0.0001"), price("BROKEN", None)]

# /funding_rate/history?symbol=BTC&limit=3, newest first [L]
LIVE_HIST = [{"oracle_price": "77169.732483", "bid_impact_price": "77127.354968", "ask_impact_price": "77134.588649",
              "funding_rate": "0.00001101", "next_funding_rate": "0.00001085", "created_at": 1789246800398},
             {"oracle_price": "77119.740008", "bid_impact_price": "77092", "ask_impact_price": "77093.756425",
              "funding_rate": "0.0000125", "next_funding_rate": "0.00001101", "created_at": 1789243200397},
             {"oracle_price": "77138.69653", "bid_impact_price": "77114.712835", "ask_impact_price": "77124",
              "funding_rate": "0.000012", "next_funding_rate": "0.0000125", "created_at": 1789239600397}]


def hrow(created, nxt, rate="0.0000125"):
    return {"oracle_price": "1", "bid_impact_price": "1", "ask_impact_price": "1", "funding_rate": rate,
            "next_funding_rate": nxt, "created_at": created}


def routes(info_rows=INFO, prices=PRICES, hist=None, headers=None):
    def h(path, p):
        hd = dict(headers or {})
        if path == "/info":
            return _R(ok(list(info_rows)), headers=hd)
        if path == "/info/prices":
            return _R(ok(list(prices)), headers=hd)
        if path == "/funding_rate/history" and hist is not None:
            r = hist(p)
            return r if isinstance(r, _R) else _R(r, headers=hd)
        raise AssertionError(path)
    return h


# --- universe ---------------------------------------------------------------------------------------------------------
def test_perp_instruments_fields_classes_bases_and_filters(clock, caplog):
    cl = PacificaPerp(session=_S(routes()))
    with caplog.at_level(logging.WARNING, logger="funding_bot.pacifica"):
        ins = {i["symbol"]: i for i in cl.perp_instruments()}
        cl.perp_instruments()
    assert "SOL-USDC" not in ins and "SOON" not in ins                  # a spot market; a listing not started yet
    got = {s: (i["cls"], i["base"], i["factor"]) for s, i in ins.items()}
    assert got == {"BTC": ("crypto", "BTC", 1.0), "ETH": ("crypto", "ETH", 1.0),
                   "kBONK": ("crypto", "BONK", 1000.0), "kSHIB": ("crypto", "SHIB", 1000.0),   # «k» = ×1000 [D]
                   "KAITO": ("crypto", "KAITO", 1.0),                                         # capital K is a name
                   "EURUSD": ("fx", "EUR", 1.0), "USDJPY": ("fx", "JPY", 1.0),                # as HL xyz:EUR / xyz:JPY
                   "PLATINUM": ("commodity", "XPT", 1.0), "CL": ("commodity", "CL", 1.0),
                   "XAU": ("commodity", "XAU", 1.0), "PAXG": ("crypto", "PAXG", 1.0),         # gold token = coin
                   "SP500": ("index", "SP500", 1.0), "NVDA": ("equity", "NVDA", 1.0),
                   "SKHYNIX": ("equity", "SKHYNIX", 1.0), "URNM": ("equity", "URNM", 1.0),    # ETF shares
                   "BP": ("crypto", "BP", 1.0), "2Z": ("crypto", "2Z", 1.0),
                   "NEWTHING": ("rwa", "NEWTHING", 1.0)}                                       # unclassified: no pairs
    b = ins["BTC"]
    assert (b["exchange"], b["interval_h"], b["quote"], b["contract"], b["min_notional"], b["onboard_ms"]) == \
        ("pacifica", 1, "USDC", "main", 10.0, 1748881333944)
    assert b["tick_size"] == 1.0 and b["step_size"] == pytest.approx(1e-5) and b["base_asset"] == "BTC"
    assert (b["cap"], b["floor"]) == (0.04, -0.04) and b["name_hint"] is None                 # ±4 %/h; no names in API
    assert b["url"] == "https://app.pacifica.fi/trade/BTC" and ins["kBONK"]["url"] == "https://app.pacifica.fi/trade/kBONK"
    assert ins["kBONK"]["tick_size"] == pytest.approx(1e-6) and ins["kBONK"]["step_size"] == 1.0
    assert sum("NEWTHING" in r.getMessage() for r in caplog.records) == 1                     # warned once, not hourly


def test_every_live_market_has_exactly_one_class():
    assert len(LIVE_PERPS) == 76
    assert [s for s in LIVE_PERPS if pacifica.perp_class(s) == "rwa"] == []
    tables = [t for _, t in pacifica.CLASSES]
    assert sum(len(t) for t in tables) == len(frozenset().union(*tables))                     # disjoint
    assert frozenset().union(*tables) == set(LIVE_PERPS)
    assert pacifica.perp_class("kbonk") == "rwa"                                               # case-sensitive symbols


# --- tick: rate, mark, index -------------------------------------------------------------------------------------------
def test_premium_takes_next_funding_per_hour_with_server_time(clock):
    s = _S(routes())
    cl = PacificaPerp(session=s)
    assert set(cl.premium()) == {"BTC", "ETH", "kBONK", "GONE"}         # no universe yet: spot «-» and rateless dropped
    cl.perp_instruments()
    p = cl.premium()
    assert set(p) == {"BTC", "ETH", "kBONK"}                             # universe known: only its perps
    b = p["BTC"]
    assert b["rate"] == pytest.approx(0.00000188)                        # next_funding (predicted), not 0.00001085 (paid)
    assert (b["mark"], b["index"], b["interval_h"]) == (77212.98, 77248.87592, 1)
    assert p["kBONK"]["rate"] == pytest.approx(-0.00000054)              # sign kept: shorts pay longs
    assert p["ETH"]["rate"] * 8 == pytest.approx(0.0001)                 # per HOUR: baseline 0.01 %/8 h ÷ 8, no conversion
    assert calc.hourly(p["ETH"]["rate"], p["ETH"]["interval_h"]) == p["ETH"]["rate"]
    now_ms = int(clock[0] * 1000)
    assert b["next_ms"] == (now_ms // H + 1) * H == HOUR + H
    assert b["obs"] == pytest.approx(1789249651.537) and b["ts_ms"] == 1789249651537      # server snapshot time
    assert [c[0] for c in s.calls] == ["/info/prices", "/info", "/info/prices"]           # one call per tick
    e = PacificaPerp(session=_S(routes(prices=[price("ETH", "0.0000125", ts=1500000000000)]))).premium()["ETH"]
    assert e["obs"] == clock[0]                                          # implausible server time → request start


# --- history ---------------------------------------------------------------------------------------------------------
def test_history_rate_is_next_funding_rate_of_the_row_and_ascending(clock):
    seen = []
    rows = LIVE_HIST + [dict(LIVE_HIST[0]), hrow(HOUR - 3 * H + 400, "0.00005"),              # duplicate; before start
                        hrow(None, "0.00001"), hrow(HOUR - 2 * H + 900, None)]               # broken rows
    cl = PacificaPerp(session=_S(routes(hist=lambda p: (seen.append(p), ok(rows, has_more=False))[1])), history_gap_s=0)
    start = HOUR - 2 * H
    out = cl.history_since("BTC", start, HOUR)
    assert [r["funding_ms"] for r in out] == [HOUR - 2 * H, HOUR - H, HOUR]                   # hour of created_at, ascending
    assert [r["rate"] for r in out] == [0.0000125, 0.00001101, 0.00001085]
    assert out[-1]["rate"] != float(LIVE_HIST[0]["funding_rate"])      # 21:00 paid 0.00001085 — funding_rate is 20:00's
    assert all(r["mark"] is None and r["exchange"] == "pacifica" and r["symbol"] == "BTC" for r in out)
    assert seen[-1] == {"symbol": "BTC", "limit": math.ceil((NOW * 1000 - start) / H) + 3}     # no time params: ignored
    assert [r["funding_ms"] for r in cl.history_since("BTC", start, HOUR - H)] == [HOUR - 2 * H, HOUR - H]
    assert cl.history_since("BTC", HOUR, HOUR - H) == [] and cl.recent_history() == []


def test_history_pages_back_with_the_cursor(clock):
    newest = [HOUR - k * H + 400 for k in range(9000)]

    def hist(p):
        i, lim = int(p.get("cursor") or 0), min(int(p["limit"]), 4000)
        more = i + lim < len(newest)
        body = ok([hrow(t, "0.00001") for t in newest[i:i + lim]], has_more=more)
        if more:
            body["next_cursor"] = str(i + lim)
        return body
    s = _S(routes(hist=hist))
    cl = PacificaPerp(session=s, history_gap_s=0)
    start = HOUR - 8500 * H
    out = cl.history_since("BTC", start, HOUR)
    assert [r["funding_ms"] for r in out] == [start + k * H for k in range(8501)]
    calls = [c[1] for c in s.calls if c[0] == "/funding_rate/history"]
    assert [c.get("cursor") for c in calls] == [None, "4000", "8000"] and {c["limit"] for c in calls} == {4000}
    n = len(calls)
    month = cl.history_since("BTC", HOUR - 720 * H, HOUR)                                     # the backfill window
    assert len(month) == 721 and len([c for c in s.calls if c[0] == "/funding_rate/history"]) == n + 1


def test_history_never_truncates_silently(clock):
    def endless(p):
        i = int(p.get("cursor") or 0)
        return ok([hrow(HOUR - (i + k) * H + 400, "0.00001") for k in range(10)], has_more=True, next_cursor=str(i + 10))
    with pytest.raises(RuntimeError, match="did not reach"):
        PacificaPerp(session=_S(routes(hist=endless)), history_gap_s=0).history_since("BTC", HOUR - 1000 * H, HOUR)
    with pytest.raises(RuntimeError, match="next_cursor"):
        PacificaPerp(session=_S(routes(hist=lambda p: ok([hrow(HOUR + 400, "0.00001")], has_more=True))),
                     history_gap_s=0).history_since("BTC", HOUR - 10 * H, HOUR)


def test_empty_answer_is_not_a_confirmation(clock):
    rows = [BTC_INFO, info("PONS", created=1788220800000), info("FRESH", created=int(NOW * 1000) - 600_000)]
    s = _S(routes(info_rows=rows, hist=lambda p: ok([], has_more=False)))
    cl = PacificaPerp(session=s, history_gap_s=0)
    with pytest.raises(RuntimeError, match="empty history"):
        cl.history_since("PONS", HOUR - 24 * H, HOUR)          # listed 31.08: it has settled — the API's [] proves nothing
    assert cl.history_since("FRESH", HOUR - 24 * H, HOUR) == []  # listed 10 min ago: nothing settled yet
    assert [c[0] for c in s.calls] == ["/info", "/funding_rate/history", "/funding_rate/history"]   # list learnt once
    clock[0] += pacifica.UNIVERSE_TTL_S
    with pytest.raises(RuntimeError, match="no market NOPE"):
        cl.history_since("NOPE", 0, HOUR)                       # unknown: refreshes the list once, then refuses
    n = len(s.calls)
    with pytest.raises(RuntimeError, match="no market btc"):
        cl.history_since("btc", 0, HOUR)                        # wrong case (the API answers [] too); list is fresh
    assert len(s.calls) == n and s.calls[-1][0] == "/info"


# --- transport: pace, credits, 429, 4xx ----------------------------------------------------------------------------------
def test_history_is_paced_across_copies_of_the_client(clock):
    s = _S(routes(hist=lambda p: ok(LIVE_HIST, has_more=False)))
    a, b = PacificaPerp(session=s), PacificaPerp(session=s)    # the collector's background copy shares the host
    assert a.history_gap_s == 7.5 == config.FUNDING_HISTORY_MIN_GAP_S.get("pacifica", 7.5)
    t = []
    for cl in (a, b, a):
        cl.history_since("BTC", HOUR - 2 * H, HOUR)
        t.append(clock[0])
    assert t[1] - t[0] == pytest.approx(7.5) and t[2] - t[1] == pytest.approx(7.5)


def test_credit_window_waits_history_and_refuses_the_tick(clock):
    state = {"hd": {"RateLimit": '"credits";r=150;t=20', "RateLimit-Policy": '"credits";q=1000;w=60'}}

    def h(path, p):
        body = ok(INFO) if path == "/info" else ok(PRICES) if path == "/info/prices" else ok(LIVE_HIST, has_more=False)
        return _R(body, headers=state["hd"])
    s = _S(h)
    cl = PacificaPerp(session=s, history_gap_s=0)
    cl.perp_instruments()                                       # 150 left, the window resets in 20 s
    assert cl.budget_used() == pytest.approx(0.85) and cl.health()["used_weight"] == 850
    t0 = clock[0]
    cl.history_since("BTC", HOUR - 2 * H, HOUR)                 # < 200 left: history waits for the reset
    assert clock[0] - t0 == pytest.approx(20)
    state["hd"] = {"ratelimit": '"credits";r=10;t=45'}
    cl.premium()
    n = len(s.calls)
    with pytest.raises(BudgetExceeded):
        cl.premium()                                            # the tick never waits: the next tick retries
    assert len(s.calls) == n
    clock[0] += 46                                              # that window has reset
    state["hd"] = {"ratelimit": '"credits";r=100;t=200'}
    cl.premium()
    with pytest.raises(BudgetExceeded):
        cl.history_since("BTC", HOUR - 2 * H, HOUR)             # reset further than HIST_MAX_WAIT_S: skipped this pass
    assert len(s.calls) == n + 1                                # the refused history call made no request
    clock[0] += 201
    assert cl.budget_used() == 0.0 and cl.health()["used_weight"] == 0


def test_429_pauses_the_host_for_every_client(clock):
    s = _S(lambda path, p: _R({"success": False, "data": None, "error": "rate limited", "code": 429}, 429,
                              {"Retry-After": "7"}))
    cl = PacificaPerp(session=s)
    with pytest.raises(BannedError):
        cl.premium()
    assert cl.banned_until == pytest.approx(clock[0] + 7) and cl.n_429 == 1 and not cl.budget_ok()
    assert cl.health()["banned_until"] == int(clock[0] + 7)
    with pytest.raises(BannedError):
        PacificaPerp(session=s).history_since("BTC", 0, 1)      # background copy: same pause, no request
    assert len(s.calls) == 1
    clock[0] += 8
    assert cl.budget_ok()
    s2 = _S(lambda path, p: _R({}, 429))
    with pytest.raises(BannedError):
        PacificaPerp(session=s2).perp_instruments()
    assert cl.banned_until == pytest.approx(clock[0] + 60)      # no Retry-After → 60 s [A]


def test_4xx_is_permanent_and_a_failed_envelope_retries(clock):
    s = _S(lambda path, p: _R({"success": False, "data": None, "error": "missing field `symbol`", "code": 400}, 400))
    with pytest.raises(PermanentHTTPError, match="400"):
        PacificaPerp(session=s).perp_instruments()
    assert len(s.calls) == 1
    s2 = _S(lambda path, p: _R({"success": False, "data": None, "error": "internal", "code": 500}))
    cl = PacificaPerp(session=s2)
    with pytest.raises(RuntimeError, match="internal"):
        cl.perp_instruments()
    assert len(s2.calls) == 3 and cl.n_err == 3
    s3 = _S(lambda path, p: _R({}, 503))
    with pytest.raises(RuntimeError):
        PacificaPerp(session=s3).premium()
    assert len(s3.calls) == config.TICK_RETRIES                 # tick: one try


# --- books via the WS stream --------------------------------------------------------------------------------------------
def bbo(sym, b, a, B="1", A="2", t=1789249861031):
    return {"channel": "bbo", "data": {"s": sym, "i": 12616006958, "li": 12616006970, "t": t, "b": b, "B": B, "a": a, "A": A}}


PRICES_FRAME = {"channel": "prices", "data": [{"symbol": "AVAX", "funding": "0.0000125", "next_funding": "0.0000125",
                                                "oracle": "7.40095", "mark": "7.396225", "mid": "7.391",
                                                "yesterday_price": "7.442", "open_interest": "39912.88",
                                                "volume_24h": "3206.4858", "timestamp": 1789249858539}]}


def test_stream_cache_books_and_obs_across_reconnects():
    st = pacifica._BboStream("wss://x/ws", "pacifica")
    st.conn_id = 1
    assert st.apply(bbo("BTC", "77184", "77185", "0.06098", "0.19775"), 100.0)
    assert st.apply(bbo("kBONK", "0.002781", "0.002784", "536289", "97262"), 101.0)
    assert st.apply(bbo("WIDE", "1.0", "1.2"), 101.0) and st.apply(bbo("CROSS", "2", "1"), 101.0)
    assert not st.apply({"channel": "subscribe", "data": {"source": "bbo", "symbol": "BTC"}}, 102.0)
    assert not st.apply({"channel": "pong"}, 102.0) and st.last_rx == 101.0          # acks and pongs are not data
    assert st.apply(PRICES_FRAME, 104.0) and st.last_rx == 104.0                     # the 3 s heartbeat
    b = st.books()
    assert set(b) == {"BTC", "kBONK"}                                                 # 18 % spread, crossed — not prices
    assert b["BTC"] == dict(bid=77184.0, ask=77185.0, bid_qty=0.06098, ask_qty=0.19775, obs=104.0)
    assert b["kBONK"]["obs"] == 104.0 and b["kBONK"]["bid_qty"] == 536289.0          # quiet market, live connection
    st.apply(bbo("BTC", "77000", "77001", t=1789249861000), 105.0)                    # an older exchange time never wins
    assert st.books()["BTC"]["bid"] == 77184.0
    st.conn_id = 2                                            # reconnected: no snapshot re-sends the quiet markets
    assert st.books()["BTC"]["obs"] == 100.0                  # its own last update — a missed change must not look fresh
    st.apply(bbo("BTC", "77190", "77191", t=1789249870000), 110.0)
    assert st.books({"BTC"}) == {"BTC": dict(bid=77190.0, ask=77191.0, bid_qty=1.0, ask_qty=2.0, obs=110.0)}
    assert st.books()["kBONK"]["obs"] == 101.0


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


def _sub(source, symbol=None):
    return {"method": "subscribe", "params": dict({"source": source}, **({"symbol": symbol} if symbol else {}))}


def test_stream_subscribes_new_symbols_pings_and_reconnects(clock):
    st = pacifica._BboStream("wss://ws.pacifica.fi/ws", "pacifica")
    st.want(["ETH", "BTC"])
    conns = [_Conn([PRICES_FRAME, bbo("BTC", "77184", "77185"), 3, PRICES_FRAME,
                    lambda: st.want(["BTC", "ETH", "kBONK"]), 20, PRICES_FRAME, 10, PRICES_FRAME], clock),
             _Conn([bbo("ETH", "2522.6", "2522.7"), "close"], clock)]
    made = []

    def connect(url, timeout):
        made.append((url, timeout))
        return conns[len(made) - 1]
    st._connect = connect
    st.backoff0 = 0.0
    st._run(max_sessions=2)
    assert made == [("wss://ws.pacifica.fi/ws", pacifica.WS_RECV_TIMEOUT_S)] * 2 and st.n_conn == 2
    assert conns[0].sent[:3] == [_sub("prices"), _sub("bbo", "BTC"), _sub("bbo", "ETH")]
    assert _sub("bbo", "kBONK") in conns[0].sent                          # a new market joins the live connection
    assert {"method": "ping"} in conns[0].sent                           # keepalive within the 60 s idle limit
    assert conns[1].sent == [_sub("prices"), _sub("bbo", "BTC"), _sub("bbo", "ETH"), _sub("bbo", "kBONK")]
    assert all(c.closed for c in conns) and "closed by server" in st.err and not st.synced.is_set()
    b = st.books()
    assert set(b) == {"BTC", "ETH"}                                      # the cache survived the reconnect
    assert b["BTC"]["obs"] == NOW and b["ETH"]["obs"] > NOW              # BTC from the first connection: own time


def test_client_books_follow_the_universe(clock):
    cl = PacificaPerp(session=_S(routes()))
    cl.perp_instruments()
    st = pacifica._BboStream("wss://x", "pacifica")
    st.start, st._waited, st.conn_id = (lambda: None), True, 1
    cl._stream = st
    st.apply(bbo("BTC", "77184", "77185"), 100.0)
    st.apply(bbo("GONE", "1", "1.001"), 100.0)                           # delisted since: not in the universe
    assert set(cl.books()) == {"BTC"}
    assert st._want == set(cl._h.markets) and "SOL-USDC" not in st._want and "kBONK" in st._want


# --- interface, pairs, identity -------------------------------------------------------------------------------------------
def _no_net(*a, **k):
    raise AssertionError("network in a client constructor")


def test_interface_shape_and_pairs_with_other_venues(clock, monkeypatch):
    monkeypatch.setattr(requests.Session, "request", _no_net)
    cl = PacificaPerp()
    assert venues.native(cl) and not venues.spot_native(cl) and cl.name == "pacifica" and cl._stream is None
    assert venues.funding_intervals(cl) is None and venues.recent_history(cl) == []      # interval fixed at 1 h
    assert not hasattr(cl, "index_legs") and not hasattr(cl, "coin_blob")
    assert pacifica.PERP_CLIENTS == {"pacifica": PacificaPerp}
    monkeypatch.setattr(config, "PERP_VENUES", ("hyperliquid", "lighter", "pacifica"))
    ins = PacificaPerp(session=_S(routes())).perp_instruments()
    hl = [dict(symbol=s, base=b, cls=c, factor=f) for s, b, c, f in (
        ("BTC", "BTC", "crypto", 1.0), ("kBONK", "BONK", "crypto", 1000.0), ("PAXG", "PAXG", "crypto", 1.0),
        ("xyz:PLATINUM", "XPT", "commodity", 1.0), ("xyz:GOLD", "XAU", "commodity", 1.0), ("xyz:CL", "CL", "commodity", 1.0),
        ("xyz:JPY", "JPY", "fx", 1.0), ("xyz:EUR", "EUR", "fx", 1.0), ("xyz:SP500", "SP500", "index", 1.0),
        ("xyz:SKHX", "SKHYNIX", "equity", 1.0), ("xyz:NVDA", "NVDA", "equity", 1.0))]
    li = [dict(symbol="US500", base="SP500", cls="index", factor=1.0), dict(symbol="1000BONK", base="BONK", cls="crypto",
                                                                             factor=1000.0),
          dict(symbol="NEWTHING", base="NEWTHING", cls="crypto", factor=1.0)]
    ff = universe.build_ff({"hyperliquid": hl, "lighter": li, "pacifica": ins})
    got = {r["key"]: (r["fa"], r["fb"]) for r in ff if "pacifica:" in r["key"]}
    assert set(got) == {"hyperliquid:BTC|pacifica:BTC", "hyperliquid:kBONK|pacifica:kBONK", "hyperliquid:PAXG|pacifica:PAXG",
                        "hyperliquid:xyz:PLATINUM|pacifica:PLATINUM", "hyperliquid:xyz:GOLD|pacifica:XAU",
                        "hyperliquid:xyz:CL|pacifica:CL", "hyperliquid:xyz:JPY|pacifica:USDJPY",
                        "hyperliquid:xyz:EUR|pacifica:EURUSD", "hyperliquid:xyz:SP500|pacifica:SP500",
                        "hyperliquid:xyz:SKHX|pacifica:SKHYNIX", "hyperliquid:xyz:NVDA|pacifica:NVDA",
                        "lighter:US500|pacifica:SP500", "lighter:1000BONK|pacifica:kBONK"}   # NEWTHING «rwa»: no pair
    assert got["lighter:1000BONK|pacifica:kBONK"] == (1000.0, 1000.0)


def test_identity_is_oracle_only_once_listed_as_oracle_venue(monkeypatch):
    """Integrator's step (config_needed): «pacifica» in identity.ORACLE_PERPS — no index composition, contracts or names
    in the API, so a pair is «не проверено: оракул площадки», never decided by price."""
    monkeypatch.setattr(identity, "ORACLE_PERPS", identity.ORACLE_PERPS | {"pacifica"})
    spot = {"gate_spot": {"coins": {"BTC": identity.coin_record("Bitcoin", [("BTC", "", True, True)], "BTC")},
                          "markets": {"BTC_USDT": ["BTC", True]}}}
    d = identity.decide_sf(identity.Resolver(spot, {}), dict(key="gate_spot:BTC_USDT|pacifica:BTC", base="BTC",
                                                             cls="crypto", spot_ex="gate_spot", spot="BTC_USDT",
                                                             spot_factor=1.0, perp_ex="pacifica", perp="BTC",
                                                             perp_factor=1.0))
    assert (d["ident"], d["ident_ev"]) == ("unknown", "oracle_only")


@pytest.mark.skipif("pacifica" not in config.PERP_VENUES, reason="the integrator has not wired pacifica into config yet")
def test_wiring_once_integrated(monkeypatch):
    from funding_bot import dashboard
    from funding_bot.client import make_clients
    six = ("backpack", "variational", "edgex", "extended", "pacifica", "apex")
    order = [v for v in config.PERP_VENUES if v in six]
    assert order == [v for v in six if v in order]                                   # the owner's order among the six
    assert config.PERP_VENUES.index("pacifica") > config.PERP_VENUES.index("lighter_rh")
    assert config.FEES_TAKER["pacifica"] == 0.0004 and "pacifica" not in config.FIXED_INTERVAL_H
    assert config.FUNDING_HISTORY_MIN_GAP_S.get("pacifica") == 7.5
    assert "pacifica" in identity.ORACLE_PERPS and "pacifica" in dashboard.BRANDS
    monkeypatch.setattr(requests.Session, "request", _no_net)
    cl = make_clients()
    assert isinstance(cl["pacifica"], PacificaPerp) and cl["pacifica"]._stream is None
