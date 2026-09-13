"""ApeX Omni on a fake HTTP session and fake WS connections (no network). Response shapes are trimmed copies of the 12-13.09
live captures (see the header of apex.py)."""
import json, logging, math, socket, time, types
import pytest
import requests
from funding_bot import apex, calc, config, identity, universe, venues
from funding_bot.apex import ApexPerp
from funding_bot.client import BannedError, BudgetExceeded, PermanentHTTPError

H = 3600_000
NOW = 1789250918.9                    # 12.09.2026 22:08:38.9 UTC — the live WS capture
NEXT_H = 1789254000_000               # 23:00 of that day, ms


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
        path = url.split("/api/v3", 1)[1]
        self.calls.append((path, dict(params or {})))
        return self.handler(path, dict(params or {}))


def _no_net(*a, **k):
    raise AssertionError("network in a test")


@pytest.fixture(autouse=True)
def _fresh():
    apex._HOSTS.clear(); apex._WARNED.clear(); apex._SS_SEEN.clear()
    yield
    apex._HOSTS.clear(); apex._WARNED.clear(); apex._SS_SEEN.clear()


@pytest.fixture
def clock(monkeypatch):
    c = [NOW]

    def sleep(s):
        c[0] += max(0.0, s)
    monkeypatch.setattr(apex, "time", types.SimpleNamespace(time=lambda: c[0], sleep=sleep, gmtime=time.gmtime,
                                                            strftime=time.strftime))
    return c


# --- recorded shapes (13.09 /symbols, trimmed to the fields the client reads) -------------------------------------------
def row(tok, name, cat=None, ct="PERPETUAL_CONTRACT", tick="0.01", step="0.01", cap="0.005", trade=True, disp=True,
        opn=True, settle="USDT", pre=False, tag=""):
    return {"symbol": f"{tok}-{settle}", "crossSymbolName": f"{tok}{settle}", "symbolDisplayName": f"{tok}{settle}",
            "baseTokenId": tok, "settleAssetId": settle, "tokenName": name, "tickSize": tick, "stepSize": step,
            "minOrderSize": step, "enableTrade": trade, "enableDisplay": disp, "enableOpenPosition": opn,
            "enableFundingSettlement": trade, "isPrelaunch": pre, "fundingMaxRate": cap, "fundingMinRate": "-" + cap,
            "fundingInterestRate": "0.0003", "category": cat, "contractType": ct, "tag": tag, "deliveryTime": None,
            "pullOffTime": None, "disableOpenPositionTime": None}


CRYPTO = [row("BTC", "Bitcoin", "L1", "UNKNOWN_CONTRACT_TYPE", tick="0.1", step="0.001", cap="0.000469"),
          row("1000PEPE", "PEPE", "MEME", tick="0.0000001", step="100"),
          row("LIT", "LIT", "DEFI", cap="0.02"),                        # Lighter's token; the name only repeats the ticker
          row("CHIP", "USD.AI", "AI", cap="0.02"),
          row("PAXG", "PAX Gold", tick="0.01", step="0.001"),
          row("GRAM", "Gram (prev. Toncoin)", step="0.1", cap="0.000305"),
          row("TON", "The Open Network", "L1", trade=False, disp=False, opn=False, cap="0.000305"),   # disabled
          row("IO", "IO", trade=True, disp=False, opn=False),                                         # hidden
          row("STBL", "STBL", "DEFI", trade=True, disp=False, opn=True),                              # hidden, openable
          row("NEWX", "New X", pre=True),                                                             # pre-launch
          row("USDCX", "Usdc X", settle="USDC")]                                                      # foreign settle
STOCKS = [row("SPCX", "Space Exploration Technologies", "STOCK", "STOCK_CONTRACT", cap="0.02", tag="7x24h"),
          row("USO", "United States Oil Fund", "COMMODITY", "STOCK_CONTRACT"),
          row("XAU", "Gold", "COMMODITY", "STOCK_CONTRACT", step="0.001", cap="0.02"),
          row("CL", "WTI Crude Oil", "COMMODITY", "STOCK_CONTRACT", cap="0.02"),
          row("SPY", "SPDR S&P 500 ETF Trust", "INDEX", "STOCK_CONTRACT"),
          row("QQQ", "Invesco QQQ Trust", "INDEX", "STOCK_CONTRACT"),
          row("SOXL", "Semicon Bull 3X ETF", None, "STOCK_CONTRACT", cap="0.02"),
          row("AAPL", "Apple Inc.", "STOCK", "STOCK_CONTRACT"),
          row("ORACLE", "Oracle Corporation", "STOCK", "STOCK_CONTRACT", cap="0.02"),
          row("ORCL", "Oracle Corporation", "STOCK", "STOCK_CONTRACT", trade=False, disp=False, opn=False),
          row("CXMT", "CXMT Corporation", "STOCK", "STOCK_CONTRACT", cap="0.02"),
          row("SKHYNIX", "SK Hynix", "STOCK", "STOCK_CONTRACT", cap="0.02"),
          row("LITHIUM", "Lithium", "COMMODITY", "STOCK_CONTRACT"),     # hypothetical: not a known commodity
          row("VIX", "Volatility Index", "INDEX", "STOCK_CONTRACT")]    # hypothetical: an index that is not a fund
PRED = [dict(row("Knicks_Win_Against_Celtics_Dec2", "Knicks win"), contractType="PREDICTION_CONTRACT")]
SYMBOLS = {"data": {"spotConfig": {}, "contractConfig": {"perpetualContract": CRYPTO, "stockContract": STOCKS,
                                                          "predictionContract": PRED, "prelaunchContract": []}},
           "timeCost": 1}
KEPT = {"BTCUSDT", "1000PEPEUSDT", "LITUSDT", "CHIPUSDT", "PAXGUSDT", "GRAMUSDT", "SPCXUSDT", "USOUSDT", "XAUUSDT",
        "CLUSDT", "SPYUSDT", "QQQUSDT", "SOXLUSDT", "AAPLUSDT", "ORACLEUSDT", "CXMTUSDT", "SKHYNIXUSDT", "LITHIUMUSDT",
        "VIXUSDT"}


def ticker_row(sym, fr="-0.00000367", mark="77211.69", index="77255.78", nft="2026-09-12T23:00:00Z"):
    return {"fundingRate": fr, "highPrice24h": "77480.4", "indexPrice": index, "lastPrice": "77211.6",
            "lowPrice24h": "76946.5", "nextFundingTime": nft, "openInterest": "1577.444", "oraclePrice": "",
            "markPrice": mark, "predictedFundingRate": "0.0000125", "price24hPcnt": "0.002", "symbol": sym,
            "tradeCount": "", "turnover24h": "556851158.6623", "volume24h": "7209.393"}


def routes(symbols=SYMBOLS, ticker=None, hist=None):
    def h(path, p):
        if path == "/symbols":
            return _R(symbols)
        if path == "/ticker":
            if ticker is not None:
                return ticker(p)
            return _R({"data": [ticker_row(p["symbol"])], "timeCost": 981})
        if path == "/history-funding" and hist is not None:
            return hist(p)
        return _R({"code": 404, "msg": "not found"}, 404)
    return h


def info_frame(rows, ts=1789250918788948, typ="snapshot"):
    return {"topic": "instrumentInfo.all", "type": typ, "ts": ts, "data": rows}


def irow(s, fr, mp, xp, ss="0"):
    return {"s": s, "p": mp, "pr": "0.002", "h": mp, "l": mp, "xp": xp, "to": "1", "v": "1", "fr": fr, "o": "1",
            "tc": "1", "ss": ss, "mp": mp}


LIVE_INFO = [irow("BTCUSDT", "-0.00000367", "77211.28", "77255.66"),
             irow("WBTCUSDT", "", "77255.13", "77255.13"),                       # collateral price row: fr ""
             irow("1000PEPEUSDT", "0.0000125", "0.003418", "0.003415"),
             irow("USOUSDT", "0.0000125", "154.6", "154.65", ss="5"),           # tradable with ss 5 [L]
             irow("TONUSDT", "0.0000125", "1.2", "1.2"),                          # disabled market
             irow("Knicks_Win_Against_Celtics_Dec2USDT", "0", "0.5", "0.5")]      # prediction market


def book_snap(sym, b, a, u, ts=1789250918678552):
    return {"topic": f"orderBook25.H.{sym}", "type": "snapshot", "data": {"s": sym, "b": b, "a": a, "u": u},
            "cs": 65036402319, "ts": ts}


def book_delta(sym, u, b=(), a=()):
    return {"topic": f"orderBook25.H.{sym}", "type": "delta", "data": {"s": sym, "b": list(b), "a": list(a), "u": u},
            "cs": 65036402346, "ts": 1789250918771807}


# BTC snapshot of 12.09 22:08:38Z [L]: both sides ASCENDING — the best bid is the LAST bid level
BTC_B = [["77097.6", "1.141"], ["77100.0", "0.006"], ["77208.4", "0.547"], ["77211.1", "4.635"]]
BTC_A = [["77211.9", "4.072"], ["77212.0", "5.120"], ["77318.0", "0.009"], ["77350.9", "0.001"]]
PEPE_B = [["0.0033304", "3303000"], ["0.0034133", "1581900"]]
PEPE_A = [["0.0034168", "1733800"], ["0.0034900", "200"]]


def stub_stream(cl, st=None):
    """Put a stream in the client without its thread (tests feed apply() directly)."""
    st = st or apex._Stream("wss://x/realtime_public?v=2&timestamp={ms}", "apex")
    st.start, st._waited = (lambda: None), True
    cl._stream = st
    return st


# --- universe ------------------------------------------------------------------------------------------------------------
def test_perp_instruments_filters_classes_bases_and_names(clock, caplog):
    s = _S(routes())
    with caplog.at_level(logging.WARNING, logger="funding_bot.apex"):
        ins = {i["symbol"]: i for i in ApexPerp(session=s).perp_instruments()}
    assert set(ins) == KEPT                   # disabled, hidden, pre-launch, non-USDT and prediction rows are out
    got = {k: (i["cls"], i["base"], i["factor"]) for k, i in ins.items()}
    assert got == {"BTCUSDT": ("crypto", "BTC", 1.0), "1000PEPEUSDT": ("crypto", "PEPE", 1000.0),
                   "LITUSDT": ("crypto", "LIT", 1.0), "CHIPUSDT": ("crypto", "CHIP", 1.0),
                   "PAXGUSDT": ("crypto", "PAXG", 1.0),                            # gold token = coin (repo rule)
                   "GRAMUSDT": ("crypto", "GRAM", 1.0),                            # not renamed to TON
                   "SPCXUSDT": ("equity", "SPCX", 1.0), "USOUSDT": ("equity", "USO", 1.0),   # oil FUND, not oil
                   "XAUUSDT": ("commodity", "XAU", 1.0), "CLUSDT": ("commodity", "CL", 1.0),
                   "SPYUSDT": ("equity", "SPY", 1.0), "QQQUSDT": ("equity", "QQQ", 1.0),
                   "SOXLUSDT": ("equity", "SOXL", 1.0), "AAPLUSDT": ("equity", "AAPL", 1.0),
                   "ORACLEUSDT": ("equity", "ORCL", 1.0),                          # Oracle Corporation → ORCL
                   # review 13.09: SPCX / CXMT are category STOCK and equity on 10+ venues at one price [L] — no hard-coded split
                   "CXMTUSDT": ("equity", "CXMT", 1.0), "SKHYNIXUSDT": ("equity", "SKHYNIX", 1.0),
                   "LITHIUMUSDT": ("rwa", "LITHIUM", 1.0), "VIXUSDT": ("rwa", "VIX", 1.0)}
    b = ins["BTCUSDT"]
    assert (b["tick_size"], b["step_size"], b["min_qty"], b["min_notional"], b["onboard_ms"]) == (0.1, 0.001, 0.001, None, 0)
    assert (b["interval_h"], b["cap"], b["floor"], b["quote"], b["contract"]) == (1, 0.000469, -0.000469, "USDT", "PERPETUAL")
    assert (b["market"], b["url"], b["exchange"], b["base_asset"]) == ("BTC-USDT", "https://omni.apex.exchange/trade/BTCUSDT",
                                                                         "apex", "BTC")
    # a name that only repeats the ticker is no name (identity must not confirm by ticker)
    names = {k: i["name_hint"] for k, i in ins.items()}
    assert names["BTCUSDT"] == "Bitcoin" and names["CHIPUSDT"] == "USD.AI" and names["GRAMUSDT"] == "Gram (prev. Toncoin)"
    assert names["1000PEPEUSDT"] is None and names["LITUSDT"] is None and ins["LITUSDT"]["token_name"] == "LIT"
    assert ins["1000PEPEUSDT"]["step_size"] == 100.0                               # the factor is from the NAME
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert "LITHIUM" in warned and "VIX" in warned and "CXMT" not in warned
    uni = apex._host(apex.REST).uni
    assert uni["dashed"]["TONUSDT"] == "TON-USDT" and "TONUSDT" not in uni["syms"]   # history of disabled rows readable
    assert [c[0] for c in s.calls] == ["/symbols"]


def test_constructor_touches_no_network_and_interface_shape(monkeypatch):
    monkeypatch.setattr(requests.Session, "request", _no_net)
    cl = ApexPerp()
    assert cl._stream is None and cl.name == "apex" and apex.PERP_CLIENTS == {"apex": ApexPerp}
    assert venues.native(cl) and not venues.spot_native(cl)
    assert venues.funding_intervals(cl) is None                                   # fixed 1 h, like Hyperliquid / Lighter
    assert cl.recent_history() == [] and venues.recent_history(cl) == []
    assert cl.history_gap_s == config.FUNDING_HISTORY_MIN_GAP_S.get("apex", apex.HISTORY_GAP_S)


# --- tick: rates from instrumentInfo.all ---------------------------------------------------------------------------------
def test_premium_from_the_ws_frame_joined_on_the_universe(clock):
    s = _S(routes())
    cl = ApexPerp(session=s)
    st = stub_stream(cl)
    assert st.apply(info_frame(LIVE_INFO), clock[0])
    p = cl.premium()
    assert set(p) == {"BTCUSDT", "1000PEPEUSDT", "USOUSDT"}       # collateral, disabled, prediction rows are not ours
    btc = p["BTCUSDT"]
    assert btc["rate"] == -0.00000367 and (btc["mark"], btc["index"]) == (77211.28, 77255.66)
    assert btc["ts_ms"] == 1789250918788 and btc["obs"] == clock[0]              # µs server ts → ms; obs = arrival
    assert btc["next_ms"] == NEXT_H and btc["interval_h"] == 1
    assert p["USOUSDT"]["rate"] == 0.0000125                                      # ss "5" is logged, never filtered
    assert [c[0] for c in s.calls] == ["/symbols"]                                # the tick itself costs no REST call
    assert st.want is not None and st._want == {i for i in KEPT}                   # books wanted for the whole universe


def test_published_rate_is_per_hour_and_needs_no_conversion(clock):
    """12.09 22:00Z [L]: the value shown 5 s after the hour equals the settled row exactly; both are fractions PER HOUR,
    the interval is 1 h, so the collector's hourly rate is the published number itself."""
    settled = {"BTC-USDT": "-0.00000294", "ETH-USDT": "-0.00000501", "1000PEPE-USDT": "0.00001131"}
    t = 1789250400_000

    def hist(p):
        return _R({"data": {"historyFunds": [{"symbol": p["symbol"], "rate": settled[p["symbol"]], "price": "77265.17",
                                              "fundingTime": t, "fundingTimestamp": t}], "totalSize": 2}, "timeCost": 1})
    syms = dict(SYMBOLS, data={"contractConfig": {"perpetualContract": [row("BTC", "Bitcoin"), row("ETH", "Ethereum"),
                                                                        row("1000PEPE", "PEPE")]}})
    cl = ApexPerp(session=_S(routes(syms, hist=hist)), history_gap_s=0)
    clock[0] = t / 1000 + 5
    stub_stream(cl).apply(info_frame([irow("BTCUSDT", "-0.00000294", "77242", "77246"),
                                      irow("ETHUSDT", "-0.00000501", "2523", "2524"),
                                      irow("1000PEPEUSDT", "0.00001131", "0.00341", "0.00341")]), clock[0])
    p = cl.premium()
    for dash, v in settled.items():
        sym = dash.replace("-", "")
        h = cl.history_since(sym, t - H, t)
        assert h[-1]["rate"] == float(v) == p[sym]["rate"]
        assert calc.hourly(p[sym]["rate"], p[sym]["interval_h"]) == float(v)   # per hour: no ÷8, no annualisation
    assert math.isclose(0.0000125 * 8, 0.0001) and math.isclose(0.0003 / 24, 0.0000125)   # baseline = 0.01 %/8 h


def test_stale_stream_falls_back_to_rest_round_robin(clock, monkeypatch):
    monkeypatch.setattr(apex, "FALLBACK_PER_TICK", 5)
    seen = []

    def tick(p):
        seen.append(p["symbol"])
        if p["symbol"] == "CLUSDT":
            return _R({"code": 3, "msg": "invalid symbol: CLUSDT"})               # one symbol failing: the rest go on
        return _R({"data": [ticker_row(p["symbol"])], "timeCost": 30})
    s = _S(routes(ticker=tick))
    cl = ApexPerp(session=s)
    st = stub_stream(cl)
    st.apply(info_frame(LIVE_INFO), clock[0] - apex.WS_FRESH_S - 1)             # the last frame is too old
    order = sorted(KEPT)
    p1 = cl.premium()
    assert seen == order[:5] and set(p1) == set(order[:5]) - {"CLUSDT"}
    r = p1[order[0]]
    assert (r["rate"], r["mark"], r["index"], r["next_ms"], r["interval_h"]) == (-0.00000367, 77211.69, 77255.78, NEXT_H, 1)
    assert r["obs"] == clock[0] and r["ts_ms"] == int(clock[0] * 1000)
    t1 = clock[0]
    clock[0] += 10
    p2 = cl.premium()
    assert seen[5:] == order[5:10] and len(p2) == 9                               # cache grows; old rows keep their obs
    assert p2[order[0]]["obs"] == t1 and p2[order[6]]["obs"] == clock[0]
    for _ in range(3):
        cl.premium()
    assert len(seen) == 25 and seen[:19] == order and seen[19:] == order[:6]      # 5 calls a tick, wraps round
    # a full window: the tick never waits — cached rows still come back, none cached → BudgetExceeded
    h = apex._host(cl.rest)
    h.calls.extend([clock[0]] * (apex.TICK_CAP - len(h.calls)))
    n = len(s.calls)
    assert len(cl.premium()) == 18 and len(s.calls) == n
    apex._host(cl.rest).rest_rows.clear()
    with pytest.raises(BudgetExceeded):
        cl.premium()
    # the stream is fresh again → no REST call at all
    clock[0] += 61
    st.apply(info_frame(LIVE_INFO), clock[0])
    n = len(s.calls)
    assert set(cl.premium()) == {"BTCUSDT", "1000PEPEUSDT", "USOUSDT"} and len(s.calls) == n


def test_next_funding_time_parsing_and_roll():
    assert apex._iso_ms("2026-09-12T23:00:00Z") == NEXT_H
    assert apex._next_hour(NEXT_H, NEXT_H - 1) == NEXT_H                          # still ahead: kept
    assert apex._next_hour(NEXT_H - H, NEXT_H - 1) == NEXT_H                      # passed → rolled on the hour grid
    assert apex._next_hour(NEXT_H - H, NEXT_H + 5) == NEXT_H + H
    assert apex._next_hour(None, NEXT_H - 1000) == NEXT_H and apex._iso_ms("") is None
    assert apex._ts_ms(1789250918788948) == 1789250918788 and apex._ts_ms(1789250918) == 1789250918000
    assert apex._ts_ms(1789250918788) == 1789250918788 and apex._ts_ms(None) is None


# --- books ---------------------------------------------------------------------------------------------------------------
def test_books_ascending_snapshot_deltas_zero_size_and_obs(clock):
    cl = ApexPerp(session=_S(routes()))
    st = stub_stream(cl)
    assert st.apply(book_snap("BTCUSDT", BTC_B, BTC_A, 20329476), 100.0)
    assert st.apply(book_snap("1000PEPEUSDT", PEPE_B, PEPE_A, 1467759), 100.5)
    assert st.apply(book_snap("TONUSDT", [["1.0", "1"]], [["1.01", "1"]], 5), 100.6)   # not in the universe
    b = cl.books()
    assert set(b) == {"BTCUSDT", "1000PEPEUSDT"}
    assert b["BTCUSDT"] == dict(bid=77211.1, ask=77211.9, bid_qty=4.635, ask_qty=4.072, obs=100.6)   # never b[0]
    assert b["1000PEPEUSDT"]["bid_qty"] == 1581900.0                                # in 1000PEPE units (price unit)
    assert st.apply(book_delta("BTCUSDT", 20329477, b=[["77211.1", "4.622"]]), 101.0)
    assert cl.books()["BTCUSDT"]["bid_qty"] == 4.622
    st.apply(book_delta("BTCUSDT", 20329478, b=[["77211.1", "0"]], a=[["77211.5", "0.3"]]), 102.0)   # size 0 deletes
    bb = cl.books()["BTCUSDT"]
    assert (bb["bid"], bb["bid_qty"], bb["ask"], bb["ask_qty"]) == (77208.4, 0.547, 77211.5, 0.3)
    st.apply(info_frame(LIVE_INFO), 110.0)                                         # obs = connection's last data frame
    assert cl.books()["1000PEPEUSDT"]["obs"] == 110.0
    st.apply(book_delta("BTCUSDT", 20329479, a=[["77211.5", "0"], ["77211.9", "0"], ["77212.0", "0"], ["77318.0", "0"],
                                                 ["77350.9", "0"]]), 111.0)
    assert "BTCUSDT" not in cl.books()                                             # one-sided book is not a price
    assert apex._book("1", "1.06") is None and apex._book("2", "1") is None and apex._book("1", "1.01") is not None


def test_sequence_gap_drops_the_book_and_asks_for_a_resubscription():
    st = apex._Stream("wss://x?timestamp={ms}", "apex")
    st.apply(book_snap("BTCUSDT", BTC_B, BTC_A, 10), 100.0)
    st.apply(book_delta("BTCUSDT", 11, b=[["77211.1", "4.6"]]), 100.1)
    st.apply(book_delta("BTCUSDT", 11, b=[["77211.1", "9.9"]]), 100.2)            # a replayed u is ignored
    assert st.books()["BTCUSDT"]["bid_qty"] == 4.6
    st.apply(book_delta("BTCUSDT", 14, b=[["77211.1", "4.5"]]), 100.3)            # 12 and 13 never came
    assert "BTCUSDT" not in st.books() and st.n_gap == 1 and st._resub == {"BTCUSDT"}
    st._resub.clear()
    st.apply(book_delta("BTCUSDT", 15), 101.0)                                     # delta without a snapshot, within 10 s
    assert st._resub == set()
    st.apply(book_delta("BTCUSDT", 16), 111.0)
    assert st._resub == {"BTCUSDT"}
    st.apply(book_snap("BTCUSDT", BTC_B, BTC_A, 40), 111.5)                        # the fresh snapshot restores it
    assert st.books()["BTCUSDT"]["bid"] == 77211.1
    assert not st.apply({"success": True, "ret_msg": "pong"}, 112.0) and not st.apply({"topic": "trade.x"}, 112.0)


class _Conn:
    def __init__(self, script, clock):
        self.script, self.clock, self.sent, self.closed, self.url = list(script), clock, [], False, None

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


def test_stream_session_subscribes_pongs_pings_resubscribes_and_reconnects(clock, monkeypatch):
    monkeypatch.setattr(apex, "BOOK_SUB_CHUNK", 2)
    srv_ping = {"op": "ping", "args": ["1789250921517"]}
    c1 = _Conn([info_frame(LIVE_INFO), book_snap("BTCUSDT", BTC_B, BTC_A, 10), srv_ping,
                book_delta("BTCUSDT", 13), 16, info_frame(LIVE_INFO), 12, 12, 12], clock)   # gap, ping, then silence
    c2 = _Conn([info_frame(LIVE_INFO), "close"], clock)
    conns, urls = [c1, c2], []

    def connect(url, timeout):
        urls.append(url)
        return conns[len(urls) - 1]
    st = apex._Stream("wss://quote.omni.apex.exchange/realtime_public?v=2&timestamp={ms}", "apex", connect)
    st.backoff0 = 0.0
    st.want(["BTCUSDT", "ETHUSDT", "1000PEPEUSDT"])
    st._run(max_sessions=2)
    assert urls[0] == f"wss://quote.omni.apex.exchange/realtime_public?v=2&timestamp={int(NOW * 1000)}"
    assert c1.sent[0] == {"op": "subscribe", "args": ["instrumentInfo.all"]}
    subs = [m["args"] for m in c1.sent[1:3]]                                       # all books, chunked by 2
    assert subs == [["orderBook25.H.1000PEPEUSDT", "orderBook25.H.BTCUSDT"], ["orderBook25.H.ETHUSDT"]]
    assert {"op": "pong", "args": ["1789250921517"]} in c1.sent and st.n_ping == 1
    i = c1.sent.index({"op": "unsubscribe", "args": ["orderBook25.H.BTCUSDT"]})    # the gap: unsubscribe, then subscribe
    assert c1.sent[i + 1] == {"op": "subscribe", "args": ["orderBook25.H.BTCUSDT"]}
    assert any(m.get("op") == "ping" for m in c1.sent)                             # client heartbeat after WS_PING_S
    assert "no market data" not in (st.err or "") and "closed by server" in st.err
    assert st.n_conn == 2 and c1.closed and c2.closed and not st.synced.is_set()
    assert st.books() == {}                                                        # books are rebuilt per connection
    assert len(c2.sent) == 3 and c2.sent[0]["args"] == ["instrumentInfo.all"]      # every book re-subscribed


def test_silent_connection_is_dropped(clock):
    c1 = _Conn([info_frame(LIVE_INFO)] + [10] * 5, clock)
    st = apex._Stream("wss://x?timestamp={ms}", "apex", lambda url, t: c1)
    assert st._session_once() is True
    assert "no market data" in st.err and c1.closed


# --- history -------------------------------------------------------------------------------------------------------------
def hist_server(stamps, symbol="BTC-USDT", calls=None):
    """/history-funding as the venue answers [L]: newest first, begin inclusive, end exclusive, limit ≤ 100 (0 = 100),
    code 3 for a bad symbol / size / page, a totalSize that is not a count."""
    def h(p):
        if calls is not None:
            calls.append(dict(p))
        if p.get("symbol") != symbol:
            return _R({"code": 3, "msg": f"invalid symbol: {p.get('symbol')}", "timeCost": 1})
        lim = int(p.get("limit") or 100)
        if lim > 100:
            return _R({"code": 3, "msg": f"invalid get page size: {lim}", "timeCost": 1})
        b, e = p.get("beginTimeInclusive", 0), p.get("endTimeExclusive", 10 ** 15)
        sel = [t for t in stamps if b <= t < e][::-1][:lim]
        rows = [{"symbol": symbol, "rate": f"{0.0000125 if k % 3 else -0.0000271:.8f}", "price": "77265.17",
                 "fundingTime": t, "fundingTimestamp": t} for k, t in enumerate(sel)]
        return _R({"data": {"historyFunds": rows, "totalSize": 6 * lim + len(rows) + 1}, "timeCost": 1})
    return h


def test_history_walks_back_by_end_time_exclusive(clock):
    now_ms = int(NOW * 1000)
    last = now_ms // H * H
    stamps = [last - k * H for k in range(40 * 24)][::-1]                          # 40 days of hourly rows
    calls = []
    cl = ApexPerp(session=_S(routes(hist=hist_server(stamps, calls=calls))), history_gap_s=0)
    start = now_ms - 30 * 86400_000
    out = cl.history_since("BTCUSDT", start, now_ms)
    want = [t for t in stamps if start <= t <= now_ms]
    assert len(want) == 720 and [r["funding_ms"] for r in out] == want            # ascending, no gap, no duplicate
    assert {r["symbol"] for r in out} == {"BTCUSDT"} and {r["exchange"] for r in out} == {"apex"}
    assert all(r["mark"] is None for r in out)                                     # the row's price is the index
    assert {r["rate"] for r in out} == {0.0000125, -0.0000271}                     # per hour, unconverted
    assert len(calls) == 8 and {c["symbol"] for c in calls} == {"BTC-USDT"} and {c["limit"] for c in calls} == {100}
    assert calls[0]["endTimeExclusive"] == now_ms + 1 and all(c["beginTimeInclusive"] == start for c in calls)
    for a, b in zip(calls, calls[1:]):
        assert b["endTimeExclusive"] < a["endTimeExclusive"]
    assert calls[1]["endTimeExclusive"] == want[-100]                              # the oldest row of page 1, exclusive
    calls.clear()
    out = cl.history_since("BTCUSDT", last - 2 * H + 1, now_ms)                   # a short repair: a few rows, one call
    assert [r["funding_ms"] for r in out] == [last - H, last] and len(calls) == 1
    assert calls[0]["limit"] == 5                                                  # 2.14 h → ceil 3 + 2 rows of slack
    assert cl.history_since("BTCUSDT", now_ms, now_ms - 1) == []


def test_history_symbols_errors_and_no_silent_truncation(clock, monkeypatch):
    now_ms = int(NOW * 1000)
    stamps = [now_ms // H * H - k * H for k in range(24)][::-1]
    s = _S(routes(hist=hist_server(stamps, symbol="TON-USDT")))
    cl = ApexPerp(session=s, history_gap_s=0)
    assert len(cl.history_since("TONUSDT", now_ms - 86400_000, now_ms)) == 24      # disabled market: history still there
    with pytest.raises(PermanentHTTPError, match="invalid symbol"):
        cl.history_since("BTCUSDT", now_ms - H, now_ms)                            # code 3 with HTTP 200
    n = len([c for c in s.calls if c[0] == "/symbols"])
    with pytest.raises(RuntimeError, match="no market NOPEUSDT"):
        cl.history_since("NOPEUSDT", 0, 1000)                                      # the list is fresh — no refetch
    assert len([c for c in s.calls if c[0] == "/symbols"]) == n
    clock[0] += apex.IDS_TTL_S
    with pytest.raises(RuntimeError, match="no market NOPEUSDT"):
        cl.history_since("NOPEUSDT", 0, 1000)
    assert len([c for c in s.calls if c[0] == "/symbols"]) == n + 1               # one refresh, then the verdict
    # a server that ignores endTimeExclusive would loop on the same page
    stuck = lambda p: _R({"data": {"historyFunds": [{"rate": "0.0000125", "fundingTime": now_ms - k * H}
                                                    for k in range(p["limit"])], "totalSize": 9}})
    cl2 = ApexPerp(session=_S(routes(hist=stuck)), history_gap_s=0)
    with pytest.raises(RuntimeError, match="did not move"):
        cl2.history_since("BTCUSDT", now_ms - 30 * 86400_000, now_ms)
    monkeypatch.setattr(apex, "HISTORY_MAX_PAGES", 2)
    big = [now_ms // H * H - k * H for k in range(800)][::-1]
    cl3 = ApexPerp(session=_S(routes(hist=hist_server(big))), history_gap_s=0)
    with pytest.raises(RuntimeError, match="did not reach"):
        cl3.history_since("BTCUSDT", now_ms - 30 * 86400_000, now_ms)


# --- transport -----------------------------------------------------------------------------------------------------------
def test_429_and_403_pause_the_host_for_every_client(clock):
    s = _S(lambda path, p: _R({"code": 429, "msg": "too many"}, 429))
    cl = ApexPerp(session=s)
    with pytest.raises(BannedError):
        cl.perp_instruments()
    assert cl.banned_until == pytest.approx(clock[0] + 60) and cl.n_429 == 1
    other = ApexPerp(session=s)                                                    # a background copy: same host
    with pytest.raises(BannedError):
        other.history_since("BTCUSDT", 0, 1000)
    assert len(s.calls) == 1
    clock[0] += 61
    s2 = _S(lambda path, p: _R({}, 403))
    with pytest.raises(BannedError, match="403"):
        ApexPerp(session=s2).perp_instruments()
    assert cl.banned_until == pytest.approx(clock[0] + apex.BAN_403_S)             # an IP ban [D]: long pause
    clock[0] += apex.BAN_403_S + 1
    s3 = _S(lambda path, p: _R({}, 429, {"Retry-After": "7"}))
    with pytest.raises(BannedError):
        ApexPerp(session=s3).perp_instruments()
    assert cl.health()["banned_until"] == int(clock[0] + 7)


def test_4xx_is_permanent_and_5xx_or_foreign_codes_retry(clock):
    with pytest.raises(PermanentHTTPError, match="404"):
        ApexPerp(session=_S(lambda path, p: _R({"code": 404}, 404))).perp_instruments()
    s = _S(lambda path, p: _R({"err": "x"}, 502))
    cl = ApexPerp(session=s)
    with pytest.raises(RuntimeError, match="502"):
        cl.perp_instruments()
    assert len(s.calls) == 3 and cl.n_err == 3
    s2 = _S(lambda path, p: _R({"code": 2, "msg": "internal server error"}))       # the dead v2 answer
    with pytest.raises(RuntimeError, match="code 2"):
        ApexPerp(session=s2).perp_instruments()
    assert len(s2.calls) == 3
    with pytest.raises(RuntimeError, match="without tradable"):
        ApexPerp(session=_S(lambda path, p: _R({"data": {"contractConfig": {}}}))).perp_instruments()


def test_history_is_paced_across_clients_and_the_tick_never_waits(clock):
    s = _S(routes(hist=lambda p: _R({"data": {"historyFunds": [], "totalSize": 1}})))
    a, b = ApexPerp(session=s, history_gap_s=0.25), ApexPerp(session=s, history_gap_s=0.25)
    t = []
    for cl in (a, b, a):
        cl.history_since("BTCUSDT", 0, 1000)
        t.append(clock[0])
    assert t[1] - t[0] == pytest.approx(0.25) and t[2] - t[1] == pytest.approx(0.25)
    h = apex._host(a.rest)
    h.calls.extend([clock[0]] * (apex.TICK_CAP - len(h.calls)))
    n = len(s.calls)
    with pytest.raises(BudgetExceeded):
        a._get(apex.PATH_TICKER, {"symbol": "BTCUSDT"}, kind="tick")
    assert len(s.calls) == n and a.health()["used_weight"] == apex.TICK_CAP
    t1 = clock[0]
    b.history_since("BTCUSDT", 0, 1000)                                            # history waits for the window
    assert clock[0] - t1 >= 59 and len(s.calls) == n + 1


# --- pairs and identity --------------------------------------------------------------------------------------------------
def test_pairs_with_other_venues_within_class(clock, monkeypatch):
    monkeypatch.setattr(config, "PERP_VENUES", ("hyperliquid", "lighter", "apex"))
    ins = ApexPerp(session=_S(routes())).perp_instruments()
    hl = [dict(symbol=s, base=b, cls=c, factor=f) for s, b, c, f in
          (("BTC", "BTC", "crypto", 1.0), ("kPEPE", "PEPE", "crypto", 1000.0), ("xyz:GOLD", "XAU", "commodity", 1.0),
           ("xyz:ORCL", "ORCL", "equity", 1.0), ("xyz:CL", "CL", "commodity", 1.0), ("xyz:CXMT", "CXMT", "equity", 1.0),
           ("xyz:SPCX", "SPCX", "equity", 1.0))]                                   # HL files SPCX as equity [L 13.09]
    lt = [dict(symbol=s, base=b, cls=c, factor=1.0) for s, b, c in
          (("WTI", "CL", "commodity"), ("USO", "USO", "equity"), ("LIT", "LIT", "crypto"))]
    ff = {r["key"]: r for r in universe.build_ff({"hyperliquid": hl, "lighter": lt, "apex": ins})}
    mine = {k for k in ff if "apex:" in k}
    assert mine == {"hyperliquid:BTC|apex:BTCUSDT", "hyperliquid:kPEPE|apex:1000PEPEUSDT",
                    "hyperliquid:xyz:GOLD|apex:XAUUSDT", "hyperliquid:xyz:ORCL|apex:ORACLEUSDT",
                    "hyperliquid:xyz:CL|apex:CLUSDT", "hyperliquid:xyz:SPCX|apex:SPCXUSDT",
                    "hyperliquid:xyz:CXMT|apex:CXMTUSDT",
                    "lighter:WTI|apex:CLUSDT", "lighter:USO|apex:USOUSDT", "lighter:LIT|apex:LITUSDT"}
    pepe = ff["hyperliquid:kPEPE|apex:1000PEPEUSDT"]
    assert (pepe["fa"], pepe["fb"]) == (1000.0, 1000.0)                            # both per 1000 PEPE
    assert all(ff[k]["va"] != "apex" for k in mine)                                # apex is last in the owner's order
    assert ff["hyperliquid:xyz:CXMT|apex:CXMTUSDT"]["cls"] == "equity"             # review 13.09: was «rwa», no pair


def test_identity_is_name_only_as_an_oracle_venue(clock, monkeypatch):
    """Integrator's step (config_needed): «apex» in identity.ORACLE_PERPS and in the collector's perp_names tuple. No
    index composition, contract or oracle feed in the API: a pair is decided by the declared name only, never by price."""
    monkeypatch.setattr(identity, "ORACLE_PERPS", identity.ORACLE_PERPS | {"apex"})
    ins = {i["symbol"]: i for i in ApexPerp(session=_S(routes())).perp_instruments()}
    names = {"apex": {s: i["name_hint"] for s, i in ins.items() if i.get("name_hint")}}
    rec = lambda n, a: identity.coin_record(n, [("ETH", a, True, True)], "")
    spots = {"gate_spot": {"coins": {"LIT": rec("Litentry", "0x" + "11" * 20), "CHIP": rec("USD.AI", "0x" + "22" * 20)},
                           "markets": {"LIT_USDT": ["LIT", True], "CHIP_USDT": ["CHIP", True]}}}
    sf = lambda sym, base, perp: dict(key=f"gate_spot:{sym}|apex:{perp}", base=base, cls="crypto", spot_ex="gate_spot",
                                      spot=sym, spot_factor=1.0, perp_ex="apex", perp=perp, perp_factor=1.0)
    R = identity.Resolver(spots, {}, perp_names=names)
    d = identity.decide_sf(R, sf("LIT_USDT", "LIT", "LITUSDT"))                    # «LIT» is no name: not confirmed
    assert (d["ident"], d["ident_ev"]) == ("unknown", "oracle_only")
    d = identity.decide_sf(R, sf("CHIP_USDT", "CHIP", "CHIPUSDT"))
    assert (d["ident"], d["ident_ev"]) == ("same", "name")


@pytest.mark.skipif("apex" not in config.PERP_VENUES, reason="the integrator has not wired apex into config yet")
def test_wiring_once_integrated(monkeypatch):
    from funding_bot import dashboard
    from funding_bot.client import make_clients
    six = ("backpack", "variational", "edgex", "extended", "pacifica", "apex")
    order = [v for v in config.PERP_VENUES if v in six]
    assert order == [v for v in six if v in order] and config.PERP_VENUES[-1] == "apex"
    assert config.FEES_TAKER["apex"] == 0.0005 and config.FIXED_INTERVAL_H.get("apex") == 1
    assert config.FUNDING_HISTORY_MIN_GAP_S.get("apex") == 0.25
    assert config.URLS.get("apex") == "https://omni.apex.exchange/trade/{symbol}"
    assert "apex" in identity.ORACLE_PERPS and "apex" in dashboard.BRANDS
    monkeypatch.setattr(requests.Session, "request", _no_net)
    cl = make_clients()
    assert isinstance(cl["apex"], ApexPerp) and cl["apex"]._stream is None
