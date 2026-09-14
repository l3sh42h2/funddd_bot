"""Фаза 1.1 по ревью 13.09: InstrumentSpec — одна структура инструмента сделки. Спецификация и её отпечаток (хеш
только по идентичности), m контракта Aster из baseAsset exchangeInfo, колонка deals.inst_json (идемпотентная миграция,
прежний код с новой схемой), запись спецификации на входе и в каждом намерении, бэкфилл сделок до фазы 1 (DQA9Q —
m = 1 по отношению цен журнала), запреты для неподтверждённого инструмента (R6). m ≠ 1 в 1.1 — по-прежнему отказ.
Фейки — из test_trade_engine, живая сделка DQA9Q — из test_marks (без сети и ключей)."""
import json, sqlite3, time
from dataclasses import replace
from decimal import Decimal as D

import pytest

from funding_bot import cabinet
from funding_bot.trade import engine as eng, marks, reconcile, store
from funding_bot.trade.store import DealState, IntentStatus
from funding_bot.trade.types import InstrumentSpec, PerpInstrument

import test_marks as tm
import test_trade_engine as fx

OLD_INSERT = ("INSERT INTO deals(id, created, state, reason, coin, chain, token, token_dec, perp_venue, symbol, "
              "leg_usd, owner_json, sim, carry, dust, updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")   # код до фазы 1


def _spec(**kw) -> InstrumentSpec:
    base = dict(chain="bsc", token=fx.TOKEN, token_dec=18, perp_venue="aster", perp_symbol=fx.SYMBOL,
                units_per_contract=D(1), perp_base_asset="AIW3", quote_asset="USDT", contract_type="PERPETUAL",
                period_h=D(1), ident_ev="contract:bsc", source="exchangeInfo:baseAsset", verified=True,
                verified_ts=1.5, px_ratio=D("1.0032"))
    return InstrumentSpec(**{**base, **kw})


def _deal_inst(con, did) -> InstrumentSpec:
    return InstrumentSpec.from_json(store.get_deal(con, did)["inst_json"])


def _spec_of(con, iid) -> dict:
    return json.loads(store.get_intent(con, iid)["spec_json"])


# ==== 1. модель ====================================================================================================
def test_spec_roundtrip_and_hash_only_by_identity():
    s = _spec()
    assert InstrumentSpec.from_json(s.to_json()) == s
    assert s.as_dict()["units_per_contract"] == "1" and s.as_dict()["verified"] is True
    assert isinstance(json.loads(store.jdump({"i": s.as_dict()}))["i"]["verified_ts"], float)
    # форма Decimal хеш не меняет
    h1000 = {replace(s, units_per_contract=x).inst_hash() for x in (D("1000"), D("1E+3"), D("1000.0"))}
    assert len(h1000) == 1
    # идентичность меняет
    for kw in (dict(token="0x" + "b2" * 20), dict(perp_symbol="AIW3USDC"), dict(token_dec=9),
               dict(units_per_contract=D(1000)), dict(chain="eth"), dict(perp_venue="binance"),
               dict(spot_units_per_token=D(2))):
        assert replace(s, **kw).inst_hash() != s.inst_hash(), kw
    # справочное — нет: бэкфилл и свежая спецификация того же контракта дают один отпечаток
    for kw in (dict(source="migration:verified_ratio"), dict(verified=False), dict(perp_base_asset=None),
               dict(period_h=D(8)), dict(ident_ev=None), dict(px_ratio=None), dict(why="x"), dict(verified_ts=None),
               dict(quote_asset=None), dict(contract_type=None)):
        assert replace(s, **kw).inst_hash() == s.inst_hash(), kw
    assert len(s.inst_hash()) == 16


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(schema=2), lambda d: d.pop("perp_symbol"), lambda d: d.update(units_per_contract="abc"),
    lambda d: d.update(units_per_contract="0"), lambda d: d.update(token_dec=None), lambda d: d.update(px_ratio="NaN")])
def test_spec_from_json_refuses_unknown_or_broken(mutate):
    d = _spec().as_dict()
    mutate(d)
    with pytest.raises(ValueError):
        InstrumentSpec.from_json(json.dumps(d))
    with pytest.raises(ValueError):
        InstrumentSpec.from_json("{нет")


def test_spec_unit_helpers_for_m_1000():
    s = _spec(units_per_contract=D(1000))
    assert s.m == 1000
    assert s.contracts_for(D("2444.6"), D(1)) == 2 and s.tokens_of(D(2)) == 2000
    assert s.px_per_token(D("40.9")) == D("0.0409")


# ==== 2. m у Aster — из baseAsset exchangeInfo ========================================================================
def test_aster_instrument_from_exchange_info():
    pytest.importorskip("eth_account")
    import test_trade_aster as ta
    from funding_bot.trade.aster_trade import AsterError, AsterTrade
    fake = ta.FakeAster()
    rows = [dict(ta.AIW3_EI, baseAsset="AIW3", quoteAsset="USDT", contractType="PERPETUAL"),
            dict(ta.AIW3_EI, symbol="1000BONKUSDT", baseAsset="1000BONK", quoteAsset="USDT"),
            dict(ta.AIW3_EI, symbol="NOBASEUSDT")]
    fake.script[("GET", "/fapi/v3/exchangeInfo")] = [(200, {"symbols": rows})]
    t = AsterTrade(session=fake)                                     # dry: публичное чтение
    a = t.instrument("AIW3USDT")
    assert a == PerpInstrument("AIW3USDT", "AIW3", "AIW3", D(1), "USDT", "PERPETUAL")
    b = t.instrument("1000BONKUSDT")
    assert (b.base_asset, b.base, b.m) == ("1000BONK", "BONK", D(1000)) and isinstance(b.m, D)
    n = t.instrument("NOBASEUSDT")
    assert n.base is None and n.m is None, "baseAsset нет — m не известен (вход откажет)"
    assert fake.n("GET", "/fapi/v3/exchangeInfo") == 1, "тот же кэш, что у фильтров"
    with pytest.raises(AsterError):
        t.instrument("ZZZUSDT")


def test_sim_perp_passes_instrument_through():
    from funding_bot.trade.sim import SimPerp
    assert SimPerp(fx.PerpPub()).instrument(fx.SYMBOL).m == 1

    class NoInst:
        venue = "aster"
    assert SimPerp(NoInst()).instrument(fx.SYMBOL) is None


# ==== 3. колонка deals.inst_json: миграция добавлением, прежний код работает ============================================
def test_schema_migration_is_idempotent_and_old_code_still_works(tmp_path):
    path = tmp_path / "trade.db"
    con = store.connect(path)
    con.execute("ALTER TABLE deals DROP COLUMN inst_json")                 # БД прежней версии
    store._COLS.pop("deals", None)
    store._cols(con, "deals")                                              # кэш без колонки — как в живом процессе
    assert "inst_json" not in store._COLS["deals"]
    con.execute(OLD_INSERT, ("DOLD1", 1.0, "DRAFT", None, "AIW3", "bsc", fx.TOKEN, 18, "aster", fx.SYMBOL, "10", "{}",
                             1, "0", "0", 1.0))
    con2 = store.connect(path)                                             # новый код: колонка добавлена
    cols = [r[1] for r in con2.execute("PRAGMA table_info(deals)")]
    assert cols[-1] == "inst_json" and "deals" not in store._COLS, "кэш колонок сброшен"
    assert store.get_deal(con2, "DOLD1")["inst_json"] is None
    assert store.set_deal_inst(con2, "DOLD1", _spec().to_json()) is True
    assert store.set_deal_inst(con2, "DOLD1", "{}") is False, "бэкфилл только в пустую колонку"
    assert store.set_deal_state(con2, "DOLD1", DealState.DRAFT, inst_json=store.get_deal(con2, "DOLD1")["inst_json"])
    store.connect(path).close()                                            # третий connect — ничего не делает
    assert [r[1] for r in con2.execute("PRAGMA table_info(deals)")] == cols
    # прежний код на новой схеме (откат на .prev): INSERT с явным списком колонок, SELECT * — лишний ключ None
    con2.execute(OLD_INSERT, ("DOLD2", 2.0, "DRAFT", None, "X", "bsc", "0x" + "b2" * 20, 18, "aster", "XUSDT", "10",
                              "{}", 1, "0", "0", 2.0))
    row = dict(con2.execute("SELECT * FROM deals WHERE id='DOLD2'").fetchone())
    assert row["inst_json"] is None and row["coin"] == "X"
    assert store.set_deal_state(con2, "DOLD2", DealState.ABORTED, expect=DealState.DRAFT)
    con.close(), con2.close()


def test_ensure_column_race_with_another_process():
    """Второй процесс успел ALTER между PRAGMA и нашим ALTER — «duplicate column» не ошибка; иное — ошибка."""
    class Con:
        def __init__(self, err):
            self.err = err

        def execute(self, sql):
            if sql.startswith("PRAGMA"):
                return []
            raise sqlite3.OperationalError(self.err)
    store._COLS["deals"] = frozenset({"id"})
    store._ensure_column(Con("duplicate column name: inst_json"), "deals", "inst_json", "TEXT")
    assert "deals" not in store._COLS
    with pytest.raises(sqlite3.OperationalError):
        store._ensure_column(Con("database is locked"), "deals", "inst_json", "TEXT")


# ==== 4-5. DQA9Q: бэкфилл m = 1 и подхват без ручных действий ============================================================
def _dqa9q_env(tmp_path, *, old_db: bool = True):
    """Живая сделка DQA9Q (4 902.151 AIW3 за 200 USDT, шорт 4 902 по 0.04093) на «боевых» фейках; old_db — трейд.db
    прежней версии (без колонки) открывается новым кодом."""
    e = fx.live_env(tmp_path)
    tm.dqa9q(e.con)
    # Accounting fixture has no credentials/config. Execution fixture must freeze
    # its synthetic wallet at construction; production JSON is never rewritten.
    e.con.execute('UPDATE deals SET owner_json=? WHERE id=?', (e.loader().frozen_json(), 'DQA9Q'))
    row = dict(e.con.execute('SELECT * FROM perp_orders').fetchone())
    e.perp._signed_ok = lambda *a, **kw: dict(
        clientOrderId=row['client_id'], symbol=row['symbol'], side=row['side'], origQty=row['qty'],
        price=row['price'], reduceOnly=bool(row['reduce_only']), orderId=row['order_id'],
        executedQty=row['executed_qty'], cumQuote=row['cum_quote'])
    if old_db:
        e.con.execute("ALTER TABLE deals DROP COLUMN inst_json")
        store._COLS.pop("deals", None)
        store._cols(e.con, "deals")
        store.connect(tmp_path / "trade.db").close()
    e.perp.pos = D(-4902)
    e.spot.bal[fx.TOKEN] = tm.Q_RAW
    return e


def test_dqa9q_backfill_on_startup_writes_verified_m1_once(tmp_path):
    e = _dqa9q_env(tmp_path)
    assert store.get_deal(e.con, "DQA9Q")["inst_json"] is None
    rep = reconcile.startup(e.con, e.legs, now=tm.NOW)
    inst = _deal_inst(e.con, "DQA9Q")
    assert inst.m == 1 and inst.verified and inst.source == "migration:verified_ratio" and inst.why is None
    assert abs(inst.px_ratio - D("1.003225")) < D("0.000001"), inst.px_ratio      # 0.04093 / 0.0407984
    assert (inst.token, inst.perp_symbol, inst.perp_base_asset, inst.quote_asset) == (fx.TOKEN, fx.SYMBOL, "AIW3",
                                                                                         "USDT")
    assert [d for d, _ in rep.inst_backfill] == ["DQA9Q"]
    evs = [ev for ev in store.events(e.con, "DQA9Q") if ev["kind"] == "inst_backfill"]
    assert len(evs) == 1 and json.loads(evs[0]["json"])["source"] == "migration:verified_ratio"
    raw = store.get_deal(e.con, "DQA9Q")["inst_json"]
    rep2 = reconcile.startup(e.con, e.legs, now=tm.NOW + 60)
    assert rep2.inst_backfill == [] and store.get_deal(e.con, "DQA9Q")["inst_json"] == raw
    assert sum(ev["kind"] == "inst_backfill" for ev in store.events(e.con, "DQA9Q")) == 1
    bk = eng.deal_book(e.con, "DQA9Q")
    assert bk.known and bk.m == 1 and bk.inst_ok and bk.inst_why is None
    assert store.get_deal(e.con, "DQA9Q")["state"] == DealState.OPEN
    # тот же отпечаток, что дала бы свежая спецификация входа по бирже (справочные поля в хеш не входят)
    assert inst.inst_hash() == _spec(period_h=D(1)).inst_hash()


def test_dqa9q_is_picked_up_without_manual_steps_full_exit(tmp_path):
    e = _dqa9q_env(tmp_path)
    reconcile.startup(e.con, e.legs, now=tm.NOW)
    from funding_bot.trade.adapters.execution_scope import prepare_active_accounts
    prepare_active_accounts(e.con, e.legs)
    deal = store.get_deal(e.con, "DQA9Q")
    chk = reconcile.check_deal(e.con, deal, e.legs_live)
    assert chk.matched is True and chk.hedged is True and chk.delta == D("0.151")
    rows, matched, problems = reconcile.positions(e.con, e.legs, now=time.time())
    assert matched is True and problems is None and [r["deal_id"] for r in rows] == ["DQA9Q"]
    assert rows[0]["delta_qty"] == D("0.151") and rows[0]["perp_qty"] == D(-4902)
    m = marks.mark_deal(e.con, deal, e.legs_live, now=time.time())
    assert m.pnl_now is not None and "errors" not in m.flags
    x = e.desk.propose_exit("DQA9Q", None, False, chat=fx.OWNER)
    assert "не подтверждён" not in x.html, "текст плана подтверждённой m = 1 сделки прежний"
    assert _spec_of(e.con, x.intent_id)["inst_hash"] == _deal_inst(e.con, "DQA9Q").inst_hash()
    fx.run_approved(e, x)
    assert store.get_deal(e.con, "DQA9Q")["state"] == DealState.CLOSED
    assert e.perp.pos == 0 and e.spot.bal[fx.TOKEN] == 0


def test_dqa9q_partial_exit_keeps_legs_even(tmp_path):
    e = _dqa9q_env(tmp_path)
    reconcile.startup(e.con, e.legs, now=tm.NOW)
    from funding_bot.trade.adapters.execution_scope import prepare_active_accounts
    prepare_active_accounts(e.con, e.legs)
    fx.run_approved(e, e.desk.propose_exit("DQA9Q", D(100), False, chat=fx.OWNER))
    bk = eng.deal_book(e.con, "DQA9Q")
    assert store.get_deal(e.con, "DQA9Q")["state"] == DealState.OPEN
    assert 0 < bk.tokens(18) < tm.Q_TOK and D(0) <= bk.delta(18) < fx.FILT.step and e.perp.pos == -bk.short


def test_dqa9q_before_startup_verdict_is_computed_on_the_fly_without_writing(tmp_path):
    """R5: до старта трейдера (CLI, «позиции») вердикт тот же, но в БД пишет только старт."""
    e = _dqa9q_env(tmp_path, old_db=False)
    bk = eng.deal_book(e.con, "DQA9Q")
    assert bk.inst_ok and bk.m == 1
    x = e.desk.propose_exit("DQA9Q", None, False, chat=fx.OWNER)
    assert _spec_of(e.con, x.intent_id)["instrument"]["source"] == "migration:verified_ratio"
    assert store.get_deal(e.con, "DQA9Q")["inst_json"] is None


# ==== 6. неподтверждённый инструмент сделки до фазы 1 (R6) =================================================================
def _partial_entry(tmp_path):
    """Вход 200 $ клипами по 50 $: «стоп» после первого свопа — хедж доведён, сделка PAUSED, вход PARTIAL."""
    e = fx.live_env(tmp_path, clip="50")
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(200), chat=fx.OWNER)
    e.spot.on_swap = lambda: store.set_paused(e.con2, True)
    fx.run_approved(e, p)
    store.set_paused(e.con, False)
    e.spot.on_swap = None
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.PAUSED
    assert store.get_intent(e.con, p.intent_id)["status"] == IntentStatus.PARTIAL
    return e, p


def _scale_sell_quotes(e, did, k):
    pre = f"fb-{did}-"
    for cid, cq in e.con.execute("SELECT client_id, cum_quote FROM perp_orders WHERE substr(client_id,1,?)=? AND "
                                 "side='SELL'", (len(pre), pre)).fetchall():
        e.con.execute("UPDATE perp_orders SET cum_quote=? WHERE client_id=?", (store.amt(D(cq) * k), cid))


def _legacy(e, did, variant):
    """Сделка «до фазы 1» (inst_json пуст), вердикт миграции которой не подтверждается."""
    e.con.execute("UPDATE deals SET inst_json=NULL WHERE id=?", (did,))
    if variant == "ratio":                     # цена контракта в журнале ×1000 цены токена
        _scale_sell_quotes(e, did, 1000)
    elif variant == "name":                    # множитель в имени символа
        e.con.execute("UPDATE deals SET symbol='1000AIW3USDT' WHERE id=?", (did,))
    else:                                      # SELL-заявок нет: голый лонг после сбоя
        pre = f"fb-{did}-"
        e.con.execute("DELETE FROM perp_orders WHERE substr(client_id,1,?)=?", (len(pre), pre))
        e.perp.pos = D(0)


@pytest.mark.parametrize("variant,why", [("ratio", "единицы не сходятся"), ("name", "множитель в имени"),
                                         ("naked", "нет исполненного шорта")])
def test_corrupted_bound_deal_cannot_close_or_grow(tmp_path, variant, why):
    e, p = _partial_entry(tmp_path)
    did = p.deal_id
    _legacy(e, did, variant)
    bk = eng.deal_book(e.con, did)
    assert bk.known and bk.m == 1 and not bk.inst_ok and why in bk.inst_why
    rep = reconcile.startup(e.con, e.legs, now=time.time())
    assert rep.inst_backfill == [] and store.get_deal(e.con, did)["inst_json"] is None, "неподтверждённый не пишется"
    assert store.get_deal(e.con, did)["state"] == DealState.PAUSED
    before = fx.sends(e)
    n_intents = e.con.execute("SELECT count(*) FROM intents").fetchone()[0]
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_resume(did, chat=fx.OWNER)
    assert "не подтверждён" in ei.value.html and "добор запрещён" in ei.value.html
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_exit(did, D(50), False, chat=fx.OWNER)
    assert "частичный выход не посчитать" in ei.value.html and f"«выход {did}» целиком" in ei.value.html
    if variant == "naked":
        with pytest.raises(eng.Refused) as ei:
            e.desk.propose_fix("rehedge", did, chat=fx.OWNER)
        assert "дохедж продажей запрещён" in ei.value.html
    assert fx.sends(e) == before and e.con.execute("SELECT count(*) FROM intents").fetchone()[0] == n_intents
    # сверка и «позиции» работают
    chk = reconcile.check_deal(e.con, store.get_deal(e.con, did), e.legs_live)
    assert chk.matched is True
    rows, _m, _p = reconcile.positions(e.con, e.legs, now=time.time())
    assert [r["deal_id"] for r in rows] == [did]
    # A post-binding mutation is not genuine legacy evidence. Even a disposal
    # proposal must fail before touching either independently scoped account.
    if variant == "naked":
        prop = e.desk.propose_fix("undo", did, chat=fx.OWNER)
    else:
        prop = e.desk.propose_exit(did, None, False, chat=fx.OWNER)
        assert "⚠️ Инструмент сделки не подтверждён: " in fx.flat(prop.html), "⚠️ в плане неподтверждённой сделки"
        assert fx.html_ok(prop.html) and not fx.RAW_NUM.search(prop.html)
    fx.run_approved(e, prop)
    assert fx.sends(e) == before
    assert store.get_intent(e.con, prop.intent_id)['status'] == IntentStatus.FAILED
    assert store.get_deal(e.con, did)['state'] != DealState.CLOSED


def test_broken_inst_json_is_unverified_with_reason(tmp_path):
    e = _dqa9q_env(tmp_path, old_db=False)
    e.con.execute("UPDATE deals SET inst_json='{\"schema\": 9}' WHERE id='DQA9Q'")
    inst = eng.deal_instrument(e.con, store.get_deal(e.con, "DQA9Q"))
    assert not inst.verified and "не читается" in inst.why and inst.m == 1
    assert not eng.deal_book(e.con, "DQA9Q").inst_ok
    with pytest.raises(eng.Refused):
        e.desk.propose_exit("DQA9Q", D(50), False, chat=fx.OWNER)


# ==== страховка исполнителя: одобрено раньше, а инструмент сделки не подтверждён ===========================================
def _unverify(e, did):
    e.con.execute("UPDATE deals SET inst_json=NULL WHERE id=?", (did,))
    _scale_sell_quotes(e, did, 1000)


def _shrink_short(e, did, k):
    """Шорт сделки меньше на k (журнал первой SELL-заявки и позиция биржи) — голый лонг k: дохедж продажей."""
    pre = f"fb-{did}-"
    cid, q = e.con.execute("SELECT client_id, executed_qty FROM perp_orders WHERE substr(client_id,1,?)=? AND "
                           "side='SELL' ORDER BY id LIMIT 1", (len(pre), pre)).fetchone()
    e.con.execute("UPDATE perp_orders SET executed_qty=? WHERE client_id=?", (store.amt(D(q) - k), cid))
    e.perp.pos += k


def test_approved_sell_rehedge_pauses_before_send_when_instrument_unverified(tmp_path):
    e = fx.live_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(200), chat=fx.OWNER)
    fx.run_approved(e, p)
    _shrink_short(e, p.deal_id, 10)
    fix = e.desk.propose_fix("rehedge", p.deal_id, chat=fx.OWNER)
    assert _spec_of(e.con, fix.intent_id)["side"] == "SELL"
    _unverify(e, p.deal_id)
    before = fx.sends(e)
    fx.run_approved(e, fix)
    assert fx.sends(e) == before
    assert 'счёт исполнения не подтверждён' in store.get_intent(e.con, fix.intent_id)['err']
    assert store.get_intent(e.con, fix.intent_id)["status"] == IntentStatus.FAILED
    assert cabinet.reason_text("inst_unverified") == "инструмент сделки не подтверждён"


@pytest.mark.parametrize("second_layer", [False, True])
def test_approved_partial_exit_sends_nothing_when_instrument_unverified(tmp_path, monkeypatch, second_layer):
    e = fx.live_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(200), chat=fx.OWNER)
    fx.run_approved(e, p)
    x = e.desk.propose_exit(p.deal_id, D(100), False, chat=fx.OWNER)
    _unverify(e, p.deal_id)
    if second_layer:                           # перекотировка пропустила — ловит сам исполнитель
        monkeypatch.setattr(e.desk, "replan", lambda it, deal: x.plan)
    before, appr = fx.sends(e), list(e.spot.approvals)
    fx.run_approved(e, x)
    assert fx.sends(e) == before and e.spot.approvals == appr
    assert store.get_intent(e.con, x.intent_id)["status"] == IntentStatus.FAILED
    assert 'счёт исполнения не подтверждён' in store.get_intent(e.con, x.intent_id)['err']


def test_approved_entry_more_pauses_before_send_when_instrument_unverified(tmp_path):
    e, p = _partial_entry(tmp_path)
    more = e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    sp = _spec_of(e.con, more.intent_id)
    assert sp["resume"] and sp["inst_hash"] == _deal_inst(e.con, p.deal_id).inst_hash()
    _unverify(e, p.deal_id)
    before = fx.sends(e)
    fx.run_approved(e, more)
    assert fx.sends(e) == before
    d = store.get_deal(e.con, p.deal_id)
    assert d["state"] == DealState.PAUSED and d["reason"] == "stop"


# ==== 7. вход пишет спецификацию; каждое намерение несёт отпечаток =======================================================
def test_entry_writes_instrument_spec_and_every_intent_carries_its_hash(tmp_path):
    e = fx.live_env(tmp_path)
    e.desk.table_loader = lambda: dict(fx.TABLE, sf_rows=[dict(fx.TABLE["sf_rows"][0], ident_ev="contract:bsc")])
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(200), chat=fx.OWNER)
    inst = _deal_inst(e.con, p.deal_id)
    assert inst.verified and inst.source == "exchangeInfo:baseAsset" and inst.m == 1
    assert (inst.perp_base_asset, inst.contract_type, inst.ident_ev) == ("AIW3", "PERPETUAL", "contract:bsc")
    assert abs(inst.px_ratio - 1) < D("0.01") and inst.token == fx.TOKEN and inst.token_dec == 18
    sp = _spec_of(e.con, p.intent_id)
    assert sp["inst_hash"] == p.plan.inputs["inst_hash"] == inst.inst_hash()
    assert sp["instrument"] == json.loads(store.get_deal(e.con, p.deal_id)["inst_json"])
    fx.run_approved(e, p)
    for prop in (e.desk.propose_exit(p.deal_id, D(50), False, chat=fx.OWNER),
                 e.desk.propose_exit(p.deal_id, None, True, chat=fx.OWNER)):
        assert _spec_of(e.con, prop.intent_id)["inst_hash"] == inst.inst_hash()
    _shrink_short(e, p.deal_id, 5)
    fix = e.desk.propose_fix("rehedge", p.deal_id, chat=fx.OWNER)
    assert _spec_of(e.con, fix.intent_id)["inst_hash"] == inst.inst_hash()


def test_sim_entry_writes_instrument_through_sim_perp(tmp_path):
    e = fx.sim_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=fx.OWNER)
    assert _deal_inst(e.con, p.deal_id).m == 1


def test_requote_of_draft_keeps_instrument_or_refuses(tmp_path):
    e = fx.live_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(100), chat=fx.OWNER)
    raw = store.get_deal(e.con, p.deal_id)["inst_json"]
    again = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(100), chat=fx.OWNER, deal_id=p.deal_id)
    assert again.deal_id == p.deal_id and store.get_deal(e.con, p.deal_id)["inst_json"] == raw
    other = _spec(token="0x" + "b2" * 20).to_json()
    e.con.execute("UPDATE deals SET inst_json=? WHERE id=?", (other, p.deal_id))
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(100), chat=fx.OWNER, deal_id=p.deal_id)
    assert "у кнопки другой" in ei.value.html
    d = store.get_deal(e.con, p.deal_id)
    assert d["state"] == DealState.DRAFT and d["inst_json"] == other, "черновик не тронут"
    e.con.execute("UPDATE deals SET inst_json=NULL WHERE id=?", (p.deal_id,))
    e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(100), chat=fx.OWNER, deal_id=p.deal_id)
    assert _deal_inst(e.con, p.deal_id).inst_hash() == InstrumentSpec.from_json(raw).inst_hash(), "пустой — дописан"
    assert fx.sends(e) == (0, 0)


# ==== 8. m не известен — отказ до свопа ================================================================================
def _raise(s):
    raise RuntimeError("exchangeInfo 503")


@pytest.mark.parametrize("fn", [lambda s: None, _raise, None,
                                lambda s: PerpInstrument(s, None, None, None, "USDT", "PERPETUAL")])
def test_unknown_multiplier_is_refused_before_any_send(tmp_path, fn):
    e = fx.live_env(tmp_path)
    e.perp.instrument = fn
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(100), chat=fx.OWNER)
    assert "Множитель контракта AIW3USDT на Aster не известен" in ei.value.html
    assert fx.sends(e) == (0, 0) and e.spot.approvals == []
    assert e.con.execute("SELECT count(*) FROM deals").fetchone()[0] == 0


# ==== 9. m ≠ 1 в шаге 1.1 — по-прежнему отказ ===========================================================================
def test_multiplier_by_name_is_still_refused(tmp_path):
    e = fx.live_env(tmp_path)
    e.desk.table_loader = lambda: dict(fx.TABLE, sf_rows=[dict(fx.TABLE["sf_rows"][0], perp="1000AIW3USDT")])
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(100), chat=fx.OWNER)
    assert "1 контракт = 1 токен" in ei.value.html
    assert fx.sends(e) == (0, 0)


@pytest.mark.parametrize("pi,text", [
    (PerpInstrument(fx.SYMBOL, "1000AIW3", "AIW3", D(1000), "USDT", "PERPETUAL"), "расходятся"),
    (PerpInstrument(fx.SYMBOL, "BTC", "BTC", D(1), "USDT", "PERPETUAL"), "другой актив")])
def test_exchange_contradicting_the_name_is_refused(tmp_path, pi, text):
    e = fx.live_env(tmp_path)
    e.perp.instrument = lambda s: pi
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(100), chat=fx.OWNER)
    assert text in ei.value.html
    assert fx.sends(e) == (0, 0)


def test_unit_refusal_returns_m_and_keeps_other_asset_always_refused():
    assert eng.unit_refusal("AIW3", "AIW3USDT") == 1 and eng.unit_refusal("AIW3", "AIW3USDT", m=D(1)) == 1
    assert eng.unit_refusal("BONK", "1000BONKUSDT", allow_multiplier=True) == 1000     # параметр есть; в 1.1 — никто
    for coin, sym in (("BONK", "1000BONKUSDT"), ("PEPE", "KPEPEUSDT"), ("BABYDOGE", "1MBABYDOGEUSDT")):
        with pytest.raises(eng.Refused) as ei:
            eng.unit_refusal(coin, sym)
        assert "1 контракт = 1 токен" in ei.value.html
    with pytest.raises(eng.Refused) as ei:
        eng.unit_refusal("AIW3", "BTCUSDT", allow_multiplier=True)
    assert "другой актив" in ei.value.html, "разрешение на множитель чужую базу не снимает"
    with pytest.raises(eng.Refused) as ei:
        eng.unit_refusal("AIW3", "AIW3USDT", m=D(1000), allow_multiplier=True)
    assert "расходятся" in ei.value.html
