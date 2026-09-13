"""Variational Omni on a fake HTTP session — no network. Listing shapes are trimmed copies of the 12.09 21:34 UTC live
capture (see the header of variational.py): BTC, 1000PEPE, STORJ, WIF, AAPL, XAU, OPENAI, US500S, NBIS, TWT, 1NEIRO,
1000000MOG, US500 carry the captured strings; marks / quotes of JPM, QNTX, CAT, SPX, 1000BONK, OPN_OPINION, B3, WIDE,
CROSSED are shape-only placeholders."""
import time, types
from datetime import datetime, timezone
from email.utils import formatdate
import pytest
from funding_bot import config, db, funding, universe, venues, variational
from funding_bot.client import BannedError, BudgetExceeded, PermanentHTTPError
from funding_bot.variational import NoHistoryError, Variational

H = 3600
T_2134 = 1789248840.0                     # 2026-09-12 21:34:00 UTC
T_2200 = 1789250400.0                     # 2026-09-12 22:00:00 UTC
T_0000 = 1789257600.0                     # 2026-09-13 00:00:00 UTC


class _R:
    def __init__(self, body, code=200, headers=None):
        self.status_code, self._b, self.headers = code, body, dict(headers or {})
        self.text = str(body)

    def json(self):
        if isinstance(self._b, str):
            raise ValueError("not JSON")                  # the Cloudflare challenge page
        return self._b

    def raise_for_status(self):
        if self.status_code >= 500:
            raise RuntimeError(str(self.status_code))


class _S:
    def __init__(self, handler):
        self.handler, self.calls, self.headers = handler, [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, timeout))
        return self.handler(len(self.calls) - 1)


@pytest.fixture(autouse=True)
def _fresh_hosts():
    variational._HOSTS.clear()
    yield
    variational._HOSTS.clear()


@pytest.fixture
def clock(monkeypatch):
    c = [T_2134]

    def sleep(s):
        c[0] += max(0.0, s)
    monkeypatch.setattr(variational, "time", types.SimpleNamespace(time=lambda: c[0], sleep=sleep, gmtime=time.gmtime,
                                                                   strftime=time.strftime))
    return c


# --- recorded shapes ---------------------------------------------------------------------------------------------------
def L(ticker, name, rate, iv_s, mark="1.0", k1=None, upd="2026-09-12T21:33:59.138556019Z"):
    x = {"ticker": ticker, "name": name, "mark_price": mark, "volume_24h": "1000.0",
         "open_interest": {"long_open_interest": "1.0", "short_open_interest": "1.0"},
         "funding_rate": rate, "funding_interval_s": iv_s, "base_spread_bps": "5.0"}
    if k1:
        x["quotes"] = {"updated_at": upd, "base": {"bid": k1[0], "ask": k1[1]}, "size_1k": {"bid": k1[0], "ask": k1[1]},
                       "size_100k": {"bid": k1[0], "ask": k1[1]}}
    return x


LISTINGS = [
    L("BTC", "Bitcoin", "0.056431", 28800, "77174.5054022029", ("77190.43", "77199.13")),
    L("1000PEPE", "Pepe", "0.033609", 28800, "0.003406312652804095", ("0.003402", "0.003407"),
      "2026-09-12T21:33:26.055913041Z"),
    L("STORJ", "Storj", "-93.621410", 3600, "0.04161771102589357", ("0.04221", "0.04235"),
      "2026-09-12T21:34:09.808991382Z"),
    L("WIF", "dogwifhat", "-0.119175", 14400, "0.1906395148266845", ("0.1903", "0.1907")),
    L("AAPL", "Apple Inc.", "0", 28800, "333.565", ("333.455", "333.591")),
    L("XAU", "Gold", "0", 14400, "4356.455394953277", ("4355.65", "4357")),
    L("OPENAI", "OpenAI", "0.05475", 28800, "1482.478463684497", ("1482.1", "1485.12")),
    L("US500S", "Swap on US 500", "0", 0, "7655.45"),                                   # swap: interval 0, no quotes
    L("NBIS", "Nebius Group N.V.", "-0.153403", 28800, "222.8077455739934", ("222.683", "222.924")),
    L("JPM", "JPMorgan Chase & Co.", "0", 28800, "300.0", ("299.9", "300.1")),
    L("QNTX", "Quantinuum Inc. Class A Common Stock", "0", 28800, "50.0", ("49.9", "50.1")),
    L("CAT", "Caterpillar Inc.", "0", 28800, "400.0", ("399.9", "400.1")),
    L("US500", "State Street SPDR S&P 500 ETF Trust", "0", 28800, "765.742884141372", ("765.6", "765.891")),
    L("TWT", "Trust Wallet", "0.1095", 14400, "0.537370444625546", ("0.537", "0.5383")),
    L("SPX", "SPX6900", "0.109500", 14400, "0.5", ("0.499", "0.501")),
    L("1NEIRO", "Neiro", "-0.254400", 14400, "0.0000875701836398998", ("0.00008759", "0.00008775")),
    L("1000000MOG", "Mog Coin", "0.1095", 14400, "0.1060441675707836", ("0.1056", "0.1064")),
    L("1000BONK", "1000BONK", "0.1095", 14400, "0.0171", ("0.0170", "0.0172")),
    L("OPN_OPINION", "Opinion", "0.1095", 14400, "0.2", ("0.199", "0.201")),
    L("B3", "B3 (Base)", "0.109500", 3600, "0.000491", ("0.00049", "0.000492")),
    L("WIDE", "Wide Coin", "0.1095", 14400, "1.0", ("1.0", "1.2")),                     # 18 % wide — not a price
    L("CROSSED", "Crossed Coin", "0.1095", 14400, "1.0", ("1.01", "1.0")),
]


def body(rows=LISTINGS):
    return {"total_volume_24h": "1", "tvl": "1", "open_interest": "1", "num_markets": len(rows), "listings": list(rows)}


def hdr(lm_ts, age="0"):
    return {"last-modified": formatdate(lm_ts, usegmt=True), "age": age, "cache-control": "public, s-maxage=60, max-age=30"}


def fixed(rows=LISTINGS, lm_ts=T_2134 - 49, age="49"):
    return lambda n: _R(body(rows), headers=hdr(lm_ts, age))


def live(clock, state):
    """Each call: the rows in state["rows"], last-modified = state["lm"] (or now − 50 s)."""
    return lambda n: _R(body(state.get("rows", LISTINGS)), headers=hdr(state.get("lm") or clock[0] - 50))


# --- rates -------------------------------------------------------------------------------------------------------------
def test_premium_converts_the_annualised_rate_per_interval(clock):
    s = _S(fixed())
    p = Variational(session=s).premium()
    assert p["BTC"]["rate"] == pytest.approx(0.056431 * 8 / 8760)
    assert p["BTC"]["rate"] == pytest.approx(5.1535e-5, rel=1e-4)                 # Binance 4.86e-5, Bybit 4.75e-5 [L]
    assert p["STORJ"]["rate"] == pytest.approx(-93.62141 / 8760)                    # −1.07 % per 1 h
    assert p["WIF"]["rate"] == pytest.approx(-0.119175 * 4 / 8760)
    assert p["OPENAI"]["rate"] == pytest.approx(5e-5)                               # the doc's fixed 0.005 %/8 h
    assert p["AAPL"]["rate"] == 0.0 and "US500S" not in p                           # swap: interval 0
    apr = {x["ticker"]: float(x["funding_rate"]) for x in LISTINGS}
    for t, r in p.items():                                                          # per hour = annual / 8760, always
        assert r["rate"] / r["interval_h"] == pytest.approx(apr[t] / 8760)
    assert {t: p[t]["interval_h"] for t in ("BTC", "WIF", "STORJ", "XAU")} == {"BTC": 8, "WIF": 4, "STORJ": 1, "XAU": 4}
    assert p["BTC"]["next_ms"] == p["WIF"]["next_ms"] == int(T_0000 * 1000)         # UTC grid of the interval
    assert p["STORJ"]["next_ms"] == int(T_2200 * 1000)
    b = p["BTC"]
    assert (b["mark"], b["index"]) == (77174.5054022029, None)                      # the oracle index is not public
    assert b["obs"] == T_2134 - 49 and b["ts_ms"] == int((T_2134 - 49) * 1000)      # the snapshot's time, not arrival
    assert s.calls == [(variational.STATS_URL, config.TICK_HTTP_TIMEOUT)]


def test_per_interval_and_interval_hours():
    assert variational.per_interval(0.1095, 1) == pytest.approx(0.0000125)          # 0.00125 %/h interest [D]
    assert variational.per_interval(0.05475, 8) == pytest.approx(0.00005)
    assert [variational.interval_h(s) for s in (3600, 14400, 28800, 0, None, "x")] == [1, 4, 8, None, None, None]


# --- universe ----------------------------------------------------------------------------------------------------------
def test_instruments_classes_bases_names_and_links(clock):
    ins = {i["symbol"]: i for i in Variational(session=_S(fixed())).perp_instruments()}
    got = {s: (i["cls"], i["base"], i["factor"]) for s, i in ins.items()}
    assert got == {"BTC": ("crypto", "BTC", 1.0), "1000PEPE": ("crypto", "PEPE", 1000.0), "STORJ": ("crypto", "STORJ", 1.0),
                   "WIF": ("crypto", "WIF", 1.0), "AAPL": ("equity", "AAPL", 1.0), "XAU": ("commodity", "XAU", 1.0),
                   "OPENAI": ("preipo", "OPENAI", 1.0), "NBIS": ("equity", "NBIS", 1.0),              # «N.V.»
                   "JPM": ("equity", "JPM", 1.0),                                  # «& Co.» — no \b before «&»
                   "QNTX": ("equity", "QNT", 1.0),                                 # config.PERP_CANON: Quantinuum
                   "CAT": ("equity", "CAT", 1.0),                                  # Caterpillar Inc., not Simons Cat
                   "US500": ("equity", "US500", 1.0),                              # the SPY share, not the S&P index
                   "TWT": ("crypto", "TWT", 1.0), "SPX": ("crypto", "SPX", 1.0),   # Trust Wallet, SPX6900 — coins
                   "1NEIRO": ("crypto", "NEIRO", 1.0), "OPN_OPINION": ("crypto", "OPN", 1.0),
                   "1000000MOG": ("crypto", "MOG", 1e6), "1000BONK": ("crypto", "BONK", 1000.0),
                   "B3": ("crypto", "B3", 1.0),                                    # 13.09: сверен — монета B3 (Base)
                   "WIDE": ("crypto", "WIDE", 1.0), "CROSSED": ("crypto", "CROSSED", 1.0)}
    assert "US500S" not in ins                                                      # swap — not a perp
    b = ins["BTC"]
    assert (b["exchange"], b["base_asset"], b["interval_h"], b["quote"], b["contract"]) == \
        ("variational", "BTC", 8, "USDC", "variational")
    assert (b["tick_size"], b["step_size"], b["min_notional"], b["onboard_ms"]) == (None, None, None, 0)
    assert b["cap"] == pytest.approx(0.16) and b["floor"] == pytest.approx(-0.16)    # 2 %/h × 8 h
    assert ins["STORJ"]["cap"] == pytest.approx(0.02)
    assert b["url"] == "https://omni.variational.io/perpetual/BTC"
    assert ins["OPN_OPINION"]["url"] == "https://omni.variational.io/perpetual/OPN_OPINION"
    names = {s: ins[s]["name_hint"] for s in ("BTC", "CAT", "1000000MOG", "1000BONK", "1NEIRO", "JPM")}
    assert names == {"BTC": "Bitcoin", "CAT": "Caterpillar Inc.", "1000000MOG": "Mog Coin", "JPM": "JPMorgan Chase & Co.",
                     "1000BONK": None,                                             # the «name» is the ticker
                     "1NEIRO": None}                                               # «Neiro» = the base (identity._tickerish)


def test_rate_evidence_downgrades_a_name_that_disagrees(clock):
    ac = variational.asset_class
    assert ac("BTC", "Bitcoin", 28800) == "crypto"                                  # no exact rate seen: the name rules
    assert ac("MYST", "Mystery", 28800, n_zero=3) == "rwa"                          # coin name, TradFi-shaped rate
    assert ac("ACME", "Acme Holdings", 28800, n_crypto=1) == "rwa"                  # equity name, crypto interest
    assert ac("XAU", "Gold", 14400, n_zero=5) == "commodity" and ac("AAPL", "Apple Inc.", 28800, n_zero=2) == "equity"
    assert ac("TWT", "Trust Wallet", 14400, n_zero=1, n_crypto=9) == "rwa"          # both seen: a conflict
    assert ac("OPENAI", "OpenAI", 28800, n_crypto=1) == "preipo"                    # fixed rate: no cross-check
    assert ac("US500S", "Swap on US 500", 0) is None and ac("XAUS", "Swap on Gold Spot", 3600) is None
    state = {"rows": [L("MYST", "Mystery", "0", 28800, "2.0", ("1.99", "2.01")),
                      L("ACME", "Acme Holdings", "0.1095", 28800, "9.0", ("8.99", "9.01")),
                      L("COIN2", "Second Coin", "0.1095", 14400, "1.0", ("0.99", "1.01"))]}
    cl = Variational(session=_S(live(clock, state)))
    assert {i["symbol"]: i["cls"] for i in cl.perp_instruments()} == {"MYST": "rwa", "ACME": "rwa", "COIN2": "crypto"}
    clock[0] += 61
    state["rows"] = [L("MYST", "Mystery", "0.1095", 28800, "2.0"), L("COIN2", "Second Coin", "0", 14400, "1.0")]
    assert {i["symbol"]: i["cls"] for i in cl.perp_instruments()} == {"MYST": "rwa", "COIN2": "rwa"}   # sticky
    assert variational._host(variational.STATS_URL).ev["MYST"] == [1, 1]


def test_premium_carries_the_round_trip_cost_at_the_quote(clock):
    """Review 13.09: no trading fee on Omni — the cost is the RFQ quote spread. spread_rt = the FULL 1k spread / mid (buy at
    ask, sell at bid), not cut at SPREAD_MAX (a wide quote is a real cost); no usable 1k quote → base_spread_bps / 1e4."""
    p = Variational(session=_S(fixed())).premium()
    assert p["BTC"]["spread_rt"] == pytest.approx(8.7 / ((77190.43 + 77199.13) / 2))           # 1.13 bps, as live
    assert p["1000PEPE"]["spread_rt"] == pytest.approx(0.000005 / 0.0034045)                     # 14.7 bps
    assert p["WIDE"]["spread_rt"] == pytest.approx(0.2 / 1.1)                                    # no book, still a cost
    assert p["CROSSED"]["spread_rt"] == pytest.approx(5.0e-4)                                    # base_spread_bps "5.0"
    assert variational.quote_rt({"ticker": "X"}) is None                                         # neither → «—»
    assert variational.quote_rt({"base_spread_bps": "-1"}) is None


# --- books -------------------------------------------------------------------------------------------------------------
def test_books_from_the_1k_quote_with_nanosecond_obs(clock):
    s = _S(fixed())
    cl = Variational(session=s)
    cl.premium()
    b = cl.books()
    assert len(s.calls) == 1                                                        # the tick's one request
    assert set(b) == {x["ticker"] for x in LISTINGS} - {"US500S", "WIDE", "CROSSED"}  # no quotes / 18 % / crossed
    btc = b["BTC"]
    assert (btc["bid"], btc["ask"]) == (77190.43, 77199.13)
    assert btc["bid_qty"] == pytest.approx(1000 / 77190.43) and btc["ask_qty"] == pytest.approx(1000 / 77199.13)
    assert btc["obs"] == pytest.approx(datetime(2026, 9, 12, 21, 33, 59, 138556, tzinfo=timezone.utc).timestamp())
    assert b["STORJ"]["obs"] == T_2134 - 49                                         # updated_at after the snapshot → capped
    assert variational.iso_ts("2026-09-12T21:33:59Z") == datetime(2026, 9, 12, 21, 33, 59, tzinfo=timezone.utc).timestamp()
    assert variational.iso_ts("2026-09-12T21:33:59.1+00:00") == pytest.approx(
        datetime(2026, 9, 12, 21, 33, 59, 100000, tzinfo=timezone.utc).timestamp())
    assert variational.iso_ts("yesterday") is None and variational.iso_ts(None) is None


def test_snapshot_time_takes_the_older_of_last_modified_and_age():
    st = variational.snapshot_time
    assert st(hdr(100_000, "5"), 100_060, 100_061) == 100_000                       # last-modified older
    assert st({"last-modified": formatdate(100_050, usegmt=True), "age": "31"}, 100_060, 100_061) == 100_029   # 22:23 [L]
    assert st({"Age": "51"}, 100_060, 100_061) == 100_009                           # no last-modified; case-insensitive
    assert st({"last-modified": formatdate(200_000, usegmt=True)}, 100_060, 100_061) == 100_060   # future: ignored
    assert st({"last-modified": "garbage", "age": "x"}, 100_060, 100_061) == 100_060


# --- budget --------------------------------------------------------------------------------------------------------------
def test_one_request_per_tick_shared_by_every_call_and_client(clock):
    s = _S(live(clock, {}))
    cl = Variational(session=s)
    cl.premium(); cl.books()
    iv = cl.funding_intervals()
    cl.perp_instruments()
    assert len(s.calls) == 1
    assert (iv["BTC"], iv["STORJ"], iv["WIF"], "US500S" in iv) == (8, 1, 4, False)
    Variational(session=s).books()                       # the collector's background copy: same host, same snapshot
    assert len(s.calls) == 1
    clock[0] += 9.5
    cl.premium(); cl.books()
    assert len(s.calls) == 2
    clock[0] += 30
    cl.funding_intervals()                               # the 15-min job reuses a snapshot younger than AUX_REUSE_S
    assert len(s.calls) == 2
    clock[0] += 31
    cl.funding_intervals()
    assert len(s.calls) == 3 and s.calls[-1][1] == config.HTTP_TIMEOUT and s.calls[0][1] == config.TICK_HTTP_TIMEOUT
    assert venues.native(cl) and not venues.spot_native(cl) and venues.funding_intervals(cl)["BTC"] == 8
    assert variational.PERP_CLIENTS == {"variational": Variational} and cl.own_history


def test_tick_refuses_a_full_window_and_the_hourly_call_waits(clock):
    s = _S(fixed())
    cl = Variational(session=s)
    h = variational._host(variational.STATS_URL)
    h.calls.extend([clock[0]] * variational.AUX_CAP)
    with pytest.raises(BudgetExceeded):
        cl.premium()                                     # the tick never waits: the next tick retries
    assert s.calls == [] and cl.health()["used_weight"] == variational.AUX_CAP
    t1 = clock[0]
    cl.perp_instruments()                                # waits for the 10 s window to drain
    assert clock[0] - t1 == pytest.approx(variational.WINDOW_S) and len(s.calls) == 1


def test_429_and_the_challenge_pause_the_host_with_doubling(clock):
    s = _S(lambda n: _R({"message": "too many"}, 429))
    cl = Variational(session=s)
    with pytest.raises(BannedError):
        cl.premium()
    assert cl.banned_until == pytest.approx(clock[0] + 60) and cl.n_429 == 1
    with pytest.raises(BannedError):
        Variational(session=s).perp_instruments()        # same host → same pause, no request
    assert len(s.calls) == 1
    clock[0] += 61
    s.handler = lambda n: _R("<!DOCTYPE html><title>Just a moment...</title>", 403)
    with pytest.raises(BannedError):
        cl.premium()                                     # Cloudflare challenge: back off, never bypass
    assert cl.banned_until == pytest.approx(clock[0] + 120)                        # second strike: doubled
    clock[0] += 121
    s.handler = fixed()
    assert "BTC" in cl.premium()                         # success resets the strikes
    clock[0] += 10
    s.handler = lambda n: _R({}, 429, {"Retry-After": "7"})
    with pytest.raises(BannedError):
        cl.premium()
    assert cl.banned_until == pytest.approx(clock[0] + 7) and cl.health()["banned_until"] == int(clock[0] + 7)
    clock[0] += 8
    s.handler = lambda n: _R({}, 429)
    with pytest.raises(BannedError):
        cl.premium()
    assert cl.banned_until == pytest.approx(clock[0] + 120) and cl.n_429 == 4


def test_other_errors_permanent_retried_and_bodies(clock):
    s = _S(lambda n: _R({"error": "not found"}, 404))
    with pytest.raises(PermanentHTTPError, match="404"):
        Variational(session=s).perp_instruments()
    assert len(s.calls) == 1
    s = _S(lambda n: _R({}, 502))
    cl = Variational(session=s)
    with pytest.raises(RuntimeError, match="502"):
        cl.perp_instruments()                            # aux: three tries
    assert len(s.calls) == 3 and cl.n_err == 3
    clock[0] += 60
    with pytest.raises(RuntimeError):
        cl.premium()                                     # tick: one try
    assert len(s.calls) == 4
    for bad in ("<html>challenge with 200</html>", {"listings": []}, {"num_markets": 0}):
        clock[0] += 60
        with pytest.raises(RuntimeError):
            Variational(session=_S(lambda n, b=bad: _R(b))).premium()


# --- history: our own ledger ---------------------------------------------------------------------------------------------
def _storj(rate, mark="0.0416", iv=3600, name="Storj", ticker="STORJ"):
    return L(ticker, name, rate, iv, mark, ("0.04", "0.0401"))


def _feed(clock, cl, state, lm_ts, rows):
    clock[0] = lm_ts + 50
    state.update(lm=lm_ts, rows=rows)
    cl.premium()


def test_ledger_turns_the_last_estimate_into_the_settlement_row(clock, tmp_path):
    state = {}
    s = _S(live(clock, state))
    cl = Variational(session=s)
    btc = L("BTC", "Bitcoin", "0.056431", 28800, "77174.5")
    _feed(clock, cl, state, T_2200 - 590, [_storj("-132.14", "0.0418"), btc])       # 21:50:10 [L series]
    _feed(clock, cl, state, T_2200 - 290, [_storj("-136.95", "0.0417"), btc])       # 21:55:10
    with pytest.raises(NoHistoryError, match="continuous from —"):
        cl.history_since("STORJ", int((T_2200 - H) * 1000) + 1)                      # nothing settled yet
    assert cl.history_since("STORJ", int(T_2200 * 1000)) == []                       # a window in the future is empty
    _feed(clock, cl, state, T_2200 + 12, [_storj("-137.66", "0.0416"), btc])        # 22:00:12: no reset at 22:00 [L]
    rows = cl.history_since("STORJ", int((T_2200 - H) * 1000) + 1)
    assert rows == [dict(exchange="variational", symbol="STORJ", funding_ms=int(T_2200 * 1000),
                         rate=pytest.approx(-136.95 / 8760), mark=0.0417)]           # the LAST estimate before 22:00
    assert cl.coverage_ms("STORJ") == int(T_2200 * 1000) and cl.coverage_ms("BTC") is None
    with pytest.raises(NoHistoryError, match="continuous from 2026-09-12 22:00"):
        cl.history_since("STORJ", int((T_2200 - H) * 1000))                          # would include 21:00 — not observed
    with pytest.raises(NoHistoryError):
        cl.history_since("BTC", int(T_2200 * 1000))                                  # its 8 h window is still running
    assert Variational(session=s).history_since("STORJ", int((T_2200 - H) * 1000) + 1) == rows   # background copy
    assert cl.recent_history() == []                                                 # the run is younger than the batch
    for k in range(1, 132):                                                          # 10 h 55 min, every 5 min
        _feed(clock, cl, state, T_2200 + 12 + k * 300, [_storj(str(-100 - k), str(0.04 + k * 1e-5)), btc])
    rec = cl.recent_history()
    lo = clock[0] - variational.RECENT_SPAN_H * H
    assert [r["funding_ms"] for r in rec] == sorted(r["funding_ms"] for r in rec)
    assert {r["symbol"] for r in rec} == {"STORJ", "BTC"} and all(r["funding_ms"] > lo * 1000 for r in rec)
    storj = {r["funding_ms"]: r for r in rec if r["symbol"] == "STORJ"}
    assert len(storj) == 10                                                          # 23:00 … 08:00
    assert storj[int((T_2200 + H) * 1000)]["rate"] == pytest.approx(-111 / 8760)     # estimate of 22:55:12 (k = 11)
    assert [r["funding_ms"] for r in rec if r["symbol"] == "BTC"] == [int(T_0000 * 1000), int((T_0000 + 8 * H) * 1000)]
    assert [r for r in rec if r["symbol"] == "BTC"][0]["rate"] == pytest.approx(0.056431 * 8 / 8760)
    assert cl.recent_history() == rec                                                # not drained
    con = db.connect(tmp_path / "v.db")                                              # the batch path of the collector
    assert funding.apply_batch(con, rec, {"STORJ": 1, "BTC": 8}, int(clock[0] * 1000)) == 12
    assert con.execute("SELECT COUNT(*) FROM funding_events WHERE exchange='variational'").fetchone()[0] == 12


def test_ledger_holes_are_holes_never_guesses(clock):
    state = {}
    cl = Variational(session=_S(live(clock, state)))
    _feed(clock, cl, state, T_2200 - 1790, [_storj("-50")])                          # 21:30:10
    _feed(clock, cl, state, T_2200 + 1210, [_storj("-51")])                          # 22:20:10: estimate 30 min old at 22:00
    _feed(clock, cl, state, T_2200 + H - 290, [_storj("-52")])                       # 22:55:10
    _feed(clock, cl, state, T_2200 + H + 10, [_storj("-53")])                        # 23:00:10
    assert [r["funding_ms"] for r in cl.history_since("STORJ", int(T_2200 * 1000) + 1)] == [int((T_2200 + H) * 1000)]
    with pytest.raises(NoHistoryError):
        cl.history_since("STORJ", int(T_2200 * 1000) - 1000)                         # 22:00 was never written
    _feed(clock, cl, state, T_0000 - 470, [_storj("-54")])                           # 23:52:10
    _feed(clock, cl, state, T_0000 + H + 610, [_storj("-55")])                       # 01:10:10: 00:00 written, 01:00 skipped
    with pytest.raises(NoHistoryError):
        cl.history_since("STORJ", int((T_2200 + H) * 1000) + 1)
    _feed(clock, cl, state, T_0000 + 2 * H - 290, [_storj("-56")])                   # 01:55:10
    _feed(clock, cl, state, T_0000 + 2 * H + 10, [_storj("-57")])                    # 02:00:10
    got = cl.history_since("STORJ", int((T_0000 + H) * 1000) + 1)
    assert [(r["funding_ms"], r["rate"]) for r in got] == [(int((T_0000 + 2 * H) * 1000), pytest.approx(-56 / 8760))]
    assert [ms for ms, *_ in variational._host(variational.STATS_URL).rows["STORJ"]] == \
        [int((T_2200 + H) * 1000), int(T_0000 * 1000), int((T_0000 + 2 * H) * 1000)]
    clock[0] += variational.LEDGER_STALE_S + 1                                        # no observation since
    with pytest.raises(NoHistoryError, match="last observed"):
        cl.history_since("STORJ", int((T_0000 + H) * 1000) + 1)
    with pytest.raises(NoHistoryError, match="not observed"):
        cl.history_since("NOPE", 0)
    # an interval change inside a window drops that window's estimate (ex-dividend eve: 8 h → 1 h [D])
    variational._HOSTS.clear()
    cl = Variational(session=_S(live(clock, state)))
    nb = lambda rate, iv: L("NBIS", "Nebius Group N.V.", rate, iv, "222.8")
    _feed(clock, cl, state, T_2134, [nb("-0.5", 28800)])                            # pending 00:00 (8 h)
    _feed(clock, cl, state, T_2134 + 360, [nb("-0.6", 3600)])                       # 21:40: now 1 h → pending 22:00
    _feed(clock, cl, state, T_2200 - 290, [nb("-0.6", 3600)])                       # 21:55:10 (21:40 alone: 20 min old)
    _feed(clock, cl, state, T_2200 + 10, [nb("-0.7", 3600)])
    assert [(r["funding_ms"], r["rate"]) for r in cl.history_since("NBIS", int((T_2200 - H) * 1000) + 1)] == \
        [(int(T_2200 * 1000), pytest.approx(-0.6 / 8760))]
    assert variational._host(variational.STATS_URL).rows["NBIS"][0][3] == 1


def test_the_same_snapshot_is_observed_once(clock):
    s = _S(fixed())                                                                  # the same CF copy every time
    cl = Variational(session=s)
    cl.premium()
    clock[0] += 10
    cl.premium()
    assert len(s.calls) == 2 and variational._host(variational.STATS_URL).ev["AAPL"] == [1, 0]


def test_backfill_does_not_confirm_depth_the_ledger_never_saw(clock, monkeypatch, tmp_path):
    monkeypatch.setattr(funding, "time", variational.time)                           # the same fake clock
    state = {}
    cl = Variational(session=_S(live(clock, state)))
    _feed(clock, cl, state, T_2200 - 290, [_storj("-136.95")])
    _feed(clock, cl, state, T_2200 + 12, [_storj("-137.66")])
    con = db.connect(tmp_path / "b.db")
    st = funding.backfill(con, {"variational": cl}, [("variational", "STORJ")], {"variational": {"STORJ": 1}})
    assert st["errors"] == 1 and st["new"] == 0
    assert db.leg_depths(con) == {} and db.leg_sync(con) == {}                      # no 30-day depth, no cursor claimed
    n = funding.repair(con, {"variational": cl}, {("variational", "STORJ"): dict(needs_repair=True, latest_missing=True,
                                                                               missing=[int(T_2200 * 1000)],
                                                                               since=int(T_2200 * 1000) - 1)},
                       {"variational": {"STORJ": 1}})
    assert n == 1                                                                    # the observed settlement goes in


# --- pairs ---------------------------------------------------------------------------------------------------------------
def test_pairs_with_other_venues_by_definition_only(clock, monkeypatch):
    if "variational" not in config.PERP_VENUES:
        monkeypatch.setattr(config, "PERP_VENUES", config.PERP_VENUES + ("variational",))
    ins = Variational(session=_S(fixed())).perp_instruments()
    p = lambda s, b, c="crypto", f=1.0: dict(symbol=s, base=b, cls=c, factor=f)
    others = {"aster": [p("B3USDT", "B3")],
              "binance": [p("BTCUSDT", "BTC"), p("NEIROUSDT", "NEIRO"), p("QNTUSDT", "QNT")],
              "hyperliquid": [p("kPEPE", "PEPE", f=1000.0), p("xyz:AAPL", "AAPL", "equity"), p("xyz:QNT", "QNT", "equity"),
                              p("xyz:GOLD", "XAU", "commodity")],
              "lighter": [p("US500", "SP500", "index"), p("OPENAI", "OPENAI", "preipo")]}
    keys = {r["key"] for r in universe.build_ff({**others, "variational": ins})}
    assert keys == {"binance:BTCUSDT|variational:BTC", "binance:NEIROUSDT|variational:1NEIRO",
                    "hyperliquid:kPEPE|variational:1000PEPE", "hyperliquid:xyz:AAPL|variational:AAPL",
                    "hyperliquid:xyz:QNT|variational:QNTX",                     # Quantinuum ≠ Quant (QNTUSDT, a coin)
                    "hyperliquid:xyz:GOLD|variational:XAU", "lighter:OPENAI|variational:OPENAI",
                    "aster:B3USDT|variational:B3"}                              # 13.09: B3 (Base) — монета
    # not paired: US500 (the SPY share ≠ Lighter's S&P index), the coin QNT
