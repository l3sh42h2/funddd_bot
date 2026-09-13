"""Тестировщик: истины трёх бирж (Binance, Aster, Hyperliquid) на подставном HTTP и чистые функции чтения дашборда/БД.
Сети нет — тела ответов повторяют замер 13.09 (поля и ловушки из шапок audit_truth/*.py)."""
from decimal import Decimal
from funding_bot import audit, db
from funding_bot.audit_truth import TRUTHS, TRUTH_ERRORS, base
from funding_bot.audit_truth.binance import BinanceTruth
from funding_bot.audit_truth.aster import AsterTruth
from funding_bot.audit_truth.hyperliquid import HyperliquidTruth, URL as HL_URL

H = 3600_000


def test_registry_discovers_the_three_moved_truths():
    assert {"binance", "aster", "hyperliquid"} <= set(TRUTHS)
    assert TRUTHS["binance"] is BinanceTruth and TRUTHS["hyperliquid"] is HyperliquidTruth
    assert not {k: v for k, v in TRUTH_ERRORS.items() if k in ("binance", "aster", "hyperliquid", "_fapi")}
    assert all(c.venue == v for v, c in TRUTHS.items())


def test_norm_base_and_dec():
    assert base.norm_base("xyz:NATGAS") == ("NATGAS", 1.0)
    assert base.norm_base("kPEPE") == ("PEPE", 1000.0) and base.norm_base("1000PEPE") == ("PEPE", 1000.0)
    assert base.norm_base("xyz:kBONK") == ("BONK", 1000.0)
    assert base.norm_base("1MBABYDOGE") == ("BABYDOGE", 1_000_000.0) and base.norm_base("1INCH") == ("1INCH", 1.0)
    assert base.dec("0.0000125") == Decimal("0.0000125") and base.dec(0.1) == Decimal("0.1")
    assert base.dec("") is None and base.dec(None) is None and base.dec("nan") is None and base.dec(True) is None


class FakeHttp:
    """Подставной HTTP истины: GET по окончанию пути, POST по type (+dex); считает вызовы."""
    def __init__(self, get=None, post=None):
        self._get, self._post, self.calls, self.log = get or {}, post or (lambda b: None), 0, []

    def get(self, url, params=None, gap=None):
        self.calls += 1; self.log.append((url, params))
        body = next(v for k, v in self._get.items() if url.endswith(k))
        return body(params) if callable(body) else body

    def post(self, url, body=None, gap=None):
        self.calls += 1; self.log.append((url, body))
        return self._post(body)


def _sym(symbol, status="TRADING", ct="PERPETUAL", quote="USDT", ut="COIN", sub=(), channel=None, base_=None):
    return dict(symbol=symbol, status=status, contractType=ct, quoteAsset=quote, underlyingType=ut, underlyingSubType=list(sub),
                channel=channel, baseAsset=base_ or symbol[: -len(quote)])


def test_fapi_truth_markets_rates_history():
    info = {"symbols": [_sym("BTCUSDT"), _sym("BTCUSDC", quote="USDC"), _sym("OMGUSDT", status="SETTLING"),
                        _sym("NEWUSDT", status="PENDING_TRADING", ct=""), _sym("BTCUSDT_260925", ct="CURRENT_QUARTER", base_="BTC"),
                        _sym("NVDAUSDT", ct="TRADIFI_PERPETUAL", ut="EQUITY"), _sym("XIAOMIUSDT", ct="TRADIFI_PERPETUAL", ut="HK_EQUITY"),
                        _sym("XAUUSDT", ct="TRADIFI_PERPETUAL", ut="COMMODITY"), _sym("BTCDOMUSDT", ut="INDEX"),
                        _sym("PREUSDT", ut="PREMARKET"), _sym("ODDUSDT", ct="TRADIFI_PERPETUAL", ut="SOMETHING")]}
    prem = [dict(symbol="BTCUSDT", lastFundingRate="0.00010000", nextFundingTime=1789257600000),
            dict(symbol="OMGUSDT", lastFundingRate="0", nextFundingTime=0),
            dict(symbol="GHOSTUSDT", lastFundingRate="0.001", nextFundingTime=1789257600000)]     # вне exchangeInfo — не рынок
    fi = [dict(symbol="BTCUSDT", fundingIntervalHours=4)]
    page1 = [dict(symbol="BTCUSDT", fundingTime=1_000 + k * 4 * H, fundingRate="0.0001") for k in range(1000)]

    def hist(p):
        return page1 if p["startTime"] <= 1_000 else [dict(symbol="BTCUSDT", fundingTime=1_000 + 1000 * 4 * H, fundingRate="-0.0002")]
    T = BinanceTruth(FakeHttp(get={"/exchangeInfo": info, "/premiumIndex": prem, "/fundingInfo": fi, "/fundingRate": hist}))
    m = T.markets()
    assert "BTCUSDT_260925" not in m                                              # поставочный — без фандинга
    assert m["BTCUSDT"]["tradable"] and m["BTCUSDT"]["interval_h"] == 4 and m["BTCUSDC"]["interval_h"] == 8  # умолчание 8
    assert not m["OMGUSDT"]["tradable"] and "SETTLING" in m["OMGUSDT"]["note"] and not m["NEWUSDT"]["tradable"]
    assert {s: m[s]["cls"] for s in ("NVDAUSDT", "XIAOMIUSDT", "XAUUSDT", "BTCDOMUSDT", "PREUSDT", "ODDUSDT", "BTCUSDT")} == \
        dict(NVDAUSDT="equity", XIAOMIUSDT="equity", XAUUSDT="commodity", BTCDOMUSDT="index", PREUSDT="preipo", ODDUSDT=None,
             BTCUSDT="crypto")
    r = T.rates()
    assert set(r) == {"BTCUSDT", "OMGUSDT"} and r["BTCUSDT"] == dict(rate=Decimal("0.0001"), interval_h=4,
                                                                      next_ms=1789257600000, kind="predicted")
    h = T.history("BTCUSDT", 0, 10**15)
    assert len(h) == 1001 and h[-1] == (1_000 + 1000 * 4 * H, Decimal("-0.0002"))   # вторая страница после полной первой


def test_aster_classes_by_subtype_and_channel():
    T = AsterTruth(FakeHttp())
    assert T.asset_class(_sym("XAUUSDT", sub=["Commodities"], channel="forex")) == "commodity"   # металлы — в канале forex
    assert T.asset_class(_sym("PAXGUSDT", sub=["Commodities"], channel="forex")) == "crypto"      # токен золота — монета
    assert T.asset_class(_sym("EURUSDT", channel="forex")) == "fx"
    assert T.asset_class(_sym("TSLAUSDT", sub=["STOCK"])) == "equity"
    assert T.asset_class(_sym("SMSNUSDT", sub=["pre-launch", "STOCK"])) == "preipo"
    assert T.asset_class(_sym("XIAOMIUSDT", channel="hkstock")) == "equity"
    assert T.asset_class(_sym("DOGEUSDT", sub=["Meme"], channel="")) == "crypto"


def test_hyperliquid_truth_all_dexes_categories_and_history():
    uni = {"": ([dict(name="BTC"), dict(name="kPEPE"), dict(name="OLD", isDelisted=True)],
                [dict(funding="0.0000125"), dict(funding="-0.00001"), dict(funding="0")]),
           "xyz": ([dict(name="xyz:NVDA"), dict(name="xyz:GOLD"), dict(name="xyz:NEW")],
                   [dict(funding="0.00005"), dict(funding="0.00001"), dict(funding="0.00002")])}

    def post(b):
        t = b["type"]
        if t == "perpDexs":
            return [None, {"name": "xyz"}]
        if t == "perpCategories":
            return [["xyz:NVDA", "stocks"], ["xyz:GOLD", "commodities"]]
        if t == "metaAndAssetCtxs":
            u, c = uni[b.get("dex", "")]
            return [{"universe": u}, c]
        if t == "fundingHistory":
            if b["startTime"] < 10 * H:
                return [dict(coin="BTC", time=k * H + 10, fundingRate="0.00001") for k in range(500)]
            return [dict(coin="BTC", time=500 * H + 10, fundingRate="0.00002")]
    http = FakeHttp(post=post)
    T = HyperliquidTruth(http)
    m = T.markets(); r = T.rates()
    assert http.calls == 4                                   # категории + dex-ы + два снимка; rates() взял тот же снимок
    assert m["BTC"]["cls"] == "crypto" and m["kPEPE"]["base"] == "kPEPE" and not m["OLD"]["tradable"]
    assert m["xyz:NVDA"]["cls"] == "equity" and m["xyz:GOLD"]["cls"] == "commodity" and m["xyz:NEW"]["cls"] is None
    assert m["xyz:NVDA"]["base"] == "NVDA" and all(x["interval_h"] == 1 for x in m.values())
    assert "OLD" not in r and r["BTC"]["rate"] == Decimal("0.0000125") and r["BTC"]["interval_h"] == 1
    assert r["BTC"]["next_ms"] % H == 0 and r["BTC"]["next_ms"] > base.now_ms()
    assert HyperliquidTruth.site_hours == 8 and HyperliquidTruth.quote_twins is False
    h = T.history("BTC", 0, 10**15)
    assert len(h) == 501 and h[0] == (10, Decimal("0.00001"))
    assert all(u == HL_URL for u, _b in http.log)


def test_collected_is_the_latest_universe_of_each_venue(tmp_path):
    """Пара, которую коллектор по замыслу отбросил полчаса назад (ST, делистинг), — уже не «собирается»."""
    con = db.connect(tmp_path / "a.db")
    now = 1_789_000_000
    row = lambda ex, s, seen: con.execute("INSERT INTO instruments(exchange,symbol,base,last_seen) VALUES(?,?,?,?)", (ex, s, s[:3], seen))
    row("gate_spot", "ABC_USDT", now); row("gate_spot", "DROP_USDT", now - 1800)
    row("binance", "ABCUSDT", now - 7200); row("binance", "XUSDT", now - 7200)
    con.commit()
    assert audit.collected_now(con) == {("gate_spot", "ABC_USDT"), ("binance", "ABCUSDT"), ("binance", "XUSDT")}
    assert audit.db_universe(con)[("binance", "ABCUSDT")] == {"base": "ABC", "cls": None, "interval_h": None}
    con.execute("ALTER TABLE instruments ADD COLUMN cls TEXT")                  # новая схема с классом — читается
    con.execute("UPDATE instruments SET cls='rwa' WHERE symbol='XUSDT'"); con.commit()
    assert audit.db_universe(con)[("binance", "XUSDT")]["cls"] == "other"      # «rwa» коллектора = «other» истины


def test_dashboard_legs_and_index_from_both_tables():
    table = {"ff_rows": [dict(key="aster:XUSDT|hyperliquid:X", base="X", cls="crypto", va="aster", sa="XUSDT", vb="hyperliquid",
                              sb="X", rate_h_a=0.00001, rate_h_b=0.00002, iv_a=4, iv_b=1, next_a=5, next_b=6, mismatch=False,
                              stale=True, windows={"24": dict(a=0.001, na=3, b=0.0005, nb=24, incomplete=False)})],
             "sf_rows": [dict(key="gate_spot:X_USDT|hyperliquid:X", base="X", cls="crypto", spot_ex="gate_spot", spot="X_USDT",
                              perp_ex="hyperliquid", perp="X", rate_h=0.00002, period=1, next_ms=6, mismatch=False,
                              windows={"24": dict(spread=0.0005, n=24, incomplete=True)})]}
    legs = audit.dashboard_legs(table)
    L = legs[("hyperliquid", "X")]
    assert L["rows"] == ["aster:XUSDT|hyperliquid:X", "gate_spot:X_USDT|hyperliquid:X"] and L["stale_rows"] == 1
    assert L["windows"]["24"] == dict(sum=0.0005, n=24, incomplete=False) and L["iv"] == 1 and L["next_ms"] == 6
    assert legs[("aster", "XUSDT")]["windows"]["24"]["sum"] == 0.001 and legs[("aster", "XUSDT")]["iv"] == 4
    idx = audit.dashboard_index(table)
    assert ("gate_spot", "X_USDT", "crypto") in idx["X"] and ("aster", "XUSDT", "crypto") in idx["X"]


def test_compare_db_missing_extra_values():
    now = 1000 * H
    truth = [(now - k * H + 7, Decimal("0.0001")) for k in range(1, 30)][::-1]
    dbr = [(ms, float(r)) for ms, r in truth if ms != now - 5 * H + 7]            # один пропуск
    dbr = [(ms + (1 if ms == now - 3 * H + 7 else 0), (0.0002 if ms == now - 7 * H + 7 else r)) for ms, r in dbr]
    res = audit.compare_db(truth, dbr, now)
    assert res["missing"] == [(now - 5 * H) // 60_000 * 60_000] and res["extra"] == [] and len(res["values"]) == 1
    assert res["n_truth"] == 29 and res["n_db"] == 28
