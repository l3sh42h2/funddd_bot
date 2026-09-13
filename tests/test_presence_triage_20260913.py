"""Разбор ошибок присутствия тестировщика 13.09 (прогон 01:02–01:13 UTC): рынок торгуется, пары есть, а на дашборде нет.

1. gate KR200_USDT — KOSPI 200 в долларах (0.80 против 1100 пунктов у HL / Bitget): исключение — config.PERP_UNPAIRED,
   одно для вселенной и тестировщика (раньше — своя база «KR200USD» в gate_fut.CANON, которую тестировщик не видел).
2. bitget B200USDT / gate B200_USDT (и H100): индексы аренды GPU Silicon Data в USD за GPU-час на обеих биржах, обе —
   pre-market. Gate ставил любой pre-market в «preipo», Bitget — в «index»: пары внутри класса не строились. Теперь
   pre-market индекса — «index» у коллектора и у истины Gate; истина Bitget знает B200 / H100 и индексы HSI / JP225 / KR200.
3. variational B3 — «rwa» по сверке с мёртвым Binance B3USDT (SETTLING); живой марк сходится с Aster / Bybit / Gate — монета.
Сети нет."""
from __future__ import annotations
from funding_bot import audit, config, gate_fut, bitget_fut, identity, universe, variational
from funding_bot.audit_truth import gate as gt_mod, bitget as bg_mod


def P(sym, base, cls="crypto", f=1.0):
    return dict(symbol=sym, base=base, cls=cls, factor=f)


def S(sym, base):
    return dict(symbol=sym, base=base, factor=1.0)


def M(base, cls="crypto", iv=8, tradable=True):
    return dict(base=base, tradable=tradable, cls=cls, interval_h=iv, name=None, note=None)


def _ffrow(va, sa, vb, sb, base, cls):
    w = {k: dict(a=None, na=0, b=None, nb=0, spread=None, incomplete=False) for k in ("24", "72", "168", "720")}
    return dict(key=f"{va}:{sa}|{vb}:{sb}", base=base, cls=cls, va=va, vb=vb, sa=sa, sb=sb, rate_h_a=0.0, rate_h_b=0.0,
                iv_a=8, iv_b=8, next_a=None, next_b=None, windows=w, stale=False, mismatch=False)


# --- 1. Gate KR200 ------------------------------------------------------------------------------------------------------
def test_gate_kr200_excluded_once_for_universe_and_tester():
    assert "KR200_USDT" in config.PERP_UNPAIRED["gate"] and "KRW" in config.PERP_UNPAIRED["gate"]["KR200_USDT"]
    assert gate_fut.perp_base("KR200", "index") == ("KR200", 1.0)          # база — тикер биржи, без «KR200USD»
    perps = {"hyperliquid": [P("xyz:KR200", "KR200", "index")], "bitget": [P("KR200USDT", "KR200", "index")],
             "gate": [P("KR200_USDT", "KR200", "index")]}
    ff = universe.build_ff(perps)
    assert {r["key"] for r in ff} == {"hyperliquid:xyz:KR200|bitget:KR200USDT"}   # пары в пунктах остаются
    assert not any("KR200_USDT" in r["key"] for r in universe.build_sf(perps, {"gate_spot": [S("KR200_USDT", "KR200")]}))
    uni = {("hyperliquid", "xyz:KR200"): dict(base="KR200", cls=None), ("bitget", "KR200USDT"): dict(base="KR200", cls=None),
           ("gate", "KR200_USDT"): dict(base="KR200", cls=None)}
    rows, _ = audit.presence("gate", {"KR200_USDT": M("KR200", "index")}, {}, uni)
    assert rows[0]["status"] == "ok_unpaired" and "KRW" in rows[0]["why"]     # было «ОШИБКА: пары hyperliquid:xyz:KR200»
    # и парой другим он не считается: у HL без строки на дашборде пара — только Bitget
    rows, _ = audit.presence("hyperliquid", {"xyz:KR200": M("KR200", "index", 1)}, {}, uni)
    assert rows[0]["status"] in ("missing", "check") and "gate" not in rows[0]["why"] and "bitget:KR200USDT" in rows[0]["why"]


def test_bitget_truth_knows_point_indices():
    """Истина Bitget: HSI / JP225 / KR200 у биржи «stock», а по составу индекса — пункты индекса (13.09 тестировщик писал
    «bitget:KR200USDT — другой актив: класс «equity» против «index»»)."""
    assert [bg_mod.asset_class(b, "stock", "YES") for b in ("KR200", "HSI", "JP225", "SP500", "NVDA", "OPENAI")] == \
        ["index", "index", "index", "index", "equity", "preipo"]


# --- 2. B200 / H100 -------------------------------------------------------------------------------------------------------
def test_pre_market_gpu_indices_are_index_on_both_sides():
    assert gate_fut.asset_class("B200", "indices", True) == gate_fut.asset_class("H100", "indices", True) == "index"
    assert gate_fut.asset_class("BP", "", True) == gate_fut.asset_class("OPENAI", "stocks", True) == "preipo"
    assert bitget_fut.asset_class("B200", "crypto", "YES") == bitget_fut.asset_class("H100", "crypto", "YES") == "index"
    # истины тестировщика — те же классы, своим разбором
    assert gt_mod.asset_class("B200", "indices", True) == gt_mod.asset_class("H100", "indices", True) == "index"
    assert gt_mod.asset_class("BP", "", True) == gt_mod.asset_class("KALSHI", "stocks", True) == "preipo"
    assert bg_mod.asset_class("B200", "crypto", "YES") == bg_mod.asset_class("H100", "crypto", "YES") == "index"
    assert bg_mod.asset_class("EURUSD", "crypto", "YES") is None                  # прочие crypto + RWA — неизвестен, как было


def test_gpu_index_pairs_built_and_same_by_class():
    perps = {"bitget": [P("B200USDT", "B200", "index"), P("H100USDT", "H100", "index")],
             "gate": [P("B200_USDT", "B200", "index"), P("H100_USDT", "H100", "index"), P("OPENAI_USDT", "OPENAI", "preipo")],
             "lighter": [P("H100", "H100", "index"), P("OPENAI", "OPENAI", "preipo")]}
    ff = universe.build_ff(perps)
    keys = {r["key"] for r in ff}
    assert {"bitget:B200USDT|gate:B200_USDT", "bitget:H100USDT|gate:H100_USDT", "gate:H100_USDT|lighter:H100",
            "bitget:H100USDT|lighter:H100"} <= keys
    R = identity.Resolver({}, {})
    for r in ff:
        if r["cls"] == "index":
            d = identity.decide_ff(R, r)
            assert (d["ident"], d["ident_ev"]) == ("same", "class_ticker"), r["key"]
    # тестировщик: Bitget B200 с Gate B200 на дашборде — «в дашборде»; без строки — «нет на дашборде», а не «другой актив»
    uni = {("gate", "B200_USDT"): dict(base="B200", cls=None)}
    mk = {"gate": {"B200_USDT": M("B200", "index")}}
    legs = audit.dashboard_legs(dict(ff_rows=[_ffrow("bitget", "B200USDT", "gate", "B200_USDT", "B200", "index")]))
    rows, _ = audit.presence("bitget", {"B200USDT": M("B200", "index")}, legs, uni)
    assert rows[0]["status"] == "present"
    rows, _ = audit.presence("bitget", {"B200USDT": M("B200", "index")}, {}, uni)
    audit.confirm_partners(rows, mk.get)
    assert rows[0]["status"] == "missing" and rows[0]["partners"] == [("gate", "B200_USDT")]


# --- 3. Variational B3 ----------------------------------------------------------------------------------------------------
def test_variational_b3_is_the_base_coin_and_pairs():
    assert "B3" not in variational.UNVERIFIED
    assert variational.asset_class("B3", "B3 (Base)", 3600) == "crypto"
    assert variational.asset_class("B3", "B3 (Base)", 3600, n_zero=1) == "rwa"   # перекрёстная улика по ставке — как была
    perps = {"aster": [P("B3USDT", "B3")], "variational": [P("B3", "B3")]}
    assert "aster:B3USDT|variational:B3" in {r["key"] for r in universe.build_ff(perps)}
    sf = universe.build_sf(perps, {"gate_spot": [S("B3_USDT", "B3")]})
    assert "gate_spot:B3_USDT|variational:B3" in {r["key"] for r in sf}
    # «тот ли актив»: у Variational только имя «B3 (Base)»; индекс Aster берёт Gate B3_USDT — монета «B3 Base» на Base
    coin = identity.coin_record("B3 Base", [("BASEEVM", "0xb3b32f9f8827d4634fe7d973fa1034ec9fddb3b3", True, True)], "B3")
    spots = {"gate_spot": {"coins": {"B3": coin}, "markets": {"B3_USDT": ["B3", True]}}}
    legs = {"aster": {"B3USDT": {"legs": [{"exchange": "gateio", "symbol": "B3_USDT", "weight": "0.13333333"},
                                          {"exchange": "coinbase", "symbol": "B3-USD*uindex(USDUSDT)", "weight": "0.4"}],
                                 "dex": {}}}}
    R = identity.Resolver(spots, legs, perp_names={"variational": {"B3": "B3 (Base)"}})
    d = identity.decide_ff(R, dict(va="aster", sa="B3USDT", vb="variational", sb="B3", cls="crypto"))
    assert d["ident"] == "same", d
    d = identity.decide_sf(R, dict(spot_ex="gate_spot", spot="B3_USDT", perp_ex="variational", perp="B3", cls="crypto",
                                   perp_factor=1.0, spot_factor=1.0))
    assert d["ident"] == "same", d
