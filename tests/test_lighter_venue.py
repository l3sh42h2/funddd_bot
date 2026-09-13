"""Lighter MAIN and Robinhood on a fake HTTP session and fake WS connections (no network except a loopback socket for
the WS codec). Response shapes are trimmed copies of the 12.09 live captures (see the header of lighter.py)."""
import base64, hashlib, json, socket, struct, threading, time, types
import pytest
from funding_bot import config, identity, lighter, universe, venues
from funding_bot.client import BannedError, BudgetExceeded, PermanentHTTPError
from funding_bot.lighter import LighterPerp, LighterRhPerp, LighterSpot, LighterRhSpot

H = 3600


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
    lighter._HOSTS.clear()
    yield
    lighter._HOSTS.clear()


@pytest.fixture
def clock(monkeypatch):
    c = [1789224000.0]                                       # 12.09.2026 14:40 UTC

    def sleep(s):
        c[0] += max(0.0, s)
    monkeypatch.setattr(lighter, "time", types.SimpleNamespace(time=lambda: c[0], sleep=sleep, gmtime=time.gmtime,
                                                               strftime=time.strftime))
    return c


# --- recorded shapes ---------------------------------------------------------------------------------------------------
def perp(sym, mid, status="active", fro=False, fpm=100, mark="1.0", index="1.0", pdec=4, sdec=1, created="1762627046949",
         clamp="4.0000"):
    return {"symbol": sym, "market_id": mid, "market_type": "perp", "status": status, "taker_fee": "0.0000",
            "min_base_amount": "250.0", "min_quote_amount": "10.000000", "created_at": created,
            "multiplier": "1.000000000000000000", "size_decimals": sdec, "price_decimals": pdec, "mark_price": mark,
            "index_price": index, "last_trade_price": 1.0, "open_interest": 1.0,
            "market_config": {"market_margin_mode": 0, "force_reduce_only": fro, "trading_hours": "", "hidden": False},
            "funding_premium_multiplier": fpm, "funding_clamp_small": "0.0500", "funding_clamp_big": clamp,
            "base_interest_rate": "0.0100"}


def spot(sym, mid, status="active", mult="1.000000000000000000", pdec=2, sdec=4):
    return {"symbol": sym, "market_id": mid, "market_type": "spot", "status": status, "taker_fee": "0.0000",
            "min_quote_amount": "10.000000", "created_at": "1787244811307", "multiplier": mult,
            "size_decimals": sdec, "price_decimals": pdec, "last_trade_price": 1.0}


def tok(sym, market="PERPS", at="CRYPTO", cats=(), name=None, backend=None):
    t = {"symbol": sym, "name": name or sym, "market": market, "asset_type": at, "categories": list(cats)}
    if backend:
        t["backend_symbol"] = backend
    return t


MAIN_PERPS = [perp("BTC", 1, mark="77394.1", index="77423.4", pdec=1, sdec=5),
              perp("1000PEPE", 4, mark="0.003384", pdec=6, sdec=0),
              perp("AI", 230, fpm=100),
              perp("EURUSD", 96, fpm=50), perp("USDJPY", 98, fpm=50), perp("USDHKD", 198, fpm=50, fro=True),
              perp("XAU", 92, fpm=50), perp("WTI", 145, fpm=50), perp("PAXG", 48),
              perp("SPY", 128, fpm=50), perp("SLV", 150, fpm=50), perp("US500", 180, fpm=50), perp("US10Y", 227, fpm=50),
              perp("H100", 182, fpm=50), perp("OPENAI", 120, fpm=1), perp("SKHYNIXUSD", 161, fpm=50),
              perp("SKHY", 216, fpm=50), perp("QNT", 190, fpm=50, fro=True), perp("DEAD", 300, status="inactive"),
              perp("NEWRWA", 301, fpm=50), perp("NEWCOIN", 302, fpm=100)]
MAIN_TOKENS = [tok("BTC", name="Bitcoin", cats=["MAJOR"]), tok("kPEPE", backend="1000PEPE", name="Pepe", cats=["MEMES"]),
               tok("AI", name="Artificial Inu", cats=["NEW", "MEMES"]),
               tok("EURUSD", at="RWA", cats=["FX"]), tok("USDJPY", at="RWA", cats=["FX"]), tok("USDHKD", cats=["NEW"]),
               tok("XAU", at="RWA", cats=["COMMODITIES", "MAJOR"]), tok("WTI", at="RWA", cats=["COMMODITIES", "MAJOR"]),
               tok("PAXG", at="RWA", cats=["COMMODITIES"]), tok("SPY", at="RWA", cats=["ETF", "MAJOR"]),
               tok("SLV", at="RWA", cats=["ETF", "COMMODITIES"]), tok("US500", at="RWA", cats=["ETF", "MAJOR"]),
               tok("US10Y", at="RWA", cats=["BONDS"]), tok("H100", at="RWA", cats=["COMPUTE"]),
               tok("OPENAI", at="RWA", cats=["STOCK", "PRE_IPO"]), tok("SKHYNIXUSD", at="RWA", cats=["STOCK", "KRW"]),
               tok("SKHY", at="RWA", cats=["STOCK"], name="SK Hynix ADR"), tok("QNT", at="RWA", cats=["STOCK"]),
               # spot side of the MAIN tokenlist
               tok("ETH/USDC", market="SPOT", name="Ethereum", cats=["MAJOR"]),
               tok("LINK/USDC", market="SPOT", name="Chainlink", cats=["DEFI"]),
               tok("SPY/USDC", market="SPOT", at="RWA", cats=["NEW", "ETF"], backend="rhSPY/USDC",
                   name="SPDR S&P 500 ETF Trust (Robinhood Tokenized Stock)"),
               tok("SPY", market="SPOT", at="RWA", cats=["NEW", "ETF", "HOT"], backend="rhSPY", name="rhSPY"),
               tok("XAUT/USDC", market="SPOT", at="RWA", cats=["NEW"], name="Tether Gold")]
MAIN_SPOTS = [spot("ETH/USDC", 2048), spot("LINK/USDC", 2050), spot("rhSPY/USDC", 2057), spot("XAUT/USDC", 2056),
              spot("AZTEC/USDC", 2055, status="inactive")]
RH_SPOTS = [spot("AAPL/USDG", 2049, mult="1.000566080061092436"), spot("SLV/USDG", 2067, pdec=3, sdec=3),
            spot("PONS/USDG", 2074, pdec=5, sdec=1), spot("ETH/USDG", 2048), spot("PREX/USDG", 2090),
            spot("AAPL/USDC", 2091)]
RH_TOKENS = [tok("AAPL/USDG", market="SPOT", at="RWA", cats=["STOCK"], name="Apple"),
             tok("SLV/USDG", market="SPOT", at="RWA", cats=["ETF", "COMMODITIES"], name="iShares Silver Trust"),
             tok("PONS/USDG", market="SPOT", cats=["NEW"], name="Pons"), tok("ETH/USDG", market="SPOT", name="Ethereum"),
             tok("PREX/USDG", market="SPOT", at="RWA", cats=["STOCK", "PRE_IPO"]),
             tok("AAPL", at="RWA", cats=["STOCK"], name="Apple")]


def routes(perps=(), spots=(), tokens=(), fr=(), fundings=None, assets=()):
    def h(path, p):
        if path == "/orderBookDetails":
            if p.get("filter") == "spot":
                return _R({"code": 200, "spot_order_book_details": list(spots)})
            return _R({"code": 200, "order_book_details": list(perps)})
        if path == "/tokenlist":
            return _R({"code": 200, "tokens": list(tokens)})
        if path == "/funding-rates":
            return _R({"code": 200, "funding_rates": list(fr)})
        if path == "/assetDetails":
            return _R({"code": 200, "asset_details": list(assets)})
        if path == "/fundings" and fundings is not None:
            return fundings(p)
        raise AssertionError(path)
    return h


# --- perps ---------------------------------------------------------------------------------------------------------------
def test_perp_instruments_classes_bases_and_filters():
    cl = LighterPerp(session=_S(routes(MAIN_PERPS, tokens=MAIN_TOKENS)))
    ins = {i["symbol"]: i for i in cl.perp_instruments()}
    # reduce-only (USDHKD, QNT) and inactive markets cannot be opened — not in the universe
    assert "USDHKD" not in ins and "QNT" not in ins and "DEAD" not in ins
    got = {s: (i["cls"], i["base"], i["factor"]) for s, i in ins.items()}
    assert got == {"BTC": ("crypto", "BTC", 1.0), "1000PEPE": ("crypto", "PEPE", 1000.0), "AI": ("crypto", "AI", 1.0),
                   "EURUSD": ("fx", "EUR", 1.0), "USDJPY": ("fx", "JPY", 1.0),          # like Hyperliquid xyz:EUR/xyz:JPY
                   "XAU": ("commodity", "XAU", 1.0), "WTI": ("commodity", "CL", 1.0),     # CL is the WTI contract
                   "PAXG": ("crypto", "PAXG", 1.0),                                        # gold token = coin
                   "SPY": ("equity", "SPY", 1.0), "SLV": ("equity", "SLV", 1.0),           # ETF shares, not the metal
                   "US500": ("index", "SP500", 1.0), "US10Y": ("index", "US10Y", 1.0), "H100": ("index", "H100", 1.0),
                   "OPENAI": ("preipo", "OPENAI", 1.0), "SKHYNIXUSD": ("equity", "SKHYNIX", 1.0),
                   "SKHY": ("equity", "SKHY", 1.0),                                        # ADR — another instrument
                   "NEWRWA": ("rwa", "NEWRWA", 1.0),                                       # no token: pairs with nothing
                   "NEWCOIN": ("crypto", "NEWCOIN", 1.0)}
    b = ins["BTC"]
    assert (b["interval_h"], b["quote"], b["contract"], b["market_id"], b["min_notional"]) == (1, "USDC", "main", 1, 10.0)
    assert b["tick_size"] == 0.1 and b["step_size"] == pytest.approx(1e-5) and b["onboard_ms"] == 1762627046949
    assert b["cap"] == pytest.approx(0.005) and b["floor"] == pytest.approx(-0.005)      # 4 %/8 h → 0.5 %/h
    assert ins["AI"]["name_hint"] == "Artificial Inu"
    assert ins["1000PEPE"]["url"] == "https://app.lighter.xyz/trade/kPEPE"               # front symbol
    rh = LighterRhPerp(session=_S(routes([perp("AAPL", 0, fpm=50)], tokens=[tok("AAPL", at="RWA", cats=["STOCK"])])))
    a = rh.perp_instruments()[0]
    assert (a["exchange"], a["cls"], a["quote"], a["contract"], a["url"]) == ("lighter_rh", "equity", "USDG", "rh", None)


def test_premium_takes_only_lighter_rows_and_converts_8h_to_hour(clock):
    fr = [{"market_id": 1, "exchange": "binance", "symbol": "BTC", "rate": 0.0001},
          {"market_id": 1, "exchange": "lighter", "symbol": "BTC", "rate": 9.6e-05},
          {"market_id": 1, "exchange": "bybit", "symbol": "BTC", "rate": 0.0002},
          {"market_id": 1, "exchange": "hyperliquid", "symbol": "BTC", "rate": 0.0000125},
          {"market_id": 4, "exchange": "lighter", "symbol": "1000PEPE", "rate": 0},             # int 0 [L]
          {"market_id": 230, "exchange": "binance", "symbol": "AI", "rate": 0.0003},           # no own row → no rate
          {"market_id": 300, "exchange": "lighter", "symbol": "DEAD", "rate": 0.001}]
    s = _S(routes(MAIN_PERPS, fr=fr))
    p = LighterPerp(session=s).premium()
    assert set(p) == {"BTC", "1000PEPE"}                                                    # DEAD inactive, AI without row
    assert p["BTC"]["rate"] == pytest.approx(1.2e-05) and p["1000PEPE"]["rate"] == 0.0
    assert (p["BTC"]["mark"], p["BTC"]["index"]) == (77394.1, 77423.4)
    now_ms = int(clock[0] * 1000)
    assert p["BTC"]["next_ms"] == (now_ms // 3600_000 + 1) * 3600_000 and p["BTC"]["obs"] == clock[0]
    assert [c[1] for c in s.calls] == ["/funding-rates", "/orderBookDetails"]


def _fnd(ts, rate, direction, value="0.1"):
    return {"timestamp": ts, "value": value, "rate": rate, "direction": direction}


def test_history_signs_percent_seconds_and_mark_from_value(clock):
    t0 = 1788865200                                                                          # on the hour, seconds
    rows = [_fnd(t0 - H, "0.0040", "short"),                                                # before start (count_back > 0)
            _fnd(t0, "0.0006", "long", "0.47138160"), _fnd(t0 + H, "0.0040", "short", "0.30950000"),
            _fnd(t0 + 2 * H, "0.0000", "long", "0.00000000"), _fnd(t0 + 3 * H, "0.0012", "", "0.9")]  # side missing
    s = _S(routes(MAIN_PERPS, fundings=lambda p: _R({"code": 200, "resolution": "1h", "fundings": rows})))
    cl = LighterPerp(session=s, history_gap_s=0)
    out = cl.history_since("BTC", t0 * 1000, (t0 + 3 * H) * 1000)
    assert [r["funding_ms"] for r in out] == [t0 * 1000, (t0 + H) * 1000, (t0 + 2 * H) * 1000]
    assert out[0]["rate"] == pytest.approx(6e-6) and out[1]["rate"] == pytest.approx(-4e-5) and out[2]["rate"] == 0.0
    assert out[0]["mark"] == pytest.approx(78563.6) and out[2]["mark"] is None and out[0]["exchange"] == "lighter"
    _h, path, p = s.calls[-1]
    assert path == "/fundings" and p == dict(market_id=1, resolution="1h", start_timestamp=t0,
                                             end_timestamp=t0 + 3 * H, count_back=0)


def test_history_pages_backwards_past_750_rows(clock):
    full = [1786000000 // H * H + i * H for i in range(800)]

    def fnd(p):
        rows = [t for t in full if p["start_timestamp"] <= t <= p["end_timestamp"]][-750:]     # the NEWEST 750 [L]
        return _R({"code": 200, "resolution": "1h", "fundings": [_fnd(t, "0.0012", "long") for t in rows]})
    s = _S(routes(MAIN_PERPS, fundings=fnd))
    out = LighterPerp(session=s, history_gap_s=0).history_since("BTC", full[0] * 1000, full[-1] * 1000)
    assert len(out) == 800 and [r["funding_ms"] for r in out] == [t * 1000 for t in full]
    fcalls = [c[2] for c in s.calls if c[1] == "/fundings"]
    assert len(fcalls) == 2 and fcalls[1]["end_timestamp"] == full[50] - 1


def test_history_learns_market_ids_once_per_host(clock):
    s = _S(routes(MAIN_PERPS, fundings=lambda p: _R({"code": 200, "fundings": []})))
    a = LighterPerp(session=s, history_gap_s=0)
    a.history_since("BTC", 0, 1000)
    b = LighterPerp(session=s, history_gap_s=0)                  # the collector's background copy (make_clients())
    b.history_since("1000PEPE", 0, 1000)
    assert [c[1] for c in s.calls] == ["/orderBookDetails", "/fundings", "/fundings"]
    with pytest.raises(RuntimeError, match="no market NOPE"):
        b.history_since("NOPE", 0, 1000)                         # ids are fresh — no refetch within IDS_TTL_S
    assert len(s.calls) == 3
    assert b.recent_history() == []


# --- transport: 429, 4xx, pacing ---------------------------------------------------------------------------------------
def test_429_pauses_the_host_for_everyone_and_honours_retry_after(clock):
    s = _S(lambda path, p: _R({"message": "too many"}, 429))
    cl = LighterPerp(session=s)
    with pytest.raises(BannedError):
        cl.premium()
    assert cl.banned_until == pytest.approx(clock[0] + 60) and cl.n_429 == 1
    sp = LighterSpot(session=s)                                  # same host → same pause, no request
    with pytest.raises(BannedError):
        sp.spot_instruments()
    assert len(s.calls) == 1
    rh = LighterRhPerp(session=_S(routes(MAIN_PERPS, fr=[])))    # another host → not paused
    assert rh.premium() == {}
    clock[0] += 61
    s2 = _S(lambda path, p: _R({}, 405, {"Retry-After": "7"}))   # doc: 405 is a rate-limit answer too
    with pytest.raises(BannedError):
        LighterPerp(session=s2).premium()
    assert cl.banned_until == pytest.approx(clock[0] + 7) and cl.health()["banned_until"] == int(clock[0] + 7)


def test_bad_param_is_permanent_and_foreign_body_code_retries(clock):
    s = _S(lambda path, p: _R({"code": 20001, "message": "invalid param "}, 400))
    with pytest.raises(PermanentHTTPError, match="400"):
        LighterPerp(session=s).perp_instruments()
    s2 = _S(lambda path, p: _R({"code": 29500, "message": "internal"}))
    cl = LighterPerp(session=s2)
    with pytest.raises(RuntimeError, match="29500"):
        cl.perp_instruments()
    assert len(s2.calls) == 3 and cl.n_err == 3


def test_history_is_paced_and_tick_refuses_a_full_window(clock):
    s = _S(routes(MAIN_PERPS, fr=[], fundings=lambda p: _R({"code": 200, "fundings": []})))
    cl = LighterPerp(session=s, history_gap_s=1.5)
    t = []
    for _ in range(3):
        cl.history_since("BTC", 0, 1000)
        t.append(clock[0])
    assert t[1] - t[0] == pytest.approx(1.5) and t[2] - t[1] == pytest.approx(1.5)
    h = lighter._host(cl.rest)
    h.calls.extend([clock[0]] * (lighter.TICK_CAP - len(h.calls)))
    n = len(s.calls)
    with pytest.raises(BudgetExceeded):
        cl.premium()                                             # the tick never waits: the next tick retries
    assert len(s.calls) == n and cl.health()["used_weight"] == lighter.TICK_CAP
    t1 = clock[0]
    cl.history_since("BTC", 0, 1000)                             # history waits for the window to drain
    assert clock[0] - t1 >= 59 and len(s.calls) == n + 1


def test_tokenlist_outage_uses_a_recent_copy(clock):
    state = {"down": False}

    def h(path, p):
        if path == "/tokenlist" and state["down"]:
            return _R({}, 503)
        return routes(MAIN_PERPS, tokens=MAIN_TOKENS)(path, p)
    cl = LighterPerp(session=_S(h))
    assert {i["symbol"]: i["cls"] for i in cl.perp_instruments()}["EURUSD"] == "fx"
    state["down"] = True
    clock[0] += lighter.TOKENS_TTL_S + 1
    assert {i["symbol"]: i["cls"] for i in cl.perp_instruments()}["EURUSD"] == "fx"
    clock[0] += lighter.TOKENS_STALE_OK_S
    with pytest.raises(RuntimeError):
        cl.perp_instruments()                                    # too old: keep the previous universe instead


# --- spots ---------------------------------------------------------------------------------------------------------------
def test_spot_instruments_stock_tokens_coins_and_pairs(monkeypatch):
    m = LighterSpot(session=_S(routes(spots=MAIN_SPOTS, tokens=MAIN_TOKENS)))
    ins = {i["symbol"]: i for i in m.spot_instruments()}
    assert set(ins) == {"ETH/USDC", "LINK/USDC", "rhSPY/USDC", "XAUT/USDC"}                # AZTEC inactive
    assert (ins["rhSPY/USDC"]["base"], ins["rhSPY/USDC"]["alt_base"]) == ("~RHSPY", "SPY")
    assert (ins["XAUT/USDC"]["base"], ins["XAUT/USDC"]["alt_base"]) == ("XAUT", None)      # RWA/NEW but not a stock
    assert ins["rhSPY/USDC"]["url"] == "https://app.lighter.xyz/trade/SPY_USDC"
    assert ins["ETH/USDC"]["tick_size"] == 0.01 and ins["ETH/USDC"]["min_notional"] == 10.0
    r = LighterRhSpot(session=_S(routes(spots=RH_SPOTS, tokens=RH_TOKENS)))
    rins = {i["symbol"]: i for i in r.spot_instruments()}
    assert set(rins) == {"AAPL/USDG", "SLV/USDG", "PONS/USDG", "ETH/USDG"}                # pre-IPO, USDC quote — out
    assert (rins["AAPL/USDG"]["base"], rins["AAPL/USDG"]["alt_base"]) == ("~AAPL", "AAPL")
    assert rins["AAPL/USDG"]["factor"] == pytest.approx(1.000566080061092436) and rins["AAPL/USDG"]["url"] is None
    assert rins["SLV/USDG"]["alt_base"] == "SLV" and rins["PONS/USDG"]["base"] == "PONS"
    # pairs: stock token ↔ equity perp only; SLV token never meets the silver perp; XAUT ↔ gold perp via the alias
    monkeypatch.setattr(config, "PERP_VENUES", config.PERP_VENUES + ("lighter", "lighter_rh"))
    monkeypatch.setattr(config, "SPOT_VENUES", config.SPOT_VENUES + ("lighter_spot", "lighter_rh_spot"))
    perps = {"lighter": [dict(symbol="XAU", base="XAU", cls="commodity", factor=1.0)],
             "lighter_rh": [dict(symbol="AAPL", base="AAPL", cls="equity", factor=1.0),
                            dict(symbol="SLV", base="SLV", cls="equity", factor=1.0),
                            dict(symbol="XAG", base="XAG", cls="commodity", factor=1.0)]}
    sf = universe.build_sf(perps, {"lighter_spot": list(ins.values()), "lighter_rh_spot": list(rins.values())})
    assert {r["key"] for r in sf} == {"lighter_spot:XAUT/USDC|lighter:XAU", "lighter_rh_spot:AAPL/USDG|lighter_rh:AAPL",
                                      "lighter_rh_spot:SLV/USDG|lighter_rh:SLV"}


def test_coin_blob_contracts_names_markets_and_shares():
    assets = [{"asset_id": 1, "symbol": "ETH", "l1_address": "0x0000000000000000000000000000000000000000",
               "multiplier": "1.000000000000000000"},
              {"asset_id": 4, "symbol": "AAPL", "l1_address": "0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9",
               "multiplier": "1.000566080061092436"},
              {"asset_id": 29, "symbol": "PONS", "l1_address": "0x39dBED3a2bd333467115dE45665cC57F813C4571",
               "multiplier": "1.000000000000000000"}]
    r = LighterRhSpot(session=_S(routes(spots=RH_SPOTS, tokens=RH_TOKENS, assets=assets)))
    blob = r.coin_blob()
    assert blob["coins"]["AAPL"]["ids"] == ["0xaf3d76f1834a1d425780943c99ea8a608f8a93f9"]
    assert blob["coins"]["AAPL"]["name"] == "Apple" and blob["coins"]["PONS"]["name"] == "Pons"
    assert blob["coins"]["PONS"]["dex"] == {"4663": ["0x39dbed3a2bd333467115de45665cc57f813c4571"]}   # Robinhood Chain
    assert blob["markets"]["AAPL/USDG"] == ["AAPL", True]
    assert blob["shares"] == {"AAPL/USDG": {"n": pytest.approx(1.000566080061092436), "src": "Lighter rh"},
                              "SLV/USDG": {"n": 1.0, "src": "Lighter rh"}}
    assert blob["need_n"] == ["AAPL/USDG", "SLV/USDG"]
    R = identity.Resolver({"lighter_rh_spot": blob}, {})
    v = identity.decide_sf(R, dict(cls="equity", spot_ex="lighter_rh_spot", spot="AAPL/USDG", base="AAPL",
                                   spot_tag="AAPL", perp_ex="lighter_rh", perp="AAPL"))
    assert v["ident"] == "same" and v["ident_ev"] == "stock_token"
    m = LighterSpot(session=_S(routes(spots=MAIN_SPOTS, tokens=MAIN_TOKENS, assets=[
        {"symbol": "ETH", "l1_address": "0x0000000000000000000000000000000000000000"},
        {"symbol": "LINK", "l1_address": "0x514910771AF9Ca656af840dff83E8264EcF986CA"},
        {"symbol": "rhSPY", "l1_address": "0xE43A5b52e317dC57770ba96BcD33C2bd5dC97c5C"}])))
    mb = m.coin_blob()
    assert mb["coins"]["ETH"]["nat"] == ["eth"] and mb["coins"]["LINK"]["name"] == "Chainlink"
    assert mb["coins"]["rhSPY"]["name"].startswith("SPDR S&P 500") and mb["markets"]["AZTEC/USDC"] == ["AZTEC", False]
    assert identity.Resolver({"lighter_spot": mb}, {}).code_of("lighter_spot", "LINK/USDC") == "LINK"


def test_interface_shape_and_ff_alignment_with_hyperliquid(monkeypatch):
    for c in (LighterPerp, LighterRhPerp):
        assert venues.native(c(session=_S(routes()))) and not venues.spot_native(c(session=_S(routes())))
    for c in (LighterSpot, LighterRhSpot):
        assert venues.spot_native(c(session=_S(routes()))) and not venues.native(c(session=_S(routes())))
    assert [c.name for c in (LighterPerp, LighterRhPerp, LighterSpot, LighterRhSpot)] == \
        ["lighter", "lighter_rh", "lighter_spot", "lighter_rh_spot"]
    assert set(lighter.PERP_CLIENTS) == {"lighter", "lighter_rh"} and set(lighter.SPOT_CLIENTS) == {"lighter_spot", "lighter_rh_spot"}
    monkeypatch.setattr(config, "PERP_VENUES", ("hyperliquid", "lighter"))
    ins = LighterPerp(session=_S(routes(MAIN_PERPS, tokens=MAIN_TOKENS))).perp_instruments()
    hl = [dict(symbol=s, base=b, cls=c, factor=1.0) for s, b, c in
          (("xyz:SP500", "SP500", "index"), ("xyz:JPY", "JPY", "fx"), ("xyz:CL", "CL", "commodity"),
           ("xyz:SKHX", "SKHYNIX", "equity"), ("BTC", "BTC", "crypto"), ("kPEPE", "PEPE", "crypto"))]
    keys = {r["key"] for r in universe.build_ff({"hyperliquid": hl, "lighter": ins})}
    assert keys == {"hyperliquid:xyz:SP500|lighter:US500", "hyperliquid:xyz:JPY|lighter:USDJPY",
                    "hyperliquid:xyz:CL|lighter:WTI", "hyperliquid:xyz:SKHX|lighter:SKHYNIXUSD",
                    "hyperliquid:BTC|lighter:BTC", "hyperliquid:kPEPE|lighter:1000PEPE"}


# --- books via the stream -----------------------------------------------------------------------------------------------
def _stats(mid, sym, bid, ask, **kw):
    return dict({"symbol": sym, "market_id": mid, "best_bid_price": bid, "best_ask_price": ask, "mid_price": "",
                 "mark_price": "1", "index_price": "1", "current_funding_rate": "0.0012", "funding_rate": "0.0012",
                 "funding_timestamp": 1789221600000}, **kw)


def _snap(key, rows, typ="subscribed"):
    return {"channel": f"{key}:all", key: {str(r["market_id"]): r for r in rows}, "timestamp": 1789224555089,
            "type": f"{typ}/{key}"}


def test_stream_snapshot_deltas_and_books(clock):
    st = lighter._Stream("wss://x/stream", "market_stats/all", "market_stats", "lighter")
    assert st.apply(_snap("market_stats", [_stats(0, "ETH", "2539.85", "2540.00"), _stats(1, "BTC", "77412.3", "77418.6"),
                                           _stats(160, "SAMSUNG", "", ""), _stats(9, "WIDE", "1.0", "1.2")]), 100.0)
    assert st.apply({"channel": "market_stats:all", "type": "update/market_stats",
                     "market_stats": {"0": {"market_id": 0, "best_bid_price": "2539.90"}}}, 105.0)   # partial delta
    assert not st.apply({"type": "pong"}, 106.0) and st.last_rx == 105.0
    cl = LighterPerp(session=_S(routes(MAIN_PERPS)))
    cl._stream, st.start, st._waited = st, (lambda: None), True
    cl._h.perp_syms = {0: "ETH", 1: "BTC", 160: "SAMSUNGUSD", 9: "WIDE"}   # books keyed by market id, not WS symbol
    b = cl.books()
    assert set(b) == {"ETH", "BTC"}                              # empty side and a 18 % spread are not prices
    assert b["ETH"] == dict(bid=2539.9, ask=2540.0, bid_qty=0.0, ask_qty=0.0, obs=105.0)   # ask kept from the snapshot
    assert b["BTC"]["obs"] == 105.0                              # obs = connection's last data frame, not BTC's update
    st.apply({"channel": "market_stats:all", "type": "update/market_stats",
              "market_stats": {"0": {"market_id": 0, "best_bid_price": "2541.00"}}}, 110.0)
    assert "ETH" not in cl.books()                               # crossed after a delta — not a price
    # a new snapshot replaces the cache: a market that vanished drops out
    st.apply(_snap("market_stats", [_stats(1, "BTC", "77000", "77001")]), 200.0)
    assert set(cl.books()) == {"BTC"}


def test_stream_book_with_crossed_or_one_sided_side_is_dropped():
    assert lighter._book("0.59", "") is None and lighter._book("2", "1") is None and lighter._book("0", "1") is None
    assert lighter._book("160000", "160804.75") is not None and lighter._book("1", "1.06") is None


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


def test_stream_reconnects_after_silence_and_close_and_pings(clock):
    delta = {"channel": "spot_market_stats:all", "type": "update/spot_market_stats",
             "spot_market_stats": {"2048": {"market_id": 2048, "best_bid_price": "2541.2", "best_ask_price": "2542.0"}}}
    conns = [_Conn([_snap("spot_market_stats", [_stats(2048, "ETH/USDC", "2541.16", "2542.29"),
                                                _stats(2049, "LIT/USDC", "4.3270", "4.3319")]),
                    25, delta, 25, delta, 25, delta], clock),          # data every 25 s → ping after 60 s; then silence
             _Conn([_snap("spot_market_stats", [_stats(2048, "ETH/USDC", "2540", "2541")]), "close"], clock)]
    made = []

    def connect(url, timeout):
        made.append(url)
        return conns[len(made) - 1]
    st = lighter._Stream("wss://api.rh.lighter.xyz/stream?readonly=true", "spot_market_stats/all", "spot_market_stats",
                         "lighter_rh_spot", connect)
    st.backoff0 = 0.0
    st._run(max_sessions=2)
    assert made == [st.url, st.url] and st.n_conn == 2 and all(c.closed for c in conns)
    assert conns[0].sent[0] == {"type": "subscribe", "channel": "spot_market_stats/all"} and {"type": "ping"} in conns[0].sent
    assert conns[1].sent == [{"type": "subscribe", "channel": "spot_market_stats/all"}]
    data, _last = st.snapshot()
    assert set(data) == {2048} and data[2048]["bid"] == "2540"   # second snapshot replaced the first
    assert "closed by server" in st.err and not st.synced.is_set()


# --- WS codec on a loopback socket ---------------------------------------------------------------------------------------
def _frame(op, payload, fin=True):
    n = len(payload)
    h = bytes([(0x80 if fin else 0) | op])
    if n < 126:
        h += bytes([n])
    elif n < 65536:
        h += bytes([126]) + struct.pack(">H", n)
    else:
        h += bytes([127]) + struct.pack(">Q", n)
    return h + payload


def _read_client_frame(sock):
    def rn(k):
        b = b""
        while len(b) < k:
            b += sock.recv(k - len(b))
        return b
    b0, b1 = rn(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", rn(2))[0]
    assert b1 & 0x80                                             # client frames are masked (RFC 6455)
    mask = rn(4)
    return b0 & 0x0F, bytes(x ^ mask[i & 3] for i, x in enumerate(rn(n)))


def test_ws_codec_fragments_ping_long_frames_and_close():
    a, b = socket.socketpair()
    a.settimeout(5); b.settimeout(5)
    big1, big2 = b"x" * 300, b"y" * 70000
    srv = threading.Thread(target=lambda: b.sendall(_frame(1, b'{"a"', fin=False) + _frame(0, b":1}") + _frame(9, b"hi")
                                                    + _frame(1, big1) + _frame(1, big2) + _frame(8, struct.pack(">H", 1000))))
    srv.start()
    ws = lighter._WS.from_socket(a)
    assert ws.recv() == (1, b'{"a":1}')
    assert ws.recv() == (1, big1)                                # the ping in between was answered
    assert ws.recv() == (1, big2) and ws.recv()[0] == 8
    srv.join()
    assert _read_client_frame(b) == (0xA, b"hi")
    ws.send_text('{"type":"ping"}')
    assert _read_client_frame(b) == (0x1, b'{"type":"ping"}')
    a.close(); b.close()


def _server(reply_accept):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0)); srv.listen(1)
    seen = {}

    def run():
        c, _ = srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += c.recv(4096)
        lines = req.decode().split("\r\n")
        seen["line"] = lines[0]
        key = next(ln.split(":", 1)[1].strip() for ln in lines if ln.lower().startswith("sec-websocket-key"))
        acc = base64.b64encode(hashlib.sha1((key + lighter._GUID).encode()).digest()).decode() if reply_accept else "bad"
        c.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Accept: {acc}\r\n\r\n".encode() + _frame(1, b'{"type":"connected"}'))
        time.sleep(0.2)
        c.close(); srv.close()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return srv.getsockname()[1], seen, t


def test_ws_handshake_checks_accept_key_and_keeps_early_frames():
    port, seen, t = _server(True)
    ws = lighter._WS(f"ws://127.0.0.1:{port}/stream?readonly=true", 5)
    assert ws.recv() == (1, b'{"type":"connected"}') and seen["line"] == "GET /stream?readonly=true HTTP/1.1"
    ws.close(); t.join()
    port, _seen, t = _server(False)
    with pytest.raises(ConnectionError, match="Accept"):
        lighter._WS(f"ws://127.0.0.1:{port}/stream", 5)
    t.join()
