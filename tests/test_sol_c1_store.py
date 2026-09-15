"""Шаг C1 связки SOL×HL: trade.db схемы 2 (ТЗ §6, §12; приёмка M03, M05, M06, G04, U14/U16 на уровне БД).

- миграция — одной транзакцией со строкой schema_version, повтор ничего не меняет, сбой посередине не оставляет
  половины схемы;
- ворота: код не стартует на БД, которой нужен читатель новее (откат кода поверх новой БД);
- БД «как живая» (DQA9Q, построена кодом выкачанного релиза) мигрирует без изменения строк и денежных единиц,
  отпечаток DQA9Q прежний, выход DQA9Q после миграции — как раньше;
- прежний код (tests/data/legacy/store_phase1.py — store.py выкачанного релиза) работает на новой схеме;
- журналы Solana и Hyperliquid живут в store; одна незакрытая сделка на scope перпа; корневая цель операции
  неизменна, «в полёте» не становится нулём; журнал маршрутов и статьи расходов — только вставка."""
import importlib.util, json, sqlite3
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

import pytest

from funding_bot.trade import engine as eng, fees as F, hyperliquid_trade as HT, reconcile, store
from funding_bot.trade.hyperliquid_trade import HlJournal, connect_journal
from funding_bot.trade.solana import ANSEM_MINT, USDC_MINT
from funding_bot.trade.solana import journal as J
from funding_bot.trade.store import DealState, OpState
from funding_bot.trade.types import InstrumentSpec

import test_marks as tm
import test_sol_routes_router as rt
import test_trade_engine as fx
from test_sol_c1_instrument import ACCT, DQA9Q_HASH, _v2

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "tests" / "data" / "legacy" / "store_phase1.py"
LEGACY_TABLES = ("flags", "tg_updates", "deals", "intents", "clips", "dex_txs", "perp_orders", "perp_fills",
                 "funding_income", "exec_events", "deal_marks")
NEW_TABLES = {"schema_version", "operations", "operation_intents", "route_candidates", "fee_events", "sol_meta",
              "sol_tx_attempts", "sol_tx_evidence", "sol_receipts", "hl_nonces", "hl_order_attempts"}
AGENT = "0x" + "cd" * 20
MASTER = "0x" + "ab" * 20


def _old_store():
    """store.py выкачанного релиза как отдельный модуль того же пакета (его относительные импорты работают)."""
    spec = importlib.util.spec_from_file_location("funding_bot.trade._store_phase1", LEGACY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _objects(con) -> list:
    return sorted(tuple(r) for r in con.execute("SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master"))


def _dump(con, tables=LEGACY_TABLES) -> dict:
    return {t: [tuple(r) for r in con.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}


def _tables(con) -> set:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _live_db(path, monkeypatch):
    """trade.db «как на VPS»: построена кодом выкачанного релиза, DQA9Q открыта, inst_json записан бэкфиллом."""
    old = _old_store()
    con = old.connect(path)
    with monkeypatch.context() as m:
        m.setattr(tm, "store", old)
        deal = tm.dqa9q(con)
        # Original fixture omitted wallet configuration; an executable migrated
        # position must retain the wallet selected when it was opened.
        con.execute('UPDATE deals SET owner_json=? WHERE id=?',
                    (json.dumps({'values': {'wallets.bsc': fx.WALLET}}), 'DQA9Q'))
    assert old.set_deal_inst(con, "DQA9Q", eng.legacy_instrument(con, deal, now=tm.NOW).to_json())
    return old, con


def _sol_deal(con, spec, did, now=tm.T0):
    return store.create_deal(con, coin="ANSEM", chain=spec.chain, token=spec.token, token_dec=spec.token_dec,
                             perp_venue=spec.perp_venue, symbol=spec.perp_symbol, leg_usd=D(150), owner_json="{}",
                             sim=True, deal_id=did, now=now, inst=spec)


# ==== схема и миграция =================================================================================================
def test_fresh_db_is_current_schema_and_reconnect_changes_nothing(tmp_path):
    p = tmp_path / "trade.db"
    con = store.connect(p)
    info = store.schema_info(con)
    assert (info["version"], info["min_reader"]) == (store.SCHEMA_VERSION, store.MIN_READER) == (5, 2)
    assert NEW_TABLES | set(LEGACY_TABLES) <= _tables(con)
    assert [r[1] for r in con.execute("PRAGMA table_info(deals)")][-2:] == ["inst_json", "perp_scope"]
    before = _objects(con)
    store.connect(p).close()
    assert _objects(con) == before and store.schema_info(con)["updated"] == info["updated"]
    con.close()


def test_live_like_dqa9q_db_migrates_without_touching_rows(tmp_path, monkeypatch):
    p = tmp_path / "trade.db"
    old, oc = _live_db(p, monkeypatch)
    rows0 = _dump(oc)
    assert "schema_version" not in _tables(oc) and "perp_scope" not in {r[1] for r in oc.execute("PRAGMA table_info(deals)")}
    oc.close()

    con = store.connect(p)                                                  # новый код: миграция
    rows1 = _dump(con)
    for t in LEGACY_TABLES:
        if t == "deals":
            assert [r[:-1] for r in rows1[t]] == rows0[t] and all(r[-1] is None for r in rows1[t])
        else:
            assert rows1[t] == rows0[t], f"{t}: исполнения и суммы не переписаны"
    assert store.schema_info(con)["version"] == store.SCHEMA_VERSION and NEW_TABLES <= _tables(con)
    deal = store.get_deal(con, "DQA9Q")
    assert InstrumentSpec.from_json(deal["inst_json"]).inst_hash() == DQA9Q_HASH
    assert eng.deal_instrument(con, deal).inst_hash() == DQA9Q_HASH, "отпечаток DQA9Q не изменился"
    bk = eng.deal_book(con, "DQA9Q")
    assert bk.known and bk.m == 1 and bk.inst_ok and bk.tokens(18) == tm.Q_TOK and bk.short == 4902
    objs = _objects(con)
    store.connect(p).close()
    assert _objects(con) == objs, "повторная миграция ничего не делает"

    # прежний код (откат на .prev) на новой схеме: схему не трогает, BSC-сделки ведёт как раньше
    oc = old.connect(p)
    assert _objects(oc) == objs
    did = old.create_deal(oc, coin="X", chain="bsc", token="0x" + "B2" * 20, token_dec=18, perp_venue="aster",
                          symbol="XUSDT", leg_usd=D(10), owner_json="{}", sim=True, now=tm.T0 + 10)
    assert old.set_deal_state(oc, did, DealState.ENTERING)
    row = old.get_deal(oc, did)
    assert row["token"] == "0x" + "b2" * 20 and row["perp_scope"] is None and row["inst_json"] is None
    old.event(oc, "old_code", deal_id=did)
    old.add_mark(oc, "DQA9Q", tm.NOW, pnl_now=D(1))
    assert old.busy_intents(oc) == 0 and [d["id"] for d in old.active_deals(oc)] == ["DQA9Q", did]
    assert old.get_deal(oc, "DQA9Q")["inst_json"] == deal["inst_json"]
    oc.close()
    con2 = store.connect(p)                                                 # и снова новый код
    assert store.get_deal(con2, did)["state"] == DealState.ENTERING and store.schema_info(con2)["version"] == store.SCHEMA_VERSION
    con.close(), con2.close()


def test_dqa9q_after_migration_exits_as_before(tmp_path, monkeypatch):
    _, oc = _live_db(tmp_path / "trade.db", monkeypatch)
    oc.close()
    e = fx.live_env(tmp_path)                                               # новый код открывает живую БД
    e.perp.pos = D(-4902)
    e.spot.bal[fx.TOKEN] = tm.Q_RAW
    reconcile.startup(e.con, e.legs, now=tm.NOW)
    chk = reconcile.check_deal(e.con, store.get_deal(e.con, "DQA9Q"), e.legs_live)
    assert chk.matched is True and chk.hedged is True and chk.delta == D("0.151")
    from funding_bot.trade.adapters.execution_scope import bind_legacy
    row = dict(e.con.execute('SELECT * FROM perp_orders').fetchone())
    e.perp._signed_ok = lambda *a, **kw: dict(
        clientOrderId=row['client_id'], symbol=row['symbol'], side=row['side'], origQty=row['qty'],
        price=row['price'], reduceOnly=bool(row['reduce_only']), orderId=row['order_id'],
        executedQty=row['executed_qty'], cumQuote=row['cum_quote'])
    bind_legacy(e.con, store.get_deal(e.con, 'DQA9Q'), e.perp)
    x = e.desk.propose_exit("DQA9Q", None, False, chat=fx.OWNER)
    assert "не подтверждён" not in x.html
    assert json.loads(store.get_intent(e.con, x.intent_id)["spec_json"])["inst_hash"] == DQA9Q_HASH
    fx.run_approved(e, x)
    assert store.get_deal(e.con, "DQA9Q")["state"] == DealState.CLOSED and e.perp.pos == 0, e.hooks.reports


def test_migration_failure_leaves_no_half_schema(tmp_path, monkeypatch):
    p = tmp_path / "trade.db"
    _, oc = _live_db(p, monkeypatch)
    rows0 = _dump(oc)
    oc.close()
    good = store.SCHEMA_V2
    monkeypatch.setattr(store, "SCHEMA_V2", good + "\nCREATE TABLE broken(x INT REFERENCES);\n")
    with pytest.raises(sqlite3.OperationalError):
        store.connect(p)
    raw = sqlite3.connect(p)
    names = {r[0] for r in raw.execute("SELECT name FROM sqlite_master")}
    assert not (NEW_TABLES & names) and "deals_one_per_perp_scope" not in names, "ни одной таблицы схемы 2"
    after = _dump(raw)
    assert [r[:len(rows0["deals"][0])] for r in after["deals"]] == rows0["deals"]
    assert {t: v for t, v in after.items() if t != "deals"} == {t: v for t, v in rows0.items() if t != "deals"}
    raw.close()
    monkeypatch.setattr(store, "SCHEMA_V2", good)
    con = store.connect(p)                                                  # повтор той же миграции — целиком
    assert store.schema_info(con)["version"] == store.SCHEMA_VERSION and NEW_TABLES <= _tables(con)
    con.close()


# ==== ворота версии ====================================================================================================
def test_gate_refuses_db_that_needs_newer_reader(tmp_path):
    p = tmp_path / "trade.db"
    con = store.connect(p)
    tm.dqa9q(con)
    future = store.SCHEMA_VERSION + 1
    con.execute("UPDATE schema_version SET version=?, min_reader=?", (future, future))
    objs, rows = _objects(con), _dump(con)
    with pytest.raises(store.SchemaTooNew, match="не запускаюсь"):
        store.connect(p)
    with pytest.raises(store.SchemaTooNew):
        store.ensure_journal_tables(con, "sol", "hl")
    with pytest.raises(store.SchemaTooNew):
        J.ensure_schema(con)
    jc = connect_journal(p)
    with pytest.raises(store.SchemaTooNew):
        HlJournal(jc)
    jc.close()
    assert _objects(con) == objs and _dump(con) == rows, "БД не тронута"
    # новее, но совместимая (min_reader ≤ версии кода) — стартуем, версию не понижаем
    con.execute("UPDATE schema_version SET min_reader=2")
    store.connect(p).close()
    info = store.schema_info(con)
    assert (info["version"], info["min_reader"]) == (future, store.MIN_READER)
    con.close()


def test_require_reader_only_grows(tmp_path):
    con = store.connect(tmp_path / "trade.db")
    store.require_reader(con, 1)
    store.require_reader(con, 2)
    assert store.schema_info(con)["min_reader"] == 2
    for bad in (store.SCHEMA_VERSION+1, 0, True, "2"):
        with pytest.raises(ValueError):
            store.require_reader(con, bad)
    con.close()


# ==== сделки: mint с регистром и одна незакрытая сделка на scope перпа ==============================================
def test_one_open_deal_per_perp_scope_and_mint_case_kept(tmp_path):
    con = store.connect(tmp_path / "trade.db")
    s = _v2()
    _sol_deal(con, s, "DSOL1")
    row = store.get_deal(con, "DSOL1")
    assert row["token"] == ANSEM_MINT and row["chain"] == "solana" and row["perp_scope"] == s.perp_scope
    assert InstrumentSpec.from_json(row["inst_json"]) == s
    _sol_deal(con, s, "DSOL2")                                              # DRAFT scope не занимает
    assert store.set_deal_state(con, "DSOL1", DealState.ENTERING)
    with pytest.raises(store.StoreBusy):
        store.set_deal_state(con, "DSOL2", DealState.ENTERING)
    assert store.set_deal_state(con, "DSOL1", DealState.ABORTED)
    assert store.set_deal_state(con, "DSOL2", DealState.ENTERING)
    # сам индекс scope: тот же scope под другим символом — отказ (не только прежний индекс площадка+символ)
    con.execute("INSERT INTO deals(id, created, state, coin, chain, token, token_dec, perp_venue, symbol, leg_usd, "
                "owner_json, sim, carry, dust, updated, perp_scope) VALUES('DRAW', 1, 'DRAFT', 'X', 'solana', 'x', 6, "
                "'hyperliquid', 'para:OTHER', '1', '{}', 1, '0', '0', 1, ?)", (s.perp_scope,))
    with pytest.raises(store.StoreBusy):
        store.set_deal_state(con, "DRAW", DealState.ENTERING)
    # v1: другой счёт на том же рынке тоже закрыт — прежний индекс (площадка, символ) строже scope
    _sol_deal(con, replace(s, perp_account=ACCT + "2"), "DSOL3")
    with pytest.raises(store.StoreBusy):
        store.set_deal_state(con, "DSOL3", DealState.ENTERING)
    # строка сделки обязана совпасть со спецификацией; Solana без schema 2 не записать (регистр mint потерялся бы)
    with pytest.raises(store.StoreError, match="расходятся"):
        store.create_deal(con, coin="ANSEM", chain="solana", token=ANSEM_MINT.lower(), token_dec=6,
                          perp_venue="hyperliquid", symbol="para:ANSEM", leg_usd=D(1), owner_json="{}", sim=True, inst=s)
    for ch in ("sol", "solana", "Solana"):
        with pytest.raises(store.StoreError, match="schema 2"):
            store.create_deal(con, coin="ANSEM", chain=ch, token=ANSEM_MINT, token_dec=6, perp_venue="hyperliquid",
                              symbol="para:ANSEM", leg_usd=D(1), owner_json="{}", sim=True)
    # BSC — как раньше: адрес нижним регистром, scope нет
    did = store.create_deal(con, coin="AIW3", chain="bsc", token="0x" + "A1" * 20, token_dec=18, perp_venue="aster",
                            symbol="AIW3USDT", leg_usd=D(10), owner_json="{}", sim=True)
    r = store.get_deal(con, did)
    assert r["token"] == "0x" + "a1" * 20 and r["perp_scope"] is None
    con.close()


# ==== корневая цель операции ============================================================================================
def test_operation_root_target_immutable_and_unknown_not_zero(tmp_path):
    con = store.connect(tmp_path / "trade.db")
    s = _v2()
    _sol_deal(con, s, "DSOL1")
    kw = dict(deal_id="DSOL1", profile_id=s.profile_id, inst_hash=s.inst_hash(), mode="live")
    op = store.create_operation(con, **kw, side="exit", target_kind="token_raw_to_sell", target_asset=s.token,
                                target_decimals=6, target_raw=1_000_000_000, fee_cap_raw=500_000,
                                bounds={"slip_bps": 100}, now=1.0)
    r = store.get_operation(con, op)
    assert (r["state"], r["approval_version"], r["target_raw"], r["confirmed_raw"], r["reserved_raw"]) == (
        "PROPOSED", 0, "1000000000", "0", "0") and r["bounds_hash"].startswith("sha256:")
    assert store.set_operation_state(con, op, OpState.APPROVED) and store.get_operation(con, op)["approval_version"] == 1
    assert store.set_operation_state(con, op, OpState.RUNNING)
    assert store.link_intent(con, op, "XAAA1") == 1 and store.link_intent(con, op, "XAAA1") == 1
    assert store.operation_reserve(con, op, 400_000_000) == 600_000_000
    assert store.operation_settle(con, op, released_raw=400_000_000, executed_raw=400_000_000) == 600_000_000
    # «стоп» после 400: продолжение — та же цель, остаток 600 (U14), не повтор 1000
    assert store.set_operation_state(con, op, OpState.STOPPED)
    assert store.operation_remaining(store.get_operation(con, op)) == 600_000_000
    assert store.set_operation_state(con, op, OpState.APPROVED, bounds={"slip_bps": 80})
    r = store.get_operation(con, op)
    assert r["approval_version"] == 2 and json.loads(r["bounds_json"]) == {"slip_bps": 80}
    assert store.link_intent(con, op, "XAAA2") == 2 and store.operation_of_intent(con, "XAAA2")["id"] == op
    with pytest.raises(store.StoreError):
        store.link_intent(con, "OOTHER", "XAAA2")
    assert store.set_operation_state(con, op, OpState.RUNNING)
    # вторая активная операция той же сделки не одобряется
    op2 = store.create_operation(con, **kw, side="entry", target_kind="stable_raw_budget", target_asset=USDC_MINT,
                                 target_decimals=6, target_raw=150_000_000)
    with pytest.raises(store.StoreBusy):
        store.set_operation_state(con, op2, OpState.APPROVED)
    store.operation_reserve(con, op, 200_000_000)
    # исход неизвестен: итог не ставится, резерв держится, остаток не растёт (U16)
    with pytest.raises(store.StoreError, match="в полёте"):
        store.set_operation_state(con, op, OpState.CLOSED)
    assert store.set_operation_state(con, op, OpState.PAUSED_UNKNOWN)
    assert store.operation_remaining(store.get_operation(con, op)) == 400_000_000
    with pytest.raises(store.StoreError):
        store.operation_reserve(con, op, 1)                                 # новых отправок нет
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("UPDATE operations SET state='CLOSED' WHERE id=?", (op,))    # и в обход кода — триггер
    with pytest.raises(ValueError):
        store.operation_settle(con, op, released_raw=100, executed_raw=101)
    # доказано: из 200 продано 150 (возврат) — остаток 450, а не 400
    assert store.operation_settle(con, op, released_raw=200_000_000, executed_raw=150_000_000) == 450_000_000
    assert store.set_operation_state(con, op, OpState.RUNNING)
    with pytest.raises(store.StoreError, match="больше остатка"):
        store.operation_reserve(con, op, 450_000_001)
    store.operation_reserve(con, op, 450_000_000)
    store.operation_settle(con, op, released_raw=450_000_000, executed_raw=450_000_000)
    assert store.set_operation_state(con, op, OpState.CLOSED)
    r = store.get_operation(con, op)
    assert (r["target_raw"], r["confirmed_raw"], r["reserved_raw"]) == ("1000000000", "1000000000", "0")
    with pytest.raises(store.StoreError):
        store.operation_settle(con, op, released_raw=1, executed_raw=0)
    assert store.set_operation_state(con, op2, OpState.APPROVED), "после итога первой — вторая одобряется"
    # цель неизменна, строки не удаляются
    for sql in ("UPDATE operations SET target_raw='5'", "UPDATE operations SET target_asset='x'",
                "UPDATE operations SET fee_cap_raw='1'", "DELETE FROM operations", "DELETE FROM operation_intents"):
        with pytest.raises(sqlite3.DatabaseError):
            con.execute(sql)
    for bad in (dict(side="entry", target_kind="token_raw_to_sell"), dict(side="exit", target_raw=0),
                dict(side="exit", target_raw=1.5), dict(side="exit", mode="readonly"), dict(side="exit", target_raw=True)):
        a = dict(kw, target_kind="token_raw_to_sell", target_asset=s.token, target_decimals=6, target_raw=1)
        a.update(bad)
        with pytest.raises(ValueError):
            store.create_operation(con, **a)
    with pytest.raises(LookupError):
        store.create_operation(con, **dict(kw, deal_id="DNONE"), side="exit", target_kind="token_raw_to_sell",
                               target_asset=s.token, target_decimals=6, target_raw=1)
    con.close()


# ==== журнал маршрутов и статьи расходов =================================================================================
def _decision():
    r = rt.mkreq(amount=100_000_000)
    un = [rt.sr.Unavailable("jupiter", rt.J_ORDER, "no_transaction")]
    return rt.decide([rt.cand(rt.J_BUILD, 1_002_000_000, r, fees=[rt.usdc(350_000)]),
                      rt.cand(rt.OKX, 1_001_000_000, r, fees=[rt.usdc(30_000)])], r, unavailable=un)


def test_route_candidates_from_decision_records(tmp_path):
    con = store.connect(tmp_path / "trade.db")
    dec = _decision()
    rows = dec.records("OP1", 3)
    assert store.add_route_candidates(con, rows, now=5.0) == 3
    assert store.add_route_candidates(con, rows, now=6.0) == 0, "повтор той же записи безвреден"
    got = store.route_candidates(con, "OP1")
    assert [x["path"] for x in got if x["selected"]] == [rt.OKX]
    okx = next(x for x in got if x["path"] == rt.OKX)
    assert D(okx["metric"]) == dec.winner_ranked.metric and okx["provider_group"] == "okx" and okx["rank_no"] == 1
    assert json.loads(okx["fees_json"])[0]["amount_raw"] == "30000" and okx["clip_seq"] == 3 and okx["eligible"] == 1
    un = next(x for x in got if x["path"] == rt.J_ORDER)
    assert un["candidate_id"] is None and json.loads(un["reasons_json"]) == ["no_transaction"] and un["selected"] == 0
    build = next(r for r in rows if r["path"] == rt.J_BUILD)
    with pytest.raises(store.StoreError):                                   # второй «выбранный» клипа и раунда
        store.add_route_candidates(con, [dict(build, candidate_id="other", selected=True)])
    with pytest.raises(TypeError):                                          # сумма float — нельзя
        store.add_route_candidates(con, [dict(build, candidate_id="f1", amount_in_raw=1.5)])
    with pytest.raises(store.StoreError):
        store.add_route_candidates(con, [dict(build, candidate_id="f2", zzz=1)])
    secret = "ab" * 32
    store.add_route_candidates(con, [dict(build, candidate_id="n1", round_no=2,
                                          notes=[f"jupiter 200: https://api.jup.ag/swap/v2/order?api-key={secret}"])])
    assert secret not in next(x for x in store.route_candidates(con, "OP1") if x["candidate_id"] == "n1")["notes_json"]
    assert len(store.route_candidates(con, "OP1")) == 4
    for sql in ("DELETE FROM route_candidates", "UPDATE route_candidates SET selected=0"):
        with pytest.raises(sqlite3.DatabaseError):
            con.execute(sql)
    con.close()


def test_fee_events_unknown_is_null_and_facts_not_rewritten(tmp_path):
    con = store.connect(tmp_path / "trade.db")
    comps = F.receipt_components(fee_lamports=5_000, fee_payer=rt.W, tip_lamports=None, rent_deposits=[2_039_280])
    assert store.add_fee_events(con, origin_kind="sol_receipt", origin_ref="SIG/entry-1", components=comps,
                                deal_id="DSOL1", clip_id=7, now=1.0) == 3
    assert store.add_fee_events(con, origin_kind="sol_receipt", origin_ref="SIG/entry-1", components=comps) == 0
    got = store.fee_events(con, deal_id="DSOL1")
    assert [(r["kind"], r["amount_raw"], r["refundable"], r["idx"]) for r in got] == [
        (c.kind, None if c.amount_raw is None else str(c.amount_raw), int(c.refundable), i) for i, c in enumerate(comps)]
    assert got[1]["kind"] == "tip" and got[1]["amount_raw"] is None, "неизвестная сумма — NULL, не 0"
    assert got[2]["refundable"] == 1 and got[0]["refundable"] == 0, "возвратный депозит отдельно"
    with pytest.raises(store.StoreError, match="другой состав"):
        store.add_fee_events(con, origin_kind="sol_receipt", origin_ref="SIG/entry-1", components=comps[:2])
    with pytest.raises(ValueError):
        store.add_fee_events(con, origin_kind="sol_receipt", origin_ref="SIG/x",
                             components=[dict(comps[0].as_record(), amount_raw=1.5)])
    with pytest.raises(store.StoreError):
        store.add_fee_events(con, origin_kind="sol_receipt", origin_ref="SIG/y",
                             components=[dict(comps[0].as_record(), bogus=1)])
    hl = dict(kind="perp_fee", asset="USDC", decimals=6, amount_raw=67_500, payer=None, included=False, estimated=False,
              valuation={"unit": "USDC", "amount": D("0.0675"), "ts": 2.0, "source": "hl:userFillsByTime"})
    assert store.add_fee_events(con, origin_kind="hl_fill", origin_ref="mainnet/acct/para:ANSEM/1/tid-9",
                                components=[hl]) == 1
    h = store.fee_events(con, origin_kind="hl_fill")[0]
    assert (h["amount_raw"], h["val_amount"], h["val_ts"], h["val_source"]) == ("67500", "0.0675", 2.0,
                                                                                 "hl:userFillsByTime")
    for sql in ("UPDATE fee_events SET amount_raw='0'", "DELETE FROM fee_events"):
        with pytest.raises(sqlite3.DatabaseError):
            con.execute(sql)
    con.close()


# ==== журналы SOL/HL живут в store ===================================================================================
def test_journals_live_in_trade_db(tmp_path):
    p = tmp_path / "trade.db"
    con = store.connect(p)
    assert J.SCHEMA is store.SOL_JOURNAL_SCHEMA and HT.JOURNAL_SCHEMA is store.HL_JOURNAL_SCHEMA
    assert {str(s) for s in J.OPEN} == set(store.SOL_TX_OPEN_STATES)
    assert {str(s) for s in J.INFLIGHT} == set(store.SOL_TX_INFLIGHT_STATES)
    objs = _objects(con)
    J.ensure_schema(con)
    j = HlJournal(con)
    assert _objects(con) == objs, "таблицы журналов уже созданы миграцией trade.db"
    n = j.allocate_nonce("mainnet", AGENT, 1000)
    j.prepare(client_id="c1", cloid="0x" + "1" * 32, kind="order", network="mainnet", master=MASTER, account=MASTER,
              signer=AGENT, action_json="{}", action_hash="0x00", nonce=n, deal_id="DSOL1", intent_id="EAAA1", clip_id=7)
    row = con.execute("SELECT deal_id, intent_id, clip_id, state FROM hl_order_attempts WHERE client_id='c1'").fetchone()
    assert tuple(row) == ("DSOL1", "EAAA1", 7, "PREPARED")
    # отдельная БД журнала адаптера (как у потока 1): только таблицы журнала
    jc = connect_journal(tmp_path / "hl.db")
    assert HlJournal(jc).allocate_nonce("mainnet", AGENT, 5) == 5
    assert {"hl_nonces", "hl_order_attempts"} <= _tables(jc) and "deals" not in _tables(jc)
    jc.close()
    # таблица журнала HL первой версии (без ссылок движка) получает колонки
    raw = sqlite3.connect(tmp_path / "old_hl.db")
    raw.executescript(store.HL_JOURNAL_SCHEMA.replace(", deal_id TEXT, intent_id TEXT, clip_id INTEGER", ""))
    assert "deal_id" not in [r[1] for r in raw.execute("PRAGMA table_info(hl_order_attempts)")]
    store.ensure_journal_tables(raw, "hl")
    assert [r[1] for r in raw.execute("PRAGMA table_info(hl_order_attempts)")][-3:] == ["deal_id", "intent_id",
                                                                                         "clip_id"]
    raw.close()
    with pytest.raises(ValueError):
        store.ensure_journal_tables(con, "evm")
    con.close()
