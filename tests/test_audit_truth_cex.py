"""Тестировщик: истины KuCoin, Bitget, Gate на подставном HTTP (формы ответов — замер 13.09 с Мака, поля и ловушки из
шапок audit_truth/{kucoin,bitget,gate}.py) и проверка независимости пакета истин от клиентов коллектора. Сети нет."""
import ast
import pathlib
from decimal import Decimal
import pytest
from funding_bot.audit_truth import TRUTHS, TRUTH_ERRORS
from funding_bot.audit_truth import kucoin as kc_mod, bitget as bg_mod, gate as gt_mod
from funding_bot.audit_truth.kucoin import KucoinTruth
from funding_bot.audit_truth.bitget import BitgetTruth
from funding_bot.audit_truth.gate import GateTruth

H = 3600_000
NOW = 1_789_256_000_000          # 13.09 23:33 UTC


class FakeHttp:
    """GET по окончанию пути (самый длинный подходящий ключ); тело — значение или функция(params)."""
    def __init__(self, get):
        self._get, self.calls, self.log = get, 0, []

    def get(self, url, params=None, gap=None):
        self.calls += 1
        self.log.append((url, dict(params or {})))
        key = max((k for k in self._get if url.endswith(k)), key=len)
        body = self._get[key]
        return body(params or {}) if callable(body) else body


# --- реестр и независимость ------------------------------------------------------------------------------------------
def test_registry_has_the_three_cex_truths():
    assert TRUTHS["kucoin"] is KucoinTruth and TRUTHS["bitget"] is BitgetTruth and TRUTHS["gate"] is GateTruth
    assert not {k: v for k, v in TRUTH_ERRORS.items() if k in ("kucoin", "bitget", "gate")}


# клиенты и расчёты коллектора: истина, которая их импортирует, повторит их ошибки
COLLECTOR = {"hyperliquid", "kucoin_fut", "bitget_fut", "gate_fut", "lighter", "backpack", "variational", "edgex",
             "extended", "pacifica", "apex", "exchanges", "client", "venues", "universe", "calc", "symbols", "spot",
             "collector", "funding", "identity", "identity_src", "dexleg", "okxdex"}


def _imports(path: pathlib.Path) -> set[str]:
    """Модули funding_bot, которые импортирует файл пакета истин (относительный уровень 1 — сам пакет истин)."""
    out = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[1] for a in node.names if a.name.startswith("funding_bot.")}
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level >= 2:
                out |= {mod.split(".")[0]} if mod else {a.name for a in node.names}
            elif node.level == 0 and mod.startswith("funding_bot"):
                parts = mod.split(".")
                out |= {parts[1]} if len(parts) > 1 else {a.name for a in node.names}
    return out


def test_truth_package_never_imports_collector_clients():
    pkg = pathlib.Path(kc_mod.__file__).parent
    bad = {p.name: sorted(_imports(p) & COLLECTOR) for p in sorted(pkg.glob("*.py"))}
    assert not {k: v for k, v in bad.items() if v}, f"истина импортирует код коллектора: {bad}"


# --- KuCoin ----------------------------------------------------------------------------------------------------------------
def kc(sym, base, quote="USDT", inverse=False, typ="FFWCSX", status="Open", ac="CRYPTO", mt="CRYPTO", stage="NORMAL",
       gran=8 * H, cur=8 * H, rate=0.0001, nxt=NOW // (8 * H) * 8 * H + 8 * H, display=None):
    return dict(symbol=sym, baseCurrency=base, displayBaseCurrency=display or base, quoteCurrency=quote, isInverse=inverse,
                type=typ, status=status, assetClass=ac, marketType=mt, marketStage=stage, fundingRateGranularity=gran,
                currentFundingRateGranularity=cur, fundingFeeRate=rate, lastTimeFundingRate=0.5, predictedFundingFeeRate=None,
                nextFundingRateDateTime=nxt, nextFundingRateTime=1121581, displaySymbol=sym)


KC_ACTIVE = [kc("XBTUSDTM", "XBT", rate=-2.3e-05), kc("XBTUSDCM", "XBT", quote="USDC"),
             kc("XBTUSDM", "XBT", quote="USD", inverse=True), kc("XBTMU26", "XBT", quote="USD", inverse=True, typ="FFICSX",
                                                                 gran=None, cur=None),
             kc("NIULAIUSDTM", "NIULAI", display="牛来"), kc("NVDAUSDTM", "NVDA", ac="STOCK", mt="NASDAQ"),
             kc("PAXGUSDTM", "PAXG", ac="METAL"), kc("XAGUSDTM", "XAG", ac="METAL"),
             kc("OPENAIUSDTM", "OPENAI", ac="STOCK", stage="PRE_MARKET"), kc("ODDUSDTM", "ODD", ac="WEIRD"),
             kc("OLDUSDTM", "OLD", ac=None, cur=None), kc("OLDSTKUSDTM", "OLDSTK", ac=None, mt="NASDAQ"),
             kc("TRUSTUSDTM", "TRUST", gran=4 * H, cur=4 * H, nxt=1_789_268_400_000),
             kc("1000BONKUSDTM", "1000BONK")]


def test_kucoin_markets_scope_bases_classes_intervals():
    http = FakeHttp({kc_mod.ACTIVE: {"code": "200000", "data": KC_ACTIVE}})
    T = KucoinTruth(http)
    m = T.markets()
    assert "XBTUSDM" not in m and "XBTMU26" not in m                         # обратный и поставочный — вне охвата
    assert m["XBTUSDTM"]["base"] == "BTC" and m["XBTUSDCM"]["base"] == "BTC"   # XBT → BTC; USDC — дубль по квоте
    assert m["NIULAIUSDTM"]["base"] == "牛来" and "NIULAI" in m["NIULAIUSDTM"]["note"]
    assert m["1000BONKUSDTM"]["base"] == "1000BONK"                          # единицу из имени читает аудитор
    assert {s: m[s]["cls"] for s in ("XBTUSDTM", "NVDAUSDTM", "PAXGUSDTM", "XAGUSDTM", "OPENAIUSDTM", "ODDUSDTM",
                                      "OLDUSDTM", "OLDSTKUSDTM")} == dict(
        XBTUSDTM="crypto", NVDAUSDTM="equity", PAXGUSDTM="crypto", XAGUSDTM="commodity", OPENAIUSDTM="preipo",
        ODDUSDTM=None, OLDUSDTM="crypto", OLDSTKUSDTM="equity")
    assert m["OLDUSDTM"]["interval_h"] == 8 and m["TRUSTUSDTM"]["interval_h"] == 4   # null current → fundingRateGranularity
    assert all(x["tradable"] for x in m.values())
    r = T.rates()
    assert http.calls == 1                                                   # markets и rates — один снимок
    assert r["XBTUSDTM"] == dict(rate=Decimal("-0.000023"), interval_h=8, next_ms=kc("A", "A")["nextFundingRateDateTime"],
                                 kind="predicted")                           # fundingFeeRate, не lastTimeFundingRate
    assert r["TRUSTUSDTM"]["next_ms"] == 1_789_268_400_000                   # сетка со сдвигом — время биржи как есть
    assert "XBTUSDM" not in r and "XBTMU26" not in r


def test_kucoin_not_open_is_listed_untradable_and_errors_are_loud():
    rows = [kc("XBTUSDTM", "XBT"), kc("HALTUSDTM", "HALT", status="Paused")]
    m = KucoinTruth(FakeHttp({kc_mod.ACTIVE: {"code": "200000", "data": rows}})).markets()
    assert m["XBTUSDTM"]["tradable"] and not m["HALTUSDTM"]["tradable"] and "Paused" in m["HALTUSDTM"]["note"]
    bad = KucoinTruth(FakeHttp({kc_mod.HISTORY: {"code": "404000", "msg": "This contract does not exist."}}))
    with pytest.raises(RuntimeError, match="404000"):
        bad.history("NOPEUSDTM", 0, NOW)
    with pytest.raises(RuntimeError, match="пустой"):
        KucoinTruth(FakeHttp({kc_mod.ACTIVE: {"code": "200000", "data": []}})).markets()


def test_kucoin_throttle_code_is_retried():
    seq = [{"code": "429000", "msg": "Too many requests"}, {"code": "200000", "data": [kc("XBTUSDTM", "XBT")]}]
    T = KucoinTruth(FakeHttp({kc_mod.ACTIVE: lambda p: seq.pop(0)}))
    T.THROTTLE_S = 0
    assert list(T.markets()) == ["XBTUSDTM"]


def test_kucoin_history_pages_backwards_from_the_newest():
    ev = [(1_780_000_000_000 + k * 4 * H, f"0.0000{k % 90 + 10}") for k in range(250)]   # 250 расчётов по 4 ч

    def hist(p):
        assert {"symbol", "from", "to"} <= set(p)                          # обе границы обязательны
        win = [(ms, r) for ms, r in ev if p["from"] <= ms <= p["to"]]
        return {"code": "200000", "data": [dict(symbol="XBTUSDTM", fundingRate=float(r), timepoint=ms)
                                           for ms, r in sorted(win, reverse=True)[:100]]}
    http = FakeHttp({kc_mod.HISTORY: hist})
    h = KucoinTruth(http).history("XBTUSDTM", ev[0][0], ev[-1][0] + H)
    assert [ms for ms, _ in h] == [ms for ms, _ in ev] and h[0][1] == Decimal(ev[0][1])
    assert http.calls == 3 and http.log[1][1]["to"] == ev[150][0] - 1       # вторая страница — до самого старого − 1
    part = KucoinTruth(FakeHttp({kc_mod.HISTORY: hist})).history("XBTUSDTM", ev[200][0], ev[210][0])
    assert [ms for ms, _ in part] == [ms for ms, _ in ev[200:211]]


# --- Bitget ----------------------------------------------------------------------------------------------------------------
def bgc(sym, coin, quote="USDT", status="normal", off="-1", lim="-1", launch="", iv="8", st="perpetual"):
    return dict(symbol=sym, baseCoin=coin, quoteCoin=quote, symbolType=st, symbolStatus=status, offTime=off,
                limitOpenTime=lim, launchTime=launch, fundInterval=iv, isRwa="NO")


def bg3(sym, coin, st="crypto", rwa="NO"):
    return dict(symbol=sym, baseCoin=coin, symbolType=st, isRwa=rwa, status="online")


BG_V2 = {"USDT-FUTURES": [bgc("BTCUSDT", "BTC"), bgc("PKXUSDT", "PKX", off="1789369200000", lim="1789358400000"),
                          bgc("NEWUSDT", "NEW", launch=str(NOW + 10 * H)), bgc("MNTUSDT", "MNT", status="maintain"),
                          bgc("EURUSDUSDT", "EURUSD"), bgc("NVDAUSDT", "NVDA"), bgc("XAUTUSDT", "XAUT"),
                          bgc("XAUUSDT", "XAU"), bgc("OPENAIUSDT", "OPENAI"), bgc("SP500USDT", "SP500"),
                          bgc("CLUSDT", "CL"), bgc("龙虾USDT", "龙虾"), bgc("BTCUSDT_261225", "BTC", st="delivery"),
                          bgc("NOV3USDT", "NOV3")],
         "USDC-FUTURES": [bgc("BTCPERP", "BTC", quote="USDC")]}
BG_V3 = {"USDT-FUTURES": [bg3("BTCUSDT", "BTC"), bg3("PKXUSDT", "PKX"), bg3("NEWUSDT", "NEW"), bg3("MNTUSDT", "MNT"),
                          bg3("EURUSDUSDT", "EURUSD", "crypto", "YES"), bg3("NVDAUSDT", "NVDA", "stock", "YES"),
                          bg3("XAUTUSDT", "XAUT", "metal", "YES"), bg3("XAUUSDT", "XAU", "metal", "YES"),
                          bg3("OPENAIUSDT", "OPENAI", "stock", "YES"), bg3("SP500USDT", "SP500", "stock", "YES"),
                          bg3("CLUSDT", "CL", "commodity", "YES"), bg3("龙虾USDT", "龙虾")],
         "USDC-FUTURES": [bg3("BTCPERP", "BTC")]}


def bg_fund(sym, rate="0.000095", iv="8", nxt=1_789_257_600_000):
    return dict(symbol=sym, fundingRate=rate, fundingRateInterval=iv, nextUpdate=str(nxt), minFundingRate="-0.003",
                maxFundingRate="0.003")


BG_FUND = {"USDT-FUTURES": [bg_fund(s["symbol"]) for s in BG_V2["USDT-FUTURES"] if s["symbolType"] == "perpetual"
                            and s["symbol"] != "NVDAUSDT"] + [bg_fund("NVDAUSDT", "-0.0002", "1", 1_789_254_000_000),
                                                             bg_fund("RWATESTMEUSDT", "0.01")],
           "USDC-FUTURES": [bg_fund("BTCPERP", "0.00004")]}


def bg_http(v3_fails=False, hist=None):
    def ok(d):
        return {"code": "00000", "msg": "success", "data": d}

    def v3(p):
        if v3_fails:
            raise RuntimeError("GET instruments: HTTP 500")
        return ok(BG_V3[p["category"]])
    return FakeHttp({bg_mod.CONTRACTS: lambda p: ok(BG_V2[p["productType"]]), bg_mod.INSTRUMENTS: v3,
                     bg_mod.FUND: lambda p: ok(BG_FUND[p["productType"]]), bg_mod.HISTORY: hist or (lambda p: ok([]))})


def test_bitget_markets_status_delisting_and_classes(monkeypatch):
    monkeypatch.setattr(bg_mod, "now_ms", lambda: NOW)
    http = bg_http()
    T = BitgetTruth(http)
    m = T.markets()
    assert "BTCUSDT_261225" not in m and "BTCPERP" in m and m["BTCPERP"]["base"] == "BTC"   # USDC-M — дубль по квоте
    assert m["BTCUSDT"]["tradable"] and m["龙虾USDT"]["tradable"] and m["龙虾USDT"]["base"] == "龙虾"
    assert not m["PKXUSDT"]["tradable"] and "делистинг назначен" in m["PKXUSDT"]["note"]    # статус ещё normal
    assert not m["NEWUSDT"]["tradable"] and "запуск" in m["NEWUSDT"]["note"]
    assert not m["MNTUSDT"]["tradable"] and "maintain" in m["MNTUSDT"]["note"]
    assert {s: m[s]["cls"] for s in ("BTCUSDT", "EURUSDUSDT", "NVDAUSDT", "XAUTUSDT", "XAUUSDT", "OPENAIUSDT",
                                      "SP500USDT", "CLUSDT", "NOV3USDT")} == dict(
        BTCUSDT="crypto", EURUSDUSDT=None, NVDAUSDT="equity", XAUTUSDT="crypto", XAUUSDT="commodity",
        OPENAIUSDT="preipo", SP500USDT="index", CLUSDT="commodity", NOV3USDT=None)       # NOV3: нет в v3 — класс неизвестен
    assert m["NVDAUSDT"]["interval_h"] == 1 and m["BTCUSDT"]["interval_h"] == 8          # интервал — из ставок, живой
    r = T.rates()
    assert "RWATESTMEUSDT" not in r and set(r) <= set(m)                                  # тестовые символы ставок — мимо
    assert r["NVDAUSDT"] == dict(rate=Decimal("-0.0002"), interval_h=1, next_ms=1_789_254_000_000, kind="predicted")
    assert r["BTCPERP"]["rate"] == Decimal("0.00004")
    assert sum(1 for u, _p in http.log if u.endswith(bg_mod.FUND)) == 2                   # один снимок на обе квоты
    m2 = BitgetTruth(bg_http(v3_fails=True)).markets()
    assert all(x["cls"] is None for x in m2.values()) and m2["BTCUSDT"]["tradable"]


def test_bitget_history_pages_by_number_newest_first():
    ev = [(1_780_000_000_000 + k * H, f"0.0000{k % 50 + 10}") for k in range(250)]
    newest = sorted(ev, reverse=True)

    def hist(p):
        assert p["pageSize"] == 100 and p["productType"] == "USDT-FUTURES"
        if p["pageNo"] > 100:
            raise RuntimeError('GET history-fund-rate: HTTP 400 {"code":"40808","msg":"0 < pageNo <=100"}')
        rows = newest[(p["pageNo"] - 1) * 100: p["pageNo"] * 100]
        return {"code": "00000", "data": [dict(symbol="BTCUSDT", fundingRate=r, fundingTime=str(ms)) for ms, r in rows]}
    http = bg_http(hist=hist)
    T = BitgetTruth(http)
    h = T.history("BTCUSDT", ev[0][0], ev[-1][0])
    assert [ms for ms, _ in h] == [ms for ms, _ in ev] and h[-1][1] == Decimal(ev[-1][1]) and http.calls == 3
    http2 = bg_http(hist=hist)
    part = BitgetTruth(http2).history("BTCUSDT", ev[-72][0], ev[-1][0])                 # 72 ч — одна страница
    assert len(part) == 72 and http2.calls == 1
    seen = []

    def usdc(p):
        seen.append(p["productType"])
        return {"code": "00000", "data": []}
    assert BitgetTruth(bg_http(hist=usdc)).history("ETHPERP", 0, NOW) == [] and seen == ["USDC-FUTURES"]

    def gone(p):
        raise RuntimeError('GET history-fund-rate: HTTP 400 {"code":"40034","msg":"Parameter NOPEUSDT does not exist"}')
    with pytest.raises(RuntimeError, match="40034"):
        BitgetTruth(bg_http(hist=gone)).history("NOPEUSDT", 0, NOW)


def test_bitget_history_end_code_after_full_pages_is_the_end():
    full = [dict(symbol="X", fundingRate="0.0001", fundingTime=str(2_000_000_000_000 - k * H)) for k in range(100)]

    def hist(p):
        if p["pageNo"] == 1:
            return {"code": "00000", "data": full}
        raise RuntimeError('HTTP 400 {"code":"40808"}')
    assert len(BitgetTruth(bg_http(hist=hist)).history("XUSDT", 0, 2 * 10**12)) == 100


# --- Gate ------------------------------------------------------------------------------------------------------------------
def gc(name, ct="", iv=28800, nxt=None, rate="0.000061", status="trading", delist=False, pre=False, launch=1_574_035_200):
    return dict(name=name, contract_type=ct, funding_interval=iv, status=status, in_delisting=delist, is_pre_market=pre,
                launch_time=launch, type="direct", funding_rate=rate, funding_rate_indicative=rate,
                funding_next_apply=nxt if nxt is not None else (NOW // (iv * 1000) + 1) * iv)


GT_CONTRACTS = [gc("BTC_USDT"), gc("MBABYDOGE_USDT"), gc("PAXG_USDT", "metals"), gc("XAU_USDT", "metals"),
                gc("IAU_USDT", "metals"), gc("USDC_USDT", "forex"), gc("EURUSD_USDT", "forex"),
                gc("NVDA_USDT", "stocks", 14400), gc("SP500_USDT", "indices"), gc("NG_USDT", "commodities"),
                gc("OPENAI_USDT", "stocks", pre=True, rate="0"), gc("OLD_USDT", delist=True),
                gc("NEW_USDT", launch=NOW // 1000 + 7200), gc("ODD_USDT", "crypto_weird"),
                gc("STORJ_USDT", iv=3600, nxt=(NOW // H + 2) * 3600),                   # перекатилось на час раньше
                gc("LAG_USDT", iv=14400, nxt=(NOW // (4 * H)) * 4 * 3600),             # отстало: расчёт уже прошёл
                gc("龙虾_USDT")]
GT_TICKERS = [dict(contract="BTC_USDT", funding_rate="0.0001", funding_rate_indicative="0.0001"),
              dict(contract="NVDA_USDT", funding_rate="-0.0003")]


def test_gate_markets_classes_units_and_rates(monkeypatch):
    monkeypatch.setattr(gt_mod, "now_ms", lambda: NOW)
    http = FakeHttp({gt_mod.CONTRACTS: GT_CONTRACTS, gt_mod.TICKERS: GT_TICKERS})
    T = GateTruth(http)
    m = T.markets()
    assert len(m) == len(GT_CONTRACTS) and m["龙虾_USDT"]["base"] == "龙虾"
    assert m["MBABYDOGE_USDT"]["base"] == "1MBABYDOGE" and "BABYDOGE" in m["MBABYDOGE_USDT"]["note"]
    assert {s: m[s]["cls"] for s in ("BTC_USDT", "PAXG_USDT", "XAU_USDT", "IAU_USDT", "USDC_USDT", "EURUSD_USDT",
                                      "NVDA_USDT", "SP500_USDT", "NG_USDT", "OPENAI_USDT", "ODD_USDT")} == dict(
        BTC_USDT="crypto", PAXG_USDT="crypto", XAU_USDT="commodity", IAU_USDT="equity", USDC_USDT="crypto",
        EURUSD_USDT="fx", NVDA_USDT="equity", SP500_USDT="index", NG_USDT="commodity", OPENAI_USDT="preipo",
        ODD_USDT=None)
    assert not m["OLD_USDT"]["tradable"] and "in_delisting" in m["OLD_USDT"]["note"]
    assert not m["NEW_USDT"]["tradable"] and m["OPENAI_USDT"]["tradable"] and m["BTC_USDT"]["tradable"]
    assert m["NVDA_USDT"]["interval_h"] == 4 and m["STORJ_USDT"]["interval_h"] == 1
    r = T.rates()
    assert sum(1 for u, _p in http.log if u.endswith(gt_mod.CONTRACTS)) == 1              # один список на markets и rates
    assert r["BTC_USDT"]["rate"] == Decimal("0.0001")                                    # тикеры свежее contracts
    assert r["EURUSD_USDT"]["rate"] == Decimal("0.000061")                               # нет в тикерах — из contracts
    assert r["NVDA_USDT"] == dict(rate=Decimal("-0.0003"), interval_h=4, next_ms=(NOW // (4 * H) + 1) * 4 * H,
                                  kind="predicted")
    assert r["STORJ_USDT"]["next_ms"] == (NOW // H + 1) * H                              # перекат раньше расчёта — назад
    assert r["LAG_USDT"]["next_ms"] == (NOW // (4 * H) + 1) * 4 * H                      # отставшее — вперёд по сетке
    assert all(x["next_ms"] > NOW for x in r.values())
    only_contracts = GateTruth(FakeHttp({gt_mod.CONTRACTS: GT_CONTRACTS,
                                         gt_mod.TICKERS: lambda p: (_ for _ in ()).throw(RuntimeError("HTTP 502"))}))
    assert only_contracts.rates()["BTC_USDT"]["rate"] == Decimal("0.000061")


def test_gate_next_ms_rules():
    iv = 1
    assert gt_mod.next_ms({"funding_next_apply": (NOW // H + 1) * 3600}, iv, NOW) == (NOW // H + 1) * H
    assert gt_mod.next_ms({"funding_next_apply": (NOW // H + 3) * 3600}, iv, NOW) == (NOW // H + 1) * H
    assert gt_mod.next_ms({"funding_next_apply": (NOW // H - 2) * 3600}, iv, NOW) == (NOW // H + 1) * H
    assert gt_mod.next_ms({"funding_next_apply": 0}, iv, NOW) is None


def test_gate_history_seconds_newest_first_and_depth(monkeypatch):
    monkeypatch.setattr(gt_mod, "now_ms", lambda: NOW)
    ev = [((NOW - 1500 * H) // 1000 + k * 3600 + 2, f"0.0000{k % 40 + 10}") for k in range(1500)]   # t в секундах +2 с

    def hist(p):
        assert p["from"] < 10**11 and p["to"] < 10**11 and p["limit"] == 1000              # секунды, не мс
        win = [(t, r) for t, r in ev if p["from"] <= t < p["to"]]
        return [dict(t=t, r=r) for t, r in sorted(win, reverse=True)[:p["limit"]]]
    http = FakeHttp({gt_mod.HISTORY: hist})
    h = GateTruth(http).history("BTC_USDT", ev[0][0] * 1000, ev[-1][0] * 1000)
    assert [ms for ms, _ in h] == [t * 1000 for t, _ in ev] and h[0][1] == Decimal(ev[0][1])
    assert http.calls == 2 and http.log[1][1]["to"] == ev[500][0]                          # to — самый старый t страницы
    http2 = FakeHttp({gt_mod.HISTORY: hist})
    GateTruth(http2).history("BTC_USDT", NOW - 400 * 86400_000, NOW)                      # год назад — биржа отдаёт 180 дней
    assert http2.log[0][1]["from"] >= NOW // 1000 - gt_mod.DEPTH_S
    with pytest.raises(RuntimeError, match="не списком"):
        GateTruth(FakeHttp({gt_mod.HISTORY: {"label": "CONTRACT_NOT_FOUND"}})).history("NOPE_USDT", 0, NOW)
