"""Ревью 13.09 (шесть новых перп-бирж), подтверждённые находки — регрессии. Без сети.
1. Variational: комиссия 0, издержка — спред котировки RFQ → «Комиссия» строки = тейкер другой ноги ×2 + полный спред.
2. SPCX / CXMT / UNITREE / CBRS — акция, а не «до IPO» / «rwa» у edgeX, Backpack, ApeX (70 пар и 21 сделка терялись).
3. Проход бэкфилла не перечитывает 30 сут событий на КАЖДОМ тике.
4. Пакет edgeX продлевает курсор через новый расчёт (иначе все ноги edgeX — в посимвольный добор каждые 4 ч)."""
from __future__ import annotations
import logging, time
from funding_bot import apex, backpack, calc, config, dashboard, db, edgex, funding, universe
from fakes import make_world

H = 3600_000


def _ff(va, sa, vb, sb, base="PEPE", fa=1000.0, fb=1000.0):
    return dict(key=f"{va}:{sa}|{vb}:{sb}", base=base, cls="crypto", va=va, vb=vb, sa=sa, sb=sb, fa=fa, fb=fb)


# --- 1. Variational: издержка в спреде котировки ------------------------------------------------------------------------
def test_variational_leg_pays_its_quote_spread_in_the_fee_column():
    """Сценарий ревью: 1000PEPE Variational × Binance, котировка $1k 0.003401 / 0.003407 (17.6 бп). Было 0.10 %, реально
    круг ≈ 0.28 %: дороже круга тейкером Backpack × ApeX (0.20 %) — строка больше не сортируется самой дешёвой."""
    it = _ff("binance", "1000PEPEUSDT", "variational", "1000PEPE")
    prem = {"rate": 0.0001, "interval_h": 8}
    row = calc.build_ff_row(it, {"interval_h": 8}, {"interval_h": 8}, prem, prem,
                            {"bid": 0.0034035, "ask": 0.0034045}, {"bid": 0.003401, "ask": 0.003407}, {}, {}, 0)
    q = 0.000006 / 0.003404
    assert abs(row["fee_q"] - q) < 1e-15 and abs(row["fee"] - (2 * 0.0005 + q)) < 1e-15
    assert 0.0027 < row["fee"] < 0.0028
    bp_apex = calc.build_ff_row(_ff("backpack", "kPEPE_USDC_PERP", "apex", "1000PEPEUSDT"), None, None, prem, prem,
                                None, None, {}, {}, 0)
    assert row["fee"] > bp_apex["fee"] == 2 * (0.0005 + 0.0005) and "fee_q" not in bp_apex
    # книги нет (устарела) — спред из ставки клиента (котировка того же снимка); нет ни того, ни другого — «—»
    r2 = calc.build_ff_row(it, None, None, prem, dict(prem, spread_rt=0.00147), None, None, {}, {}, 0)
    assert abs(r2["fee"] - (0.001 + 0.00147)) < 1e-15
    r3 = calc.build_ff_row(it, None, None, prem, prem, None, None, {}, {}, 0)
    assert r3["fee"] is None and r3["fee_q"] is None
    assert calc.leg_cost("binance") == 0.001 and calc.leg_cost("variational") is None
    assert calc.leg_cost("variational", {"bid": 99.0, "ask": 101.0}) == 0.02


def test_spot_futures_deal_with_a_variational_perp_pays_the_quote_too():
    sf = dict(key="binance_spot:PEPEUSDT|variational:1000PEPE", base="PEPE", cls="crypto", spot_ex="binance_spot",
              spot="PEPEUSDT", spot_asset="PEPE", spot_factor=1.0, perp_ex="variational", perp="1000PEPE",
              perp_factor=1000.0)
    prem = {"rate": 0.0001, "interval_h": 8}
    row = calc.build_sf_row(sf, None, prem, {"bid": 0.003401, "ask": 0.003407}, None, {}, 0)
    q = 0.000006 / 0.003404
    assert abs(row["fee"] - (2 * 0.0010 + q)) < 1e-15 and abs(row["fee_q"] - q) < 1e-15
    assert calc.build_sf_row(sf, None, prem, None, None, {}, 0)["fee"] is None
    # DEX-спот «всё включено» + перп Variational: круг клипа + спред котировки перпа
    row = calc.build_sf_row(dict(sf, dex=True), None, prem, {"bid": 0.003401, "ask": 0.003407}, None, {}, 0,
                            dex={"cost": 0.004, "url": None})
    assert abs(row["fee"] - (0.004 + q)) < 1e-15
    plain = calc.build_sf_row(dict(sf, perp_ex="binance", perp="1000PEPEUSDT"), None, prem, None, None, {}, 0)
    assert abs(plain["fee"] - 2 * (0.0010 + 0.0005)) < 1e-15 and "fee_q" not in plain


def test_page_explains_the_variational_fee_cell():
    assert "function feeTip(r)" in dashboard.JS and "fee_q" in dashboard.JS
    assert dashboard.JS.count("${feeAttr(r)}>${pct(r.fee, 2)}") == 2          # ячейка «Комиссия» обеих таблиц
    assert config.FEES_TAKER["variational"] == 0.0 and config.QUOTE_COST_VENUES == {"variational"}


# --- 2. классы SPCX / CXMT / UNITREE / CBRS -----------------------------------------------------------------------------
def test_listed_equities_are_equity_on_the_three_venues():
    for t in ("SPCX", "CXMT", "UNITREE", "CBRS"):
        assert edgex.asset_class(t, True, False, False) == "equity"
        assert backpack.perp_class(f"{t}.US", "STOCK") == "equity"
        assert apex.asset_class("stockContract", t, "STOCK", f"{t} name") == "equity"
    for t in ("OPENAI", "ANTHROPIC"):                                          # частные на всех площадках — «до IPO»
        assert edgex.asset_class(t, True, False, False) == "preipo"
        assert backpack.perp_class(f"{t}.US", "STOCK") == "preipo"
        assert apex.asset_class("stockContract", t, "STOCK", t) == "preipo"
    assert not hasattr(apex, "UNLISTED")


def test_spcx_pairs_across_all_venues_that_list_it_as_a_share():
    e = lambda s: dict(symbol=s, base="SPCX", cls="equity", factor=1.0)
    perps = {"binance": [e("SPCXUSDT")], "hyperliquid": [e("xyz:SPCX")], "backpack": [e("SPCX.US_USDC_PERP")],
             "edgex": [e("SPCXUSDC")], "apex": [e("SPCXUSDT")],
             "bitget": [dict(symbol="SPCXUSDT", base="SPCX", cls="preipo", factor=1.0)]}    # старая Bitget — решение владельца
    ff = universe.build_ff(perps)
    assert len(ff) == 10 and {r["cls"] for r in ff} == {"equity"}                             # C(5, 2)
    spots = {"binance_spot": [dict(symbol="SPCXBUSDT", base="~SPCX", base_asset="SPCXB", factor=1.0, alt_base="SPCX")]}
    sf = {r["perp_ex"] for r in universe.build_sf(perps, spots)}
    assert sf == {"binance", "hyperliquid", "backpack", "edgex", "apex"}


def test_class_split_at_the_same_price_is_logged_once(tmp_path, caplog):
    e = lambda s, cls="equity", f=1.0: dict(symbol=s, base="SPCX" if "SPCX" in s else "QNT", cls=cls, factor=f)
    perps = {"aster": [e("SPCXUSDT"), e("QNTUSDT", "crypto")], "binance": [e("SPCXUSDT"), e("QNTUSDT", "crypto")],
             "hyperliquid": [e("xyz:SPCX", "preipo"), e("xyz:QNT")]}
    marks = {"aster": {"SPCXUSDT": 150.1, "QNTUSDT": 80.0}, "binance": {"SPCXUSDT": 150.2, "QNTUSDT": 80.1},
             "hyperliquid": {"xyz:SPCX": 150.0, "xyz:QNT": 31.0}}                               # Quantinuum ≠ Quant
    got = universe.class_splits(perps, marks)
    assert got == [dict(base="SPCX", venue="hyperliquid", symbol="xyz:SPCX", cls="preipo", major="equity", n_major=2)]
    marks["hyperliquid"]["xyz:SPCX"] = 1500.0                                                   # другая единица — не про класс
    assert universe.class_splits(perps, marks) == []
    # живой срез 13.09: BP — «до IPO» у KuCoin (0.490) и Gate (0.595), монета у Pacifica (0.547 ≈ их медиана): большинство
    # само расходится на 20 % — это не «та же цена», в журнал не идёт
    bp = {"kucoin": [dict(symbol="BPUSDTM", base="BP", cls="preipo", factor=1.0)],
          "gate": [dict(symbol="BP_USDT", base="BP", cls="preipo", factor=1.0)],
          "pacifica": [dict(symbol="BP", base="BP", cls="crypto", factor=1.0)]}
    assert universe.class_splits(bp, {"kucoin": {"BPUSDTM": 0.48964}, "gate": {"BP_USDT": 0.5954},
                                      "pacifica": {"BP": 0.54736}}) == []
    marks["hyperliquid"]["xyz:SPCX"] = 150.0
    from funding_bot.collector import Collector
    col = Collector(clients=make_world(), db_path=tmp_path / "c.db", table_path=tmp_path / "t.json", sleep=lambda s: None,
                    background=False)
    col.prem = {v: {s: {"mark": m} for s, m in ms.items()} for v, ms in marks.items()}
    with caplog.at_level(logging.WARNING, logger="funding_bot.collector"):
        col._log_class_splits(perps)
        col._log_class_splits(perps)                                                            # тот же — не повторяется
    msgs = [r.getMessage() for r in caplog.records if "класс рынка" in r.getMessage()]
    assert len(msgs) == 1 and "hyperliquid:xyz:SPCX preipo ≠ equity ×2" in msgs[0]


# --- 3. проход бэкфилла и перечитывание событий -------------------------------------------------------------------------
def test_backfill_pass_does_not_reload_events_on_every_tick(tmp_path, monkeypatch):
    """Было: прогресс каждой ноги ставил «грязно», и каждый тик прохода (до MAINT_DEEP_BUDGET_S = 300 с) перечитывал все
    события окна — на 14 площадках ~1.5 млн строк, 2–4 с на главном потоке. Теперь — не чаще раза в 60 с."""
    from funding_bot.collector import Collector
    t = [time.time()]
    w = make_world()
    col = Collector(clients=w, db_path=tmp_path / "c.db", table_path=tmp_path / "t.json", now=lambda: t[0],
                    sleep=lambda s: None, background=False)
    col.once()
    legs = [k for k in col.legs() if k[0] == "hyperliquid"] * 3
    assert len(legs) >= 12
    loads, orig = [0], db.funding_events_since

    def counting(*a, **k):
        loads[0] += 1
        return orig(*a, **k)
    monkeypatch.setattr(db, "funding_events_since", counting)
    col.refresh_events(force=True)
    loads[0] = 0
    h = w["hyperliquid"]
    hs = h.history_since

    def tick_between_legs(symbol, start_ms, end_ms=None):
        t[0] += 10                                     # тик главного потока (10 с) между ногами прохода
        col.refresh_events()
        return hs(symbol, start_ms, end_ms)
    h.history_since = tick_between_legs
    col._run_backfill(col.con, w, legs, col.intervals(), 30, force=True)
    assert col.backfill_state["done"] == len(legs)                         # счётчики страницы идут как прежде
    assert loads[0] <= len(legs) * 10 // 60 + 1, loads[0]                  # было: по перечитыванию на каждый тик


# --- 4. пакет edgeX продлевает курсоры через расчёт ---------------------------------------------------------------------
T = 1789243200000                      # 12.09.2026 20:00 UTC — расчёт edgeX (сетка 4 ч)
GRACE = config.SETTLE_GRACE_S * 1000
SYMS = [f"S{i:03d}USDC" for i in range(170)]                               # 170 контрактов, как живой пакет 12.09


def _rows(ft, prev=True):
    return [dict(exchange="edgex", symbol=s, funding_ms=ft, rate=1e-5, mark=None, **({"prev_ms": ft - 4 * H} if prev else {}))
            for s in SYMS]


def _need_repair(con, now_ms):
    ivs = {s: 4 for s in SYMS}
    nxt = {"edgex": {s: (now_ms // (4 * H) + 1) * 4 * H for s in SYMS}}
    comp = funding.completeness(db.funding_events_since(con, now_ms - 31 * 86400_000), [("edgex", s) for s in SYMS],
                                {"edgex": ivs}, now_ms, 720, nxt, depth=db.leg_depths(con), sync=db.leg_sync(con))
    return sum(1 for g in comp.values() if g["needs_repair"])


def _legs(con, cursor):
    for s in SYMS:
        db.set_leg_depth(con, "edgex", s, T - 30 * 86400_000, synced_to=cursor)


def test_edgex_batch_moves_every_cursor_across_the_settlement(tmp_path):
    ivs = {s: 4 for s in SYMS}
    # как было: курсоры после прошлого пакета (T − 5 мин − льгота); пакет T+10:30 пишет строки, но не двигает ни одного
    con0 = db.connect(tmp_path / "old.db")
    _legs(con0, T - 300_000 - GRACE)
    t0 = T + 630_000
    funding.apply_batch(con0, _rows(T, prev=False), ivs, t0)
    assert all(v < T for v in db.leg_sync(con0).values()) and _need_repair(con0, t0) == 170
    # теперь: строка несёт prev_ms — курсор, покрывший прошлый расчёт, идёт через новый
    con = db.connect(tmp_path / "new.db")
    _legs(con, T - 300_000 - GRACE)
    funding.apply_batch(con, _rows(T), ivs, t0)
    assert set(db.leg_sync(con).values()) == {t0 - GRACE} and _need_repair(con, t0) == 0
    # досборы каждые 15 мин до следующего расчёта: курсор не заходит за T + 4 ч (тот расчёт ещё не в пакете)
    t = t0
    while t < T + 4 * H + 900_000:
        t += 900_000
        funding.apply_batch(con, _rows(T), ivs, t)
    assert set(db.leg_sync(con).values()) == {T + 4 * H - 1}
    t1 = T + 4 * H + 630_000                                               # следующий расчёт появился в пакете
    assert _need_repair(con, t1) == 170                                    # до пакета — законно ждёт
    funding.apply_batch(con, _rows(T + 4 * H), ivs, t1)
    assert set(db.leg_sync(con).values()) == {t1 - GRACE} and _need_repair(con, t1) == 0


def test_edgex_batch_never_jumps_a_hole_or_a_shorter_interval(tmp_path):
    con = db.connect(tmp_path / "h.db")
    _legs(con, T - 8 * H + 60_000)                                         # расчёта 16:00 в БД нет
    t0 = T + 630_000
    funding.apply_batch(con, _rows(T), {s: 4 for s in SYMS}, t0)
    assert set(db.leg_sync(con).values()) == {T - 8 * H + 60_000} and _need_repair(con, t0) == 170
    con2 = db.connect(tmp_path / "i.db")
    _legs(con2, T - 4 * H + 60_000)
    funding.apply_batch(con2, _rows(T), {s: 1 for s in SYMS}, t0)          # коллектор уже видит 1 ч — строже
    assert set(db.leg_sync(con2).values()) == {T - 4 * H + 60_000}
