from funding_bot import exchanges, universe, config, venues
from fakes import make_world

FF_KEYS = {"aster:ABCUSDT|binance:ABCUSDT", "aster:MEMEUSDT|binance:MEMEUSDT", "aster:1000PEPEUSDT|binance:1000PEPEUSDT",
           "aster:XYZUSDT|binance:XYZUSDT", "aster:NATGASUSDT|binance:NATGASUSDT", "aster:GPROUSD1|binance:GPROUSDT",
           "aster:ABCUSDT|hyperliquid:ABC", "aster:1000PEPEUSDT|hyperliquid:kPEPE", "aster:NATGASUSDT|hyperliquid:xyz:NATGAS",
           "binance:ABCUSDT|hyperliquid:ABC", "binance:BONLYUSDT|hyperliquid:BONLY", "binance:1000PEPEUSDT|hyperliquid:kPEPE",
           "binance:NATGASUSDT|hyperliquid:xyz:NATGAS"}
BN = "binance_spot:"
SF_KEYS = {BN + "ABCUSDT|aster:ABCUSDT", BN + "MEMEUSDT|aster:MEMEUSDT", BN + "PEPEUSDT|aster:1000PEPEUSDT",
           BN + "AIUSDT|aster:AIUSDT", BN + "ABCUSDT|binance:ABCUSDT", BN + "MEMEUSDT|binance:MEMEUSDT",
           BN + "PEPEUSDT|binance:1000PEPEUSDT", BN + "BONLYUSDT|binance:BONLYUSDT", BN + "ABCUSDT|hyperliquid:ABC",
           BN + "BONLYUSDT|hyperliquid:BONLY", BN + "PEPEUSDT|hyperliquid:kPEPE",
           # 12.09: спот Gate — ABC со всеми тремя перпами, XYZ (спота Binance нет) с Aster и Binance
           "gate_spot:ABC_USDT|aster:ABCUSDT", "gate_spot:ABC_USDT|binance:ABCUSDT", "gate_spot:ABC_USDT|hyperliquid:ABC",
           "gate_spot:XYZ_USDT|aster:XYZUSDT", "gate_spot:XYZ_USDT|binance:XYZUSDT"}
AI_SF, MEME_SF = BN + "AIUSDT|aster:AIUSDT", BN + "MEMEUSDT|aster:MEMEUSDT"
MEME_FF = "aster:MEMEUSDT|binance:MEMEUSDT"


def _perps(w, vs=config.PERP_VENUES):
    return {v: venues.perp_instruments(w[v]) for v in vs if v in w}


def _spot(w):
    return {v: venues.spot_instruments(w[v]) for v in config.SPOT_VENUES if v in w}


def test_instruments_per_venue_tradfi_quotes_hip3():
    w = make_world(); p = _perps(w)
    assert {i["symbol"] for i in p["aster"]} == {"ABCUSDT", "XYZUSDT", "MEMEUSDT", "ONLYAUSDT", "1000PEPEUSDT", "AIUSDT",
                                                 "NATGASUSDT", "GPROUSD1"}                    # USD1 взят: USDT-двойника нет
    assert {i["symbol"] for i in p["binance"]} == {"ABCUSDT", "XYZUSDT", "MEMEUSDT", "1000PEPEUSDT", "BONLYUSDT",
                                                   "NATGASUSDT", "GPROUSDT"}                  # TradFi взят, ABCUSDC — дубль
    raw = {i["symbol"] for i in exchanges.perp_instruments(w["binance"])}
    assert "ABCUSDC" in raw and "ETHBTC" not in raw and "ABCUSDT_260101" not in raw           # BTC-квота и квартальный — нет
    hl = {i["symbol"]: i for i in p["hyperliquid"]}
    assert set(hl) == {"ABC", "kPEPE", "BONLY", "xyz:NATGAS"}                                 # делистнутая отброшена
    assert hl["xyz:NATGAS"]["base"] == "NATGAS" and hl["xyz:NATGAS"]["contract"] == "xyz"
    assert hl["kPEPE"]["base"] == "PEPE" and hl["kPEPE"]["factor"] == 1000.0 and all(i["interval_h"] == 1 for i in hl.values())
    bn = {i["symbol"]: i for i in p["binance"]}
    assert (bn["NATGASUSDT"]["cls"], bn["GPROUSDT"]["cls"], bn["ABCUSDT"]["cls"]) == ("commodity", "equity", "crypto")
    assert hl["xyz:NATGAS"]["cls"] == "commodity" and hl["ABC"]["cls"] == "crypto"
    ac = lambda **kw: exchanges.asset_class({"contractType": "PERPETUAL", "underlyingType": "COIN", **kw})
    assert ac(underlyingSubType=["STOCK", "Semiconductor"]) == "equity"                            # Aster: акция
    assert ac(underlyingSubType=["Commodities"], baseAsset="PAXG") == "crypto"                     # токен золота — монета
    assert ac(underlyingSubType=["pre-launch", "STOCK"]) == "preipo" and ac(underlyingType="INDEX") == "index"
    assert ac(underlyingSubType=["Meme"]) == "crypto"
    assert exchanges.asset_class(dict(contractType="TRADIFI_PERPETUAL", underlyingType="PREMARKET")) == "preipo"
    assert next(i for i in p["aster"] if i["symbol"] == "XYZUSDT")["interval_h"] == 1         # из fundingInfo


def test_keep_best_quote_one_market_per_coin_per_venue():
    mk = lambda s, b, q, f=1.0: dict(symbol=s, base=b, quote=q, factor=f)
    ins = [mk("ABCUSDC", "ABC", "USDC"), mk("ABCUSDT", "ABC", "USDT"), mk("XU", "X", "U"), mk("XUSD1", "X", "USD1"),
           mk("PEPEUSDT", "PEPE", "USDT"), mk("1000PEPEUSDT", "PEPE", "USDT", 1000.0)]
    kept = {i["symbol"] for i in exchanges.keep_best_quote(ins)}
    assert kept == {"ABCUSDT", "XUSD1", "PEPEUSDT", "1000PEPEUSDT"}     # множитель — другой рынок, не дубль
    # живой прогон 12.09: Quant QNTUSDT (монета) и Quantinuum QNTXUSDT (акция, канон QNT) — не дубли по квоте
    two = [dict(symbol="QNTUSDT", base="QNT", quote="USDT", factor=1.0, cls="crypto"),
           dict(symbol="QNTXUSDT", base="QNT", quote="USDT", factor=1.0, cls="equity")]
    assert {i["symbol"] for i in exchanges.keep_best_quote(two)} == {"QNTUSDT", "QNTXUSDT"}


def test_build_ff_every_market_pair_in_owner_order():
    ff = universe.build_ff(_perps(make_world()))
    assert {r["key"] for r in ff} == FF_KEYS
    for r in ff:
        assert config.PERP_VENUES.index(r["va"]) < config.PERP_VENUES.index(r["vb"])      # A — раньше в списке владельца
    ng = next(r for r in ff if r["key"] == "binance:NATGASUSDT|hyperliquid:xyz:NATGAS")
    assert (ng["sa"], ng["sb"], ng["base"]) == ("NATGASUSDT", "xyz:NATGAS", "NATGAS")
    ff2 = universe.build_ff(_perps(make_world(), ("aster", "binance")))                   # третьей площадки нет — её пар нет
    assert {r["key"] for r in ff2} == {k for k in FF_KEYS if "hyperliquid" not in k}


def test_several_markets_of_one_coin_give_every_combination():
    """HIP-3: xyz:NET и para:NET — разные перпы; раньше из них брался один (первый по алфавиту)."""
    a = [dict(symbol="NETUSDT", base="NET", factor=1.0)]
    h = [dict(symbol="xyz:NET", base="NET", factor=1.0), dict(symbol="para:NET", base="NET", factor=1.0)]
    ff = universe.build_ff({"binance": a, "hyperliquid": h})
    assert [r["key"] for r in ff] == ["binance:NETUSDT|hyperliquid:para:NET", "binance:NETUSDT|hyperliquid:xyz:NET"]


def test_build_sf_rows_are_deals_not_coins():
    w = make_world()
    sf = universe.build_sf(_perps(w), _spot(w))
    keys = [r["key"] for r in sf]
    assert set(keys) == SF_KEYS and len(keys) == len(set(keys))      # ABC — шесть сделок: 2 спота × 3 перпа
    hp = next(r for r in sf if r["key"] == BN + "PEPEUSDT|hyperliquid:kPEPE")
    assert hp["perp_factor"] == 1000.0 and hp["spot_factor"] == 1.0 and hp["spot"] == "PEPEUSDT" and hp["base"] == "PEPE"
    assert hp["legacy_key"] == "hyperliquid:kPEPE" and hp["spot_ex"] == "binance_spot"
    gx = next(r for r in sf if r["key"] == "gate_spot:XYZ_USDT|aster:XYZUSDT")
    assert gx["spot_asset"] == "XYZ" and "legacy_key" not in gx
    assert not any(r["perp"] in ("ONLYAUSDT", "NATGASUSDT", "xyz:NATGAS") for r in sf)   # спота нет — сделки нет
    abc = [r["key"] for r in sf if r["base"] == "ABC"]
    assert abc[:3] == [BN + "ABCUSDT|aster:ABCUSDT", BN + "ABCUSDT|binance:ABCUSDT", BN + "ABCUSDT|hyperliquid:ABC"]  # споты в порядке владельца


def test_whole_line_stock_tokens_aliases_and_classes():
    """Сверка с чужим скринером 12.09: у CRCL там 16 спредов (токенизированные акции), у SPORTFUN 6 (спот FUN) — у нас
    было 0. Пары — только внутри класса актива; синоним — только на тех биржах, где он верен."""
    sp = lambda sym, base, alt=None, ba=None: dict(symbol=sym, base=("~" + (ba or base).upper()) if alt else base,
                                                   base_asset=ba or base, factor=1.0, alt_base=alt)
    perps = {"binance": [dict(symbol="CRCLUSDT", base="CRCL", factor=1.0, cls="equity"),
                         dict(symbol="SPORTFUNUSDT", base="SPORTFUN", factor=1.0, cls="crypto"),
                         dict(symbol="QNTUSDT", base="QNT", factor=1.0, cls="crypto"),             # Quant — монета
                         dict(symbol="QNTXUSDT", base="QNT", factor=1.0, cls="equity"),            # Quantinuum (канон QNTX → QNT)
                         dict(symbol="CLUSDT", base="CL", factor=1.0, cls="commodity"),            # нефть
                         dict(symbol="XAUUSDT", base="XAU", factor=1.0, cls="commodity"),
                         dict(symbol="AIGENSYNUSDT", base="AIGENSYN", factor=1.0, cls="crypto"),
                         dict(symbol="ALLUSDT", base="ALL", factor=1.0, cls="index")],
             "hyperliquid": [dict(symbol="xyz:QNT", base="QNT", factor=1.0, cls="equity"),
                             dict(symbol="xyz:EUR", base="EUR", factor=1.0, cls="fx")],
             "aster": [dict(symbol="ABCUSDT", base="ABC", factor=1.0, cls="crypto")]}
    spots = {"gate_spot": [sp("CRCLX_USDT", "CRCLX", "CRCL"), sp("CRCLON_USDT", "CRCLON", "CRCL"), sp("FUN_USDT", "FUN"),
                           sp("QNT_USDT", "QNT"), sp("QNTG_USDT", "QNTG", "QNT"), sp("AI_USDT", "AI"), sp("PAXG_USDT", "PAXG"),
                           sp("TWT_USDT", "TWT"), sp("ABC_USDT", "ABC")],
             "bitget_spot": [sp("RCRCLUSDT", "RCRCL", "CRCL", "rCRCL"), sp("RCLUSDT", "RCL", "CL", "rCL"), sp("AIUSDT", "AI"),
                             sp("EURUSDT", "EUR")]}
    sf = {r["key"]: r for r in universe.build_sf(perps, spots)}
    assert set(sf) == {"gate_spot:CRCLX_USDT|binance:CRCLUSDT", "gate_spot:CRCLON_USDT|binance:CRCLUSDT",
                       "bitget_spot:RCRCLUSDT|binance:CRCLUSDT", "gate_spot:FUN_USDT|binance:SPORTFUNUSDT",
                       "gate_spot:QNT_USDT|binance:QNTUSDT",                       # монета Quant — со спотом Quant
                       "gate_spot:QNTG_USDT|binance:QNTXUSDT", "gate_spot:QNTG_USDT|hyperliquid:xyz:QNT",   # акция — с акцией
                       "gate_spot:PAXG_USDT|binance:XAUUSDT",                       # золото ← PAXG
                       "bitget_spot:AIUSDT|binance:AIGENSYNUSDT",                   # Gensyn = AI только на KuCoin/Bitget
                       "bitget_spot:EURUSDT|hyperliquid:xyz:EUR",                   # валюта ← спот той же валюты
                       "gate_spot:ABC_USDT|aster:ABCUSDT"}
    # не пары: rCL (Colgate) — не нефть; AI на Gate — Sleepless AI; индекс ALL — не монета TWT
    assert sf["bitget_spot:RCRCLUSDT|binance:CRCLUSDT"]["spot_tag"] == "rCRCL" and sf["gate_spot:FUN_USDT|binance:SPORTFUNUSDT"]["spot_tag"] == "FUN"
    assert sf["gate_spot:QNTG_USDT|hyperliquid:xyz:QNT"]["cls"] == "equity" and sf["gate_spot:QNT_USDT|binance:QNTUSDT"]["cls"] == "crypto"
    from funding_bot import calc
    row = calc.build_sf_row(sf["gate_spot:CRCLX_USDT|binance:CRCLUSDT"], {"interval_h": 8}, None, None, None, {}, 0)
    assert row["spot_label"] == "gate·CRCLX" and row["urls"]["spot"].endswith("/trade/CRCLX_USDT") and row["cls"] == "equity"
    # futures/futures: Quant (монета) и Quantinuum (акция) — не пара; Quantinuum Binance (QNTX) — пара с xyz:QNT
    ff = {r["key"] for r in universe.build_ff(perps)}
    assert "binance:QNTXUSDT|hyperliquid:xyz:QNT" in ff and "binance:QNTUSDT|hyperliquid:xyz:QNT" not in ff


def test_million_multiplier_perp_pairs_with_plain_spot():
    """Проверка исправлений 12.09: перп Binance 1MBABYDOGE не находил спот BABYDOGE на Gate/KuCoin/Bitget."""
    from funding_bot.symbols import norm_symbol_factor
    b, f = norm_symbol_factor("1MBABYDOGE")
    perp = [dict(symbol="1MBABYDOGEUSDT", base=b, factor=f)]
    spot = [dict(symbol="BABYDOGE_USDT", base="BABYDOGE", base_asset="BABYDOGE", factor=1.0)]
    sf = universe.build_sf({"binance": perp}, {"gate_spot": spot})
    assert [(r["key"], r["perp_factor"], r["spot_factor"]) for r in sf] == \
        [("gate_spot:BABYDOGE_USDT|binance:1MBABYDOGEUSDT", 1_000_000.0, 1.0)]
