"""Тестировщик: истины пяти новых площадок (variational, edgex, extended, pacifica, apex) на подставном HTTP.
Сети нет — тела ответов повторяют форму замера 13.09 (поля и ловушки из шапок audit_truth/*.py)."""
import ast, inspect
from datetime import datetime, timezone
from decimal import Decimal
import pytest
from funding_bot.audit_truth import TRUTHS, TRUTH_ERRORS, base
from funding_bot.audit_truth import variational as V, edgex as E, extended as X, pacifica as P, apex as A

H = 3600_000
FIVE = ("variational", "edgex", "extended", "pacifica", "apex")


class FakeHttp:
    """GET по окончанию пути (тело или функция от params); журнал (url, params, gap)."""
    def __init__(self, get):
        self._get, self.log, self.calls = get, [], 0

    def get(self, url, params=None, gap=None):
        self.calls += 1
        self.log.append((url, dict(params or {}), gap))
        for k, v in self._get.items():
            if url.endswith(k):
                return v(dict(params or {})) if callable(v) else v
        raise AssertionError(f"неожиданный запрос {url}")

    def post(self, url, body=None, gap=None):
        raise AssertionError("POST у этих истин не бывает")


def paths(http, suffix):
    return [p for u, p, _g in http.log if u.endswith(suffix)]


# --- реестр и независимость -------------------------------------------------------------------------------------------
def test_registry_has_the_five_truths():
    for v in FIVE:
        assert v in TRUTHS and TRUTHS[v].venue == v
    assert not {k: e for k, e in TRUTH_ERRORS.items() if k in FIVE}
    assert TRUTHS["variational"] is V.VariationalTruth and TRUTHS["apex"] is A.ApexTruth


FORBIDDEN = {"hyperliquid", "kucoin_fut", "bitget_fut", "gate_fut", "lighter", "backpack", "variational", "edgex",
             "extended", "pacifica", "apex", "exchanges", "client", "venues", "universe", "calc", "symbols", "collector"}


@pytest.mark.parametrize("mod", [V, E, X, P, A])
def test_truth_modules_do_not_import_collector_clients(mod):
    """Главное свойство тестировщика: истина не повторяет ошибок клиента — модули пакета берут из проекта только base."""
    for node in ast.walk(ast.parse(inspect.getsource(mod))):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1:
                assert node.module == "base", f"{mod.__name__}: from .{node.module}"
            elif node.level >= 2:
                names = {node.module} if node.module else {a.name for a in node.names}
                assert not names & FORBIDDEN, f"{mod.__name__}: from .. import {names}"
            else:
                assert not (node.module or "").startswith("funding_bot"), f"{mod.__name__}: {node.module}"
        elif isinstance(node, ast.Import):
            assert not any(a.name.startswith("funding_bot") for a in node.names)


# --- Variational ------------------------------------------------------------------------------------------------------
def _vl(t, name, apr, iv):
    return dict(ticker=t, name=name, funding_rate=apr, funding_interval_s=iv, mark_price="1", quotes={})


VBODY = {"total_volume_24h": "1", "listings": [
    _vl("BTC", "Bitcoin", "0.050542", 28800), _vl("ETH", "Ethereum", "0.1095", 28800), _vl("STORJ", "Storj", "-93.62", 3600),
    _vl("OPENAI", "OpenAI", "0.05475", 28800), _vl("XAU", "Gold", "0", 28800), _vl("CAT", "Caterpillar Inc.", "0", 14400),
    _vl("US500", "State Street SPDR S&P 500 ETF Trust", "0", 14400), _vl("JPM", "JPMorgan Chase & Co.", "0", 14400),
    _vl("QQQ", "Invesco QQQ Trust, Series 1", "0", 14400), _vl("TWT", "Trust Wallet", "0.1095", 14400),
    _vl("GIGGLE", "Giggle Fund", "0.1095", 14400), _vl("CRCL", "Circle Internet Group", "0", 14400),
    _vl("ODD", "Odd Labs", "0", 14400), _vl("FAKE", "Fake Holdings Inc.", "0.1095", 14400),
    _vl("PAXG", "PAX Gold", "0.1095", 14400), _vl("USOILP", "Swap on WTI Crude Oil", "0", 0),
    _vl("HALF", "Half Hour", "0.1095", 5400)]}


def test_variational_markets_classes_and_swaps():
    T = V.VariationalTruth(FakeHttp({"/metadata/stats": VBODY}))
    m = T.markets()
    assert m["BTC"] == dict(base="BTC", tradable=True, cls="crypto", interval_h=8, name="Bitcoin", note=None)
    assert not m["USOILP"]["tradable"] and m["USOILP"]["cls"] == "other" and "своп" in m["USOILP"]["note"]
    assert {s: m[s]["cls"] for s in ("OPENAI", "XAU", "CAT", "US500", "JPM", "QQQ", "TWT", "GIGGLE", "CRCL", "PAXG")} == dict(
        OPENAI="preipo", XAU="commodity", CAT="equity", US500="equity", JPM="equity", QQQ="equity", TWT="crypto",
        GIGGLE="crypto", CRCL="equity", PAXG="crypto")
    assert m["ODD"]["cls"] is None and "0" in m["ODD"]["note"]              # имя монеты, а процентная часть TradFi
    assert m["FAKE"]["cls"] is None and "0.1095" in m["FAKE"]["note"]       # «акция», а процент монет
    assert m["STORJ"]["interval_h"] == 1 and m["HALF"]["interval_h"] == 2 and "округл" in m["HALF"]["note"]


def test_variational_rates_are_annual_converted_per_interval_and_history_is_none():
    T = V.VariationalTruth(FakeHttp({"/metadata/stats": VBODY}))
    T.markets()
    r = T.rates()
    assert T.http.calls == 1                                                # markets + rates — один снимок
    assert r["ETH"]["rate"] == Decimal("0.0001") and r["ETH"]["interval_h"] == 8   # 0.1095 × 8 / 8760 = 0.01 %/8 ч
    assert r["OPENAI"]["rate"] == Decimal("0.00005")                        # pre-IPO 0.005 %/8 ч по документации
    assert r["BTC"]["rate"] == Decimal("0.050542") * 8 / 8760 and r["BTC"]["kind"] == "predicted"
    assert r["STORJ"]["rate"] == Decimal("-93.62") / 8760 and r["STORJ"]["interval_h"] == 1
    assert "USOILP" not in r
    now = base.now_ms()
    assert now < r["BTC"]["next_ms"] <= now + 8 * H and r["BTC"]["next_ms"] % (8 * H) == 0
    assert T.history("BTC", 0, now) is None and "не публикует" in T.history_note


def test_variational_empty_body_raises():
    with pytest.raises(RuntimeError):
        V.VariationalTruth(FakeHttp({"/metadata/stats": {"listings": []}})).markets()


# --- edgeX ------------------------------------------------------------------------------------------------------------
def _ec(cid, name, base_id, stock=False, fx=False, display=True, trade=True):
    return dict(contractId=cid, contractName=name, baseCoinId=base_id, quoteCoinId="1000", enableTrade=trade,
                enableDisplay=display, enableOpenPosition=True, fundingRateIntervalMin="240", isStock=stock, isFx=fx)


EMETA = {"code": "SUCCESS", "data": {
    "coinList": [dict(coinId="1000", coinName="USDC"), dict(coinId="1001", coinName="BTC"), dict(coinId="1003", coinName="HAJIMI"),
                 dict(coinId="1004", coinName="XAU"), dict(coinId="1005", coinName="NVDA"), dict(coinId="1006", coinName="EUR"),
                 dict(coinId="1007", coinName="PAXG"), dict(coinId="1009", coinName="OPENAI")],
    "contractList": [_ec("30000001", "BTCUSDC", "1001"), _ec("30000003", "哈基米USDC", "1003"), _ec("30000005", "XAUUSDC", "1004"),
                     _ec("30000006", "NVDAUSDC", "1005", stock=True), _ec("30000010", "EURUSDC", "1006", fx=True, display=False),
                     _ec("30000007", "PAXGUSDC", "1007"), _ec("30000009", "OPENAIUSDC", "1009", stock=True)]}}
ELABELS = {"code": "SUCCESS", "data": [
    {"name": "Commodities TradeFi", "productCategory": "AppTradFi", "contracts": [{"contractId": "30000001"}]},  # не V2
    {"name": "Commodities V2", "productCategory": "PrepV2", "contracts": [{"contractId": "30000005"}]},
    {"name": "Pre-IPO V2", "productCategory": "PrepV2", "contracts": [{"contractId": "30000009"}]}]}


def test_edgex_markets_base_from_coin_list_flags_and_label_classes():
    T = E.EdgexTruth(FakeHttp({"/meta/getMetaData": EMETA, "/contract-labels": ELABELS}))
    m = T.markets()
    assert m["哈基米USDC"]["base"] == "HAJIMI" and m["BTCUSDC"]["interval_h"] == 4 and m["BTCUSDC"]["tradable"]
    assert not m["EURUSDC"]["tradable"] and "enableDisplay" in m["EURUSDC"]["note"]
    assert {s: m[s]["cls"] for s in m} == {"BTCUSDC": "crypto", "哈基米USDC": "crypto", "XAUUSDC": "commodity",
                                          "NVDAUSDC": "equity", "EURUSDC": "fx", "PAXGUSDC": "crypto",
                                          "OPENAIUSDC": "preipo"}


def test_edgex_labels_down_leaves_non_stock_class_unknown():
    def boom(_p):
        raise RuntimeError("HTTP 500")
    m = E.EdgexTruth(FakeHttp({"/meta/getMetaData": EMETA, "/contract-labels": boom})).markets()
    assert m["BTCUSDC"]["cls"] is None and m["XAUUSDC"]["cls"] is None and m["NVDAUSDC"]["cls"] == "equity"


def test_edgex_rates_forecast_next_time_and_batches(monkeypatch):
    monkeypatch.setattr(E, "BATCH", 3)
    now = base.now_ms()
    ft = now // (4 * H) * (4 * H)

    def latest(p):
        rows = []
        for cid in p["contractId"].split(","):
            r = dict(contractId=cid, fundingTime=str(ft), fundingRateIntervalMin="240", fundingRate="-0.00005532",
                     forecastFundingRate="-0.00005719", predictedFundingRate="0.00005000", isSettlement=False)
            if cid == "30000003":
                r["forecastFundingRate"] = ""                                   # минута расчёта: прогноза нет
            if cid == "30000005":
                r["fundingTime"] = str(ft - 8 * H)                              # отставший снимок
            rows.append(r)
        return {"code": "SUCCESS", "data": rows}
    T = E.EdgexTruth(FakeHttp({"/meta/getMetaData": EMETA, "/contract-labels": ELABELS,
                               "/funding/getLatestFundingRate": latest}))
    r = T.rates()
    assert len(paths(T.http, "/getLatestFundingRate")) == 3                   # 7 контрактов порциями по 3
    assert r["BTCUSDC"] == dict(rate=Decimal("-0.00005719"), interval_h=4, next_ms=ft + 4 * H, kind="predicted")
    assert r["哈基米USDC"]["rate"] == Decimal("-0.00005532") and r["哈基米USDC"]["kind"] == "last"
    assert r["XAUUSDC"]["next_ms"] == ft + 4 * H                             # по сетке, не fundingTime + 4 ч в прошлом


def test_edgex_history_settlement_rows_pages_and_errors():
    S = 1_789_000_000_000 // (4 * H) * (4 * H)

    def page(p):
        assert p["contractId"] == "30000001" and p["filterSettlementFundingRate"] == "true" and p["size"] == 100
        if not p.get("offsetData"):
            rows = [dict(fundingTime=str(S + k * 4 * H), fundingRate="0.0001", isSettlement=True) for k in (5, 4, 3)]
            rows.insert(1, dict(fundingTime=str(S + 4 * 4 * H + 60_000), fundingRate="0.9", isSettlement=False))
            return {"code": "SUCCESS", "data": {"dataList": rows, "nextPageOffsetData": "tok2"}}
        assert p["offsetData"] == "tok2"
        return {"code": "SUCCESS", "data": {"dataList": [dict(fundingTime=str(S + k * 4 * H), fundingRate="-0.0002",
                                                               isSettlement=True) for k in (2, 1, 0)], "nextPageOffsetData": ""}}
    T = E.EdgexTruth(FakeHttp({"/meta/getMetaData": EMETA, "/contract-labels": ELABELS, "/getFundingRatePage": page}))
    h = T.history("BTCUSDC", S + 4 * H, S + 5 * 4 * H)
    assert [ms for ms, _ in h] == [S + k * 4 * H for k in (1, 2, 3, 4, 5)] and h[0][1] == Decimal("-0.0002")
    assert all(g == T.HIST_GAP_S for u, _p, g in T.http.log if u.endswith("/getFundingRatePage"))
    with pytest.raises(RuntimeError):
        T.history("NOPEUSDC", 0, 1)
    bad = E.EdgexTruth(FakeHttp({"/meta/getMetaData": {"code": "GATEWAY_INTERNAL_ERROR", "data": None}}))
    with pytest.raises(RuntimeError):
        bad.markets()


# --- Extended ---------------------------------------------------------------------------------------------------------
def _xm(name, cat, sub, status="ACTIVE", vis=True, ref="null", typ="PERPETUAL", fr="0.000013", nxt=None, asset=None):
    return dict(name=name, type=typ, category=cat, subCategory=sub, status=status, active=True, visibleOnUi=vis,
                referenceMarket=ref, assetName=asset or name.rsplit("-", 1)[0], description=name,
                marketStats=dict(fundingRate=fr, nextFundingRate=nxt, markPrice="1"))


def _xbody(nxt):
    return {"status": "OK", "data": [
        _xm("BTC-USD", "Crypto", "L1", nxt=nxt), _xm("PAXG-USD", "Crypto", "Commodity"),
        _xm("NVDA-USD", "RWA", "Equity", status="DELISTED", vis=False), _xm("NVDA_24_5-USD", "RWA", "Equity", fr="-0.000002"),
        _xm("DRAM-USD", "RWA", "ETF/Index", ref="us_equity"), _xm("SPX500m-USD", "RWA", "ETF/Index", ref="us_index_fut"),
        _xm("XNG-USD", "RWA", "Commodity", ref="commodity_cme"), _xm("EUR-USD", "RWA", "FX", ref="fx"),
        _xm("OPENAI-USD", "RWA", "Pre-market", ref="always_open"), _xm("PLACE_JPY-USD", "RWA", "TradFi", status="DELISTED"),
        _xm("FTM-USD", "L1", "L1", status="DELISTED"), _xm("MKR-USD", "Crypto", "DeFi", status="REDUCE_ONLY", vis=False),
        _xm("1000PEPE-USD", "Crypto", "Meme", fr=None), _xm("USDTSPOT-USD", "Crypto", "Stable", typ="SPOT")]}


def test_extended_markets_status_twins_and_classes():
    T = X.ExtendedTruth(FakeHttp({"/info/markets": _xbody(0)}))
    m = T.markets()
    assert "USDTSPOT-USD" not in m                                            # SPOT — не перп
    assert m["BTC-USD"]["tradable"] and m["BTC-USD"]["interval_h"] == 1 and m["BTC-USD"]["base"] == "BTC"
    assert m["NVDA_24_5-USD"]["base"] == "NVDA" and "24/5" in m["NVDA_24_5-USD"]["note"]
    assert not m["NVDA-USD"]["tradable"] and "DELISTED" in m["NVDA-USD"]["note"] and not m["MKR-USD"]["tradable"]
    assert {s: m[s]["cls"] for s in m} == {
        "BTC-USD": "crypto", "PAXG-USD": "crypto", "NVDA-USD": "equity", "NVDA_24_5-USD": "equity", "DRAM-USD": "equity",
        "SPX500m-USD": "index", "XNG-USD": "commodity", "EUR-USD": "fx", "OPENAI-USD": "preipo", "PLACE_JPY-USD": None,
        "FTM-USD": "crypto", "MKR-USD": "crypto", "1000PEPE-USD": "crypto"}


def test_extended_rates_hourly_with_next_time_field():
    now = base.now_ms()
    nh = base.next_boundary_ms(now, 1)
    T = X.ExtendedTruth(FakeHttp({"/info/markets": _xbody(nh)}))
    T.markets()
    r = T.rates()
    assert T.http.calls == 1
    assert r["BTC-USD"] == dict(rate=Decimal("0.000013"), interval_h=1, next_ms=nh, kind="predicted")
    assert r["NVDA_24_5-USD"]["rate"] == Decimal("-0.000002") and r["PAXG-USD"]["next_ms"] == nh   # 0 → сетка часа
    assert "1000PEPE-USD" not in r


def test_extended_history_floors_to_hour_and_pages_backwards():
    S = 1_788_000_000_000 // H * H
    end = S + 1100 * H

    def hist(p):
        assert p["startTime"] == S
        if p["endTime"] == end + X.LATE_MS:
            rows = [dict(m="BTC-USD", f="0.00001", T=end - k * H + 800) for k in range(998)]   # часы end … end−997H
            rows.insert(3, dict(m="BTC-USD", f="0.5", T=end - 2 * H + 559_000))           # опоздавший дубль того же часа
            rows.insert(5, dict(m="ETH-USD", f="0.7", T=end - 7 * H + 900))                  # чужой рынок
            assert len(rows) == X.PAGE                                                        # полная страница → назад
            return {"status": "OK", "data": rows}
        assert p["endTime"] == end - 997 * H + 800 - 1                                        # min(T) − 1
        # end − 998H = S + 102H … и один час до начала окна (отсекается)
        return {"status": "OK", "data": [dict(m="BTC-USD", f="-0.00002", T=S + k * H + 900) for k in range(102, -2, -1)]}
    T = X.ExtendedTruth(FakeHttp({"/info/BTC-USD/funding": hist}))
    h = T.history("BTC-USD", S, end)
    ms = [x for x, _ in h]
    assert ms[0] == S and ms[-1] == end and all(x % H == 0 for x in ms)
    assert dict(h)[end - 2 * H] == Decimal("0.00001")                          # раньше пришедшая строка часа
    assert len(paths(T.http, "/funding")) == 2


# --- Pacifica ---------------------------------------------------------------------------------------------------------
def _pinfo(now):
    return {"success": True, "data": [
        dict(symbol="BTC", instrument_type="perpetual", created_at=1748881333944, base_asset="BTC"),
        dict(symbol="kBONK", instrument_type="perpetual", created_at=1748881333944),
        dict(symbol="SOL-USDC", instrument_type="spot", created_at=1748881333944),
        dict(symbol="NEWX", instrument_type="perpetual", created_at=now + 3 * H),
        dict(symbol="MU", instrument_type="perpetual", created_at=1780000000000)], "error": None, "code": None}


PPRICES = {"success": True, "data": [
    dict(symbol="BTC", funding="0.00000531", next_funding="0.00000939", timestamp=1789256461533),
    dict(symbol="kBONK", funding="0.0000125", next_funding="-0.00002"),
    dict(symbol="SOL-USDC", funding="0", next_funding="0"), dict(symbol="GHOST", funding="0", next_funding="0.1")]}


def test_pacifica_markets_rates_next_funding():
    now = base.now_ms()
    T = P.PacificaTruth(FakeHttp({"/info": _pinfo(now), "/info/prices": PPRICES}))
    m = T.markets()
    assert set(m) == {"BTC", "kBONK", "NEWX", "MU"} and m["kBONK"]["base"] == "kBONK" and m["kBONK"]["cls"] == "crypto"
    assert not m["NEWX"]["tradable"] and "объявлен" in m["NEWX"]["note"]
    assert m["MU"]["cls"] is None and "документации" in m["MU"]["note"] and m["MU"]["tradable"]
    r = T.rates()
    assert set(r) == {"BTC", "kBONK"}                                         # спот и не-рынки не берутся
    assert r["BTC"] == dict(rate=Decimal("0.00000939"), interval_h=1, next_ms=base.next_boundary_ms(now, 1),
                            kind="predicted")


def test_pacifica_history_pays_next_funding_rate_of_the_hour_row_with_cursor():
    now = base.now_ms()
    top = now // H * H
    start = top - 71 * H

    def hist(p):
        assert p["symbol"] == "BTC" and p["limit"] == -(-(now - start) // H) + P.HIST_SLACK
        if "cursor" not in p:
            rows = [dict(created_at=top - k * H + 398, funding_rate=f"0.0000{k:02d}", next_funding_rate=f"0.0001{k:02d}")
                    for k in range(0, 40)]
            return {"success": True, "data": rows, "next_cursor": "c1", "has_more": True}
        assert p["cursor"] == "c1"
        rows = [dict(created_at=top - k * H + 398, funding_rate="0", next_funding_rate="-0.0003") for k in range(40, 80)]
        return {"success": True, "data": rows, "next_cursor": None, "has_more": False}
    T = P.PacificaTruth(FakeHttp({"/funding_rate/history": hist}))
    h = dict(T.history("BTC", start, now))
    assert h[top] == Decimal("0.000100") and h[top - 5 * H] == Decimal("0.000105")      # next_funding_rate, не funding_rate
    assert min(h) == start and h[start] == Decimal("-0.0003") and len(h) == 72
    assert all(g == P.PacificaTruth.HIST_GAP_S for _u, _p, g in T.http.log)


def test_pacifica_empty_history_and_failed_body_raise():
    T = P.PacificaTruth(FakeHttp({"/funding_rate/history": {"success": True, "data": [], "has_more": False}}))
    with pytest.raises(RuntimeError, match="пуста"):
        T.history("btc", 0, base.now_ms())
    with pytest.raises(RuntimeError):
        P.PacificaTruth(FakeHttp({"/info": {"success": False, "data": None, "error": "x"}})).markets()


# --- ApeX -------------------------------------------------------------------------------------------------------------
def _ar(cross, dash, tok, cat=None, name=None, on=True, display=None):
    d = on if display is None else display
    return dict(crossSymbolName=cross, symbol=dash, baseTokenId=tok, tokenName=name or tok, category=cat, settleAssetId="USDT",
                enableTrade=on, enableDisplay=d, enableOpenPosition=on and d, isPrelaunch=False)


ASYMS = {"data": {"contractConfig": {
    "perpetualContract": [_ar("BTCUSDT", "BTC-USDT", "BTC", "L1", "Bitcoin"), _ar("TONUSDT", "TON-USDT", "TON", on=False),
                          _ar("IOUSDT", "IO-USDT", "IO", display=False), _ar("PAXGUSDT", "PAXG-USDT", "PAXG"),
                          _ar("1000PEPEUSDT", "1000PEPE-USDT", "1000PEPE", "MEME", "PEPE")],
    "stockContract": [_ar("NVDAUSDT", "NVDA-USDT", "NVDA", "STOCK", "NVIDIA"),
                      _ar("USOUSDT", "USO-USDT", "USO", "COMMODITY", "United States Oil Fund"),
                      _ar("XAUUSDT", "XAU-USDT", "XAU", "COMMODITY", "Gold"),
                      _ar("SPYUSDT", "SPY-USDT", "SPY", "INDEX", "SPDR S&P 500 ETF Trust"),
                      _ar("SOXLUSDT", "SOXL-USDT", "SOXL", None, "Semicon Bull 3X ETF"),
                      _ar("OPENAIUSDT", "OPENAI-USDT", "OPENAI", "STOCK", "OpenAI"),
                      _ar("ODDUSDT", "ODD-USDT", "ODD", "INDEX", "Odd Index"), _ar("WEIRDUSDT", "WEIRD-USDT", "WEIRD")],
    "predictionContract": [_ar("EVENTUSDT", "EVENT-USDT", "EVENT")], "prelaunchContract": []}}, "timeCost": 100}


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_apex_markets_groups_flags_and_classes():
    T = A.ApexTruth(FakeHttp({"/symbols": ASYMS}))
    m = T.markets()
    assert "EVENTUSDT" not in m                                               # событийные рынки — не перпы
    assert m["BTCUSDT"]["tradable"] and m["BTCUSDT"]["name"] == "Bitcoin" and m["1000PEPEUSDT"]["base"] == "1000PEPE"
    assert not m["TONUSDT"]["tradable"] and not m["IOUSDT"]["tradable"] and "enableDisplay" in m["IOUSDT"]["note"]
    assert {s: m[s]["cls"] for s in ("BTCUSDT", "PAXGUSDT", "NVDAUSDT", "USOUSDT", "XAUUSDT", "SPYUSDT", "SOXLUSDT",
                                     "OPENAIUSDT", "ODDUSDT", "WEIRDUSDT")} == dict(
        BTCUSDT="crypto", PAXGUSDT="crypto", NVDAUSDT="equity", USOUSDT="equity", XAUUSDT="commodity", SPYUSDT="equity",
        SOXLUSDT="equity", OPENAIUSDT="preipo", ODDUSDT="index", WEIRDUSDT=None)


def test_apex_rates_per_symbol_ticker_and_failure_share():
    now = base.now_ms()
    nh = base.next_boundary_ms(now, 1)

    def ticker(p):
        if p["symbol"] == "NVDAUSDT":
            return {"data": [], "timeCost": 1}                                # тикер без строки
        return {"data": [dict(symbol=p["symbol"], fundingRate="0.00000113", predictedFundingRate="0.0000125",
                              nextFundingTime=_iso(nh))], "timeCost": 1}
    T = A.ApexTruth(FakeHttp({"/symbols": ASYMS, "/ticker": ticker}))
    r = T.rates()
    asked = [p["symbol"] for p in paths(T.http, "/ticker")]
    assert "TONUSDT" not in asked and "IOUSDT" not in asked and "BTCUSDT" in asked   # только торгуемые, символ без дефиса
    assert r["BTCUSDT"] == dict(rate=Decimal("0.00000113"), interval_h=1, next_ms=nh, kind="predicted")
    assert "NVDAUSDT" not in r and "NVDAUSDT" in T.rate_errors               # 1 из 11 — не выше порога

    def ticker_bad(p):
        if p["symbol"] in ("NVDAUSDT", "SPYUSDT"):
            return {"code": 3, "msg": "invalid symbol"}
        return ticker(p)
    with pytest.raises(RuntimeError, match="не ответил"):
        A.ApexTruth(FakeHttp({"/symbols": ASYMS, "/ticker": ticker_bad})).rates()


def test_apex_history_dashed_symbol_and_window_paging():
    S = 1_788_000_000_000 // H * H
    end = S + 150 * H

    def hist(p):
        assert p["symbol"] == "BTC-USDT" and p["limit"] == 100 and p["beginTimeInclusive"] == S
        hi = p["endTimeExclusive"]
        top = (hi - 1) // H * H
        rows = [dict(symbol="BTC-USDT", rate="0.0000125", price="1", fundingTime=top - k * H) for k in range(100)
                if top - k * H >= S]
        return {"data": {"historyFunds": rows, "totalSize": 999}, "timeCost": 1}
    T = A.ApexTruth(FakeHttp({"/symbols": ASYMS, "/history-funding": hist}))
    h = T.history("BTCUSDT", S, end)
    assert [ms for ms, _ in h] == [S + k * H for k in range(151)]
    ex = [p["endTimeExclusive"] for p in paths(T.http, "/history-funding")]
    assert ex[0] == end + 1 and ex[1] == end - 99 * H                          # исключающая граница = старейшая строка
    with pytest.raises(RuntimeError):
        T.history("NOPEUSDT", S, end)
