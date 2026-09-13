"""Gate USDT perpetuals (gate_fut.GateFut) on a fake HTTP session — no network. Response shapes are trimmed copies of
the 12.09 live captures (contracts / tickers / funding_rate / index_constituents; see the header of gate_fut.py)."""
import json, time, types
import pytest
from funding_bot import config, gate_fut, identity, identity_src, spot, universe, venues
from funding_bot.client import BannedError, PermanentHTTPError
from funding_bot.gate_fut import GateFut

H = 3600
T0 = 1789224000.0                                   # 12.09.2026 14:40:00 UTC


class _R:
    def __init__(self, body, code=200, headers=None):
        self.status_code, self._b, self.headers = code, body, headers or {}
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self): return self._b

    def raise_for_status(self):
        if self.status_code >= 500:
            raise RuntimeError(str(self.status_code))


class _S:
    def __init__(self, handler):
        self.handler, self.calls, self.headers = handler, [], {}

    def get(self, url, params=None, timeout=None):
        path = url.split("/api/v4", 1)[1]
        self.calls.append((path, dict(params or {}), timeout))
        return self.handler(path, dict(params or {}))


@pytest.fixture
def clock(monkeypatch):
    c = [T0]

    def sleep(s):
        c[0] += max(0.0, s)
    fake = types.SimpleNamespace(time=lambda: c[0], sleep=sleep, gmtime=time.gmtime, strftime=time.strftime)
    monkeypatch.setattr(gate_fut, "time", fake)
    monkeypatch.setattr(spot, "time", fake)          # SpotClient.get: pause bookkeeping on the same clock
    return c


# --- recorded shapes ---------------------------------------------------------------------------------------------------
def contract(name, ctype="", pre=False, iv=28800, quanto="1", mark="1.0", index="1.0", cap="0.02", tick="0.0001",
             size_min=1, decimal=False, status="trading", delist=False, launch=1700000000, rate="0.0001", nxt=1789228800):
    return {"name": name, "type": "direct", "status": status, "in_delisting": delist, "is_pre_market": pre,
            "contract_type": ctype, "quanto_multiplier": quanto, "order_price_round": tick, "order_size_min": size_min,
            "enable_decimal": decimal, "funding_rate": rate, "funding_rate_indicative": rate, "funding_interval": iv,
            "funding_next_apply": nxt, "funding_offset": 0, "funding_rate_limit": cap, "mark_price": mark,
            "index_price": index, "launch_time": launch, "create_time": launch, "taker_fee_rate": "0.00075",
            "maker_fee_rate": "-0.0001"}


def ticker(name, rate="0.0001", mark="1.0", index="1.0", bid="0.99", bsz="10", ask="1.01", asz="20", quanto="1"):
    return {"contract": name, "funding_rate": rate, "funding_rate_indicative": rate, "mark_price": mark,
            "index_price": index, "highest_bid": bid, "highest_size": bsz, "lowest_ask": ask, "lowest_size": asz,
            "last": mark, "quanto_multiplier": quanto}


CONTRACTS = [
    contract("BTC_USDT", quanto="0.0001", mark="77396.9", index="77422.8", cap="0.003", tick="0.1", launch=1574035200,
             rate="0.000049"),
    contract("PEPE_USDT", quanto="10000000", mark="0.000003383", size_min=0, decimal=True, cap="0.015"),
    contract("MBABYDOGE_USDT", quanto="100", mark="0.0003824"),
    contract("STORJ_USDT", iv=3600, rate="-0.019486", cap="0.02"),
    contract("IOST_USDT", iv=3600, rate="-0.001338"),
    contract("ETH_USDT", iv=14400),
    contract("EDGEX_USDT"), contract("EDGE_USDT"), contract("RON_USDT"), contract("TSTBSC_USDT"),
    contract("BROCCOLI_USDT"), contract("4STOCK_USDT", quanto="100"), contract("0G_USDT"),
    contract("BP_USDT", pre=True, cap="0.000001", rate="0"),
    contract("AAPL_USDT", "stocks", quanto="0.01", mark="333.22", cap="0.01"),
    contract("OPENAI_USDT", "stocks", pre=True, cap="0.000001", rate="0"),
    contract("TQQQX_USDT", "stocks"), contract("FUTUON_USDT", "stocks"),
    contract("CXMT_USDT", "stocks", iv=3600, rate="0"),
    contract("NAS100_USDT", "indices"), contract("B200_USDT", "indices", pre=True, cap="0.000001"),
    contract("CL_USDT", "commodities"), contract("NG_USDT", "commodities"),
    contract("XAU_USDT", "metals", quanto="0.0001", cap="0.005"), contract("XCU_USDT", "metals"),
    contract("PAXG_USDT", "metals", cap="0.0002"), contract("IAU_USDT", "metals"), contract("SLV_USDT", "metals"),
    contract("EURUSD_USDT", "forex", cap="0.0002"), contract("USDC_USDT", "forex"),
    contract("龙虾_USDT"),
    contract("OLD_USDT", delist=True), contract("HALT_USDT", status="delisting"),
    contract("SOON_USDT", launch=int(T0) + 86400), contract("WEIRD_USDT", "bonds"),
]


def world(tickers=None, contracts=None, out_us=None, on_tick=None):
    """Handler over the recorded shapes; on_tick(clock-advance hook) runs inside the tickers call."""
    contracts = CONTRACTS if contracts is None else contracts
    tickers = tickers if tickers is not None else [
        ticker("BTC_USDT", "0.000049", "77396.9", "77451.16", "77418", "78201", "77418.1", "14535", "0.0001"),
        ticker("PEPE_USDT", mark="0.000003383", bid="0.000003382", ask="0.000003384", bsz="3", asz="2.5",
               quanto="10000000"),
        ticker("STORJ_USDT", "-0.019711"), ticker("IOST_USDT", "-0.001334"), ticker("ETH_USDT"),
        ticker("EMPTY_USDT", bid="0", ask="1.0"), ticker("CROSS_USDT", bid="1.02", ask="1.01"),
        ticker("OLD_USDT"),                       # delisting: in tickers, not in the universe
    ]

    def h(path, p):
        if path == "/futures/usdt/contracts":
            return _R(contracts)
        if path == "/futures/usdt/tickers":
            if on_tick:
                on_tick()
            hdr = {"x-gate-ratelimit-requests-remain": "197", "x-gate-ratelimit-limit": "200"}
            if out_us is not None:
                hdr["x-out-time"] = str(out_us)
            return _R(tickers, headers=hdr)
        raise AssertionError(f"unexpected {path}")
    return h


def _ins(cl):
    return {i["symbol"]: i for i in cl.perp_instruments()}


# --- universe -----------------------------------------------------------------------------------------------------------
def test_perp_instruments_classes_bases_units_and_filters(clock):
    cl = GateFut(session=_S(world()))
    ins = _ins(cl)
    assert not {"OLD_USDT", "HALT_USDT", "SOON_USDT"} & set(ins)            # delisting / not trading / not launched
    cls = {s: i["cls"] for s, i in ins.items()}
    assert cls["BTC_USDT"] == cls["4STOCK_USDT"] == cls["PAXG_USDT"] == cls["USDC_USDT"] == "crypto"
    assert cls["BP_USDT"] == cls["OPENAI_USDT"] == "preipo"                   # pre-market монеты и акции
    assert cls["B200_USDT"] == "index"                   # pre-market индекса — индекс (13.09: тот же, что Bitget B200USDT)
    assert cls["AAPL_USDT"] == cls["CXMT_USDT"] == cls["IAU_USDT"] == cls["SLV_USDT"] == "equity"   # IAU/SLV = ETFs
    assert cls["NAS100_USDT"] == "index" and cls["EURUSD_USDT"] == "fx" and cls["WEIRD_USDT"] == "rwa"
    assert cls["XAU_USDT"] == cls["XCU_USDT"] == cls["CL_USDT"] == cls["NG_USDT"] == "commodity"
    base = {s: (i["base"], i["factor"]) for s, i in ins.items()}
    assert base["MBABYDOGE_USDT"] == ("BABYDOGE", 1_000_000.0)             # a million BABYDOGE per unit of price
    assert base["PEPE_USDT"] == ("PEPE", 1.0) and base["0G_USDT"] == ("0G", 1.0) and base["4STOCK_USDT"] == ("4STOCK", 1.0)
    assert [base[s][0] for s in ("EDGEX_USDT", "EDGE_USDT", "RON_USDT", "TSTBSC_USDT", "BROCCOLI_USDT")] == \
        ["EDGE", "EDGE", "RONIN", "TST", "BROCCOLI714"]
    assert [base[s][0] for s in ("NG_USDT", "XCU_USDT", "TQQQX_USDT", "FUTUON_USDT", "EURUSD_USDT", "XAU_USDT")] == \
        ["NATGAS", "COPPER", "TQQQ", "FUTU", "EUR", "XAU"]
    btc = ins["BTC_USDT"]
    assert (btc["interval_h"], btc["cap"], btc["floor"], btc["tick_size"], btc["step_size"]) == (8, 0.003, -0.003, 0.1, 0.0001)
    assert btc["min_notional"] == pytest.approx(7.73969) and btc["onboard_ms"] == 1574035200000
    assert (btc["quote"], btc["contract"], btc["exchange"], btc["base_asset"]) == ("USDT", "PERPETUAL", "gate", "BTC")
    assert ins["STORJ_USDT"]["interval_h"] == 1 and ins["ETH_USDT"]["interval_h"] == 4
    assert ins["PEPE_USDT"]["min_notional"] == pytest.approx(33.83)      # enable_decimal, size_min 0 → one contract
    assert ins["BP_USDT"]["cap"] == 0.000001
    assert ins["龙虾_USDT"]["url"] == "https://www.gate.com/futures/USDT/%E9%BE%99%E8%99%BE_USDT"
    assert ins["龙虾_USDT"]["base"] == "龙虾"


def test_empty_or_foreign_universe_raises(clock):
    with pytest.raises(RuntimeError, match="without tradable"):
        GateFut(session=_S(world(contracts=[contract("OLD_USDT", delist=True)]))).perp_instruments()
    with pytest.raises(RuntimeError, match="expected a list"):
        GateFut(session=_S(lambda p, q: _R({"label": "X"}))).perp_instruments()


# --- tick ---------------------------------------------------------------------------------------------------------------
def test_premium_and_books_share_one_slow_tickers_call_obs_is_server_time(clock):
    """461 KB take 32–40 s from the Mac: the snapshot is as old as x-out-time, and books() must not download it again."""
    started = clock[0]

    def slow():
        clock[0] += 35.0                                   # the body streams for 35 s
    s = _S(world(out_us=int((started + 2.5) * 1e6), on_tick=slow))
    cl = GateFut(session=s)
    cl.perp_instruments()
    n0 = len(s.calls)
    p = cl.premium()
    b = cl.books()
    assert len(s.calls) == n0 + 1                          # one tickers call for both
    assert s.calls[-1][2] == config.TICK_HTTP_TIMEOUT      # tick call: short timeout, one try
    assert p["BTC_USDT"]["obs"] == started + 2.5 == b["BTC_USDT"]["obs"]
    assert p["BTC_USDT"]["ts_ms"] == int((started + 2.5) * 1000)
    assert (p["BTC_USDT"]["rate"], p["BTC_USDT"]["mark"], p["BTC_USDT"]["index"]) == (0.000049, 77396.9, 77451.16)
    assert p["STORJ_USDT"]["rate"] == -0.019711
    assert "OLD_USDT" not in p and "OLD_USDT" not in b     # not in the universe
    clock[0] += 6
    cl.premium()
    assert len(s.calls) == n0 + 2                          # TTL (5 s from arrival) passed → a new snapshot
    assert cl.used_weight == 3 and cl.budget == pytest.approx(0.015)


def test_obs_falls_back_to_request_start_without_a_sane_server_time(clock):
    s = _S(world(out_us=int((T0 - 3600) * 1e6)))            # server clock an hour off
    cl = GateFut(session=s)
    assert cl.premium()["BTC_USDT"]["obs"] == T0
    cl2 = GateFut(session=_S(world()))                      # no header at all
    assert cl2.books()["BTC_USDT"]["obs"] == T0


def test_books_units_and_bad_books(clock):
    cl = GateFut(session=_S(world()))
    cl.perp_instruments()
    b = cl.books()
    assert (b["BTC_USDT"]["bid"], b["BTC_USDT"]["ask"]) == (77418.0, 77418.1)
    assert b["BTC_USDT"]["bid_qty"] == pytest.approx(7.8201) and b["BTC_USDT"]["ask_qty"] == pytest.approx(1.4535)
    assert b["PEPE_USDT"]["bid_qty"] == pytest.approx(30_000_000)   # contracts × quanto_multiplier
    cl2 = GateFut(session=_S(world()))                     # before the universe: every *_USDT row is taken
    b2 = cl2.books()
    assert "EMPTY_USDT" not in b2 and "CROSS_USDT" not in b2 and "OLD_USDT" in b2


def test_next_ms_is_the_grid_of_the_live_interval_not_funding_next_apply(clock):
    """14:59:19 on 12.09: funding_next_apply already said 16:00 for the 1 h contracts — the grid says 15:00."""
    clock[0] = 1789225159.0
    contracts = [c for c in CONTRACTS if c["name"] in ("BTC_USDT", "STORJ_USDT", "ETH_USDT")]
    contracts = [dict(c, funding_interval=14400) if c["name"] == "STORJ_USDT" else c for c in contracts]
    s = _S(world(contracts=contracts))
    cl = GateFut(session=s, ctx_ttl_s=0)
    cl.perp_instruments()
    p = cl.premium()
    assert p["STORJ_USDT"]["next_ms"] == 1789228800_000      # still 4 h in the universe → 16:00
    assert p["BTC_USDT"]["next_ms"] == 1789228800_000 and p["ETH_USDT"]["next_ms"] == 1789228800_000
    s.handler = world(contracts=[dict(c, funding_interval=3600) if c["name"] == "STORJ_USDT" else c for c in contracts])
    assert cl.funding_intervals() == {"BTC_USDT": 8, "STORJ_USDT": 1, "ETH_USDT": 4}
    assert cl.premium()["STORJ_USDT"]["next_ms"] == 1789225200_000   # 4 h → 1 h picked up without a rebuild: 15:00


# --- history ------------------------------------------------------------------------------------------------------------
def hist_world(rows, seen=None):
    """rows: [(t_s, "r")]. Emulates the live semantics: [from, to), NEWEST `limit` rows of the range, newest first."""
    def h(path, p):
        assert path == "/futures/usdt/funding_rate" and p["contract"]
        if seen is not None:
            seen.append((p, gate_fut.time.time()))
        if int(p["limit"]) > 1000:
            return _R({"label": "INVALID_PARAM_VALUE", "message": "limit"}, code=400)
        sel = sorted((x for x in rows if p["from"] <= x[0] < p["to"]), reverse=True)[:int(p["limit"])]
        return _R([{"r": r, "t": t} for t, r in sel])
    return h


def test_history_seconds_window_filter_ascending(clock):
    grid = [(int(T0) - k * H + 2, f"{-0.0001 * k:.6f}") for k in range(0, 24 * 40)]   # 40 days of 1 h, t = grid + 2 s
    seen = []
    cl = GateFut(session=_S(hist_world(grid, seen)), history_gap_s=0)
    start_ms, end_ms = int((T0 - 30 * 86400) * 1000), int(T0 * 1000)
    rows = cl.history_since("STORJ_USDT", start_ms, end_ms)
    p = seen[0][0]
    assert (p["from"], p["to"], p["limit"]) == (start_ms // 1000, end_ms // 1000 + 1, 1000)   # SECONDS, never ms
    assert len(seen) == 1 and len(rows) == 720
    assert [r["funding_ms"] for r in rows] == sorted(r["funding_ms"] for r in rows)
    assert all(start_ms <= r["funding_ms"] <= end_ms for r in rows) and rows[-1]["funding_ms"] == (int(T0) - H + 2) * 1000
    assert rows[0] == dict(exchange="gate", symbol="STORJ_USDT", funding_ms=rows[0]["funding_ms"], rate=rows[0]["rate"],
                           mark=None)
    assert venues.history_since(cl, "STORJ_USDT", end_ms - 3 * H * 1000, end_ms)[-1]["rate"] == -0.0001
    assert cl.history_since("STORJ_USDT", end_ms, end_ms - 1) == []


def test_history_zero_rate_rows_are_real_rows(clock):
    grid = [(int(T0) - k * 8 * H, "0") for k in range(1, 4)]                  # XAU weekend: settles r «0»
    rows = GateFut(session=_S(hist_world(grid)), history_gap_s=0).history_since("XAU_USDT", int((T0 - 86400) * 1000))
    assert [r["rate"] for r in rows] == [0.0, 0.0, 0.0]


def test_history_pages_backwards_and_refuses_a_silent_cut(clock, monkeypatch):
    grid = [(int(T0) - k * H, "0.0001") for k in range(1, 24 * 60 + 1)]        # 60 days of 1 h = 1440 rows
    seen = []
    cl = GateFut(session=_S(hist_world(grid, seen)), history_gap_s=0)
    rows = cl.history_since("CXMT_USDT", int((T0 - 60 * 86400) * 1000), int(T0 * 1000))
    assert len(rows) == 1440 and len({r["funding_ms"] for r in rows}) == 1440 and len(seen) == 2
    assert seen[1][0]["to"] == min(t for t, _ in grid[:1000])                  # to = the oldest t of the first page
    monkeypatch.setattr(gate_fut, "HISTORY_MAX_PAGES", 1)
    with pytest.raises(RuntimeError, match="did not reach"):
        cl.history_since("CXMT_USDT", int((T0 - 60 * 86400) * 1000), int(T0 * 1000))


def test_history_clamps_to_180_days_and_is_paced(clock):
    seen = []
    cl = GateFut(session=_S(hist_world([], seen)), history_gap_s=0.1)
    cl.history_since("BTC_USDT", int((T0 - 200 * 86400) * 1000))
    assert seen[0][0]["from"] >= int(T0) - 180 * 86400                          # older → 400 at Gate
    cl.history_since("BTC_USDT", int((T0 - 86400) * 1000))
    assert seen[1][1] - seen[0][1] == pytest.approx(0.1, abs=1e-6)            # history_gap_s between calls


# --- transport ----------------------------------------------------------------------------------------------------------
def test_429_pauses_at_least_ten_seconds_and_blocks_calls(clock):
    s = _S(lambda p, q: _R({"label": "TOO_MANY_REQUESTS"}, code=429,
                           headers={"x-gate-ratelimit-reset-timestamp": str(int(clock[0]))}))
    cl = GateFut(session=s)
    with pytest.raises(BannedError):
        cl.premium()
    assert cl.n_429 == 1 and cl.banned_until >= clock[0] + 10                   # reset header = now → useless alone
    with pytest.raises(BannedError):
        cl.books()
    assert len(s.calls) == 1                                                    # no request during the pause
    clock[0] += 11
    s.handler = lambda p, q: _R({}, code=429, headers={"x-gate-ratelimit-reset-timestamp": str(int(clock[0]) + 30)})
    with pytest.raises(BannedError):
        cl.premium()
    assert cl.banned_until == pytest.approx(clock[0] + 31)                      # a later reset wins


def test_4xx_is_permanent_and_5xx_retries(clock):
    s = _S(lambda p, q: _R({"label": "MISSING_REQUIRED_PARAM", "message": "Missing required parameter: contract"}, 400))
    with pytest.raises(PermanentHTTPError, match="MISSING_REQUIRED_PARAM"):
        GateFut(session=s, history_gap_s=0).history_since("", int((T0 - 3600) * 1000))
    assert len(s.calls) == 1
    n = []
    s2 = _S(lambda p, q: (n.append(1), _R({}, 502) if len(n) < 3 else _R([{"r": "0.0001", "t": int(T0) - 60}]))[1])
    assert len(GateFut(session=s2, history_gap_s=0).history_since("BTC_USDT", int((T0 - 3600) * 1000))) == 1
    assert len(s2.calls) == 3                                                   # history: three tries


# --- index composition --------------------------------------------------------------------------------------------------
def leg(ex, sym, price="1", w="0.2"):
    return {"exchange": ex, "symbols": [sym], "price": price, "weight": w}


INDEX = {
    "BTC_USDT": [leg("Binance", "BTC_USDT"), leg("Gate", "BTC_USDT"), leg("KuCoin", "BTC_USDT"),
                 leg("Bitget", "BTC_USDT"), leg("OKX", "BTC_USDT"), leg("Coinbase", "BTC_USD"),
                 leg("BinanceIndex", "BTC_USDT", w="0")],
    "MBABYDOGE_USDT": [leg("Bitget", "BABYDOGE_USDT", "0.0000000003827"), leg("Gate", "BABYDOGE_USDT"),
                       leg("OKX", "BABYDOGE_USDT")],
    "NG_USDT": [leg("BinanceFutures", "NATGAS_USDT", w="0.25"), leg("BinanceIndex", "NATGAS_USDT", w="0"),
                leg("GateFutures", "NG_USDT", w="0.25"), leg("Hyperliquid:XYZ", "NATGAS_USDT", w="0.25"),
                leg("OKXFutures", "NG_USDT", w="0.25")],
    "MEMECOIN_USDT": [leg("GateFutures", "MEMECOIN_USDT"), leg("UniswapV3", "MEME_WETH", w="0.4"),
                      leg("UniswapV4", "MEME_USDG", w="0.4")],
    "AGG_USDT": [leg("Gate", "AGGON_USDT", w="0.25"), leg("Ondo", "AGGON_USDT", w="0.25"),
                 leg("Massive", "AGG_USD", w="0"), leg("Infoway", "AGG_USD", w="0.25")],
    "龙虾_USDT": [leg("PancakeV3", "龙虾_USDT", w="1")],
}


def idx_world(fail=None):
    def h(path, p):
        if path == "/futures/usdt/contracts":
            return _R(CONTRACTS + [contract("MEMECOIN_USDT"), contract("AGG_USDT", "stocks")])
        assert path.startswith("/futures/usdt/index_constituents/")
        sym = path.rsplit("/", 1)[1]
        from urllib.parse import unquote
        sym = unquote(sym)
        if fail and sym in fail:
            return fail[sym]
        if sym not in INDEX:
            return _R({"label": "INVALID_PARAM_VALUE", "message": "invalid index"}, 400)
        return _R({"index": sym, "time": int(T0 * 1000), "constituents": INDEX[sym]})
    return h


def test_index_legs_shapes_units_zero_weights_and_no_index(clock, monkeypatch):
    searched = []
    monkeypatch.setattr(identity_src, "_get_json", lambda url: searched.append(url) or {"pairs": []})
    s = _S(idx_world())
    cl = GateFut(session=s, legs_gap_s=0)
    cl.perp_instruments()
    res = identity_src.index_legs(cl, ["BTC_USDT", "MBABYDOGE_USDT", "NG_USDT", "MEMECOIN_USDT", "AGG_USDT",
                                       "OPENAI_USDT", "龙虾_USDT"])
    sym = lambda k: [(x["exchange"], x["symbol"]) for x in res[k]["legs"]]
    assert sym("BTC_USDT") == [("binance", "BTCUSDT"), ("gateio", "BTC_USDT"), ("kucoin", "BTC-USDT"),
                               ("bitget", "BTCUSDT"), ("okx", "BTC_USDT"), ("coinbase", "BTC_USD")]   # weight-0 dropped
    assert sym("MBABYDOGE_USDT") == [("bitget", "BABYDOGEUSDT*1000000"), ("gateio", "BABYDOGE_USDT*1000000"),
                                     ("okx", "BABYDOGE_USDT*1000000")]
    assert sym("NG_USDT") == [("binance_future", "NATGASUSDT"), ("gateio_futures", "NG_USDT"),
                              ("hyperliquid", "NATGAS_USDT"), ("okx_futures", "NG_USDT")]
    assert sym("MEMECOIN_USDT") == [("gateio_futures", "MEMECOIN_USDT"), ("uniswapv3", "MEME-WETH"),
                                    ("uniswapv4", "MEME-USDG")]
    assert sym("AGG_USDT") == [("gateio", "AGGON_USDT"), ("ondo", "AGGON_USDT"), ("infoway", "AGG_USD")]
    assert res["OPENAI_USDT"] == {"legs": None, "dex": {}}                    # pre-market: 400 «invalid index»
    assert res["BTC_USDT"]["legs"][0]["raw"] == "Binance BTC_USDT" and res["NG_USDT"]["legs"][0]["weight"] == "0.25"
    # DEX-only indices get the shared DexScreener search, indices with our markets do not
    assert set(res["MEMECOIN_USDT"]["dex"]) == {"MEME WETH", "MEME USDG"} and res["BTC_USDT"]["dex"] == {}
    assert len(searched) == 3 and res["龙虾_USDT"]["legs"] == [
        {"exchange": "pancakeswapv3", "symbol": "龙虾-USDT", "weight": "1", "raw": "PancakeV3 龙虾_USDT"}]
    assert any(p.endswith("/%E9%BE%99%E8%99%BE_USDT") for p, _q, _t in s.calls)   # path URL-quoted


def test_index_legs_read_by_the_resolver(clock, monkeypatch):
    monkeypatch.setattr(identity_src, "_get_json", lambda url: {"pairs": []})
    cl = GateFut(session=_S(idx_world()), legs_gap_s=0)
    cl.perp_instruments()
    res = cl.index_legs(["BTC_USDT", "MBABYDOGE_USDT", "NG_USDT", "MEMECOIN_USDT"])
    rec = identity.coin_record("Baby Doge Coin", [("BSC", "0x" + "bd" * 20, True, True)], "")
    R = identity.Resolver({"gate_spot": {"coins": {"BABYDOGE": rec, "BTC": identity.coin_record("Bitcoin", [], "")},
                                         "markets": {"BABYDOGE_USDT": ["BABYDOGE", True], "BTC_USDT": ["BTC", True]}},
                           "binance_spot": {"coins": {}, "markets": {"BTCUSDT": ["BTC", True]}},
                           "bitget_spot": {"coins": {}, "markets": {"BTCUSDT": ["BTC", True]}},
                           "kucoin_spot": {"coins": {}, "markets": {"BTC-USDT": ["BTC", True]}}}, {"gate": res})
    kinds = lambda k: [(L["kind"], L.get("venue"), L.get("mult")) for L in map(R._parse, res[k]["legs"])]
    assert kinds("BTC_USDT") == [("our", "binance_spot", 1.0), ("our", "gate_spot", 1.0), ("our", "kucoin_spot", 1.0),
                                 ("our", "bitget_spot", 1.0), ("cex", "okx", 1.0), ("cex", "coinbase", 1.0)]
    assert ("our", "gate_spot", 1_000_000.0) in kinds("MBABYDOGE_USDT")        # = perp_factor / spot_factor (identity)
    assert [k for k, _v, _m in kinds("NG_USDT")][:2] == ["bnperp", "perpref"] and kinds("NG_USDT")[2][0] == "vendor"
    assert [k for k, _v, _m in kinds("MEMECOIN_USDT")] == ["perpref", "dex", "dex"]


def test_index_legs_failures(clock, monkeypatch):
    monkeypatch.setattr(identity_src, "_get_json", lambda url: {"pairs": []})
    forbidden = _R({"label": "FORBIDDEN"}, 403)
    cl = GateFut(session=_S(idx_world({"BTC_USDT": forbidden})), legs_gap_s=0)
    with pytest.raises(PermanentHTTPError):                                    # a foreign 4xx is a source failure …
        cl.index_legs(["BTC_USDT", "NG_USDT"])
    cl = GateFut(session=_S(idx_world({"MBABYDOGE_USDT": forbidden})), legs_gap_s=0)
    assert list(cl.index_legs(["NG_USDT", "MBABYDOGE_USDT", "BTC_USDT"])) == ["NG_USDT"]   # … that cuts the batch
    cl = GateFut(session=_S(idx_world({"NG_USDT": _R({}, 502)})), legs_gap_s=0)
    assert list(cl.index_legs(["NG_USDT", "BTC_USDT"])) == ["BTC_USDT"]        # 5xx of one contract: skipped
    with pytest.raises(RuntimeError, match="failed for all"):
        GateFut(session=_S(idx_world({"NG_USDT": _R({}, 502)})), legs_gap_s=0).index_legs(["NG_USDT"])
    cl = GateFut(session=_S(idx_world()), legs_gap_s=1.0)
    assert list(cl.index_legs(["BTC_USDT", "NG_USDT", "MEMECOIN_USDT"], deadline_s=0.5)) == ["BTC_USDT"]


# --- the repo around it ---------------------------------------------------------------------------------------------------
def test_interface_and_pairs_with_other_venues(clock, monkeypatch):
    monkeypatch.setattr(config, "PERP_VENUES", ("aster", "binance", "hyperliquid", "kucoin", "bitget", "gate", "lighter"))
    cl = GateFut(session=_S(world()))
    assert venues.native(cl) and venues.recent_history(cl) == [] and not venues.spot_native(cl)
    gate = cl.perp_instruments()
    binance = [dict(symbol=s, base=b, cls=c, factor=f) for s, b, c, f in (
        ("NATGASUSDT", "NATGAS", "commodity", 1.0), ("RONINUSDT", "RONIN", "crypto", 1.0),
        ("1MBABYDOGEUSDT", "BABYDOGE", "crypto", 1e6), ("BTCUSDT", "BTC", "crypto", 1.0), ("COPPERUSDT", "COPPER", "commodity", 1.0))]
    keys = {r["key"]: r for r in universe.build_ff({"binance": binance, "gate": gate})}
    assert {"binance:NATGASUSDT|gate:NG_USDT", "binance:RONINUSDT|gate:RON_USDT", "binance:BTCUSDT|gate:BTC_USDT",
            "binance:1MBABYDOGEUSDT|gate:MBABYDOGE_USDT", "binance:COPPERUSDT|gate:XCU_USDT"} <= set(keys)
    assert keys["binance:1MBABYDOGEUSDT|gate:MBABYDOGE_USDT"]["fb"] == 1e6
    # spot/futures: Gate's RONIN perp finds Gate's RON spot through the checked alias (config.SPOT_ALIASES)
    spots = {"gate_spot": [dict(symbol="RON_USDT", base="RON", factor=1.0), dict(symbol="NATGAS_USDT", base="NATGAS", factor=1.0)]}
    sf = {r["key"] for r in universe.build_sf({"gate": gate}, spots)}
    assert "gate_spot:RON_USDT|gate:RON_USDT" in sf
    assert not any("NG_USDT" in k for k in sf)                                 # commodity: no spot unless an alias says so
