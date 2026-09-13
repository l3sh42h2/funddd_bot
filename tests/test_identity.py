"""«Не тот актив» по составу индекса и контрактам, не по цене (владелец 12.09: «надо отмечать их по контрактам где это
возможно, либо по другим признакам, но не курс»). Случаи — из живого среза 12.09 (исследование со скептиком)."""
import pytest
from funding_bot import identity, identity_src, calc, config
from funding_bot.client import PermanentHTTPError
from funding_bot.identity import coin_record, Resolver, decide_sf, decide_ff

L = lambda ex, sym, w=1.0: {"exchange": ex, "symbol": sym, "weight": str(w)}
E = lambda *legs, dex=None: {"legs": list(legs), "dex": dex or {}}


def rec(name, *addrs, chain="ETH", dep=True, wd=True, code=""):
    return coin_record(name, [(chain, a, dep, wd) for a in (addrs or ("",))], code)


def blob(markets, coins, **kw):
    return dict(markets={s: list(m) for s, m in markets.items()}, coins=coins, **kw)


def sf(sv, spot, pv, perp, pf=1.0, spf=1.0, cls="crypto", **kw):
    return dict(key=f"{sv}:{spot}|{pv}:{perp}", cls=cls, spot_ex=sv, spot=spot, perp_ex=pv, perp=perp, perp_factor=pf,
                spot_factor=spf, **kw)


def ff(va, sa, vb, sb, cls="crypto"):
    return dict(key=f"{va}:{sa}|{vb}:{sb}", cls=cls, va=va, sa=sa, vb=vb, sb=sb, fa=1.0, fb=1.0)


def test_ids_names_and_coin_records():
    assert identity.norm_id("0xF39E4B21C84E737DF08E2C3B32541D856F508E48") == "0xf39e4b21c84e737df08e2c3b32541d856f508e48"   # EIP-55
    assert identity.norm_id("invalid-CAT-8700683-380") is None and identity.norm_id("0x" + "0" * 40) is None
    assert identity.norm_id("BNB") is None and identity.norm_id("eosio.token") is None and identity.norm_id("12345") is None
    assert identity.norm_id("0xb6af" + "1" * 36 + "_bak") == "0xb6af" + "1" * 36                            # Bitget: списанный
    assert identity.norm_id("VELO_GDM4RQUQQUVSKQA7S6EM7XBZP3FCGH4Q7CL6TABQ7B2BEJ5ERARM2M5M") == "GDM4RQUQQUVSKQA7S6EM7XBZP3FCGH4Q7CL6TABQ7B2BEJ5ERARM2M5M"
    assert identity.norm_id("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v") == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"   # регистр значим
    assert identity.name_eq("Yooldo", "Yooldo Games") and identity.name_eq("OntologyGas", "Ontology Gas")
    assert not identity.name_eq("Memecoin", "A Meme Coin") and not identity.name_eq("Sleepless AI", "Artificial Inu")
    one = coin_record("Harmony", [("ONE", "", False, False)], "ONE")
    assert one["nat"] == ["one"] and one["dep_closed"] and one["wd_closed"]
    assert coin_record("X", [("ETH", "", True, True)], "X")["nat"] == []        # пустой адрес на Ethereum — пропуск, не родная
    assert coin_record("Bitcoin", [("BTC", "", True, True)], "BTC")["nat"] == ["btc"]


def test_edge_index_uses_other_market_of_same_venue():
    """EDGE: индекс перпа берёт Gate EDGEX (edgeX); Gate EDGE — другая монета (Definitive), KuCoin EDGE — edgeX."""
    spots = {"gate_spot": blob({"EDGE_USDT": ["EDGE", True], "EDGEX_USDT": ["EDGEX", True]},
                               {"EDGE": rec("Definitive", "0x" + "ed" * 20, chain="BASEEVM"), "EDGEX": rec("edgeX", "0x" + "b0" * 20)}),
             "kucoin_spot": blob({"EDGE-USDT": ["EDGE", True]}, {"EDGE": rec("edgeX", "0x" + "b0" * 20)})}
    R = Resolver(spots, {"binance": {"EDGEUSDT": E(L("gateio", "EDGEX_USDT"), L("kucoin", "EDGE-USDT"))}})
    g = decide_sf(R, sf("gate_spot", "EDGE_USDT", "binance", "EDGEUSDT"))
    assert (g["ident"], g["ident_ev"]) == ("other", "index_other_market") and "EDGEX" in g["ident_why"]
    assert decide_sf(R, sf("gate_spot", "EDGEX_USDT", "binance", "EDGEUSDT"))["ident_ev"] == "index_market"   # синоним
    assert decide_sf(R, sf("kucoin_spot", "EDGE-USDT", "binance", "EDGEUSDT"))["ident"] == "same"


def test_absence_from_index_is_never_other():
    """ONG есть в индексе через Gate, KuCoin в индексе нет — монета та же (имя); у Bitget ни имени, ни контракта — не «не тот»."""
    spots = {"gate_spot": blob({"ONG_USDT": ["ONG", True]}, {"ONG": coin_record("OntologyGas", [("ONT", "", True, True)], "ONG")}),
             "kucoin_spot": blob({"ONG-USDT": ["ONG", True]},
                                 {"ONG": coin_record("Ontology Gas", [("ont", "02" + "0" * 38, False, False)], "ONG")}),
             "bitget_spot": blob({"ONGUSDT": ["ONG", True]}, {"ONG": coin_record(None, [], "ONG")})}
    R = Resolver(spots, {"binance": {"ONGUSDT": E(L("gateio", "ONG_USDT"))}})
    k = sf("kucoin_spot", "ONG-USDT", "binance", "ONGUSDT")
    assert decide_sf(R, k)["ident_ev"] == "name" and identity.transfers(R, k) == "ввод и вывод закрыты"
    b = decide_sf(R, sf("bitget_spot", "ONGUSDT", "binance", "ONGUSDT"))
    assert b["ident"] == "unknown" and b["ident_ev"] == "spot_blank"


def test_contract_conflict_and_bridged_copy():
    """UP: индекс Aster — Unitas (Gate), KuCoin UP — Superform: «не тот». FLOW: контракты разные, имя то же — мост."""
    spots = {"gate_spot": blob({"UP_USDT": ["UP", True], "FLOW_USDT": ["FLOW", True]},
                               {"UP": rec("Unitas", "0x000008d2175f9aeaddb2430c26f8a6f73c5a0000"), "FLOW": rec("Flow", "0x" + "f2" * 20)}),
             "kucoin_spot": blob({"UP-USDT": ["UP", True], "FLOW-USDT": ["FLOW", True]},
                                 {"UP": rec("Superform", "0x5b2193fdc451c1f847be09ca9d13a4bf60f8c86b"), "FLOW": rec("Flow", "0x" + "f1" * 20)})}
    R = Resolver(spots, {"aster": {"UPUSDT": E(L("gateio", "UP_USDT")), "FLOWUSDT": E(L("gateio", "FLOW_USDT"))}})
    up = decide_sf(R, sf("kucoin_spot", "UP-USDT", "aster", "UPUSDT"))
    assert (up["ident"], up["ident_ev"]) == ("other", "contract_conflict") and "Superform" in up["ident_why"]
    assert decide_sf(R, sf("kucoin_spot", "FLOW-USDT", "aster", "FLOWUSDT"))["ident_ev"] == "name_bridged"


def _dex_world(alpha, liq):
    pool = {"chain": "robinhood", "dex": "uniswap", "labels": ["v4"], "b": ["AI", "0x" + "2e" * 20, "Artificial Inu"],
            "q": ["USDG", "0x" + "5f" * 20, "Global Dollar"], "liq": liq}
    spots = {"binance_spot": blob({"AIUSDT": ["AI", True]}, {"AI": rec("Sleepless AI", "0x" + "bd" * 20)}, alpha=alpha)}
    return Resolver(spots, {"aster": {"AIUSDT": E(L("uniswap_v4", "AI-USDG"), dex={"AI USDG": [pool]})}})


def test_dex_only_index_resolved_only_when_unambiguous():
    """Aster AI — пул Uniswap токена сети Robinhood (Artificial Inu), спот AI — Sleepless AI. Поиск пула по символу —
    ловушка тикеров: берётся, только если единственный токен Alpha с этим символом совпал и пул доминирует."""
    one = [{"s": "AI", "n": "Artificial Inu", "a": "0x" + "2e" * 20, "c": "4663", "id": "A1"}]
    row = sf("binance_spot", "AIUSDT", "aster", "AIUSDT")
    d = decide_sf(_dex_world(one, 3_000_000), row)
    assert (d["ident"], d["ident_ev"]) == ("other", "contract_conflict") and "Artificial Inu" in d["ident_why"]
    two = one + [{"s": "AI", "n": "Other AI", "a": "0x" + "33" * 20, "c": "56", "id": "A2"}]
    assert decide_sf(_dex_world(two, 3_000_000), row)["ident_ev"] == "dex_unresolved"
    assert decide_sf(_dex_world(one, 10_000), row)["ident_ev"] == "dex_unresolved"      # пул тоньше $25k — не свидетельство


def test_stale_leg_is_not_evidence():
    """Первый прогон 12.09: нога на неторгуемый рынок Binance (FUNUSDT, BREAK — старая FunFair) дала ложный «не тот»
    спотам Sport.fun. Рынок без торгов — не свидетельство."""
    spots = {"binance_spot": blob({"FUNUSDT": ["FUN", False]}, {"FUN": rec("FunFair", "0x" + "fa" * 20)}),
             "gate_spot": blob({"FUN_USDT": ["FUN", True]}, {"FUN": rec("Sport.fun", "0x" + "5f" * 20)})}
    R = Resolver(spots, {"aster": {"FUNUSDT": E(L("binance", "FUNUSDT"))}})
    d = decide_sf(R, sf("gate_spot", "FUN_USDT", "aster", "FUNUSDT"))
    assert (d["ident"], d["ident_ev"]) == ("unknown", "stale_only")


def test_hl_oracle_market_and_unit_check():
    spots = {"binance_spot": blob({"PEPEUSDT": ["PEPE", True]}, {"PEPE": rec("Pepe", "0x" + "69" * 20)})}
    R = Resolver(spots, {"binance": {"1000PEPEUSDT": E(L("binance", "PEPEUSDT*1000"))}})
    assert decide_sf(R, sf("binance_spot", "PEPEUSDT", "hyperliquid", "kPEPE", pf=1000))["ident_ev"] == "hl_oracle_market"
    assert decide_sf(R, sf("binance_spot", "PEPEUSDT", "binance", "1000PEPEUSDT", pf=1000))["ident_ev"] == "index_market"
    bad = decide_sf(R, sf("binance_spot", "PEPEUSDT", "binance", "1000PEPEUSDT", pf=1))          # наш множитель неверен
    assert (bad["ident"], bad["ident_ev"]) == ("unknown", "unit")


def test_same_contract_with_closed_deposits_is_same_asset():
    """ESPORTS 12.09: цена Gate на 30 % ниже перпа, флаг по цене считал монету чужой. Контракт тот же (Alpha Yooldo), ввод
    на Gate закрыт — это отдельная пометка ⊘, а не «не тот актив»."""
    spots = {"binance_spot": blob({"BTCUSDT": ["BTC", True]}, {},
                                  alpha=[{"s": "ESPORTS", "n": "Yooldo", "a": "0x" + "f3" * 20, "c": "56", "id": "A9"}]),
             "gate_spot": blob({"ESPORTS_USDT": ["ESPORTS", True]}, {"ESPORTS": rec("Yooldo", "0x" + "F3" * 20, dep=False)})}
    legs = {"binance": {"ESPORTSUSDT": E(L("pancakeswapV3", "WBNB-ESPORTS"), L("binance_alpha", "ESPORTSUSDT"),
                                         L("binance_future", "ESPORTSUSDT"))}}
    R = Resolver(spots, legs)
    row = sf("gate_spot", "ESPORTS_USDT", "binance", "ESPORTSUSDT")
    assert decide_sf(R, row)["ident_ev"] == "contract" and identity.transfers(R, row) == "ввод закрыт"


def test_ff_index_reference_preipo_and_no_sources():
    R = Resolver({"binance_spot": blob({"BTCUSDT": ["BTC", True]}, {})},
                 {"aster": {"OPENAIUSDT": E(L("binance", "OPENAIUSDT"))}}, bn_assets={"OPENAIUSDT": "OPENAI"})
    assert decide_ff(R, ff("aster", "OPENAIUSDT", "binance", "OPENAIUSDT", "preipo"))["ident_ev"] == "index_ref"
    hl = decide_ff(R, ff("binance", "OPENAIUSDT", "hyperliquid", "vntl:OPENAI", "preipo"))
    assert (hl["ident"], hl["ident_ev"]) == ("unknown", "preipo")               # единицы: оценка в $B против доли
    rows = [sf("binance_spot", "BTCUSDT", "binance", "BTCUSDT")]
    assert identity.resolve([], rows, None) == {"sf:unknown": 1}
    assert rows[0]["ident_ev"] == "no_sources" and rows[0]["mismatch"] is False


def test_stock_token_multiplier_verdict_label_and_gap():
    """Владелец 12.09: NFLXX — 10 акций в токене, пересчитываем на одну. Без множителя строка «не проверена»."""
    row = sf("gate_spot", "NFLXX_USDT", "binance", "NFLXUSDT", cls="equity", base="NFLX", spot_tag="NFLXX")
    gate = lambda shares: blob({"NFLXX_USDT": ["NFLXX", True]}, {}, need_n=["NFLXX_USDT"], shares=shares)
    assert decide_sf(Resolver({"gate_spot": gate({})}, {}), row)["ident_ev"] == "no_multiplier"
    d = decide_sf(Resolver({"gate_spot": gate({"NFLXX_USDT": {"n": 10.0, "src": "xStocks"}})}, {}), row)
    assert d["ident"] == "same" and "10 акц." in d["ident_why"]
    r = calc.build_sf_row(dict(row, spot_factor=10.0, spot_asset="NFLXX"), {"interval_h": 8}, {"rate": 0.0001, "mark": 100.0},
                          None, {"bid": 999.0, "ask": 1001.0}, {}, 0)
    assert r["spot_label"] == "gate·NFLXX ×10" and abs(r["gap"]) < 1e-12        # 100 за акцию против 1000/10


def test_collector_applies_share_multiplier_to_stock_tokens(tmp_path):
    from fakes import make_world
    from funding_bot.collector import Collector
    col = Collector(clients=make_world(), db_path=tmp_path / "c.db", table_path=tmp_path / "t.json", background=False)
    col.id_spots["gate_spot"] = {"shares": {"NFLXX_USDT": {"n": 10.0, "src": "xStocks"}}}
    stock = dict(symbol="NFLXX_USDT", base="~NFLXX", alt_base="NFLX", factor=1.0)
    coin = dict(symbol="NFLX_USDT", base="NFLX", factor=1.0)
    assert col._with_shares("gate_spot", stock)["factor"] == 10.0 and col._with_shares("gate_spot", coin)["factor"] == 1.0


# --- сеть: разбор ответов (identity_src) ----------------------------------------------------------------------
class _Gate:
    name = "gate_spot"

    def get(self, path, **kw):
        return {"/spot/currencies": [
            {"currency": "NFLXX", "name": "Netflix xStock", "category": ["stocks", "xstocks"],
             "chains": [{"name": "SOL", "addr": "XsEH7wWfJJu2ZT3UCFeVfALnVA6CP5ur7Ee11KmzVpL", "deposit_disabled": False, "withdraw_disabled": False}]},
            {"currency": "NFLXON", "name": "Netflix (Ondo)", "category": ["stocks", "ondo-stocks"],
             "chains": [{"name": "ETH", "addr": "0x032deC3372F25C41EA8054B4987a7c4832CDB338", "deposit_disabled": False, "withdraw_disabled": False}]},
            {"currency": "EDGE", "name": "Definitive",
             "chains": [{"name": "BASEEVM", "addr": "0xED6E000DEF95780FB89734C07EE2CE9F6DCAF110", "deposit_disabled": True, "withdraw_disabled": False}]},
            {"currency": "BTC", "name": "Bitcoin", "chains": [{"name": "BTC", "addr": "", "deposit_disabled": False, "withdraw_disabled": False}]}],
            "/spot/currency_pairs": [
            {"id": "NFLXX_USDT", "base": "NFLXX", "quote": "USDT", "trade_status": "tradable"},
            {"id": "NFLXON_USDT", "base": "NFLXON", "quote": "USDT", "trade_status": "tradable"},
            {"id": "EDGE_USDT", "base": "EDGE", "quote": "USDT", "trade_status": "untradable"},
            {"id": "BTC_USDT", "base": "BTC", "quote": "USDT", "trade_status": "tradable"}]}[path]


def test_gate_blob_contracts_trading_and_share_multipliers(monkeypatch):
    calls = []

    def get_json(url, timeout=None):
        calls.append(url)
        if "xstocks" in url:
            assert "NFLXx" in url
            return {"currentMultiplier": 10, "newMultiplier": 0, "activationDateTime": 0}
        return {"tokens": [{"chainId": 1, "address": "0x032deC3372F25C41EA8054B4987a7c4832CDB338", "symbol": "NFLXon"},
                           {"chainId": 56, "address": "0x7048F5227b032326cC8DBC53cF3FdDD947a2c757", "symbol": "NFLXon"}]}
    eth = []

    def eth_call(rpc, to, data):
        eth.append((rpc, to, data))
        return {"result": "0x" + format(10 * 10 ** 18, "064x") + "0" * 64}
    monkeypatch.setattr(identity_src, "_get_json", get_json)
    monkeypatch.setattr(identity_src, "_eth_call", eth_call)
    monkeypatch.setattr(identity_src.time, "sleep", lambda s: None)
    b = identity_src.coin_blob(_Gate())
    assert b["markets"]["EDGE_USDT"] == ["EDGE", False] and b["markets"]["BTC_USDT"] == ["BTC", True]
    e = b["coins"]["EDGE"]
    assert e["ids"] == ["0xed6e000def95780fb89734c07ee2ce9f6dcaf110"] and e["dep_closed"] and not e["wd_closed"]
    assert b["coins"]["BTC"]["nat"] == ["btc"]
    assert b["shares"] == {"NFLXX_USDT": {"n": 10.0, "src": "xStocks"}, "NFLXON_USDT": {"n": 10.0, "src": "Ondo"}}
    assert b["need_n"] == ["NFLXON_USDT", "NFLXX_USDT"]
    rpc, to, data = eth[0]                                                       # BSC-оракул, адрес токена на BSC
    assert to == config.ONDO_ORACLES[0][1] and data == "0x0a562827" + "7048f5227b032326cc8dbc53cf3fddd947a2c757".rjust(64, "0")


def test_xstocks_split_activation(monkeypatch):
    import time as _t
    monkeypatch.setattr(identity_src.time, "sleep", lambda s: None)
    past, future = _t.time() - 60, _t.time() + 3600
    for at, want in ((past, 4.0), (future, 1.0), (past * 1000, 4.0)):
        monkeypatch.setattr(identity_src, "_get_json",
                            lambda url, timeout=None, at=at: {"currentMultiplier": 1, "newMultiplier": 4, "activationDateTime": at})
        assert identity_src._xstocks({"CRWDX_USDT": "CRWDx"}) == {"CRWDX_USDT": {"n": want, "src": "xStocks"}}


def test_index_legs_400_means_no_index_and_dex_search_only_without_our_markets(monkeypatch):
    class Cl:
        name = "binance"

        def get(self, path, params=None, **kw):
            assert path == "/fapi/v1/constituents"
            s = params["symbol"]
            if s == "BADUSDT":
                raise PermanentHTTPError("400 Invalid symbol")
            legs = {"SIRENUSDT": [L("pancakeswapV3", "SIREN-WBNB"), L("binance_future", "SIRENUSDT")],
                    "ABCUSDT": [L("binance", "ABCUSDT"), L("pancakeswapV3", "ABC-WBNB")]}[s]
            return {"symbol": s, "constituents": legs}
    q = []
    monkeypatch.setattr(identity_src, "_get_json", lambda url, timeout=None: q.append(url) or {"pairs": []})
    monkeypatch.setattr(identity_src.time, "sleep", lambda s: None)
    out = identity_src.index_legs(Cl(), ["SIRENUSDT", "ABCUSDT", "BADUSDT"])
    assert out["BADUSDT"]["legs"] is None and len(out["ABCUSDT"]["legs"]) == 2
    assert list(out["SIRENUSDT"]["dex"]) == ["SIREN WBNB"] and out["ABCUSDT"]["dex"] == {}
    assert len(q) == 1 and q[0].endswith("SIREN%20WBNB")


# --- ревью 12.09 -------------------------------------------------------------------------------------------------
def _legs_client(fail: dict):
    class Cl:
        name = "binance"

        def get(self, path, params=None, **kw):
            s = params["symbol"]
            if s in fail:
                raise fail[s]
            return {"symbol": s, "constituents": [L("binance", s)]}
    return Cl()


def test_index_legs_bad_symbol_is_skipped_and_foreign_4xx_never_means_no_index(monkeypatch):
    """П.1: 5xx у одного символа губил всю пачку, и тот же символ снова первым — обход площадки стоял. П.2: 403 экрана
    записывался как «индекса нет» на сутки поверх хороших данных."""
    monkeypatch.setattr(identity_src.time, "sleep", lambda s: None)
    out = identity_src.index_legs(_legs_client({"BAD": RuntimeError("502")}), ["AAA", "BAD", "CCC"])
    assert set(out) == {"AAA", "CCC"}
    forbidden = PermanentHTTPError("403 for https://fapi.binance.com/...: Forbidden")
    with pytest.raises(PermanentHTTPError):
        identity_src.index_legs(_legs_client({"AAA": forbidden}), ["AAA", "CCC"])
    part = identity_src.index_legs(_legs_client({"CCC": forbidden}), ["AAA", "CCC", "DDD"])
    assert set(part) == {"AAA"}                                   # пачка оборвана, 403 не записан как «нет индекса»
    gone = identity_src.index_legs(_legs_client({"AAA": PermanentHTTPError('400 for x: {"code":-1121,"msg":"Invalid symbol."}')}), ["AAA"])
    assert gone == {"AAA": {"legs": None, "dex": {}}}
    with pytest.raises(RuntimeError):
        identity_src.index_legs(_legs_client({"AAA": RuntimeError("502"), "CCC": RuntimeError("502")}), ["AAA", "CCC"])


def test_no_alpha_list_means_no_dex_evidence():
    """П.4: пустой список Alpha читался как «токенов с этим символом нет» — пул по поиску становился доказательством."""
    assert decide_sf(_dex_world([], 3_000_000), sf("binance_spot", "AIUSDT", "aster", "AIUSDT"))["ident_ev"] == "dex_unresolved"


def test_similar_names_never_prove_same():
    """П.5: Bitcoin ⊂ Bitcoin Cash — нестрогое сходство имён не даёт «тот» при разных контрактах (и не даёт «не тот»)."""
    spots = {"gate_spot": blob({"BTC_USDT": ["BTC", True]}, {"BTC": rec("Bitcoin Cash", "0x" + "11" * 20)}),
             "kucoin_spot": blob({"BTC-USDT": ["BTC", True]}, {"BTC": rec("Bitcoin", "0x" + "22" * 20)})}
    R = Resolver(spots, {"binance": {"BTCUSDT": E(L("kucoin", "BTC-USDT"))}})
    d = decide_sf(R, sf("gate_spot", "BTC_USDT", "binance", "BTCUSDT"))
    assert (d["ident"], d["ident_ev"]) == ("unknown", "names_similar")
    assert identity.name_eq("Filecoin", "Filecoin (IPFS)") and not identity.name_eq("Filecoin", "Filecoin (IPFS)", strict=True)


def _col(tmp_path):
    from fakes import make_world
    from funding_bot.collector import Collector
    return Collector(clients=make_world(), db_path=tmp_path / "c.db", table_path=tmp_path / "t.json", background=False)


def test_resolver_failure_leaves_rows_unverified_with_note(tmp_path, monkeypatch):
    """П.3: исключение в проверке снимало все «≠» молча. Теперь строки «не проверено» и заметка на странице."""
    col = _col(tmp_path); col.step_universe()
    assert col.sf and col.notes.get("identity") is None

    def boom(*a, **kw):
        raise KeyError("coins")
    monkeypatch.setattr(identity, "Resolver", boom)
    col._resolve()
    assert all(r["ident_ev"] == "resolver_error" and not r["mismatch"] for r in col.sf + col.ff)
    assert "KeyError" in col.notes["identity"]


def test_index_sweep_never_rides_with_funding_catchup(tmp_path):
    """П.6: пачка составов индекса в одном задании с досбором истории задерживала его на десятки секунд."""
    col = _col(tmp_path); col.step_universe()
    col._schedule_aux(col.now())
    kinds = col._aux["binance"][3]
    assert "incr" in kinds and "legs" not in kinds
    for f, *_ in list(col._aux.values()):
        f.result(timeout=10)
    col._collect()
    col._schedule_aux(col.now())
    assert col._aux["binance"][3] == ["legs"]


def test_take_coins_keeps_alpha_and_stale_multiplier_expires(tmp_path):
    col = _col(tmp_path)
    now = int(col.now())
    col.id_spots["binance_spot"] = {"alpha": [{"s": "AI"}], "coins": {}, "markets": {}}
    col._take_coins("binance_spot", {"coins": {"X": {}}, "markets": {"XUSDT": ["X", True]}, "alpha": None})
    assert col.id_spots["binance_spot"]["alpha"] == [{"s": "AI"}]
    col.id_spots["gate_spot"] = {"shares": {"A_USDT": {"n": 10.0, "src": "xStocks"},
                                            "B_USDT": {"n": 4.0, "src": "xStocks", "stale": True, "stale_since": now - 8 * 86400}}}
    col._take_coins("gate_spot", {"coins": {}, "markets": {}, "shares": {}, "need_n": ["A_USDT", "B_USDT"]})
    sh = col.id_spots["gate_spot"]["shares"]
    assert sh["A_USDT"]["stale"] and sh["A_USDT"]["n"] == 10.0 and "B_USDT" not in sh     # неделя без ответа — выбыл


def test_coins_update_shares_in_place_without_rebuild(tmp_path):
    """Выкат 12.09: приход списка монет пересобирал вселенную, а пересборка — проверку полноты за 30 сут: тик 11–17 с."""
    col = _col(tmp_path)
    col.sf = [dict(key="gate_spot:NFLXX_USDT|binance:NFLXUSDT", cls="equity", spot_ex="gate_spot", spot="NFLXX_USDT",
                   spot_factor=1.0, perp_ex="binance", perp="NFLXUSDT", perp_factor=1.0, base="NFLX"),
              dict(key="kucoin_spot:NFLXX-USDT|binance:NFLXUSDT", cls="equity", spot_ex="kucoin_spot", spot="NFLXX_USDT",
                   spot_factor=1.0, perp_ex="binance", perp="NFLXUSDT", perp_factor=1.0, base="NFLX")]
    col._rebuild_due = False
    col._take_coins("gate_spot", {"coins": {}, "markets": {}, "shares": {"NFLXX_USDT": {"n": 10.0, "src": "xStocks"}},
                                  "need_n": ["NFLXX_USDT"]})
    assert col.sf[0]["spot_factor"] == 10.0 and col.sf[1]["spot_factor"] == 1.0      # только своей площадки
    assert not col._rebuild_due and col._resolve_due


def test_xstocks_non_numeric_activation_keeps_current(monkeypatch):
    monkeypatch.setattr(identity_src.time, "sleep", lambda s: None)
    monkeypatch.setattr(identity_src, "_get_json", lambda url, timeout=None: {
        "currentMultiplier": 10, "newMultiplier": 20, "activationDateTime": "2026-07-02T13:30:00Z"})
    assert identity_src._xstocks({"NFLXX_USDT": "NFLXx"}) == {"NFLXX_USDT": {"n": 10.0, "src": "xStocks"}}


# --- рейтинг надёжности A/B/C (владелец 13.09: «ставь с названием монеты рейтинг надежности, A, B, C») -----------------
REL_CODES = {"idx", "leg", "contract", "ref", "mkt", "cex", "vendor", "alpha", "code", "native", "stock", "gold", "fx",
             "class", "pool2", "name", "bridged", "oracle", "pool"}
AIW3 = "0x37e94fc028903e74275478b65160d6d2c0e8880b"


def rel(d):
    """(буква, код, подробность); у «тот» код — из списка, который знает страница (dashboard REL_WHY)."""
    if d["ident"] == "same":
        assert d["rel"] in ("A", "B", "C") and d["rel_ev"] in REL_CODES, d
    return d["rel"], d["rel_ev"], d["rel_d"]


def _aiw3_world():
    """Живой срез 13.09, сделка DQA9Q: индекс Aster AIW3USDT — пул pancakeswap AIW3-USDT (0.9) и apollox (0.1); токен найден
    поиском DexScreener по тикеру (единственный доминирующий пул), токенов Alpha с тикером AIW3 нет. Спот Gate — тот же
    контракт на BSC."""
    pool = {"chain": "bsc", "dex": "pancakeswap", "labels": ["v3"], "b": ["AIW3", AIW3, "AIW3"],
            "q": ["USDT", "0x55d398326f99059ff775485246999027b3197955", "Tether USD"], "liq": 1_057_727.0}
    spots = {"binance_spot": blob({"BTCUSDT": ["BTC", True]}, {},
                                  alpha=[{"s": "OTHER", "n": "Other", "a": "0x" + "0f" * 20, "c": "56", "id": "A0"}]),
             "gate_spot": blob({"AIW3_USDT": ["AIW3", True]}, {"AIW3": rec("AIW3", AIW3, chain="BSC")})}
    legs = {"aster": {"AIW3USDT": E(L("pancakeswap", "AIW3-USDT", 0.9), L("apollox", "AIW3USDT", 0.1),
                                    dex={"AIW3 USDT": [pool]})}}
    return Resolver(spots, legs)


def test_reliability_grade_sf():
    """A — рынок или контракт указала биржа; B — первоисточник (Alpha, код той же биржи, сеть, реестр акций); C — догадка
    (имя, пул по тикеру, оракул HL). ident / ident_ev — прежние, рейтинг — новые поля."""
    # EDGE: Gate EDGEX — сам рынок индекса (A idx); KuCoin EDGE в индексе нет, но контракт — у монеты рынка индекса (A leg)
    spots = {"gate_spot": blob({"EDGE_USDT": ["EDGE", True], "EDGEX_USDT": ["EDGEX", True]},
                               {"EDGE": rec("Definitive", "0x" + "ed" * 20, chain="BASEEVM"), "EDGEX": rec("edgeX", "0x" + "b0" * 20)}),
             "kucoin_spot": blob({"EDGE-USDT": ["EDGE", True]}, {"EDGE": rec("edgeX", "0x" + "b0" * 20)})}
    R = Resolver(spots, {"binance": {"EDGEUSDT": E(L("gateio", "EDGEX_USDT"))}})
    d = decide_sf(R, sf("gate_spot", "EDGEX_USDT", "binance", "EDGEUSDT"))
    assert (d["ident_ev"], rel(d)) == ("index_market", ("A", "idx", None))
    d = decide_sf(R, sf("kucoin_spot", "EDGE-USDT", "binance", "EDGEUSDT"))
    assert (d["ident_ev"], rel(d)) == ("contract", ("A", "leg", None))
    d = decide_sf(R, sf("gate_spot", "EDGE_USDT", "binance", "EDGEUSDT"))
    assert d["ident"] == "other" and rel(d) == (None, None, None)                 # у «≠» буквы нет
    # ONG: KuCoin — только имя (контракта у индекса нет) → C, подробность — имя
    spots = {"gate_spot": blob({"ONG_USDT": ["ONG", True]}, {"ONG": coin_record("OntologyGas", [("ONT", "", True, True)], "ONG")}),
             "kucoin_spot": blob({"ONG-USDT": ["ONG", True]},
                                 {"ONG": coin_record("Ontology Gas", [("ont", "02" + "0" * 38, False, False)], "ONG")})}
    R = Resolver(spots, {"binance": {"ONGUSDT": E(L("gateio", "ONG_USDT"))}})
    d = decide_sf(R, sf("kucoin_spot", "ONG-USDT", "binance", "ONGUSDT"))
    assert (d["ident_ev"], rel(d)) == ("name", ("C", "name", "Ontology Gas"))
    # FLOW: контракты разные, имя то же — мост? C
    spots = {"gate_spot": blob({"FLOW_USDT": ["FLOW", True]}, {"FLOW": rec("Flow", "0x" + "f2" * 20)}),
             "kucoin_spot": blob({"FLOW-USDT": ["FLOW", True]}, {"FLOW": rec("Flow", "0x" + "f1" * 20)})}
    R = Resolver(spots, {"aster": {"FLOWUSDT": E(L("gateio", "FLOW_USDT"))}})
    assert rel(decide_sf(R, sf("kucoin_spot", "FLOW-USDT", "aster", "FLOWUSDT"))) == ("C", "bridged", "Flow")
    # PEPE: оракул HL по тикеру — C; перп Binance, чей индекс берёт этот рынок, — A; неверный множитель — «?», без буквы
    spots = {"binance_spot": blob({"PEPEUSDT": ["PEPE", True]}, {"PEPE": rec("Pepe", "0x" + "69" * 20)})}
    R = Resolver(spots, {"binance": {"1000PEPEUSDT": E(L("binance", "PEPEUSDT*1000"))}})
    assert rel(decide_sf(R, sf("binance_spot", "PEPEUSDT", "hyperliquid", "kPEPE", pf=1000))) == ("C", "oracle", None)
    assert rel(decide_sf(R, sf("binance_spot", "PEPEUSDT", "binance", "1000PEPEUSDT", pf=1000))) == ("A", "idx", None)
    d = decide_sf(R, sf("binance_spot", "PEPEUSDT", "binance", "1000PEPEUSDT", pf=1))
    assert (d["ident"], rel(d)) == ("unknown", (None, None, None))
    # ESPORTS: индекс — только Binance Alpha (токен по тикеру в списке Alpha) → B
    spots = {"binance_spot": blob({"BTCUSDT": ["BTC", True]}, {},
                                  alpha=[{"s": "ESPORTS", "n": "Yooldo", "a": "0x" + "f3" * 20, "c": "56", "id": "A9"}]),
             "gate_spot": blob({"ESPORTS_USDT": ["ESPORTS", True]}, {"ESPORTS": rec("Yooldo", "0x" + "F3" * 20, dep=False)})}
    R = Resolver(spots, {"binance": {"ESPORTSUSDT": E(L("binance_alpha", "ESPORTSUSDT"), L("binance_future", "ESPORTSUSDT"))}})
    d = decide_sf(R, sf("gate_spot", "ESPORTS_USDT", "binance", "ESPORTSUSDT"))
    assert (d["ident_ev"], rel(d)) == ("contract", ("B", "alpha", None))
    # акции, золото, валюта — по реестру и классу площадок → B
    gate = blob({"NFLXX_USDT": ["NFLXX", True]}, {}, need_n=["NFLXX_USDT"], shares={"NFLXX_USDT": {"n": 10.0, "src": "xStocks"}})
    R = Resolver({"gate_spot": gate}, {})
    row = sf("gate_spot", "NFLXX_USDT", "binance", "NFLXUSDT", cls="equity", base="NFLX", spot_tag="NFLXX")
    assert rel(decide_sf(R, row)) == ("B", "stock", None)
    assert rel(decide_sf(R, sf("gate_spot", "XAUT_USDT", "binance", "XAUUSDT", cls="commodity", base="XAU"))) == ("B", "gold", None)
    assert rel(decide_sf(R, sf("gate_spot", "EUR_USDT", "binance", "EURUSDT", cls="fx", base="EUR"))) == ("B", "fx", None)
    # код монеты в пространстве той же биржи: KuCoin-перп без состава индекса, Binance-перп с индексом «только из себя»
    spots = {"kucoin_spot": blob({"ABC-USDT": ["ABC", True]}, {"ABC": rec("Abc", "0x" + "ab" * 20)}),
             "binance_spot": blob({"XYZUSDT": ["XYZ", True]}, {"XYZ": rec("Xyz", "0x" + "cd" * 20)})}
    R = Resolver(spots, {"binance": {"XYZUSDT": E(L("binance_future", "XYZUSDT"))}}, bn_assets={"XYZUSDT": "XYZ"})
    d = decide_sf(R, sf("kucoin_spot", "ABC-USDT", "kucoin", "ABCUSDTM"))
    assert (d["ident_ev"], rel(d)) == ("venue_code", ("B", "code", None))
    d = decide_sf(R, sf("binance_spot", "XYZUSDT", "binance", "XYZUSDT"))
    assert (d["ident_ev"], rel(d)) == ("venue_code", ("B", "code", None))
    # Harmony ONE: родная сеть — B; имя плюс общая сеть — тоже B (досчитка «первого правила»); сеть лишь от оракула HL — C
    one = lambda name: coin_record(name, [("ONE", "", True, True)], "ONE")
    spots = {"gate_spot": blob({"ONE_USDT": ["ONE", True]}, {"ONE": one("Harmony")}),
             "kucoin_spot": blob({"ONE-USDT": ["ONE", True]}, {"ONE": one("Harmony ONE")}),
             "bitget_spot": blob({"ONEUSDT": ["ONE", True]}, {"ONE": one(None)})}
    R = Resolver(spots, {"binance": {"ONEUSDT": E(L("gateio", "ONE_USDT"))}})
    d = decide_sf(R, sf("kucoin_spot", "ONE-USDT", "binance", "ONEUSDT"))
    assert (d["ident_ev"], rel(d)) == ("native_chain", ("B", "native", None))
    d = decide_sf(R, sf("bitget_spot", "ONEUSDT", "hyperliquid", "ONE"))
    assert (d["ident_ev"], rel(d)) == ("native_chain", ("C", "oracle", None))
    spots["kucoin_spot"] = blob({"ONE-USDT": ["ONE", True]}, {"ONE": one("Harmony")})
    R = Resolver(spots, {"binance": {"ONEUSDT": E(L("gateio", "ONE_USDT"))}})
    d = decide_sf(R, sf("kucoin_spot", "ONE-USDT", "binance", "ONEUSDT"))
    assert (d["ident_ev"], rel(d)) == ("name", ("B", "native", None))            # не C: сеть доказана
    # AIW3: пул индекса найден поиском по тикеру — C, подробность — пул
    R = _aiw3_world()
    d = decide_sf(R, sf("gate_spot", "AIW3_USDT", "aster", "AIW3USDT"))
    assert (d["ident"], d["ident_ev"], rel(d)) == ("same", "contract", ("C", "pool", "pancakeswap AIW3-USDT"))
    assert identity.dex_token_rel(R, "aster", "AIW3USDT") == {("56", AIW3): ("C", "pool", "pancakeswap AIW3-USDT")}


def test_reliability_is_by_direct_record_not_by_merged_group():
    """Ревью 13.09, случай F: доверенная нога Gate (контракт только в Ethereum) и одноимённая запись «код Binance» (BSC)
    склеены в одну группу по имени. Спот с одним лишь BSC-контрактом доказан записью «код» — B, а не A от ноги по соседству."""
    eth, bsc = "0x" + "e1" * 20, "0x" + "b5" * 20
    spots = {"gate_spot": blob({"F_USDT": ["F", True]}, {"F": rec("Fcoin", eth)}),
             "binance_spot": blob({"FUSDC": ["F", True]}, {"F": rec("Fcoin", bsc, chain="BSC")}),
             "kucoin_spot": blob({"F-USDT": ["F", True]}, {"F": rec("Fcoin", bsc, chain="BSC")})}
    legs = {"aster": {"FUSDT": E(L("gateio", "F_USDT"), L("binance", "FUSDT"))},
            "binance": {"FUSDT": E(L("binance_future", "FUSDT"))}}
    R = Resolver(spots, legs, bn_assets={"FUSDT": "F"})
    I = R.identity("aster", "FUSDT")
    assert {eth, bsc} <= I["tokens"] and I["tok_rel"][eth][:2] == ("A", "leg")      # одна группа, обе записи приняты
    d = decide_sf(R, sf("kucoin_spot", "F-USDT", "aster", "FUSDT"))
    assert (d["ident"], d["ident_ev"], rel(d)) == ("same", "contract", ("B", "code", None))
    assert identity.dex_token_rel(R, "aster", "FUSDT") == {("56", bsc): ("B", "code", None)}
    assert identity.dex_tokens(R, "aster", "FUSDT") == [("56", bsc, False)]      # набор токенов — прежний


def test_reliability_grade_ff():
    x, z = "0x" + "c1" * 20, "0x" + "c2" * 20
    one = coin_record("Harmony", [("ONE", "", True, True)], "ONE")
    spots = {"gate_spot": blob({"X_USDT": ["X", True], "ONE_USDT": ["ONE", True]}, {"X": rec("Xcoin", x), "ONE": one}),
             "kucoin_spot": blob({"X-USDT": ["X", True], "Z-USDT": ["Z", True], "ONE-USDT": ["ONE", True]},
                                 {"X": rec("Xcoin", x), "Z": rec("Xcoin", z), "ONE": one}),
             "binance_spot": blob({"BTCUSDT": ["BTC", True]}, {},
                                  alpha=[{"s": "AX", "n": "Xcoin", "a": x, "c": "56", "id": "A1"}])}
    legs = {"binance": {"XUSDT": E(L("gateio", "X_USDT")), "ONEUSDT": E(L("gateio", "ONE_USDT")),
                        "CUSDT": E(L("okex", "C-USDT")), "VUSDT": E(L("pyth", "V//USD")),
                        "PUSDT": E(L("pancakeswapV3", "P-WBNB"))},
            "aster": {"XUSDT": E(L("gateio", "X_USDT")), "KUSDT": E(L("kucoin", "X-USDT")), "ZUSDT": E(L("kucoin", "Z-USDT")),
                      "AXUSDT": E(L("binance_alpha", "AXUSDT")), "CUSDT": E(L("okex", "C-USDT")),
                      "VUSDT": E(L("pyth", "V//USD")), "PUSDT": E(L("pancakeswapV3", "P-WBNB")),
                      "OPENAIUSDT": E(L("binance", "OPENAIUSDT"))}}
    R = Resolver(spots, legs, bn_assets={"OPENAIUSDT": "OPENAI"}, perp_names={"lighter": {"X": "Xcoin"}})
    g = lambda va, sa, vb, sb, cls="crypto": (lambda d: (d["ident_ev"], rel(d)))(decide_ff(R, ff(va, sa, vb, sb, cls)))
    assert g("aster", "OPENAIUSDT", "binance", "OPENAIUSDT", "preipo") == ("index_ref", ("A", "ref", None))
    assert g("binance", "XUSDT", "aster", "XUSDT") == ("shared_leg", ("A", "mkt", None))         # оба индекса — Gate X
    assert g("binance", "XUSDT", "gate", "X_USDT") == ("shared_leg", ("B", "code", None))        # у Gate — код своего спота
    assert g("binance", "XUSDT", "hyperliquid", "X") == ("shared_leg", ("C", "oracle", None))    # оракул HL по тикеру
    assert g("binance", "CUSDT", "aster", "CUSDT") == ("shared_leg", ("A", "cex", None))         # оба — OKX C-USDT
    assert g("binance", "VUSDT", "aster", "VUSDT") == ("shared_leg", ("A", "vendor", None))
    assert g("binance", "PUSDT", "aster", "PUSDT") == ("shared_leg", ("B", "pool2", None))       # пул назван парой тикеров
    assert g("binance", "TSLAUSDT", "gate", "TSLA_USDT", "equity") == ("class_ticker", ("B", "class", None))
    assert g("binance", "XUSDT", "aster", "KUSDT") == ("contract", ("A", "contract", None))      # обе стороны — рынки индекса
    assert g("binance", "XUSDT", "aster", "AXUSDT") == ("contract", ("B", "alpha", None))        # одна — токен Alpha
    assert g("binance", "XUSDT", "aster", "ZUSDT") == ("name_bridged", ("C", "bridged", "Xcoin"))
    assert g("binance", "XUSDT", "lighter", "X") == ("name", ("C", "name", "Xcoin"))             # имя рынка Lighter
    assert g("binance", "ONEUSDT", "kucoin", "ONEUSDTM") == ("name", ("B", "native", None))      # имя + общая сеть


def test_rating_does_not_touch_verdicts():
    """Рейтинг — только новые ключи; ident / ident_ev / ident_why / mismatch прежние, а после смены «тот» → «не проверено»
    буква не залипает (resolve делает it.update)."""
    spots = {"gate_spot": blob({"EDGE_USDT": ["EDGE", True], "EDGEX_USDT": ["EDGEX", True]},
                               {"EDGE": rec("Definitive", "0x" + "ed" * 20, chain="BASEEVM"), "EDGEX": rec("edgeX", "0x" + "b0" * 20)}),
             "kucoin_spot": blob({"EDGE-USDT": ["EDGE", True]}, {"EDGE": rec("edgeX", "0x" + "b0" * 20)})}
    R = Resolver(spots, {"binance": {"EDGEUSDT": E(L("gateio", "EDGEX_USDT"), L("kucoin", "EDGE-USDT"))}})
    rows = [sf("gate_spot", "EDGEX_USDT", "binance", "EDGEUSDT"), sf("gate_spot", "EDGE_USDT", "binance", "EDGEUSDT"),
            sf("kucoin_spot", "EDGE-USDT", "binance", "EDGEUSDT")]
    pairs = [ff("aster", "EDGEUSDT", "binance", "EDGEUSDT")]
    identity.resolve(pairs, rows, R)
    assert [(r["ident"], r["ident_ev"], r["mismatch"], r["rel"], r["rel_ev"]) for r in rows] == [
        ("same", "index_market", False, "A", "idx"), ("other", "index_other_market", True, None, None),
        ("same", "index_market", False, "A", "idx")]
    assert rows[0]["ident_why"] == "рынок gate EDGEX есть в индексе перпа" and "EDGEX" in rows[1]["ident_why"]
    assert (pairs[0]["ident"], pairs[0]["rel"]) == ("unknown", None)
    for d in [decide_sf(R, r) for r in rows] + [decide_ff(R, pairs[0])]:
        assert set(d) == {"ident", "ident_ev", "ident_why", "rel", "rel_ev", "rel_d"}
    identity.resolve(pairs, rows, None)                                  # источники пропали — «не проверено»
    assert all(r["ident"] == "unknown" and r["rel"] is None and r["rel_ev"] is None and r["rel_d"] is None
               for r in rows + pairs)


def test_calc_copies_rating_only_for_same_rows():
    """table.json — ключи рейтинга только у «тот» (строк десятки тысяч); подробность — только у C; подсказка ident_why у
    «тот» — по-прежнему None."""
    b = dict(key="binance_spot:XUSDT|aster:XUSDT", base="X", perp_ex="aster", perp="XUSDT", spot_ex="binance_spot", spot="XUSDT")
    row = lambda **kw: calc.build_sf_row(dict(b, **kw), None, {"rate": 0.0001, "mark": 1.0}, None, None, {}, 0)
    r = row(ident="same", ident_ev="contract", ident_why="общий контракт", rel="C", rel_ev="pool", rel_d="pancakeswap X-USDT")
    assert (r["rel"], r["rel_ev"], r["rel_d"], r["ident_why"]) == ("C", "pool", "pancakeswap X-USDT", None)
    r = row(ident="same", ident_ev="index_market", ident_why="рынок", rel="A", rel_ev="idx", rel_d=None)
    assert (r["rel"], r["rel_ev"]) == ("A", "idx") and "rel_d" not in r
    for v in ("unknown", "other"):
        assert not {"rel", "rel_ev", "rel_d"} & set(row(ident=v, ident_ev="x", ident_why="y", rel="A", rel_ev="idx"))
    assert not {"rel", "rel_ev", "rel_d"} & set(row(ident="same", ident_ev="contract", ident_why=None))   # старый элемент
    item = dict(key="aster:X|binance:X", base="X", va="aster", vb="binance", sa="XUSDT", sb="XUSDT", ident="same",
                ident_ev="shared_leg", ident_why="оба индекса", rel="B", rel_ev="code", rel_d=None)
    f = calc.build_ff_row(item, None, None, None, None, None, None, {}, {}, 0)
    assert (f["rel"], f["rel_ev"], f["ident_why"]) == ("B", "code", None) and "rel_d" not in f
