"""Спот-нога OKX DEX (владелец 12.09: «в спот добавить okx dex»; «почему для этих монет нет okx dex? надо сделать»):
токены — только доказанные контрактами, строка — от $50k ликвидности, «Комиссия» — всё включено, мост — с пометкой."""
import time
from funding_bot import identity, dexleg, calc, config
from funding_bot.identity import coin_record, Resolver
from funding_bot.okxdex import NoLiquidity

ESP = "0xf39e4b21c84e737df08e2c3b32541d856f508e48"
BTCB = "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c"
USDT = config.OKX_DEX_STABLES["56"][0]
L = lambda ex, sym, w=1.0: {"exchange": ex, "symbol": sym, "weight": str(w)}


class FakeOkx:
    """Как okxdex.OkxDex: цены, ликвидность, имена, котировки (пул 0.2 % на сторону, газ $0.03)."""
    def __init__(self):
        self.liq, self.price, self.names, self.calls = {}, {}, {}, []
        self.no_liq, self.honeypot = set(), set()
        self.used_weight = 0; self.last_ok_ts = time.time(); self.n_429 = 0; self.n_err = 0; self.banned_until = 0

    def enabled(self): return True

    def health(self):
        return {"exchange": "okxdex", "used_weight": 0, "budget": 0.0, "last_ok_ts": int(self.last_ok_ts), "n_429": 0,
                "n_err": 0, "banned_until": 0, "stale_s": config.OKX_DEX_STALE_S}

    def price_info(self, toks):
        self.calls.append(("info", len(toks)))
        return {t: dict(liq=self.liq[t], price=self.price.get(t, 0), vol24=0) for t in toks if t in self.liq}

    def basic_info(self, toks):
        return {t: self.names[t] for t in toks if t in self.names}

    def market_prices(self, toks):
        self.calls.append(("px", len(toks)))
        return {t: (self.price[t], int(time.time() * 1000)) for t in toks if t in self.price}

    def quote(self, chain, frm, to, amount):
        self.calls.append(("q", chain, frm, to))
        sdec = config.OKX_DEX_STABLES[chain][1]
        if frm == config.OKX_DEX_STABLES[chain][0]:                      # покупка токена
            tok = (chain, to)
            if tok in self.no_liq:
                raise NoLiquidity("82000")
            usd = amount / 10 ** sdec
            got = usd / self.price[tok] * 0.998
            return dict(to_amount=int(got * 10 ** 18), to_decimals=18, price_impact=-0.1, gas_usd=0.03, buy_tax=0.0,
                        sell_tax=0.0, honeypot=tok in self.honeypot, from_decimals=sdec)
        tok = (chain, frm)
        usd = amount / 10 ** 18 * self.price[tok] * 0.998
        return dict(to_amount=int(usd * 10 ** sdec), to_decimals=sdec, price_impact=-0.1, gas_usd=0.015, buy_tax=0.0,
                    sell_tax=0.0, honeypot=False, from_decimals=18)


def _R():
    """ESPORTS: индекс Binance — Alpha-токен Yooldo на BSC; BTC: индекс — KuCoin BTC, у которой есть BTCB на BSC (мост)."""
    spots = {"binance_spot": dict(markets={"BTCUSDT": ["BTC", True]}, coins={},
                                  alpha=[{"s": "ESPORTS", "n": "Yooldo", "a": ESP, "c": "56", "id": "A1"}]),
             "kucoin_spot": dict(markets={"BTC-USDT": ["BTC", True]},
                                 coins={"BTC": coin_record("Bitcoin", [("btc", "", True, True), ("bsc", BTCB, True, True),
                                                                        ("eth", "0x" + "ab" * 20, True, True)], "BTC")})}
    legs = {"binance": {"ESPORTSUSDT": {"legs": [L("binance_alpha", "ESPORTSUSDT")], "dex": {}},
                        "BTCUSDT": {"legs": [L("kucoin", "BTC-USDT")], "dex": {}}}}
    return Resolver(spots, legs)


INS = {"binance": {"ESPORTSUSDT": dict(symbol="ESPORTSUSDT", base="ESPORTS", factor=1.0, cls="crypto"),
                   "BTCUSDT": dict(symbol="BTCUSDT", base="BTC", factor=1.0, cls="crypto"),
                   "NVDAUSDT": dict(symbol="NVDAUSDT", base="NVDA", factor=1.0, cls="equity")}}


def test_coin_record_keeps_contracts_on_dex_chains_only():
    r = coin_record("Bitcoin", [("BEP20", BTCB.upper().replace("0X", "0x"), True, True), ("SOL", "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh", True, True),
                                ("ERC20", "0x" + "ab" * 20, True, True)], "BTC")
    assert r["dex"] == {"56": [BTCB], "501": ["3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh"]}   # Ethereum — не наша сеть


def test_dex_tokens_come_only_from_proven_contracts_and_mark_bridges():
    R = _R()
    assert identity.dex_tokens(R, "binance", "ESPORTSUSDT") == [("56", ESP, False)]
    assert identity.dex_tokens(R, "binance", "BTCUSDT") == [("56", BTCB, True)]      # родная сеть BTC — не BSC: мост


def _leg():
    d = dexleg.DexLeg(FakeOkx())
    d.set_candidates(_R(), INS)
    return d


def test_flow_liquidity_threshold_all_in_fee_and_rows():
    d = _leg()
    assert set(d.cand) == {("binance", "ESPORTSUSDT"), ("binance", "BTCUSDT")}      # акции — не сюда
    d.cl.liq = {("56", ESP): 116_000.0, ("56", BTCB): 40_000.0}                    # BTCB тоньше порога владельца $50k
    d.cl.price = {("56", ESP): 0.0109, ("56", BTCB): 60_000.0}
    d.cl.names = {("56", ESP): {"name": "Yooldo", "symbol": "ESPORTS"}}
    res = d.job(d.plan(time.time()))
    d.apply(res)
    assert ("56", BTCB) not in res["q"] and ("56", ESP) in res["q"]                 # котировки — только ликвидным
    rows = d.rows(INS)
    assert [r["key"] for r in rows] == [f"okxdex:56:{ESP}|binance:ESPORTSUSDT"]
    r = rows[0]
    assert r["spot_ex"] == "okxdex" and r["spot_tag"] == "bsc" and r["ident"] == "same" and not r["dex"]["bridged"]
    ex = d.extra(r, time.time())
    q = d.q[("56", ESP)]
    assert abs(q["rt"] - (1 - 0.998 * 0.998)) < 1e-6 and abs(q["gas"] - 0.045) < 1e-9
    assert abs(ex["cost"] - (q["rt"] + 0.045 / config.OKX_DEX_QUOTE_USD)) < 1e-12
    row = calc.build_sf_row(r, {"interval_h": 8}, {"rate": 0.001, "mark": 0.011}, None, d.book(r["spot"]), {}, 0, dex=ex)
    assert abs(row["fee"] - (ex["cost"] + 2 * config.FEES_TAKER["binance"])) < 1e-12
    assert row["spot_label"] == "okx·bsc" and row["urls"]["spot"] == f"https://web3.okx.com/token/bsc/{ESP}"
    assert abs(row["gap"] - (0.011 / 0.0109 - 1)) < 1e-12 and row["dex"]["liq"] == 116_000.0


def test_bridge_label_honeypot_and_no_liquidity():
    d = _leg()
    d.cl.liq = {("56", ESP): 116_000.0, ("56", BTCB): 9_000_000.0}
    d.cl.price = {("56", ESP): 0.0109, ("56", BTCB): 60_000.0}
    d.cl.names = {("56", BTCB): {"name": "BTCB Token", "symbol": "BTCB"}}
    d.cl.honeypot = {("56", ESP)}
    d.apply(d.job(d.plan(time.time())))
    rows = {r["base"]: r for r in d.rows(INS)}
    assert set(rows) == {"BTC"}                                                     # honeypot выбыл
    assert rows["BTC"]["dex"]["bridged"] and rows["BTC"]["spot_tag"] == "bsc·BTCB"
    d2 = _leg()
    d2.cl.liq = {("56", ESP): 116_000.0}; d2.cl.price = {("56", ESP): 0.0109}; d2.cl.no_liq = {("56", ESP)}
    d2.apply(d2.job(d2.plan(time.time())))
    r = d2.rows(INS)[0]
    ex = d2.extra(r, time.time())
    assert ex["cost"] is None and "нет ликвидности" in ex["tip"]["err"]              # «Комиссия» — «—», причина в подсказке


def test_quote_budget_and_oldest_first(monkeypatch):
    d = _leg()
    d.cl.liq = {("56", ESP): 116_000.0, ("56", BTCB): 9_000_000.0}
    d.cl.price = {("56", ESP): 0.0109, ("56", BTCB): 60_000.0}
    d.apply(d.job(d.plan(time.time())))
    d.q[("56", BTCB)]["t"] -= 1000
    assert d.plan(time.time())["quotes"][0] == ("56", BTCB)                         # самая старая котировка — первой
    monkeypatch.setattr(config, "OKX_DEX_QUOTE_BUDGET_S", -1)
    assert d.job(d.plan(time.time()))["q"] == {}                                   # бюджет задания исчерпан — котировок нет
    p = d.plan(time.time())
    assert p["info"] == [] and not p["full_info"]                                  # ликвидность — раз в час, не каждый раз


def test_review_cross_rate_legs_do_not_give_usdc_perp_btc_tokens():
    """Ревью 12.09 (high): индекс USDCUSDT — отношения BTCUSDT/BTCUSDC и т.п.; их читали как рынок BTC — перп USDC получал
    BTCB и Binance-Peg ETH с пометкой «тот же актив». «ONTTRY/USDTTRY» — рынок ONT, как раньше."""
    spots = {"binance_spot": dict(markets={"BTCUSDT": ["BTC", True], "BTCUSDC": ["BTC", True], "ONTTRY": ["ONT", True]},
                                  coins={"BTC": coin_record("Bitcoin", [("btc", "", True, True), ("bsc", BTCB, True, True)], "BTC"),
                                         "ONT": coin_record("Ontology", [("ont", "", True, True)], "ONT")},
                                  alpha=[{"s": "X", "n": "x", "a": "0x" + "11" * 20, "c": "56", "id": "A"}])}
    legs = {"binance": {"USDCUSDT": {"legs": [L("binance_cross", "BTCUSDT/BTCUSDC")], "dex": {}},
                        "ONTUSDT": {"legs": [L("binance", "ONTTRY/USDTTRY")], "dex": {}}}}
    R = Resolver(spots, legs)
    assert identity.dex_tokens(R, "binance", "USDCUSDT") == [] and not R.identity("binance", "USDCUSDT")["markets"]
    assert ("binance_spot", "ONT") in R.identity("binance", "ONTUSDT")["markets"]


def test_review_stable_is_never_a_candidate_and_bridge_marks_only_explicit_names():
    d = dexleg.DexLeg(FakeOkx())
    stable = config.OKX_DEX_STABLES["501"][0]
    R = type("R", (), {"identity": lambda self, v, s: {"dex": {("501", stable), ("501", "W1tn3ss")}, "natives": set()}})()
    d.set_candidates(R, {"binance": {"XUSDT": dict(symbol="XUSDT", base="X", factor=1.0, cls="crypto")}})
    assert d.tokens() == [("501", "W1tn3ss")]                                    # USDC на Solana — стейбл клипа, не кандидат
    rx = dexleg._BRIDGE_NAME
    assert not rx.search("Wormhole Token") and not rx.search("LayerZero")          # родные токены проектов-мостов
    assert rx.search("Binance-Peg Ethereum Token") and rx.search("Wrapped Ether (Wormhole)") and rx.search("USDC (PoS)")


def test_review_transient_quote_error_keeps_good_quote_and_retries_first():
    d = _leg()
    d.cl.liq = {("56", ESP): 116_000.0, ("56", BTCB): 9_000_000.0}
    d.cl.price = {("56", ESP): 0.0109, ("56", BTCB): 60_000.0}
    d.apply(d.job(d.plan(time.time())))
    good = dict(d.q[("56", ESP)])
    d.apply(dict(t=time.time(), px={}, info={}, q={("56", ESP): dict(t=time.time(), err="ReadTimeout")}, full_info=False, err=None))
    q = d.q[("56", ESP)]
    assert q["rt"] == good["rt"] and q["t"] == good["t"] and q["last_err"] == "ReadTimeout"   # хорошая котировка жива
    r = next(r for r in d.rows(INS) if r["base"] == "ESPORTS")
    assert d.extra(r, time.time())["cost"] is not None
    d.apply(dict(t=time.time(), px={}, info={}, q={("56", ESP): dict(t=time.time(), err="нет ликвидности на клип")},
                 full_info=False, err=None))
    assert d.extra(r, time.time())["cost"] is None                                  # ответ по существу — заменяет
    d.q.pop(("56", BTCB))
    d.apply(dict(t=time.time(), px={}, info={}, q={("56", BTCB): dict(t=time.time(), err="RuntimeError")}, full_info=False, err=None))
    assert d.q[("56", BTCB)]["t"] == 0.0 and d.plan(time.time())["quotes"][0] == ("56", BTCB)    # повтор — первым


def test_review_offline_alpha_token_is_not_picked_when_a_live_one_exists():
    spots = {"binance_spot": dict(markets={"XUSDT": ["X", True]}, coins={},
                                  alpha=[{"s": "X", "n": "Old X", "a": "0x" + "01" * 20, "c": "56", "id": "A1", "off": True},
                                         {"s": "X", "n": "New X", "a": "0x" + "02" * 20, "c": "56", "id": "A2", "off": False}])}
    R = Resolver(spots, {"binance": {"XUSDT": {"legs": [L("binance_alpha", "XUSDT")], "dex": {}}}})
    assert identity.dex_tokens(R, "binance", "XUSDT") == [("56", "0x" + "02" * 20, False)]


def test_review_okx_health_age_counts_from_start_not_epoch():
    from funding_bot.okxdex import OkxDex
    h = OkxDex(key="k", secret="s", passphrase="p").health()
    assert time.time() - h["last_ok_ts"] < 60


def test_new_legs_from_dex_rows_trigger_completeness_at_once(tmp_path):
    """Владелец 12.09: «!» у новых ног (Aster BONER, SHROOM… — спот только на DEX) — «устранить причину»: бэкфилл
    глубины назначался лишь у ближайшего досбора через ≤15 мин. Новые ноги — проверка полноты сразу."""
    from fakes import make_world
    from funding_bot.collector import Collector
    col = Collector(clients=make_world(), db_path=tmp_path / "c.db", table_path=tmp_path / "t.json", background=False)
    col.step_universe()
    col._set_dex_rows([])
    col._repair_due = False
    row = dict(key="okxdex:4663:0xb0|aster:ONLYAUSDT", perp_ex="aster", perp="ONLYAUSDT", spot_ex="okxdex", spot="4663:0xb0")
    col._set_dex_rows([row])
    assert col._repair_due and ("aster", "ONLYAUSDT") in col.legs()
    col._repair_due = False
    col._set_dex_rows([row])                                                         # те же ноги — повторно не назначается
    assert not col._repair_due


def test_snapshot_restore_keeps_quotes_across_restart():
    """Владелец 12.09: «сделай чтоб не падал» — рестарт обнулял котировки клипа, «Комиссия» DEX-строк была «—» ~40 мин."""
    d = _leg()
    d.cl.liq = {("56", ESP): 116_000.0}; d.cl.price = {("56", ESP): 0.0109}
    d.apply(d.job(d.plan(time.time())))
    d.track("k", 0.01, 123.0)
    import json
    snap = json.loads(json.dumps(d.snapshot()))                                     # как через БД
    d2 = _leg()
    d2.restore(snap)
    r = d2.rows(INS)[0]
    assert d2.extra(r, time.time())["cost"] == d.extra(r, time.time())["cost"] is not None
    assert d2.book(r["spot"]) == d.book(r["spot"]) and d2.ema["k"] == (0.01, 123.0)
    d3 = _leg(); d3.restore({"q": {"bad": 1}})                                       # кривой снимок — с пустого
    assert d3.q == {} and d3.info == {}


def test_collector_restart_restores_dex_state(tmp_path):
    from fakes import make_world
    from funding_bot import db
    from funding_bot.collector import Collector
    w = make_world(); w["okxdex"] = FakeOkx()
    col = Collector(clients=w, db_path=tmp_path / "c.db", table_path=tmp_path / "t.json", background=False)
    col.dex.q = {("56", ESP): dict(t=time.time(), rt=0.004, gas=0.045, tax=0.0, err=None)}
    db.save_ident_src(col.con, "okxdex", {"#state": col.dex.snapshot()})
    w2 = make_world(); w2["okxdex"] = FakeOkx()
    col2 = Collector(clients=w2, db_path=tmp_path / "c.db", table_path=tmp_path / "t.json", background=False)
    assert col2.dex.q[("56", ESP)]["rt"] == 0.004


def test_collector_dex_rows_end_to_end(tmp_path):
    from fakes import make_world
    from funding_bot.collector import Collector
    from test_client_collector import _collector, _ident, _by
    w = make_world()
    addr = "0x" + "ab" * 20
    w["binance_spot"].coins_blob["coins"]["ABC"] = coin_record("Abc Coin", [("ETH", addr, True, True), ("BSC", addr, True, True)], "ABC")
    ok = FakeOkx(); ok.liq = {("56", addr): 500_000.0}; ok.price = {("56", addr): 10.0}
    w["okxdex"] = ok
    col, t = _collector(tmp_path, w)
    col.once(); _ident(col, t)
    tbl = _ident(col, t)
    assert "okxdex" in tbl["spot_venues"]
    sf = _by(tbl["sf_rows"])
    keys = {k for k in sf if k.startswith("okxdex:")}
    assert f"okxdex:56:{addr}|binance:ABCUSDT" in keys and f"okxdex:56:{addr}|aster:ABCUSDT" in keys
    r = sf[f"okxdex:56:{addr}|binance:ABCUSDT"]
    assert r["spot_label"] == "okx·bsc" and r["fee"] is not None and not r["stale"] and abs(r["gap"]) < 0.01
    assert tbl["health"]["okxdex"]["stale_s"] == config.OKX_DEX_STALE_S
    assert ("binance", "ABCUSDT") in col.legs()
    # ловушка 10.09: токен Alpha «A Meme Coin» доказан только для Aster MEME; перп Binance MEME — Memecoin («не тот»)
    meme = ("4663", "0x" + "38" * 20)
    assert meme in [(c, a) for c, a, *_ in col.dex.cand[("aster", "MEMEUSDT")]]
    assert all((c, a) != meme for c, a, *_ in col.dex.cand.get(("binance", "MEMEUSDT"), []))


# --- 12.09: DEX-строки всем перпам монеты (владелец: «не вижу пары с монетой ANSEM, хотя фандинг там интересный») ----
ANSEM = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
WRONG = "0x" + "e1" * 20
_i = lambda sym, base, f=1.0, cls="crypto": dict(symbol=sym, base=base, factor=f, cls=cls)
ANS_INS = {"aster": {"ANSEMUSDT": _i("ANSEMUSDT", "ANSEM"), "FOOUSDT": _i("FOOUSDT", "FOO")},
           "binance": {"ANSEMUSDT": _i("ANSEMUSDT", "ANSEM")},
           "hyperliquid": {"para:ANSEM": _i("para:ANSEM", "ANSEM"), "xyz:ANSEM": _i("xyz:ANSEM", "ANSEM", cls="equity")},
           "gate": {"ANSEM_USDT": _i("ANSEM_USDT", "ANSEM"), "FOO_USDT": _i("FOO_USDT", "FOO")},
           "lighter": {"ANSEM": _i("ANSEM", "ANSEM"), "FOO": _i("FOO", "FOO")},
           "lighter_rh": {"1000ANSEM": _i("1000ANSEM", "ANSEM", 1000.0)}}


def _ansem():
    """Живой срез 12.09: токен ANSEM на Solana доказан индексом Aster (Gate ANSEM_USDT) и спотом Gate того же кода у перпа
    Gate. У HIP-3 para:ANSEM описание с чужим индексом (имя не совпало), Lighter — имя из tokenlist (у RH — 1000ANSEM);
    перп Binance ANSEM — другая монета (контракт на BSC, другое имя); xyz:ANSEM — акция; FOO — токена на сетях DEX нет."""
    spots = {"gate_spot": dict(markets={"ANSEM_USDT": ["ANSEM", True], "FOO_USDT": ["FOO", True]},
                               coins={"ANSEM": coin_record("Blknoiz", [("SOL", ANSEM, True, True)], "ANSEM"),
                                      "FOO": coin_record("Foo", [("ETH", "0x" + "f0" * 20, True, True)], "FOO")}),
             "binance_spot": dict(markets={"ANSEMUSDT": ["ANSEM", True]},
                                  coins={"ANSEM": coin_record("Wrong Coin", [("BSC", WRONG, True, True)], "ANSEM")})}
    legs = {"aster": {"ANSEMUSDT": {"legs": [L("gateio", "ANSEM_USDT")], "dex": {}}},
            "binance": {"ANSEMUSDT": {"legs": [L("binance", "ANSEMUSDT")], "dex": {}}}}
    ann = {"para:ANSEM": "Tracks the price of Ansem Classic (ANSEM), index from OKX, Bybit and MEXC"}
    names = {"lighter": {"ANSEM": "Blknoiz", "FOO": "Foo"}, "lighter_rh": {"1000ANSEM": "Blknoiz"}}
    return Resolver(spots, legs, hl_ann=ann, perp_names=names)


def test_dex_token_is_offered_to_every_perp_of_the_coin_except_other():
    R = _ansem()
    own = {t[:2] for v, m in ANS_INS.items() for s, i in m.items() if i["cls"] == "crypto"
           for t in identity.dex_tokens(R, v, s)}
    d = dexleg.DexLeg(FakeOkx())
    d.set_candidates(R, ANS_INS)
    got = {p: {(c, a): (dd or {}).get("ident", "own") for c, a, _b, dd in lst} for p, lst in d.cand.items()}
    sol, bsc = ("501", ANSEM), ("56", WRONG)
    assert got[("aster", "ANSEMUSDT")] == {sol: "own"} and got[("gate", "ANSEM_USDT")] == {sol: "own"}   # как раньше
    assert got[("binance", "ANSEMUSDT")] == {bsc: "own"}                  # «не тот» против токена ANSEM — строки нет
    assert got[("hyperliquid", "para:ANSEM")] == {sol: "unknown", bsc: "unknown"}   # доказательств нет ни в одну сторону
    assert got[("lighter", "ANSEM")] == {sol: "same"}                     # то же имя; токен Binance — у «не того» перпа
    assert got[("lighter_rh", "1000ANSEM")] == {sol: "same"}
    assert ("hyperliquid", "xyz:ANSEM") not in d.cand                     # акция — не монета
    assert not any(p[1].startswith("FOO") for p in d.cand)                # у FOO доказанного токена нет — ничего
    assert set(d.tokens()) == own == {sol, bsc}                           # токены (и запросы OKX) — прежние


def test_ansem_like_rows_unknown_mark_same_by_name_and_multiplier():
    d = dexleg.DexLeg(FakeOkx())
    d.set_candidates(_ansem(), ANS_INS)
    sol = ("501", ANSEM)
    d.cl.liq = {sol: 2_000_000.0, ("56", WRONG): 10_000.0}; d.cl.price = {sol: 0.004, ("56", WRONG): 1.0}
    d.cl.names = {sol: {"name": "Blknoiz", "symbol": "ANSEM"}}
    d.apply(d.job(d.plan(time.time())))
    rows = {r["perp_ex"] + ":" + r["perp"]: r for r in d.rows(ANS_INS)}
    assert set(rows) == {"aster:ANSEMUSDT", "gate:ANSEM_USDT", "hyperliquid:para:ANSEM", "lighter:ANSEM",
                         "lighter_rh:1000ANSEM"}                          # BSC-токен тоньше порога — строк нет
    assert (rows["aster:ANSEMUSDT"]["ident"], rows["aster:ANSEMUSDT"]["ident_ev"]) == ("same", "contract")
    hl = rows["hyperliquid:para:ANSEM"]
    ex = d.extra(hl, time.time())
    row = calc.build_sf_row(hl, {"interval_h": 1}, {"rate": 0.00026, "mark": 0.004}, None, d.book(hl["spot"]), {}, 0, dex=ex)
    assert row["ident"] == "unknown" and not row["mismatch"] and row["spot_label"] == "okx·sol"   # страница: «?»
    assert "aster ANSEMUSDT" in row["ident_why"] and "не проверена" in row["ident_why"]
    rh = rows["lighter_rh:1000ANSEM"]
    assert rh["ident"] == "same" and rh["ident_ev"] == "dex_link:name" and rh["perp_factor"] == 1000.0
    row = calc.build_sf_row(rh, {"interval_h": 1}, {"rate": 0.0001, "mark": 4.0}, None, d.book(rh["spot"]), {}, 0,
                            dex=d.extra(rh, time.time()))
    assert abs(row["gap"]) < 1e-9 and row["ident_why"] is None             # 1000 токенов = 1 контракт; «тот» — без пометки


def test_bridge_mark_follows_the_token_to_other_perps():
    """BTCB на BSC доказан для Binance BTCUSDT (индекс — KuCoin BTC, родная сеть btc): мост и у перпа Lighter BTC."""
    R = _R()
    R.perp_names["lighter"] = {"BTC": "Bitcoin"}
    d = dexleg.DexLeg(FakeOkx())
    d.set_candidates(R, dict(INS, lighter={"BTC": _i("BTC", "BTC")}))
    (c, a, br, dd), = d.cand[("lighter", "BTC")]
    assert (c, a) == ("56", BTCB) and br and dd["ident"] == "same"
    d.cl.liq = {("56", BTCB): 9_000_000.0}; d.cl.price = {("56", BTCB): 60_000.0}
    d.cl.names = {("56", BTCB): {"name": "BTCB Token", "symbol": "BTCB"}}
    d.apply(d.job(d.plan(time.time())))
    r = next(r for r in d.rows(dict(INS, lighter={"BTC": _i("BTC", "BTC")})) if r["perp_ex"] == "lighter")
    assert r["dex"]["bridged"] and r["spot_tag"] == "bsc·BTCB"


def _links(R, ins):
    d = dexleg.DexLeg(FakeOkx())
    d.set_candidates(R, ins)
    return d, {p: {(c, a): ((dd or {}).get("ident", "own"), (dd or {}).get("ident_ev")) for c, a, _b, dd in lst}
               for p, lst in d.cand.items()}


def test_review_link_peers_are_the_whole_coin_group_not_only_token_sources():
    """Ревью 12.09 (ловушка MEME): Binance MEME — Memecoin с контрактом только в Ethereum, DEX-токенов не даёт; Aster MEME —
    Alpha «A Meme Coin» в Robinhood. Lighter «Memecoin» с Binance «тот», а Binance против Aster «не тот» — токен Aster
    Lighter получать не должен (раньше сверстниками считались только перпы-источники токенов — строка «?» была)."""
    rh = "0x" + "38" * 20
    spots = {"binance_spot": dict(markets={"MEMEUSDT": ["MEME", True]},
                                  coins={"MEME": coin_record("Memecoin", [("ETH", "0x" + "b1" * 20, True, True)], "MEME")},
                                  alpha=[{"s": "MEME", "n": "A Meme Coin", "a": rh, "c": "4663", "id": "A1"}])}
    legs = {"aster": {"MEMEUSDT": {"legs": [L("binance_alpha", "MEMEUSDT")], "dex": {}}},
            "binance": {"MEMEUSDT": {"legs": [L("binance", "MEMEUSDT")], "dex": {}}}}
    R = Resolver(spots, legs, perp_names={"lighter": {"MEME": "Memecoin"}, "lighter_rh": {"MEME": "A Meme Coin"}})
    ins = {"aster": {"MEMEUSDT": _i("MEMEUSDT", "MEME")}, "binance": {"MEMEUSDT": _i("MEMEUSDT", "MEME")},
           "lighter": {"MEME": _i("MEME", "MEME")}, "lighter_rh": {"MEME": _i("MEME", "MEME")}}
    d, got = _links(R, ins)
    assert identity.decide_ff(R, dict(va="binance", sa="MEMEUSDT", vb="lighter", sb="MEME"))["ident"] == "same"
    assert ("lighter", "MEME") not in d.cand and ("binance", "MEMEUSDT") not in d.cand
    assert got[("lighter_rh", "MEME")] == {("4663", rh): ("same", "dex_link:name")}    # то же имя, что у токена Aster
    assert d.tokens() == [("4663", rh)]


_UP_SOL = "UPsoL1111111111111111111111111111111111111"
_UP_BSC = "0x" + "c3" * 20


def test_review_link_by_ticker_only_name_is_unknown_not_same():
    """Ревью 12.09 (ловушка UP): KuCoin пишет имя монеты тикером («UP»), у Lighter имя рынка «UP», у HIP-3 «price of UP (UP)».
    decide_ff даёт «тот» по имени, но это совпадение тикеров — связь DEX-токена «?», а не «тот» без пометки. Настоящее имя
    (не тикер) — по-прежнему «тот» (ANSEM «Blknoiz» — тест выше)."""
    spots = {"kucoin_spot": dict(markets={"UP-USDT": ["UP", True]},
                                 coins={"UP": coin_record("UP", [("SOL", _UP_SOL, True, True)], "UP")})}
    R = Resolver(spots, {"binance": {}}, hl_ann={"flx:UP": "Tracks the price of UP (UP)."}, perp_names={"lighter": {"UP": "Up"}})
    ins = {"kucoin": {"UPUSDTM": _i("UPUSDTM", "UP")}, "lighter": {"UP": _i("UP", "UP")},
           "hyperliquid": {"flx:UP": _i("flx:UP", "UP")}}
    d, got = _links(R, ins)
    sol = ("501", _UP_SOL)
    assert got[("kucoin", "UPUSDTM")] == {sol: ("own", None)}
    assert got[("lighter", "UP")] == {sol: ("unknown", "dex_link:name_ticker")}
    assert got[("hyperliquid", "flx:UP")] == {sol: ("unknown", "dex_link:name_ticker")}
    d.cl.liq = {sol: 1_000_000.0}; d.cl.price = {sol: 1.0}
    d.apply(d.job(d.plan(time.time())))
    r = next(r for r in d.rows(ins) if r["perp_ex"] == "lighter")
    row = calc.build_sf_row(r, {"interval_h": 1}, {"rate": 0.0001, "mark": 1.0}, None, d.book(r["spot"]), {}, 0,
                            dex=d.extra(r, time.time()))
    assert row["ident"] == "unknown" and "тикер" in row["ident_why"]                 # страница: «?» с причиной


def test_review_link_contracts_disjoint_ticker_names_is_not_bridge_same():
    """KuCoin UP (имя «UP», Solana) и Gate UP (имя «UP», BSC): контракты разные, имена — лишь тикеры. decide_ff даёт
    «тот» (name_bridged), но токен одной биржи другой перп получает только с «?»."""
    spots = {"kucoin_spot": dict(markets={"UP-USDT": ["UP", True]},
                                 coins={"UP": coin_record("UP", [("SOL", _UP_SOL, True, True)], "UP")}),
             "gate_spot": dict(markets={"UP_USDT": ["UP", True]},
                               coins={"UP": coin_record("UP", [("BSC", _UP_BSC, True, True)], "UP")})}
    R = Resolver(spots, {"binance": {}})
    _d, got = _links(R, {"kucoin": {"UPUSDTM": _i("UPUSDTM", "UP")}, "gate": {"UP_USDT": _i("UP_USDT", "UP")}})
    assert got[("kucoin", "UPUSDTM")][("56", _UP_BSC)] == ("unknown", "dex_link:name_ticker")
    assert got[("gate", "UP_USDT")][("501", _UP_SOL)] == ("unknown", "dex_link:name_ticker")


def test_review_link_same_contradicted_by_a_peer_is_unknown():
    """Gate X: спот Gate с тем же кодом — «Alpha X» (как у токена Alpha перпа Aster), но индекс Gate берёт OKX X, как и
    индекс Binance X, а Binance X — «Other X» с другим контрактом, «не тот» против Aster. Свидетельства спорят — «?»."""
    tok = "0x" + "a1" * 20
    spots = {"gate_spot": dict(markets={"X_USDT": ["X", True]}, coins={"X": coin_record("Alpha X", [], "X")}),
             "binance_spot": dict(markets={"XUSDT": ["X", True]},
                                  coins={"X": coin_record("Other X", [("ETH", "0x" + "0e" * 20, True, True)], "X")},
                                  alpha=[{"s": "X", "n": "Alpha X", "a": tok, "c": "56", "id": "A1"}])}
    legs = {"aster": {"XUSDT": {"legs": [L("binance_alpha", "XUSDT")], "dex": {}}},
            "binance": {"XUSDT": {"legs": [L("okex", "X-USDT"), L("binance", "XUSDT")], "dex": {}}},
            "gate": {"X_USDT": {"legs": [L("okex", "X-USDT")], "dex": {}}}}
    R = Resolver(spots, legs)
    ins = {"aster": {"XUSDT": _i("XUSDT", "X")}, "binance": {"XUSDT": _i("XUSDT", "X")}, "gate": {"X_USDT": _i("X_USDT", "X")}}
    ff = lambda a, b: identity.decide_ff(R, dict(va=a[0], sa=a[1], vb=b[0], sb=b[1]))["ident"]
    assert ff(("aster", "XUSDT"), ("gate", "X_USDT")) == "same" and ff(("binance", "XUSDT"), ("gate", "X_USDT")) == "same"
    assert ff(("aster", "XUSDT"), ("binance", "XUSDT")) == "other"
    _d, got = _links(R, ins)
    assert got[("gate", "X_USDT")] == {("56", tok): ("unknown", "dex_link:conflict")}
    assert ("binance", "XUSDT") not in got


def test_review_link_excludes_non_crypto_classes():
    """Акция, сырьё, rwa Lighter (без токена в tokenlist) с той же базой — не монета: строк DEX нет, даже с тем же именем."""
    R = _ansem()
    R.perp_names["lighter"] = {"ANSEM": "Blknoiz"}
    ins = dict(ANS_INS, lighter={"ANSEM": _i("ANSEM", "ANSEM", cls="rwa")},
               hyperliquid={"xyz:ANSEM": _i("xyz:ANSEM", "ANSEM", cls="equity"), "flx:ANSEM": _i("flx:ANSEM", "ANSEM", cls="commodity")})
    d, _got = _links(R, ins)
    assert not any(p[0] in ("lighter", "hyperliquid") for p in d.cand)


def test_dex_link_failure_keeps_own_tokens(monkeypatch):
    def boom(*a, **kw):
        raise KeyError("x")
    monkeypatch.setattr(identity, "dex_links", boom)
    d = dexleg.DexLeg(FakeOkx())
    d.set_candidates(_ansem(), ANS_INS)
    assert d.cand[("aster", "ANSEMUSDT")] == [("501", ANSEM, False, None)] and ("lighter", "ANSEM") not in d.cand


# --- рейтинг надёжности A/B/C DEX-строк (владелец 13.09) -----------------------------------------------------------
def _all_liquid(d):
    for t in d.tokens():
        d.info[t] = dict(liq=1e9)


def _rel(r):
    return r["ident"], r["ident_ev"], r.get("rel"), r.get("rel_ev"), r.get("rel_d")


def test_own_dex_token_rating_is_the_class_of_its_direct_index_record():
    """ident_ev своей DEX-строки — по-прежнему «contract» (его читает трейдер), сила — rel: контракт монеты рынка из индекса
    A, токен Alpha и код той же биржи B, пул по поиску тикера C (AIW3)."""
    d = _leg()                                                            # _R(): ESPORTS — Alpha, BTC — нога KuCoin
    assert d.own_rel == {("binance", "ESPORTSUSDT"): {("56", ESP): ("B", "alpha", None)},
                         ("binance", "BTCUSDT"): {("56", BTCB): ("A", "leg", None)}}
    assert d.cand == {("binance", "ESPORTSUSDT"): [("56", ESP, False, None)],
                      ("binance", "BTCUSDT"): [("56", BTCB, True, None)]}          # кортежи cand — прежние
    _all_liquid(d)
    assert {r["base"]: _rel(r) for r in d.rows(INS)} == {"ESPORTS": ("same", "contract", "B", "alpha", None),
                                                         "BTC": ("same", "contract", "A", "leg", None)}
    # AIW3 (сделка DQA9Q): пул индекса Aster найден поиском по тикеру
    from test_identity import _aiw3_world, AIW3
    ins = {"aster": {"AIW3USDT": _i("AIW3USDT", "AIW3")}}
    d = dexleg.DexLeg(FakeOkx())
    d.set_candidates(_aiw3_world(), ins)
    assert d.cand == {("aster", "AIW3USDT"): [("56", AIW3, False, None)]}
    _all_liquid(d)
    (r,) = d.rows(ins)
    assert _rel(r) == ("same", "contract", "C", "pool", "pancakeswap AIW3-USDT")
    row = calc.build_sf_row(r, {"interval_h": 8}, {"rate": 0.0001, "mark": 1.0}, None, None, {}, 0, dex=d.extra(r, time.time()))
    assert (row["ident_ev"], row["rel"], row["rel_ev"], row["rel_d"]) == ("contract", "C", "pool", "pancakeswap AIW3-USDT")


def test_linked_dex_token_rating_is_the_weakest_link():
    """ANSEM: свой токен Aster — нога gateio (A), свой токен перпа Gate — код своего спота (B); Lighter получает токен по
    одному имени рынка — C «имя» при любом классе токена; HIP-3 para:ANSEM «не проверено» — без буквы."""
    d = dexleg.DexLeg(FakeOkx())
    d.set_candidates(_ansem(), ANS_INS)
    sol = ("501", ANSEM)
    assert d.own_rel[("aster", "ANSEMUSDT")] == {sol: ("A", "leg", None)}
    assert d.own_rel[("gate", "ANSEM_USDT")] == {sol: ("B", "code", None)}
    _all_liquid(d)
    by = {(r["perp_ex"], r["perp"], r["spot"]): r for r in d.rows(ANS_INS)}
    S = f"501:{ANSEM}"
    assert _rel(by[("aster", "ANSEMUSDT", S)]) == ("same", "contract", "A", "leg", None)
    assert _rel(by[("gate", "ANSEM_USDT", S)]) == ("same", "contract", "B", "code", None)
    assert _rel(by[("binance", "ANSEMUSDT", f"56:{WRONG}")]) == ("same", "contract", "A", "leg", None)
    assert _rel(by[("lighter", "ANSEM", S)]) == ("same", "dex_link:name", "C", "name", "Blknoiz")
    assert _rel(by[("lighter_rh", "1000ANSEM", S)])[2:4] == ("C", "name")
    assert _rel(by[("hyperliquid", "para:ANSEM", S)])[0] == "unknown" and _rel(by[("hyperliquid", "para:ANSEM", S)])[2:] == (None,) * 3
    row = calc.build_sf_row(by[("hyperliquid", "para:ANSEM", S)], {"interval_h": 1}, {"rate": 0.0001, "mark": 1.0}, None,
                            None, {}, 0, dex=d.extra(by[("hyperliquid", "para:ANSEM", S)], time.time()))
    assert not {"rel", "rel_ev", "rel_d"} & set(row)                               # у «?» ключей нет
    # связь через общий рынок обоих индексов (A) и свой токен Alpha (B) — слабейшее звено: B
    tok = "0x" + "a7" * 20
    spots = {"gate_spot": dict(markets={"Q_USDT": ["Q", True]}, coins={"Q": coin_record("Qcoin", [], "Q")}),
             "binance_spot": dict(markets={"BTCUSDT": ["BTC", True]}, coins={},
                                  alpha=[{"s": "Q", "n": "Qcoin", "a": tok, "c": "56", "id": "A1"}])}
    legs = {"aster": {"QUSDT": {"legs": [L("binance_alpha", "QUSDT"), L("gateio", "Q_USDT")], "dex": {}}},
            "binance": {"QUSDT": {"legs": [L("gateio", "Q_USDT")], "dex": {}}}}
    ins = {"aster": {"QUSDT": _i("QUSDT", "Q")}, "binance": {"QUSDT": _i("QUSDT", "Q")}}
    d = dexleg.DexLeg(FakeOkx())
    d.set_candidates(Resolver(spots, legs), ins)
    _all_liquid(d)
    by = {r["perp_ex"]: r for r in d.rows(ins)}
    assert _rel(by["aster"]) == ("same", "contract", "B", "alpha", None)
    assert _rel(by["binance"]) == ("same", "dex_link:shared_leg", "B", "alpha", None)


def test_dex_link_failure_keeps_own_token_rating(monkeypatch):
    def boom(*a, **kw):
        raise KeyError("x")
    monkeypatch.setattr(identity, "dex_links", boom)
    d = dexleg.DexLeg(FakeOkx())
    d.set_candidates(_ansem(), ANS_INS)
    _all_liquid(d)
    r = next(r for r in d.rows(ANS_INS) if r["perp_ex"] == "aster")
    assert _rel(r) == ("same", "contract", "A", "leg", None)
