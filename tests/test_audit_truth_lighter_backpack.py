"""Тестировщик: истины Lighter (основной инстанс и Robinhood Chain) и Backpack на подставном HTTP / сокете. Сети нет —
тела повторяют замер 12-13.09 (поля и ловушки из шапок audit_truth/lighter.py и audit_truth/backpack.py)."""
import inspect, json, re, socket, struct, threading, time
from datetime import datetime, timezone
from decimal import Decimal
import pytest
from funding_bot.audit_truth import TRUTHS, TRUTH_ERRORS, base
from funding_bot.audit_truth import lighter as L
from funding_bot.audit_truth import backpack as B

H = 3600_000


class FakeHttp:
    """Подставной HTTP истины: GET по окончанию пути; тело — значение или функция(params)."""
    def __init__(self, get):
        self._get, self.log, self.calls = get, [], 0

    def get(self, url, params=None, gap=None):
        self.calls += 1; self.log.append((url, dict(params or {}), gap))
        body = next(v for k, v in self._get.items() if url.endswith(k))
        return body(params) if callable(body) else body

    def post(self, url, body=None, gap=None):
        raise AssertionError("POST у Lighter / Backpack не нужен")


# --- реестр и независимость -------------------------------------------------------------------------------------
def test_registry_has_both_lighter_instances_and_backpack():
    assert TRUTHS["lighter"] is L.LighterTruth and TRUTHS["lighter_rh"] is L.LighterRhTruth
    assert TRUTHS["backpack"] is B.BackpackTruth
    assert not {k: v for k, v in TRUTH_ERRORS.items() if k in ("lighter", "lighter_rh", "backpack")}
    assert L.LighterRhTruth.REST == "https://api.rh.lighter.xyz/api/v1" != L.LighterTruth.REST
    assert L.LighterRhTruth.WS.startswith("wss://api.rh.lighter.xyz/")


def test_truths_import_nothing_of_the_collector():
    for mod in (L, B):
        rel = re.findall(r"^from\s+(\.[\w.]*)\s+import", inspect.getsource(mod), re.M)
        assert set(rel) <= {".base"}, rel                      # только общий base пакета истин
        assert "funding_bot." not in inspect.getsource(mod)


# --- Lighter: рынки -----------------------------------------------------------------------------------------------
def _m(sym, mid, status="active", fro=False, hidden=False, created=1743182782837):
    return dict(symbol=sym, market_id=mid, market_type="perp", status=status, created_at=str(created),
                market_config=dict(force_reduce_only=fro, hidden=hidden, trading_hours=""))


DET = {"code": 200, "spot_order_book_details": [], "order_book_details": [
    _m("BTC", 1), _m("1000PEPE", 4), _m("AAPL", 10), _m("SLV", 11), _m("XAU", 12), _m("PAXG", 13), _m("EURUSD", 14),
    _m("USDHKD", 15), _m("SAMSUNGUSD", 16), _m("SAMSUNG", 140, status="inactive"), _m("US500", 17),
    _m("KRCOMP", 142, status="inactive"), _m("H100", 18), _m("OPENAI", 19), _m("AAOI", 20), _m("NEWCOIN", 21),
    _m("US10Y", 22), _m("DIA", 152, fro=True), _m("HID", 23, hidden=True), _m("SOON", 24, created=4_000_000_000_000),
    _m("SPY", 25), _m("DUP", 30, status="inactive"), _m("DUP", 31)]}


def _tok(sym, cats, name=None, at="RWA", market="PERPS", backend=None):
    t = dict(symbol=sym, market=market, asset_type=at, categories=cats, name=name or sym)
    if backend:
        t["backend_symbol"] = backend
    return t


TOK = {"code": 200, "tokens": [
    _tok("BTC", ["LAYER_1"], "Bitcoin", at="CRYPTO"), _tok("kPEPE", ["MEMES"], "Pepe", at="CRYPTO", backend="1000PEPE"),
    _tok("AAPL", ["STOCK"], "Apple"), _tok("SLV", ["ETF", "COMMODITIES"], "iShares Silver Trust"),
    _tok("XAU", ["COMMODITIES", "MAJOR"], "Gold"), _tok("PAXG", ["COMMODITIES"], "PAX Gold"),
    _tok("EURUSD", ["FX"], "EUR vs USD"), _tok("USDHKD", ["NEW"], at="CRYPTO"),
    _tok("SAMSUNGUSD", ["STOCK", "KRW"], "Samsung Electronics Co., Ltd."),
    _tok("SAMSUNG", ["STOCK", "KRW"], "Samsung Electronics Co., Ltd."), _tok("US500", ["ETF", "MAJOR"]),
    _tok("KRCOMP", ["ETF", "KRW"], "Korea Composite Stock Price Index"), _tok("H100", ["COMPUTE"]),
    _tok("OPENAI", ["STOCK", "PRE_IPO"], "OPENAI Pre IPO"), _tok("AAOI", ["NEW"]), _tok("US10Y", ["BONDS"]),
    _tok("DIA", ["ETF"], "State Street SPDR Dow Jones Industrial Average ETF"),
    _tok("SPY", ["ETF", "MAJOR"], "SPDR S&P 500 ETF Trust"),
    _tok("BTC", ["STOCK"], market="SPOT")]}                 # строка спота — не для перпов


def test_lighter_markets_tradable_classes_and_bases():
    http = FakeHttp({"/orderBookDetails": DET, "/tokenlist": TOK})
    m = L.LighterTruth(http, ws=lambda *a: {}).markets()
    assert m["BTC"]["tradable"] and m["BTC"]["cls"] == "crypto" and m["BTC"]["name"] == "Bitcoin"
    assert all(x["interval_h"] == 1 for x in m.values())
    assert m["1000PEPE"]["cls"] == "crypto" and m["1000PEPE"]["base"] == "1000PEPE"    # класс по backend_symbol
    assert base.norm_base(m["1000PEPE"]["base"]) == ("PEPE", 1000.0)
    cls = {s: m[s]["cls"] for s in ("AAPL", "SLV", "XAU", "PAXG", "EURUSD", "USDHKD", "SAMSUNGUSD", "US500", "KRCOMP",
                                     "H100", "OPENAI", "AAOI", "NEWCOIN", "US10Y", "DIA", "SPY")}
    assert cls == dict(AAPL="equity", SLV="equity", XAU="commodity", PAXG="crypto", EURUSD="fx", USDHKD="fx",
                       SAMSUNGUSD="equity", US500="index", KRCOMP="index", H100="index", OPENAI="preipo", AAOI="equity",
                       NEWCOIN=None, US10Y="index", DIA="equity", SPY="equity")
    assert (m["EURUSD"]["base"], m["USDHKD"]["base"], m["SAMSUNGUSD"]["base"], m["SAMSUNG"]["base"]) == \
        ("EUR", "HKD", "SAMSUNG", "SAMSUNG")
    assert "нет в tokenlist" in m["NEWCOIN"]["note"] and m["NEWCOIN"]["tradable"]
    assert not m["SAMSUNG"]["tradable"] and "статус inactive" in m["SAMSUNG"]["note"]
    assert not m["DIA"]["tradable"] and "force_reduce_only" in m["DIA"]["note"]
    assert not m["HID"]["tradable"] and "hidden" in m["HID"]["note"]
    assert not m["SOON"]["tradable"] and "до запуска" in m["SOON"]["note"]
    assert m["DUP"]["tradable"] and "market_id 31" in m["DUP"]["note"]                # повтор символа: торгуемый
    assert http.log[0] == (L.LighterTruth.REST + "/orderBookDetails", {"filter": "perp"}, None)
    assert all(u.startswith(L.LighterTruth.REST) for u, _p, _g in http.log)


def test_lighter_rh_is_its_own_host():
    http = FakeHttp({"/orderBookDetails": DET, "/tokenlist": TOK})
    T = L.LighterRhTruth(http, ws=lambda *a: {})
    T.markets()
    assert T.venue == "lighter_rh" and all(u.startswith("https://api.rh.lighter.xyz/api/v1/") for u, _p, _g in http.log)


def test_lighter_tokenlist_down_leaves_classes_unknown_but_markets_listed():
    def boom(_p):
        raise RuntimeError("HTTP 503")
    m = L.LighterTruth(FakeHttp({"/orderBookDetails": DET, "/tokenlist": boom}), ws=lambda *a: {}).markets()
    assert m["AAPL"]["cls"] is None and m["PAXG"]["cls"] == "crypto" and m["EURUSD"]["cls"] == "fx"


# --- Lighter: ставки ------------------------------------------------------------------------------------------------
def test_lighter_rates_from_ws_percent_per_hour():
    snap = {"1": dict(market_id=1, symbol="BTC", current_funding_rate="0.0010", funding_rate="0.0012",
                      funding_timestamp=1789254000000),
            "4": dict(market_id=4, symbol="1000PEPE", current_funding_rate="-0.0091"),
            "7": dict(market_id=7, symbol="ODD", current_funding_rate="")}
    seen = []

    def ws(url, ch, key, t):
        seen.append((url, ch, key)); return snap
    http = FakeHttp({})
    T = L.LighterTruth(http, ws=ws)
    r = T.rates()
    assert r["BTC"]["rate"] == Decimal("0.00001") and r["BTC"]["interval_h"] == 1 and r["BTC"]["kind"] == "predicted"
    assert r["1000PEPE"]["rate"] == Decimal("-0.000091") and "ODD" not in r
    assert r["BTC"]["next_ms"] % H == 0 and 0 < r["BTC"]["next_ms"] - base.now_ms() <= H
    assert seen == [(L.LighterTruth.WS, "market_stats/all", "market_stats")] and http.calls == 0
    assert T.rates_source.startswith("WS")


def test_lighter_rates_fall_back_to_rest_per_8h_lighter_rows_only():
    def ws_down(*a):
        raise ConnectionError("нет сети")
    fr = {"code": 200, "funding_rates": [dict(market_id=1, exchange="binance", symbol="BTC", rate=4.429e-05),
                                         dict(market_id=1, exchange="lighter", symbol="BTC", rate=8e-05),
                                         dict(market_id=4, exchange="hyperliquid", symbol="1000PEPE", rate=0.0001),
                                         dict(market_id=4, exchange="lighter", symbol="1000PEPE", rate=-0.000728)]}
    T = L.LighterTruth(FakeHttp({"/orderBookDetails": DET, "/funding-rates": fr}), ws=ws_down)
    r = T.rates()
    assert r["BTC"]["rate"] == Decimal("0.00001") and r["1000PEPE"]["rate"] == Decimal("-0.000091")
    assert set(r) == {"BTC", "1000PEPE"} and "REST" in T.rates_source and "нет сети" in T.rates_source


# --- Lighter: история -----------------------------------------------------------------------------------------------
def _fundings(calls):
    def f(p):
        calls.append(p)
        assert p["resolution"] == "1h" and p["count_back"] == 0 and p["market_id"] == 1
        lo, hi = p["start_timestamp"], p["end_timestamp"]
        ts = [t for t in range(-(-lo // 3600) * 3600, hi + 1, 3600)][-750:]     # 750 САМЫХ НОВЫХ в окне
        return {"code": 200, "resolution": "1h", "fundings": [
            dict(timestamp=t, value="0.9", rate="0.0012", direction="short" if (t // 3600) % 3 == 0 else "long")
            for t in ts]}
    return f


def test_lighter_history_unsigned_percent_direction_and_backward_paging():
    calls = []
    T = L.LighterTruth(FakeHttp({"/orderBookDetails": DET, "/fundings": _fundings(calls)}), ws=lambda *a: {})
    end_s = 1789254000
    start_ms, end_ms = (end_s - 999 * 3600) * 1000, end_s * 1000
    h = T.history("BTC", start_ms, end_ms)
    assert len(h) == 1000 and h[0][0] == start_ms and h[-1][0] == end_ms
    assert all(a[0] < b[0] for a, b in zip(h, h[1:]))
    sign = {ms: r for ms, r in h}
    assert sign[end_ms] == (Decimal("-0.000012") if (end_s // 3600) % 3 == 0 else Decimal("0.000012"))
    assert {abs(r) for r in sign.values()} == {Decimal("0.000012")} and any(r < 0 for r in sign.values())
    assert len(calls) == 2 and calls[0]["end_timestamp"] == end_s and calls[1]["end_timestamp"] < calls[0]["end_timestamp"]
    assert calls[0]["start_timestamp"] == start_ms // 1000                                   # секунды, не мс


def test_lighter_signed_rate_edge_cases_and_unknown_symbol():
    assert L.signed_rate(dict(rate="0.0000", direction="")) == 0
    assert L.signed_rate(dict(rate="0.0107", direction="short")) == Decimal("-0.000107")
    with pytest.raises(RuntimeError):
        L.signed_rate(dict(rate="0.0012", direction=""))
    T = L.LighterTruth(FakeHttp({"/orderBookDetails": DET}), ws=lambda *a: {})
    with pytest.raises(RuntimeError, match="нет в orderBookDetails"):
        T.history("NOPE", 0, 10 ** 13)


# --- Lighter: HTTP и WebSocket -------------------------------------------------------------------------------------
class Resp:
    def __init__(self, code, body):
        self.status_code, self._b, self.text, self.headers = code, body, json.dumps(body), {}

    def json(self):
        return self._b


def test_lighter_http_405_is_the_ip_limit_pause_then_retry(monkeypatch):
    sleeps = []
    monkeypatch.setattr(L.time, "sleep", lambda s: sleeps.append(s))
    h = L.LighterHttp("lighter", 0.0)
    seq = [Resp(405, {}), Resp(429, {}), Resp(200, {"code": 200, "x": 1})]
    monkeypatch.setattr(h.s, "request", lambda *a, **k: seq.pop(0))
    assert h.get("https://mainnet.zklighter.elliot.ai/api/v1/x") == {"code": 200, "x": 1}
    assert sleeps.count(L.BAN_S) == 2
    h2 = L.LighterHttp("lighter", 0.0)
    monkeypatch.setattr(h2.s, "request", lambda *a, **k: Resp(200, {"code": 20001, "message": "invalid param"}))
    with pytest.raises(RuntimeError, match="20001"):
        h2.get("https://mainnet.zklighter.elliot.ai/api/v1/x")


def _frame(op, payload, fin=True):
    n = len(payload)
    hdr = bytes([(0x80 if fin else 0) | op])
    if n < 126:
        hdr += bytes([n])
    elif n < 65536:
        hdr += bytes([126]) + struct.pack(">H", n)
    else:
        hdr += bytes([127]) + struct.pack(">Q", n)
    return hdr + payload


def _client_frames(data: bytes):
    """Кадры клиента (маскированные) → [(op, payload)]."""
    out, i = [], 0
    while i + 2 <= len(data):
        op, n, j = data[i] & 0x0F, data[i + 1] & 0x7F, i + 2
        assert data[i + 1] & 0x80                                  # клиент обязан маскировать
        if n == 126:
            n, j = struct.unpack(">H", data[j:j + 2])[0], j + 2
        elif n == 127:
            n, j = struct.unpack(">Q", data[j:j + 8])[0], j + 8
        mask, j = data[j:j + 4], j + 4
        out.append((op, bytes(x ^ mask[k & 3] for k, x in enumerate(data[j:j + n]))))
        i = j + n
    return out


def _serve(sock, frames):
    t = threading.Thread(target=lambda: sock.sendall(b"".join(frames)), daemon=True)
    t.start()
    return t


def test_ws_snapshot_fragments_ping_and_the_first_full_snapshot():
    a, b = socket.socketpair()
    a.settimeout(5)
    msg = json.dumps({"type": "subscribed/market_stats", "channel": "market_stats:all", "pad": "x" * 70000,
                      "market_stats": {"1": {"market_id": 1, "symbol": "BTC", "current_funding_rate": "0.0010"}}}).encode()
    t = _serve(b, [_frame(0x1, b'{"type":"connected"}'), _frame(0x1, msg[:40000], fin=False), _frame(0x9, b"hb"),
                   _frame(0x0, msg[40000:], fin=True)])
    res = L.ws_snapshot("wss://x/stream", "market_stats/all", "market_stats", 5, connect=lambda url, tm: L.MiniWS(a))
    t.join(5)
    assert res == {"1": {"market_id": 1, "symbol": "BTC", "current_funding_rate": "0.0010"}}
    b.settimeout(2)
    data = b""
    while True:
        chunk = b.recv(65536)
        if not chunk:
            break
        data += chunk
    fr = _client_frames(data)
    assert json.loads(fr[0][1]) == {"type": "subscribe", "channel": "market_stats/all"} and fr[0][0] == 0x1
    assert (0xA, b"hb") in fr and fr[-1][0] == 0x8                  # ответ на ping и закрытие
    b.close()


def test_ws_snapshot_close_frame_is_an_error():
    a, b = socket.socketpair()
    a.settimeout(5)
    _serve(b, [_frame(0x8, struct.pack(">H", 1000))]).join(5)
    with pytest.raises(ConnectionError):
        L.ws_snapshot("wss://x", "market_stats/all", "market_stats", 5, connect=lambda url, tm: L.MiniWS(a))
    b.close()


# --- Backpack -------------------------------------------------------------------------------------------------------
def _bm(sym, bs, rwa=None, state="Open", visible=True, created="2025-01-21T06:34:54.691858", iv=3600000, mt="PERP"):
    return dict(symbol=sym, baseSymbol=bs, quoteSymbol="USDC", marketType=mt, orderBookState=state, visible=visible,
                createdAt=created, fundingInterval=iv, rwaMarketType=rwa, fundingRateUpperBound="150",
                fundingRateLowerBound="-150")


MK = [_bm("BTC_USDC_PERP", "BTC"), _bm("kBONK_USDC_PERP", "kBONK"), _bm("NVDA.US_USDC_PERP", "NVDA.US", "STOCK"),
      _bm("SPY.US_USDC_PERP", "SPY.US", "INDEX"), _bm("US500_USDC_PERP", "US500", "INDEX"),
      _bm("OPENAI_USDC_PERP", "OPENAI", "STOCK"), _bm("ODDRWA_USDC_PERP", "ODDRWA", "BOND"),
      _bm("AMZN.US_USDC_PERP", "AMZN.US", "STOCK", state="PostOnly", visible=False),
      _bm("IP_USDC_PERP", "IP", state="Closed"), _bm("FUT_USDC_PERP", "FUT", created="2099-01-01T00:00:00"),
      _bm("TWOH_USDC_PERP", "TWOH", iv=7200000), _bm("HALF_USDC_PERP", "HALF", iv=5400000),
      _bm("SOL_USDC", "SOL", mt="SPOT")]


def test_backpack_markets_state_classes_bases_intervals():
    m = B.BackpackTruth(FakeHttp({"/markets": MK})).markets()
    assert "SOL_USDC" not in m
    assert m["BTC_USDC_PERP"]["tradable"] and m["BTC_USDC_PERP"]["cls"] == "crypto" and m["BTC_USDC_PERP"]["interval_h"] == 1
    assert m["kBONK_USDC_PERP"]["base"] == "kBONK" and base.norm_base("kBONK") == ("BONK", 1000.0)
    assert (m["NVDA.US_USDC_PERP"]["base"], m["NVDA.US_USDC_PERP"]["cls"]) == ("NVDA", "equity")
    assert (m["SPY.US_USDC_PERP"]["base"], m["SPY.US_USDC_PERP"]["cls"]) == ("SPY", "equity")      # ETF-пай, не индекс
    assert "ETF" in m["SPY.US_USDC_PERP"]["note"]
    assert m["US500_USDC_PERP"]["cls"] == "index" and m["OPENAI_USDC_PERP"]["cls"] == "preipo"
    assert m["ODDRWA_USDC_PERP"]["cls"] == "other"
    assert not m["AMZN.US_USDC_PERP"]["tradable"] and "PostOnly" in m["AMZN.US_USDC_PERP"]["note"] \
        and "visible=false" in m["AMZN.US_USDC_PERP"]["note"]
    assert not m["IP_USDC_PERP"]["tradable"] and "Closed" in m["IP_USDC_PERP"]["note"]
    assert not m["FUT_USDC_PERP"]["tradable"] and "до запуска" in m["FUT_USDC_PERP"]["note"]
    assert m["TWOH_USDC_PERP"]["interval_h"] == 2 and m["TWOH_USDC_PERP"]["tradable"]
    assert m["HALF_USDC_PERP"]["interval_h"] is None and not m["HALF_USDC_PERP"]["tradable"]


def test_backpack_rates_fraction_per_interval_no_conversion():
    mp = [dict(symbol="BTC_USDC_PERP", fundingRate="0.0000125", markPrice="77219.1", indexPrice="77246.3",
               nextFundingTimestamp=1789257600000),
          dict(symbol="TWOH_USDC_PERP", fundingRate="-0.00006", nextFundingTimestamp=1789264800000),
          dict(symbol="GHOST_USDC_PERP", fundingRate="0.001", nextFundingTimestamp=1789257600000)]
    http = FakeHttp({"/markets": MK, "/markPrices": mp})
    r = B.BackpackTruth(http).rates()                           # без markets(): интервалы взяты сами
    assert r["BTC_USDC_PERP"] == dict(rate=Decimal("0.0000125"), interval_h=1, next_ms=1789257600000, kind="predicted")
    assert r["TWOH_USDC_PERP"]["interval_h"] == 2 and r["TWOH_USDC_PERP"]["rate"] == Decimal("-0.00006")
    assert "GHOST_USDC_PERP" not in r and any(u.endswith("/markets") for u, _p, _g in http.log)


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _funding_rates(rows_newest_first, calls):
    def f(p):
        calls.append(p)
        return [r for r in rows_newest_first if r["symbol"] == p["symbol"]][p["offset"]:p["offset"] + p["limit"]]
    return f


NOW_H = 1789257600000


def _rows(n_back=1500):
    return [dict(symbol="BTC_USDC_PERP", intervalEndTimestamp=_iso(NOW_H + H - k * H), fundingRate=f"0.0000{125 + k % 7}")
            for k in range(n_back + 2)]                          # k=0 — интервал В ПРОЦЕССЕ (конец в будущем)


def test_backpack_history_newest_first_offset_paging_drops_in_progress_and_fresh(monkeypatch):
    now = NOW_H + 2 * 60_000                                     # 00:02 — расчёт за 00:00 ещё не окончательный
    monkeypatch.setattr(B, "now_ms", lambda: now)
    calls = []
    T = B.BackpackTruth(FakeHttp({"/fundingRates": _funding_rates(_rows(), calls)}))
    start = now - 1000 * H
    h = T.history("BTC_USDC_PERP", start, now)
    assert [c["limit"] for c in calls] == [1000, 1000] and [c["offset"] for c in calls] == [0, 1000]
    assert h[-1][0] == NOW_H - H and h[0][0] == NOW_H - 999 * H and len(h) == 999
    assert all(a[0] < b[0] for a, b in zip(h, h[1:])) and h[-1][1] == Decimal("0.0000127")   # k=2 → 125+2
    monkeypatch.setattr(B, "now_ms", lambda: NOW_H + 10 * 60_000)                           # 00:10 — уже окончательный
    calls.clear()
    h2 = T.history("BTC_USDC_PERP", NOW_H - 72 * H, NOW_H + 10 * 60_000)
    assert h2[-1][0] == NOW_H and len(h2) == 73 and calls[0]["limit"] == 76 and len(calls) == 1


def test_backpack_empty_answer_is_an_error_unless_the_market_is_fresh(monkeypatch):
    now = NOW_H + 30 * 60_000
    monkeypatch.setattr(B, "now_ms", lambda: now)
    T = B.BackpackTruth(FakeHttp({"/fundingRates": [], "/markets": [
        _bm("OLD_USDC_PERP", "OLD"), _bm("NEW_USDC_PERP", "NEW", created=_iso(now - 20 * 60_000))]}))
    T.markets()
    assert T.history("NEW_USDC_PERP", now - 72 * H, now) == []
    with pytest.raises(RuntimeError, match="пустой ответ"):
        T.history("OLD_USDC_PERP", now - 72 * H, now)
    with pytest.raises(RuntimeError, match="пустой ответ"):
        T.history("TYPO_USDC_PERP", now - 72 * H, now)           # неизвестный символ отвечает 200 []


def test_backpack_iso_is_utc_without_zone():
    assert B.iso_ms("2026-09-12T22:00:00") == 1789250400000 == B.iso_ms("2026-09-12T22:00:00Z")
    assert B.iso_ms("2025-01-21T06:34:54.691858") == B.iso_ms("2025-01-21T06:34:54") + 691
    assert B.iso_ms("") is None and B.iso_ms(None) is None
