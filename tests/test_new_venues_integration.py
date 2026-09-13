"""Интеграция новых перп-площадок 12.09 (владелец: «добавить фьючи kucoin bitget и gate, lighter; lighter нужны 2 вида:
мейн и robinhood, оба спот и фьюч»): конфиг и порядок владельца, клиенты, пары только внутри класса актива, ссылки и
подписи, галочки дашборда, «тот ли актив» для новых площадок, коллектор с площадкой без страницы рынка. Без сети."""
from __future__ import annotations
import time
from funding_bot import config, venues, universe, calc, dashboard, identity, db
from funding_bot.identity import Resolver, coin_record, decide_sf, decide_ff
from fakes import make_world, FakeNative, grid

NEW_PERPS = ("kucoin", "bitget", "gate", "lighter", "lighter_rh")
# 13.09: «добавь биржи backpack, variational, edgex, extended, pacifica, apex во фьючи» — в этом порядке после lighter_rh
NEW_13 = ("backpack", "variational", "edgex", "extended", "pacifica", "apex")


def test_owner_order_and_every_venue_has_fee_and_interval_rule():
    assert config.PERP_VENUES == ("aster", "binance", "hyperliquid") + NEW_PERPS + NEW_13
    assert config.SPOT_VENUES == ("binance_spot", "gate_spot", "kucoin_spot", "bitget_spot", "lighter_spot", "lighter_rh_spot")
    assert all(v in config.FEES_TAKER for v in config.PERP_VENUES + config.SPOT_VENUES)   # calc: KeyError иначе
    # Lighter, Backpack, Extended, ApeX — каждый час на часе; KuCoin/Bitget/Gate, Variational, edgeX меняют интервал на ходу,
    # Pacifica пропускала расчёты — не «неизменные»
    assert config.FIXED_INTERVAL_H == {"hyperliquid": 1, "lighter": 1, "lighter_rh": 1, "backpack": 1, "extended": 1, "apex": 1}
    assert "lighter_rh" not in config.URLS and "lighter_rh_spot" not in config.URLS      # страницы рынка RH нет
    assert "extended" not in config.URLS          # у Extended ссылка только из инструмента (uiName), шаблон по имени API врёт
    assert {v: config.FEES_TAKER[v] for v in NEW_13} == {"backpack": 0.0005, "variational": 0.0, "edgex": 0.00045,
                                                         "extended": 0.00025, "pacifica": 0.0004, "apex": 0.0005}
    assert set(config.STALE_S_BY_VENUE) <= set(config.PERP_VENUES)


def test_make_clients_builds_new_venues_without_network(monkeypatch):
    import requests

    def no_net(*a, **k):
        raise AssertionError("сеть в конструкторе клиента")
    monkeypatch.setattr(requests.Session, "request", no_net)
    from funding_bot.client import make_clients
    cl = make_clients()
    assert [v for v in config.PERP_VENUES if v in cl] == list(config.PERP_VENUES)
    for v in NEW_PERPS:
        assert venues.native(cl[v]) and not venues.spot_native(cl[v]) and cl[v].name == v
    for v in ("lighter_spot", "lighter_rh_spot"):
        assert venues.spot_native(cl[v]) and not venues.native(cl[v]) and hasattr(cl[v], "coin_blob")
    assert cl["lighter"]._stream is None and cl["lighter_spot"]._stream is None      # WebSocket — только с первым books()
    assert cl["gate"].history_gap_s == 0.1 and cl["lighter_rh"].history_gap_s == 1.5  # темп истории — из конфига


def test_funding_intervals_hook_of_native_clients():
    class Live:
        def perp_instruments(self): return []
        def funding_intervals(self): return {"IOSTUSDT": 1}

    class Fixed:
        def perp_instruments(self): return []
    assert venues.funding_intervals(Live()) == {"IOSTUSDT": 1} and venues.funding_intervals(Fixed()) is None


def _p(sym, base, cls="crypto", factor=1.0, url=None):
    return dict(symbol=sym, base=base, cls=cls, factor=factor, url=url)


def _s(sym, base, factor=1.0, alt=None, url=None):
    return dict(symbol=sym, base=("~" + base) if alt else base, base_asset=base, factor=factor, alt_base=alt, url=url)


def test_new_venues_pair_in_owner_order_and_only_within_class():
    perps = {"binance": [_p("BTCUSDT", "BTC"), _p("QNTUSDT", "QNT")],                    # Quant — монета
             "hyperliquid": [_p("xyz:AAPL", "AAPL", "equity")],
             "kucoin": [_p("XBTUSDTM", "BTC"), _p("AAPLUSDTM", "AAPL", "equity")],
             "bitget": [_p("BTCUSDT", "BTC"), _p("QNTSTOCKUSDT", "QNT", "equity"),       # Quantinuum — акция
                        _p("1000BONKUSDT", "BONK", factor=1000.0)],
             "gate": [_p("BTC_USDT", "BTC"), _p("MBABYDOGE_USDT", "BABYDOGE", factor=1e6)],
             "lighter": [_p("BTC", "BTC"), _p("AAPL", "AAPL", "equity")],
             "lighter_rh": [_p("AAPL", "AAPL", "equity"), _p("BTC", "BTC")]}
    ff = universe.build_ff(perps)
    for r in ff:
        assert config.PERP_VENUES.index(r["va"]) < config.PERP_VENUES.index(r["vb"])        # A — раньше у владельца
    assert len([r for r in ff if r["base"] == "BTC"]) == 15                                  # 6 площадок с BTC: C(6,2)
    keys = {r["key"] for r in ff}
    assert {"kucoin:XBTUSDTM|lighter_rh:BTC", "lighter:BTC|lighter_rh:BTC", "binance:BTCUSDT|gate:BTC_USDT"} <= keys
    aapl = [r for r in ff if r["base"] == "AAPL"]
    assert len(aapl) == 6 and {r["cls"] for r in aapl} == {"equity"}                        # HL, KuCoin, Lighter ×2
    assert not any(r["base"] == "QNT" for r in ff)                                           # монета ≠ акция
    spots = {"gate_spot": [_s("BONK_USDT", "BONK"), _s("BABYDOGE_USDT", "BABYDOGE")],
             "lighter_spot": [_s("BTC/USDC", "BTC", url="https://app.lighter.xyz/trade/BTC_USDC")],
             "lighter_rh_spot": [_s("AAPL/USDG", "AAPL", factor=1.000566, alt="AAPL")]}
    sf = {r["key"]: r for r in universe.build_sf(perps, spots)}
    bonk = sf["gate_spot:BONK_USDT|bitget:1000BONKUSDT"]
    assert (bonk["perp_factor"], bonk["spot_factor"]) == (1000.0, 1.0)
    assert sf["gate_spot:BABYDOGE_USDT|gate:MBABYDOGE_USDT"]["perp_factor"] == 1e6
    rh = {k for k in sf if k.startswith("lighter_rh_spot:")}                                 # токен акции — только акциям
    assert rh == {"lighter_rh_spot:AAPL/USDG|" + x for x in ("hyperliquid:xyz:AAPL", "kucoin:AAPLUSDTM", "lighter:AAPL",
                                                              "lighter_rh:AAPL")}
    btc = [r for k, r in sf.items() if k.startswith("lighter_spot:BTC/USDC|")]
    assert len(btc) == 6 and all(r["spot_url"] == "https://app.lighter.xyz/trade/BTC_USDC" for r in btc)


def test_links_come_from_the_instrument_then_the_template_and_rh_has_none():
    enc = "https://www.bitget.com/futures/usdt/%E9%BE%99%E8%99%BEUSDT"                     # 龙虾USDT, как кладёт bitget_fut
    item = dict(key="bitget:龙虾USDT|lighter_rh:X", base="X", cls="crypto", va="bitget", vb="lighter_rh", sa="龙虾USDT",
                sb="X", fa=1.0, fb=1.0)
    row = calc.build_ff_row(item, {"interval_h": 8, "url": enc}, {"interval_h": 1, "url": None}, None, None, None, None,
                            {}, {}, 0)
    assert row["urls"] == [enc, None] and (row["la"], row["lb"]) == ("bitget", "lighter·rh")
    assert abs(row["fee"] - 2 * 0.0006) < 1e-15                                              # Lighter — 0 %
    it2 = dict(item, va="kucoin", sa="XBTUSDTM", vb="gate", sb="BTC_USDT")
    row = calc.build_ff_row(it2, {"interval_h": 8}, {"interval_h": 8}, None, None, None, None, {}, {}, 0)
    assert row["urls"] == ["https://www.kucoin.com/trade/futures/XBTUSDTM", "https://www.gate.com/futures/USDT/BTC_USDT"]
    assert abs(row["fee"] - 2 * (0.0006 + 0.0005)) < 1e-15
    sf = dict(key="lighter_rh_spot:AAPL/USDG|lighter:AAPL", base="AAPL", cls="equity", spot_ex="lighter_rh_spot",
              spot="AAPL/USDG", spot_asset="AAPL", spot_factor=1.0, spot_tag="AAPL", spot_url=None, perp_ex="lighter",
              perp="AAPL", perp_factor=1.0)
    row = calc.build_sf_row(sf, {"interval_h": 1, "url": "https://app.lighter.xyz/trade/AAPL"}, None, None, None, {}, 0)
    assert row["urls"] == {"spot": None, "perp": "https://app.lighter.xyz/trade/AAPL"}
    assert row["spot_label"] == "lighter·rh·AAPL" and row["fee"] == 0.0
    assert calc.label("hyperliquid", "xyz:NET") == "hyperliquid·xyz" and calc.label("binance", "BTCUSDT") == "binance"


def test_dashboard_pickers_list_the_new_venues_and_links_tolerate_no_page():
    sp, pp = dashboard.pickers(config.PERP_VENUES, config.SPOT_VENUES)
    assert [x[1] for x in pp] == ["ApeX", "Aster", "Backpack", "Binance", "Bitget", "edgeX", "Extended", "Gate", "Hyperliquid",
                                  "KuCoin", "Lighter", "Lighter Robinhood", "Pacifica", "Variational"]
    assert all(x[2] != "#888" for x in pp)                                                    # у каждой — свой значок
    assert {x[0] for x in pp} == set(config.PERP_VENUES)
    assert {(x[0], x[1]) for x in sp} >= {("lighter_spot", "Lighter"), ("lighter_rh_spot", "Lighter Robinhood")}
    assert "href ? `<a" in dashboard.JS                                                       # нет страницы — имя без ссылки


# --- «тот ли актив» ---------------------------------------------------------------------------------------------------
def _blob(markets, coins):
    return {"coins": coins, "markets": markets}


def _rec(name, addr):
    return coin_record(name, [("ETH", addr, True, True)], "")


BTC = coin_record("Bitcoin", [("BTC", "", True, True)], "BTC")


def _sf(spot_ex, spot, perp_ex, perp, base, pf=1.0):
    return dict(key=f"{spot_ex}:{spot}|{perp_ex}:{perp}", base=base, cls="crypto", spot_ex=spot_ex, spot=spot,
                spot_factor=1.0, perp_ex=perp_ex, perp=perp, perp_factor=pf)


def _ff(va, sa, vb, sb, base):
    return dict(key=f"{va}:{sa}|{vb}:{sb}", base=base, cls="crypto", va=va, sa=sa, vb=vb, sb=sb, fa=1.0, fb=1.0)


def test_new_cex_perps_weak_same_venue_code_evidence():
    """Состава индекса KuCoin/Bitget/Gate коллектор пока не собирает: монета СВОЕЙ спот-площадки с тем же кодом —
    слабое свидетельство, как «venue_code» у Binance; по нему остальные споты решаются контрактом."""
    bonk, edgex, definitive = "0x" + "b0" * 20, "0x" + "ed" * 20, "0x" + "de" * 20
    spots = {"kucoin_spot": _blob({"BTC-USDT": ["BTC", True]}, {"BTC": BTC}),
             "bitget_spot": _blob({"BONKUSDT": ["BONK", True], "EDGEUSDT": ["EDGE", True]},
                                  {"BONK": _rec("Bonk", bonk), "EDGE": _rec("edgeX", edgex)}),
             "gate_spot": _blob({"BONK_USDT": ["BONK", True], "EDGE_USDT": ["EDGE", True], "BTC_USDT": ["BTC", True]},
                                {"BONK": _rec("Bonk", bonk), "EDGE": _rec("Definitive", definitive), "BTC": BTC})}
    R = Resolver(spots, {})
    d = decide_sf(R, _sf("kucoin_spot", "BTC-USDT", "kucoin", "XBTUSDTM", "BTC"))            # XBT у KuCoin — это BTC
    assert (d["ident"], d["ident_ev"]) == ("same", "venue_code") and "kucoin" in d["ident_why"]
    d = decide_sf(R, _sf("bitget_spot", "BONKUSDT", "bitget", "1000BONKUSDT", "BONK", pf=1000.0))
    assert (d["ident"], d["ident_ev"]) == ("same", "venue_code")                              # единица ×1000 сходится
    d = decide_sf(R, _sf("gate_spot", "BONK_USDT", "bitget", "1000BONKUSDT", "BONK", pf=1000.0))
    assert (d["ident"], d["ident_ev"]) == ("same", "contract")                                # чужой спот — по контракту
    d = decide_sf(R, _sf("bitget_spot", "EDGEUSDT", "gate", "EDGE_USDT", "EDGE"))              # Gate EDGE = Definitive
    assert (d["ident"], d["ident_ev"]) == ("other", "contract_conflict")
    assert decide_ff(R, _ff("kucoin", "XBTUSDTM", "gate", "BTC_USDT", "BTC"))["ident"] == "same"
    assert identity.venue_code("gate", "MBABYDOGE_USDT") == ("BABYDOGE", 1e6)
    # монеты с тем же кодом на своём споте нет — «не проверено» со своей причиной, а не догадка по тикеру
    assert R.identity("gate", "WAT_USDT")["status"] == "index_not_collected"
    assert R.identity("kucoin", "WATUSDTM")["status"] == "no_index"                           # у KuCoin в индексе лишь биржи
    assert Resolver({}, {}).identity("bitget", "BTCUSDT")["status"] == "no_sources"
    assert R.identity("binance", "BTCUSDT")["status"] == "no_index"                           # Binance — как было


def test_a_name_equal_to_the_ticker_is_no_name_for_a_conflict():
    """Живой срез 12.09: KuCoin NIGHT (Cardano, «имя» NIGHT) и Binance NIGHT «Midnight» (BSC) — одна монета: индекс Binance
    NIGHTUSDT берёт KuCoin NIGHT-USDT. Разные контракты при «имени»-тикере — «не проверено», а не «≠»; настоящие разные
    имена (Gate EDGE — Definitive против edgeX) по-прежнему «≠» (тест выше)."""
    ada = "0691b2fecca1ac4f53cb6dfb00b7013e561d1f34403b957cbb5af1fa.4e49474854"
    spots = {"kucoin_spot": _blob({"NIGHT-USDT": ["NIGHT", True]},
                                  {"NIGHT": coin_record("NIGHT", [("ADA", ada, True, True)], "NIGHT")}),
             "binance_spot": _blob({"NIGHTUSDT": ["NIGHT", True]},
                                   {"NIGHT": coin_record("Midnight", [("BSC", "0xfe930c2d63aed9b82fc4dbc801920dd2c1a3224f",
                                                                       True, True), ("ADA", None, True, True)], "NIGHT")})}
    R = Resolver(spots, {})
    d = decide_sf(R, _sf("binance_spot", "NIGHTUSDT", "kucoin", "NIGHTUSDTM", "NIGHT"))
    assert (d["ident"], d["ident_ev"], d["ident"] == "other") == ("unknown", "contract_disjoint_no_name", False)
    assert "тикер" in d["ident_why"]


def test_commodity_and_fx_names_meet_by_definition():
    from funding_bot import lighter, gate_fut
    assert lighter.perp_base("XCU", "commodity") == ("COPPER", 1.0)                       # Aster/Lighter XCU = медь
    assert lighter.perp_base("BRENTOIL", "commodity") == ("BZ", 1.0)
    assert gate_fut.perp_base("XCU", "commodity") == ("COPPER", 1.0)
    assert {config.PERP_CANON["commodity"][k] for k in ("PLATINUM", "PALLADIUM")} == {"XPT", "XPD"}
    assert config.PERP_CANON["fx"] == {"EURUSD": "EUR", "GBPUSD": "GBP", "USDJPY": "JPY"}
    perps = {"hyperliquid": [_p("xyz:PLATINUM", "XPT", "commodity"), _p("xyz:JPY", "JPY", "fx")],
             "bitget": [_p("XPTUSDT", "XPT", "commodity"), _p("USDJPYUSDT", "JPY", "fx")]}
    assert {r["key"] for r in universe.build_ff(perps)} == {"hyperliquid:xyz:PLATINUM|bitget:XPTUSDT",
                                                           "hyperliquid:xyz:JPY|bitget:USDJPYUSDT"}


def test_lighter_perps_are_unknown_unless_name_evidence():
    spots = {"gate_spot": _blob({"AI_USDT": ["AI", True], "AINU_USDT": ["AINU", True]},
                                {"AI": _rec("Sleepless AI", "0x" + "a1" * 20), "AINU": _rec("Artificial Inu", "0x" + "a2" * 20)})}
    d = decide_sf(Resolver(spots, {}), _sf("gate_spot", "AI_USDT", "lighter", "AI", "AI"))
    assert (d["ident"], d["ident_ev"]) == ("unknown", "oracle_only")
    R = Resolver(spots, {}, perp_names={"lighter": {"AI": "Artificial Inu"}})                  # имя из tokenlist Lighter
    d = decide_sf(R, _sf("gate_spot", "AI_USDT", "lighter", "AI", "AI"))
    assert (d["ident"], d["ident_ev"]) == ("unknown", "name_only")                            # имя разное — но не «≠»
    d = decide_sf(R, _sf("gate_spot", "AINU_USDT", "lighter", "AI", "AI"))
    assert (d["ident"], d["ident_ev"]) == ("same", "name")
    d = decide_ff(R, _ff("lighter", "AI", "lighter_rh", "AI", "AI"))
    assert (d["ident"], d["ident_ev"]) == ("unknown", "oracle_only")


def test_collected_gate_and_bitget_index_legs_when_the_collector_gets_them():
    from funding_bot.gate_fut import GateFut
    legs_gate = GateFut.legs_of([{"exchange": "Gate", "symbols": ["BTC_USDT"], "weight": "0.5"},
                                 {"exchange": "Binance", "symbols": ["BTC_USDT"], "weight": "0.5"}])
    spots = {"gate_spot": _blob({"BTC_USDT": ["BTC", True]}, {"BTC": BTC}),
             "binance_spot": _blob({"BTCUSDT": ["BTC", True]}, {"BTC": BTC})}
    self_only = {"legs": [{"exchange": "bitget_cross", "symbol": "BTC_USDT", "weight": "1"}], "dex": {}}
    R = Resolver(spots, {"gate": {"BTC_USDT": {"legs": legs_gate, "dex": {}}}, "bitget": {"BTCUSDT": self_only}})
    d = decide_sf(R, _sf("gate_spot", "BTC_USDT", "gate", "BTC_USDT", "BTC"))
    assert (d["ident"], d["ident_ev"]) == ("same", "index_market")                            # рынок спота — в индексе
    assert R.identity("bitget", "BTCUSDT")["status"] == "self_only"                           # индекс — только своя цена
    R2 = Resolver(dict(spots, bitget_spot=_blob({"BTCUSDT": ["BTC", True]}, {"BTC": BTC})), {"bitget": {"BTCUSDT": self_only}})
    assert decide_sf(R2, _sf("bitget_spot", "BTCUSDT", "bitget", "BTCUSDT", "BTC"))["ident_ev"] == "venue_code"
    # словарь бирж из индексов Gate/Bitget и множитель «*1e+06» (так пишет bitget_fut._leg у 1MBABYDOGE)
    P = Resolver({}, {})._parse
    assert P({"exchange": "okx", "symbol": "BABYDOGE_USDT*1e+06", "weight": "1"})["mult"] == 1e6
    assert P({"exchange": "okx", "symbol": "PEPE_USDT*1000", "weight": "1"})["mult"] == 1000.0
    assert P({"exchange": "huobi", "symbol": "BTC_USDT", "weight": "1"})["kind"] == "cex"
    assert P({"exchange": "binance_index", "symbol": "BTC_USDT", "weight": "1"})["kind"] == "perpref"
    assert P({"exchange": "intrinio", "symbol": "NVDA_USD", "weight": "1"})["kind"] == "vendor"
    assert P({"exchange": "pancakeswapv4", "symbol": "CAKE-WBNB", "weight": "1"})["kind"] == "dex"


def test_collector_table_with_new_venues_and_a_venue_without_market_page(tmp_path):
    from funding_bot.collector import Collector
    w = make_world()
    now_ms = int(time.time() * 1000)
    g = FakeNative("gate", iv_h=8)
    g.markets = {"ABC_USDT": dict(base="ABC", rate=0.0003, mark=10.0, bid=9.99, ask=10.01,
                                  url="https://www.gate.com/futures/USDT/ABC_USDT")}
    g.history = {"ABC_USDT": grid(now_ms, 8, 0.0003)}
    rh = FakeNative("lighter_rh", iv_h=1)
    rh.markets = {"ABC": dict(base="ABC", rate=0.00002, mark=10.0, bid=9.99, ask=10.01, url=None)}
    rh.history = {"ABC": grid(now_ms, 1, 0.00002)}
    w.update(gate=g, lighter_rh=rh)
    t = [time.time()]
    col = Collector(clients=w, db_path=tmp_path / "c.db", table_path=tmp_path / "t.json", now=lambda: t[0],
                    sleep=lambda s: None, background=False)
    tbl = col.once()
    assert col.venues == ["aster", "binance", "hyperliquid", "gate", "lighter_rh"]
    ff = {r["key"]: r for r in tbl["ff_rows"]}
    k = "gate:ABC_USDT|lighter_rh:ABC"
    assert ff[k]["urls"] == ["https://www.gate.com/futures/USDT/ABC_USDT", None] and ff[k]["lb"] == "lighter·rh"
    assert abs(ff[k]["spread_h"] - (0.0003 / 8 - 0.00002)) < 1e-15
    assert {"aster:ABCUSDT|gate:ABC_USDT", "hyperliquid:ABC|lighter_rh:ABC"} <= set(ff)
    sf = {r["key"]: r for r in tbl["sf_rows"]}
    assert sf["gate_spot:ABC_USDT|lighter_rh:ABC"]["urls"]["perp"] is None
    assert {("gate", "ABC_USDT"), ("lighter_rh", "ABC")} <= set(db.leg_depths(col.con))      # история новых ног собрана
    html = dashboard.render(tbl)
    assert "Lighter Robinhood" in html and "Gate" in html


# --- 13.09: backpack, variational, edgex, extended, pacifica, apex ----------------------------------------------------------
def test_make_clients_builds_the_13_09_venues_without_network(monkeypatch):
    import requests

    def no_net(*a, **k):
        raise AssertionError("сеть в конструкторе клиента")
    monkeypatch.setattr(requests.Session, "request", no_net)
    from funding_bot.client import make_clients
    cl = make_clients()
    for v in NEW_13:
        assert venues.native(cl[v]) and not venues.spot_native(cl[v]) and cl[v].name == v
    for v in ("backpack", "edgex", "pacifica", "apex"):
        assert cl[v]._stream is None                                  # WebSocket — только с первым premium()/books()
        assert cl[v].history_gap_s == config.FUNDING_HISTORY_MIN_GAP_S[v]
    assert cl["extended"].history_gap_s == 0.25
    # у Variational публичной истории нет: своя книга расчётов, глубина с пола не собирается (collector.own_history)
    from funding_bot import collector
    assert [v for v in NEW_13 if collector.own_history(cl[v])] == ["variational"]
    # интервалы на ходу — у тех, кто их меняет; у «неизменных» и Pacifica хука нет (коллектор возьмёт интервал вселенной)
    assert {v for v in NEW_13 if hasattr(cl[v], "funding_intervals")} == {"backpack", "variational", "edgex"}


def test_13_09_perps_pair_within_class_unit_and_owner_order():
    perps = {"binance": [_p("BTCUSDT", "BTC"), _p("1000PEPEUSDT", "PEPE", factor=1000.0)],
             "hyperliquid": [_p("xyz:SP500", "SP500", "index"), _p("SPX", "SPX")],
             "bitget": [_p("1000BONKUSDT", "BONK", factor=1000.0), _p("B3USDT", "B3")],
             "lighter": [_p("CXMT", "CXMT", "rwa")],
             "backpack": [_p("BTC_USDC_PERP", "BTC"), _p("kBONK_USDC_PERP", "BONK", factor=1000.0),
                          _p("NVDA.US_USDC_PERP", "NVDA", "equity")],
             "variational": [_p("BTC", "BTC"), _p("1000PEPE", "PEPE", factor=1000.0), _p("B3", "B3", "rwa")],
             "edgex": [_p("BTCUSDC", "BTC"), _p("NVDAUSDC", "NVDA", "equity")],
             "extended": [_p("BTC-USD", "BTC"), _p("SPX500m-USD", "SP500", "index"), _p("SPX-USD", "SPX")],
             "pacifica": [_p("BTC", "BTC"), _p("kBONK", "BONK", factor=1000.0)],
             "apex": [_p("BTCUSDT", "BTC"), _p("CXMTUSDT", "CXMT", "rwa")]}
    ff = universe.build_ff(perps)
    for r in ff:
        assert config.PERP_VENUES.index(r["va"]) < config.PERP_VENUES.index(r["vb"])        # A — раньше у владельца
    assert len([r for r in ff if r["base"] == "BTC"]) == 21                                  # 7 площадок с BTC: C(7,2)
    keys = {r["key"]: r for r in ff}
    bonk = keys["backpack:kBONK_USDC_PERP|pacifica:kBONK"]
    assert (bonk["fa"], bonk["fb"]) == (1000.0, 1000.0)
    assert "bitget:1000BONKUSDT|backpack:kBONK_USDC_PERP" in keys
    assert keys["binance:1000PEPEUSDT|variational:1000PEPE"]["cls"] == "crypto"
    assert keys["hyperliquid:xyz:SP500|extended:SPX500m-USD"]["cls"] == "index"               # индекс — только индексу
    assert "hyperliquid:SPX|extended:SPX-USD" in keys and not any(
        r["base"] == "SP500" and "SPX-USD" in (r["sa"], r["sb"]) for r in ff)                # монета SPX6900 ≠ S&P 500
    assert keys["backpack:NVDA.US_USDC_PERP|edgex:NVDAUSDC"]["cls"] == "equity"
    # «rwa» (не распознан) — пара ни с чем, в том числе с таким же нераспознанным рынком другой биржи; монета B3 ≠ «rwa» B3
    assert not any(r["base"] in ("CXMT", "B3") for r in ff)
    spots = {"gate_spot": [_s("BONK_USDT", "BONK"), _s("PEPE_USDT", "PEPE"), _s("B3_USDT", "B3"), _s("CXMT_USDT", "CXMT")],
             "bitget_spot": [_s("rNVDAUSDT", "NVDA", alt="NVDA")]}
    sf = {r["key"]: r for r in universe.build_sf(perps, spots)}
    assert sf["gate_spot:BONK_USDT|pacifica:kBONK"]["perp_factor"] == 1000.0
    assert sf["gate_spot:PEPE_USDT|variational:1000PEPE"]["perp_factor"] == 1000.0
    assert {k for k in sf if k.startswith("bitget_spot:rNVDAUSDT|")} == {"bitget_spot:rNVDAUSDT|backpack:NVDA.US_USDC_PERP",
                                                                       "bitget_spot:rNVDAUSDT|edgex:NVDAUSDC"}
    assert not any(r["perp_ex"] in ("variational", "apex", "lighter") and r["base"] in ("B3", "CXMT") for r in sf.values())
    # строка пары: комиссия по прайс-листу обеих площадок, ссылка — из инструмента, иначе шаблон (у Extended шаблона нет)
    row = calc.build_ff_row(bonk, {"interval_h": 1, "url": "https://backpack.exchange/trade/kBONK_USD_PERP"},
                            {"interval_h": 1}, {"rate": 0.0001, "interval_h": 1}, {"rate": 0.00002, "interval_h": 1},
                            None, None, {}, {}, 0)
    assert abs(row["fee"] - 2 * (0.0005 + 0.0004)) < 1e-15 and abs(row["spread_h"] - 0.00008) < 1e-15
    assert row["urls"] == ["https://backpack.exchange/trade/kBONK_USD_PERP", "https://app.pacifica.fi/trade/kBONK"]
    assert (row["la"], row["lb"]) == ("backpack", "pacifica")
    it = dict(bonk, key="variational:BTC|extended:BTC-USD", va="variational", sa="BTC", vb="extended", sb="BTC-USD",
              fa=1.0, fb=1.0)
    row = calc.build_ff_row(it, {"interval_h": 8}, {"interval_h": 1, "url": None}, {"rate": 0.0004, "interval_h": 8},
                            {"rate": 0.00001, "interval_h": 1}, None, None, {}, {}, 0)
    # ревью 13.09: у Variational комиссии нет, но издержка — спред котировки RFQ; котировки нет — «—», а не «0 %»
    assert row["fee"] is None and row["fee_q"] is None and row["urls"][1] is None
    assert abs(row["spread_h"] - (0.0004 / 8 - 0.00001)) < 1e-15                             # Variational 8 ч → в час
    row = calc.build_ff_row(it, {"interval_h": 8}, {"interval_h": 1, "url": None},
                            {"rate": 0.0004, "interval_h": 8, "spread_rt": 0.0012}, {"rate": 0.00001, "interval_h": 1},
                            None, None, {}, {}, 0)
    assert abs(row["fee"] - (2 * 0.00025 + 0.0012)) < 1e-15 and row["fee_q"] == 0.0012


def test_a_market_with_a_foreign_price_unit_pairs_with_nothing():
    """Живой срез 13.09: Extended XIAOMI-USD — 26.28 (HKD за акцию), у Bitget/Gate/Aster/Lighter/edgeX — 3.36 USD. Множителем
    не привести (курс плавает): рынок из config.PERP_UNPAIRED не встаёт ни в futures/futures, ни в spot/futures."""
    assert "XIAOMI-USD" in config.PERP_UNPAIRED["extended"]
    perps = {"bitget": [_p("XIAOMIUSDT", "XIAOMI", "equity")], "edgex": [_p("XIAOMIUSDC", "XIAOMI", "equity")],
             "extended": [_p("XIAOMI-USD", "XIAOMI", "equity"), _p("NVDA-USD", "NVDA", "equity")],
             "apex": [_p("NVDAUSDT", "NVDA", "equity")]}
    assert {r["key"] for r in universe.build_ff(perps)} == {"bitget:XIAOMIUSDT|edgex:XIAOMIUSDC",
                                                           "extended:NVDA-USD|apex:NVDAUSDT"}
    spots = {"bitget_spot": [_s("rXIAOMIUSDT", "XIAOMI", alt="XIAOMI")]}
    assert {r["perp_ex"] for r in universe.build_sf(perps, spots)} == {"bitget", "edgex"}


def test_the_six_are_oracle_venues_and_their_declared_names_reach_identity():
    from funding_bot import collector
    assert set(NEW_13) <= identity.ORACLE_PERPS
    inst = {"extended": {"MKR-USD": dict(base="MKR", name_hint="MKR"), "AINU-USD": dict(base="AINU", name_hint="Artificial Inu")},
            "apex": {"1000PEPEUSDT": dict(base="PEPE", name_hint="Pepe"), "CHIPUSDT": dict(base="CHIP", name_hint="USD.AI")},
            "edgex": {"AINUUSDC": dict(base="AINU", name_hint=None)},
            "lighter": {"AI": dict(base="AI", name_hint="Artificial Inu"), "AAVE": dict(base="AAVE", name_hint="Aave")},
            "binance": {"BTCUSDT": dict(base="BTC", name_hint="ignored: not an oracle venue")}}
    pn = collector.perp_names(inst)
    # у новых площадок имя, равное тикеру («MKR», «Pepe» у 1000PEPE), — не имя; у Lighter — как было 12.09 («Aave» остаётся);
    # площадки без оракула сюда не попадают
    assert pn == {"apex": {"CHIPUSDT": "USD.AI"}, "edgex": {}, "extended": {"AINU-USD": "Artificial Inu"},
                  "lighter": {"AI": "Artificial Inu", "AAVE": "Aave"}}
    spots = {"gate_spot": _blob({"AINU_USDT": ["AINU", True]}, {"AINU": _rec("Artificial Inu", "0x" + "a2" * 20)})}
    R = Resolver(spots, {}, perp_names=pn)
    d = decide_sf(R, _sf("gate_spot", "AINU_USDT", "extended", "AINU-USD", "AINU"))
    assert (d["ident"], d["ident_ev"]) == ("same", "name")                                   # заявленное имя совпало
    d = decide_sf(R, _sf("gate_spot", "AINU_USDT", "edgex", "AINUUSDC", "AINU"))
    assert (d["ident"], d["ident_ev"]) == ("unknown", "oracle_only")                          # имени нет — не проверено
    for v, s in (("backpack", "X_USDC_PERP"), ("variational", "X"), ("edgex", "XUSDC"), ("extended", "X-USD"),
                 ("pacifica", "X"), ("apex", "XUSDT")):
        assert R.identity(v, s)["status"] == "oracle_only"                                    # не «no_index»
    d = decide_ff(R, _ff("pacifica", "AINU", "apex", "AINUUSDT", "AINU"))
    assert (d["ident"], d["ident_ev"]) == ("unknown", "oracle_only")                          # и по тикеру — не «тот»


class _OwnHistory(FakeNative):
    """Как Variational: публичной истории нет — клиент отказывает за окно, которое не наблюдал."""
    own_history = True

    def history_since(self, symbol, start_ms, end_ms=None):
        self.calls.append(("history", symbol))
        raise RuntimeError("own ledger did not observe the window")


def test_collector_variational_stale_allowance_and_no_deep_backfill(tmp_path):
    from funding_bot import collector
    from funding_bot.collector import Collector
    now = time.time()
    assert collector._fresh({"obs": now - 100}, now, "variational") and not collector._fresh({"obs": now - 100}, now, "binance")
    assert not collector._fresh({"obs": now - 100}, now) and collector._fresh({"obs": now - 30}, now, "backpack")
    w = make_world()
    va = _OwnHistory("variational", iv_h=8)
    va.markets = {"ABC": dict(base="ABC", rate=0.0004, mark=10.0, bid=9.99, ask=10.01,
                              url="https://omni.variational.io/perpetual/ABC")}
    ex = FakeNative("extended", iv_h=1)
    ex.markets = {"ABC-USD": dict(base="ABC", rate=0.00002, mark=10.0, bid=9.99, ask=10.01, url=None)}
    ex.history = {"ABC-USD": grid(int(now * 1000), 1, 0.00002)}
    w.update(variational=va, extended=ex)
    t = [now]
    col = Collector(clients=w, db_path=tmp_path / "c.db", table_path=tmp_path / "t.json", now=lambda: t[0],
                    sleep=lambda s: None, background=False)
    tbl = col.once()
    assert col.venues == ["aster", "binance", "hyperliquid", "variational", "extended"]
    ff = {r["key"]: r for r in tbl["ff_rows"]}
    k = "variational:ABC|extended:ABC-USD"
    assert abs(ff[k]["spread_h"] - (0.0004 / 8 - 0.00002)) < 1e-15
    # ревью 13.09: Variational — комиссия 0, издержка — полный спред котировки (9.99 / 10.01 → 0.2 %) + тейкер Extended ×2
    assert abs(ff[k]["fee"] - (2 * 0.00025 + 0.02 / 10.0)) < 1e-12 and abs(ff[k]["fee_q"] - 0.002) < 1e-12
    assert "fee_q" not in ff["aster:ABCUSDT|binance:ABCUSDT"]            # у прочих строк ключа нет (размер table.json)
    assert ff[k]["urls"] == ["https://omni.variational.io/perpetual/ABC", None]
    assert not any(c[0] == "history" for c in va.calls)                  # глубину с пола у Variational не просят вовсе
    assert ("extended", "ABC-USD") in set(db.leg_depths(col.con))        # у остальных новых — как у всех
    assert tbl["health"]["variational"]["stale_s"] == config.STALE_S_BY_VENUE["variational"]
    assert "stale_s" not in tbl["health"]["extended"]
    col.start_backfill()                                                 # разовый бэкфилл (CLI) — тоже без Variational
    assert not any(c[0] == "history" for c in va.calls)
    html = dashboard.render(tbl)
    assert "Variational" in html and "Extended" in html
