"""Шаг C2: движок связки SOL × HL на фейках сети (tests/sol_c2_world.py) — вход $150 одним клипом, полный выход,
возврат DEX, UNKNOWN свопа, UNKNOWN / частичная / отвергнутая IOC, «стоп», рестарт на каждой границе отправки
(без повторной покупки и двойного хеджа), объём пилота (честные отказы), BSC и SOL в одной БД."""
import json
from decimal import Decimal as D
import pytest
from funding_bot.trade import reconcile, sol_exec, store
from funding_bot.trade import sol_flow
from funding_bot.trade.engine import Refused, deal_book
from funding_bot.trade.solana import ANSEM_MINT
from funding_bot.trade.store import ClipState, DealState, IntentStatus, OpState, PerpOrderState
import sol_c2_world as W
from sol_c2_world import Crash, approve_run, enter, entry_cmd, make_world, restart

pytest.importorskip("solders")
pytest.importorskip("eth_account")


def deal_of(w, prop):
    return store.get_deal(w.con, prop.deal_id)


def op_of(w, iid):
    return store.operation_of_intent(w.con, iid)


def attempts(w):
    return [dict(r) for r in w.con.execute("SELECT attempt_id, state, signature, logical_action_id FROM "
                                           "sol_tx_attempts ORDER BY created")]


def orders(w, did):
    return [dict(r) for r in w.con.execute("SELECT client_id, side, state, qty, executed_qty, reduce_only FROM "
                                           "perp_orders WHERE client_id LIKE ? ORDER BY id", (f"fb-{did}-%",))]


def exit_deal(w, did):
    prop = w.desk.propose_exit(did, None, False, chat=None)
    approve_run(w, prop)
    return prop


# --- вход и полный выход ---------------------------------------------------------------------------------------
def test_entry_150_one_clip_hedge_only_after_finalized_receipt(tmp_path):
    w = make_world(tmp_path)
    prop = w.desk.propose_profile_entry(entry_cmd(), chat=None)
    assert "Вход ANSEM" in prop.html and "para:ANSEM" in prop.html and "Jupiter" in prop.html
    assert "только показ" in prop.html                        # Order виден, но не исполним
    approve_run(w, prop)
    d = deal_of(w, prop)
    assert d["state"] == DealState.OPEN, w.hooks.reports
    assert d["token"] == ANSEM_MINT and d["chain"] == "solana"           # mint с регистром (S01)
    bk = deal_book(w.con, d["id"])
    assert (bk.tokens_raw, bk.short) == (903_000_000, D(903))
    assert w.venue.pos == D(-903)
    # один своп, одна подпись; хедж — только после finalized чека (пилот: confirmation_trigger = finalized)
    at = attempts(w)
    assert len(at) == 1 and at[0]["state"] == "FINALIZED_OK" and len(w.sol.sends) == 1
    t = w.sol.txs[at[0]["signature"]]
    (side, sz, px, ro, beh, ts), = w.venue.calls
    assert (side, sz, ro) == ("SELL", D(903), False) and ts >= t["land_t"] + w.sol.FINALITY_S
    # корневая операция: бюджет исполнен фактическим списанием, в полёте 0
    op = op_of(w, prop.intent_id)
    assert (op["state"], op["target_raw"], op["confirmed_raw"], op["reserved_raw"]) == (
        "OPEN", "150000000", "150000000", "0")
    rc = store.route_candidates(w.con, op["id"])
    assert any(r["selected"] and r["path"] == "jupiter_build_v2" for r in rc if r["clip_seq"] == 1)
    assert {r["path"] for r in rc} >= {"jupiter_order_v2", "jupiter_build_v2", "okx_solana_v6"}
    fees = store.fee_events(w.con, deal_id=d["id"])
    assert [f["kind"] for f in fees] == ["network_total"] and fees[0]["amount_raw"] == "5020"
    hl = w.perp.journal.con.execute("SELECT deal_id, intent_id, clip_id, cloid FROM hl_order_attempts WHERE "
                                    "kind='order'").fetchall()
    assert len(hl) == 1 and hl[0][0] == d["id"] and hl[0][1] == prop.intent_id and hl[0][3].startswith("0x")  # H13
    lev = w.perp.journal.con.execute("SELECT kind, state FROM hl_order_attempts WHERE kind='updateLeverage'").fetchall()
    assert [tuple(r) for r in lev] == [("updateLeverage", "OK")] and w.venue.lev == 1     # isolated 1x до свопа (H09)
    assert "Вход ANSEM выполнен" in w.hooks.reports[-1] and "ноги ровно" in w.hooks.reports[-1]


def test_full_exit_sells_deal_tokens_and_closes_short(tmp_path):
    w = make_world(tmp_path)
    did = enter(w).deal_id
    w.sol.ansem += 50_000_000                                   # свои токены владельца в кошельке — не сделки (S15)
    prop = exit_deal(w, did)
    d = store.get_deal(w.con, did)
    assert d["state"] == DealState.CLOSED, w.hooks.reports
    assert w.venue.pos == 0 and w.sol.ansem == 50_000_000
    assert w.venue.calls[-1][:4] == ("BUY", D(903), w.venue.calls[-1][2], True)
    op = op_of(w, prop.intent_id)
    assert (op["state"], op["target_kind"], op["confirmed_raw"]) == ("CLOSED", "full_position_snapshot", "903000000")
    assert "сделка закрыта" in w.hooks.reports[-1]
    for r in w.hooks.reports:                                  # V02: у SOL/HL нет подписей BSC/Aster
        assert not any(x in r for x in ("USDT", "BNB", "Aster", "bscscan", "OKX DEX·"))
        assert not W.RAW.search(r)


def test_exit_dex_refund_keeps_residual_hedged_not_closed(tmp_path):
    """U09/§8 пример 2: продано меньше (роутер вернул часть) — откуп только S − target(остаток), сделка не CLOSED."""
    w = make_world(tmp_path)
    did = enter(w).deal_id
    w.sol.script.append({"beh": "land", "refund": 103_000_000})
    prop = exit_deal(w, did)
    d = store.get_deal(w.con, did)
    bk = deal_book(w.con, did)
    assert d["state"] == DealState.OPEN and (bk.tokens_raw, bk.short) == (103_000_000, D(103))
    assert w.venue.calls[-1][:2] == ("BUY", D(800)) and w.venue.pos == D(-103)
    assert op_of(w, prop.intent_id)["state"] == "PARTIAL"
    assert any(e["kind"] == "exit_residual" for e in store.events(w.con, did))
    assert "остаток" in w.hooks.reports[-1]
    # повторный «выход» доводит до CLOSED
    exit_deal(w, did)
    assert store.get_deal(w.con, did)["state"] == DealState.CLOSED and w.venue.pos == 0


# --- UNKNOWN свопа: пауза без новых действий; исход — только чтениями и теми же байтами -----------------------------
def _unknown_swap(tmp_path):
    w = make_world(tmp_path)
    w.sol.blackhole = True                          # ответа узла нет, транзакции в сети не видно
    w.sol.down = {"b"}                              # второй узел молчит — отсутствие не доказать (X04)
    prop = w.desk.propose_profile_entry(entry_cmd(), chat=None)
    approve_run(w, prop)
    return w, prop


def test_swap_unknown_pauses_no_hedge_no_new_route(tmp_path):
    w, prop = _unknown_swap(tmp_path)
    d = deal_of(w, prop)
    (c,) = store.clips_of(w.con, prop.intent_id)
    op = op_of(w, prop.intent_id)
    assert d["state"] == DealState.PAUSED and c["state"] == ClipState.DEX_UNKNOWN
    assert (op["state"], op["reserved_raw"], op["confirmed_raw"]) == ("PAUSED_UNKNOWN", "150000000", "0")
    assert deal_book(w.con, d["id"]).tokens_raw is None       # UNKNOWN не превращается в ноль
    assert w.venue.calls == [] and len(attempts(w)) == 1       # хеджа по котировке нет, второй подписи нет
    assert len(set(w.sol.sends)) == 1                          # повторы — те же байты
    assert "исход неизвестен" in w.hooks.reports[-1]
    for fn in (lambda: w.desk.propose_exit(d["id"], None, False, None),
               lambda: w.desk.propose_fix("rehedge", d["id"], None)):
        with pytest.raises(Refused, match="(?i)исход прошлой отправки неизвестен"):
            fn()
    # узел вернулся, транзакция так и не села: истечение доказано по высоте у двух RPC → токены не двигались
    w.sol.down = set()
    w.clock.sleep(120)
    assert w.engine.recover_sol(d) == []
    (c,) = store.clips_of(w.con, prop.intent_id)
    op = op_of(w, prop.intent_id)
    assert c["state"] == ClipState.DEX_REVERTED and op["reserved_raw"] == "0" and op["confirmed_raw"] == "0"
    assert attempts(w)[0]["state"] == "EXPIRED_NOT_LANDED" and len(attempts(w)) == 1
    assert w.sol.usdc == 500_000_000 and w.venue.calls == []


def test_swap_unknown_then_late_landing_applied_once_no_auto_hedge(tmp_path):
    w, prop = _unknown_swap(tmp_path)
    did = prop.deal_id
    a = attempts(w)[0]
    w.sol.blackhole = False
    w.sol.land(w.sol.sends[0], a["signature"], {"beh": "land"}, at=w.clock())
    w.sol.down = set()
    w.clock.sleep(20)
    assert w.engine.recover_sol(store.get_deal(w.con, did)) == []
    assert w.engine.recover_sol(store.get_deal(w.con, did)) == []          # второй раз — ничего не меняет
    bk = deal_book(w.con, did)
    assert (bk.tokens_raw, bk.short) == (903_000_000, D(0)) and w.venue.calls == []   # X06: сам не хеджирует
    assert len(store.fee_events(w.con, deal_id=did)) == 1 and op_of(w, prop.intent_id)["state"] == "PAUSED_RISK"
    fix = w.desk.propose_fix("rehedge", did, None)
    approve_run(w, fix)
    assert w.venue.pos == D(-903) and [c[:2] for c in w.venue.calls] == [("SELL", D(903))]
    assert deal_book(w.con, did).short == D(903)


# --- Hyperliquid: UNKNOWN, частичная, отказ -------------------------------------------------------------------------
def test_ioc_unknown_resolved_by_saved_cloid_without_repeat(tmp_path):
    w = make_world(tmp_path)
    w.venue.script = ["timeout"]                               # исполнилась, ответ потерян (H11, X07)
    prop = enter(w)
    assert deal_of(w, prop)["state"] == DealState.OPEN, w.hooks.reports
    assert len(w.venue.calls) == 1 and w.venue.pos == D(-903)
    assert [o["state"] for o in orders(w, prop.deal_id)] == ["FILLED"]


def test_ioc_lost_proven_not_placed_then_new_child(tmp_path):
    """Не дошла до биржи: «не выставлена» доказывается только после expiresAfter; новая дочерняя — новым cloid и лишь
    в сроке хеджа владельца."""
    toml = W.F.sol_toml({"limits.sol_best_hyperliquid": {"max_clip_usdc": '"150"', "max_operation_usdc": '"150"',
                                                         "max_total_position_usdc": '"150"',
                                                         "hedge_deadline_ms": "90000"}})
    w = make_world(tmp_path, toml=toml)
    w.venue.script = ["lost"]
    prop = enter(w)
    assert deal_of(w, prop)["state"] == DealState.OPEN, w.hooks.reports
    os_ = orders(w, prop.deal_id)
    assert [o["state"] for o in os_] == ["NOT_PLACED", "FILLED"] and os_[0]["client_id"] != os_[1]["client_id"]
    assert w.venue.pos == D(-903)


def test_ioc_lost_after_hedge_deadline_pauses_with_open_long(tmp_path):
    """Срок хеджа владельца (20 с) короче доказательства «не выставлена» (~40 с): новая заявка не шлётся, пауза с
    открытой экспозицией и командой «дохедж»."""
    w = make_world(tmp_path)
    w.venue.script = ["lost"]
    prop = enter(w)
    d = deal_of(w, prop)
    assert d["state"] == DealState.PAUSED and [o["state"] for o in orders(w, d["id"])] == ["NOT_PLACED"]
    assert "срок хеджа вышел" in w.hooks.reports[-1] and f"дохедж {d['id']}" in w.hooks.reports[-1]


def test_ioc_unknown_unresolvable_pauses_and_sells_nothing(tmp_path):
    w = make_world(tmp_path)
    w.venue.script = ["timeout"]
    w.venue.status_fail = True                                 # orderStatus не отвечает — исход не доказать
    prop = enter(w)
    d = deal_of(w, prop)
    assert d["state"] == DealState.PAUSED and op_of(w, prop.intent_id)["state"] == "PAUSED_UNKNOWN"
    assert [o["state"] for o in orders(w, d["id"])] == ["UNKNOWN"] and len(w.venue.calls) == 1
    assert len(w.sol.sends) == 1                               # спот не продаётся, пока возможен неизвестный шорт
    assert "исход неизвестен" in w.hooks.reports[-1]


def test_partial_ioc_then_only_remainder(tmp_path):
    w = make_world(tmp_path)
    w.venue.script = [("partial", D(600))]                     # H10: 600 из 903 — дальше только 303
    prop = enter(w)
    assert deal_of(w, prop)["state"] == DealState.OPEN
    assert [(c[0], c[1]) for c in w.venue.calls] == [("SELL", D(903)), ("SELL", D(303))]
    assert [o["executed_qty"] for o in orders(w, prop.deal_id)] == ["600", "303"]


def test_hl_reject_after_swap_pauses_with_open_long(tmp_path):
    w = make_world(tmp_path)
    w.venue.script = [("error", "Insufficient margin to place order. asset=180025")]
    prop = enter(w)
    d = deal_of(w, prop)
    bk = deal_book(w.con, d["id"])
    assert d["state"] == DealState.PAUSED and (bk.tokens_raw, bk.short) == (903_000_000, D(0))
    assert op_of(w, prop.intent_id)["state"] == "PAUSED_RISK"
    assert len(w.sol.sends) == 1                               # аварийная продажа спота в пилоте выключена
    txt = w.hooks.reports[-1]
    assert "без хеджа" in txt and f"дохедж {d['id']}" in txt and "откат" not in txt


def test_surplus_over_approved_capacity_pauses_risk(tmp_path):
    """G09: пришло больше одобренной ёмкости — хедж только в ёмкости, дальше PAUSED_RISK (продажа излишка выключена)."""
    w = make_world(tmp_path)
    w.sol.script = [{"beh": "land", "out": 933_000_000}]
    prop = enter(w)
    d = deal_of(w, prop)
    bk = deal_book(w.con, d["id"])
    assert (bk.tokens_raw, bk.short) == (933_000_000, D(923)) and d["state"] == DealState.PAUSED
    assert op_of(w, prop.intent_id)["state"] == "PAUSED_RISK"
    assert "больше одобренного" in json.loads(store.events(w.con, d["id"])[-1]["json"])["text"]


# --- «стоп» -------------------------------------------------------------------------------------------------------
def test_stop_after_swap_still_finishes_hedge(tmp_path):
    """X08: «стоп» между чеком свопа и хеджем — хедж исполненной ноги доводится, нового не начинается."""
    w = make_world(tmp_path)
    w.sol.on_land = lambda t: store.set_paused(w.con, True)
    prop = enter(w)
    assert deal_of(w, prop)["state"] == DealState.OPEN and w.venue.pos == D(-903)
    with pytest.raises(Refused, match="«стоп»"):
        w.desk.propose_exit(prop.deal_id, None, False, None)


def test_stop_before_swap_sends_nothing(tmp_path):
    w = make_world(tmp_path)
    prop = w.desk.propose_profile_entry(entry_cmd(), chat=None)
    store.set_paused(w.con, True)
    approve_run(w, prop)
    assert deal_of(w, prop)["state"] == DealState.ABORTED and w.sol.sends == [] and w.venue.calls == []
    assert op_of(w, prop.intent_id)["state"] == "ABANDONED"
    assert store.get_intent(w.con, prop.intent_id)["status"] == IntentStatus.FAILED


# --- рестарт на каждой границе отправки ---------------------------------------------------------------------------
def _boom(*a, **k):
    raise Crash()


@pytest.mark.parametrize("where", ["sign", "journal_broadcast", "after_send", "before_hl", "hl_after_fill"])
def test_restart_on_every_send_boundary_no_rebuy_no_double_hedge(tmp_path, monkeypatch, where):
    w = make_world(tmp_path)
    if where == "sign":
        monkeypatch.setattr(sol_exec, "sign_checked", _boom)
    elif where == "journal_broadcast":
        monkeypatch.setattr(sol_exec.J, "mark_broadcast", _boom)
    elif where == "after_send":
        w.sol.script = [{"beh": "crash_after"}]
    elif where == "before_hl":
        monkeypatch.setattr(sol_flow.SolEngine, "_hl_hedge", _boom)
    else:
        w.venue.script = ["crash"]
    prop = w.desk.propose_profile_entry(entry_cmd(), chat=None)
    with pytest.raises(Crash):
        approve_run(w, prop)
    monkeypatch.undo()
    restart(w)
    rep = reconcile.startup(w.con, w.reg, now=w.clock())
    (dr,) = rep.deals
    did = prop.deal_id
    bk = deal_book(w.con, did)
    at = attempts(w)
    assert len(at) == 1                                        # ни одной второй подписи/попытки свопа
    if where == "sign":                                        # не подписана — в сеть уйти не могла
        assert w.sol.sends == [] and at[0]["state"] == "ABANDONED_UNSIGNED"
        assert dr.new == DealState.ABORTED and (bk.tokens_raw, bk.short) == (0, D(0))
        assert op_of(w, prop.intent_id)["reserved_raw"] == "0"
        return
    if where == "journal_broadcast":                           # подписана, не отправлялась: те же байты (X02)
        signed = w.con.execute("SELECT signed_payload FROM sol_tx_attempts").fetchone()[0]
        assert w.sol.sends == [bytes(signed)]
    assert at[0]["state"] == "FINALIZED_OK" and len(w.sol.txs) == 1
    assert bk.tokens_raw == 903_000_000 and len(store.fee_events(w.con, deal_id=did)) == 1
    if where == "hl_after_fill":                               # заявка ушла и исполнилась: найдена по cloid, один раз
        assert bk.short == D(903) and len(w.venue.calls) == 1 and dr.check.matched is True
        with pytest.raises(Refused, match="(?i)ноги ровно"):
            w.desk.propose_fix("rehedge", did, None)
        return
    assert bk.short == D(0) and w.venue.calls == [] and dr.new == DealState.PAUSED   # X06: голый лонг виден
    fix = w.desk.propose_fix("rehedge", did, None)
    approve_run(w, fix)
    assert [c[:2] for c in w.venue.calls] == [("SELL", D(903))] and deal_book(w.con, did).short == D(903)
    exit_deal(w, did)
    assert store.get_deal(w.con, did)["state"] == DealState.CLOSED and len(attempts(w)) == 2


# --- объём пилота и предпроверки до свопа ---------------------------------------------------------------------------
def test_unified_account_refused_before_swap(tmp_path):
    w = make_world(tmp_path)
    w.venue.abstraction = "unifiedAccount"
    with pytest.raises(Refused, match="не поддержан"):
        w.desk.propose_profile_entry(entry_cmd(), chat=None)
    assert w.con.execute("SELECT count(*) FROM deals").fetchone()[0] == 0 and w.sol.sends == []


def test_identity_expired_refused_even_in_dry(tmp_path):
    reg = W.registry_text(**{"identity.expires_at": "2026-09-01T00:00:00+00:00"})
    w = make_world(tmp_path, keys_mode=None, registry=reg)
    with pytest.raises(Refused, match="срок истёк"):
        w.desk.propose_profile_entry(entry_cmd(), chat=None)


def test_pilot_scope_refusals(tmp_path):
    w = make_world(tmp_path)
    with pytest.raises(Refused, match="только sol-auto"):
        w.desk.propose_profile_entry(entry_cmd(policy="jupiter"), chat=None)
    with pytest.raises(Refused, match="лимита max_clip_usdc"):
        w.desk.propose_profile_entry(entry_cmd(usdc=D(151)), chat=None)
    did = enter(w).deal_id
    with pytest.raises(Refused, match="уже есть сделка"):            # G04: второй владелец net-позиции HL
        w.desk.propose_profile_entry(entry_cmd(), chat=None)
    with pytest.raises(Refused, match="(?i)частичный выход в пилоте выключен"):
        w.desk.propose_exit(did, D(50), False, None)
    with pytest.raises(Refused, match="«выход перп» в пилоте выключен"):
        w.desk.propose_exit(did, None, True, None)
    with pytest.raises(Refused, match="«откат» в пилоте выключен"):
        w.desk.propose_fix("undo", did, None)
    with pytest.raises(Refused, match="продолжать нечего"):
        w.desk.propose_resume(did, None)


def test_entry_interrupted_resume_is_refused_no_rebuy(tmp_path):
    w = make_world(tmp_path)
    w.venue.script = [("error", "Insufficient margin to place order. asset=180025")]
    did = enter(w).deal_id
    with pytest.raises(Refused, match="(?i)добор в пилоте выключен"):
        w.desk.propose_resume(did, None)
    assert len(w.sol.sends) == 1


def test_wallet_below_book_refuses_exit(tmp_path):
    """U10: кошелёк меньше журнала — выход не начинается и min(кошелёк, книга) не продаётся."""
    w = make_world(tmp_path)
    did = enter(w).deal_id
    w.sol.ansem = 800_000_000
    with pytest.raises(Refused, match="меньше, чем по журналу"):
        w.desk.propose_exit(did, None, False, None)


def test_hl_position_mismatch_refuses_exit(tmp_path):
    """U18: позиция HL ≠ журналу — формула выхода не применяется к выбранному наугад S."""
    w = make_world(tmp_path)
    did = enter(w).deal_id
    w.venue.pos = D(-800)
    with pytest.raises(Refused, match="≠ журнал сделки"):
        w.desk.propose_exit(did, None, False, None)


# --- симуляция (dry): тот же код движка, в сеть ничего не уходит ------------------------------------------------------
def test_dry_run_entry_and_exit_same_engine_code(tmp_path):
    w = make_world(tmp_path, keys_mode=None)
    prop = enter(w)
    d = deal_of(w, prop)
    assert d["sim"] == 1 and d["state"] == DealState.OPEN, w.hooks.reports
    assert deal_book(w.con, d["id"]).short == D(903)
    exit_deal(w, d["id"])
    assert store.get_deal(w.con, d["id"])["state"] == DealState.CLOSED
    assert attempts(w) == [] and w.sol.sends == [] and w.hl.exchange_calls() == [] and w.venue.pos == 0
    assert all("🧪" in r for r in w.hooks.reports)


# --- BSC и SOL в одной БД: каждая сделка — своими ногами (X12, изоляция профилей) ------------------------------------
def test_bsc_and_sol_deals_in_one_db_each_with_own_legs(tmp_path):
    import test_trade_engine as TE
    from funding_bot.trade import marks
    from funding_bot.trade.owner import SOL_HL
    from funding_bot.trade.runtime import RuntimeRegistry
    env = TE.sim_env(tmp_path)
    bprop = env.desk.propose_entry("AIW3", "okx·bsc", "aster", D(200), chat=None)
    TE.run_approved(env, bprop)
    assert store.get_deal(env.con, bprop.deal_id)["state"] == DealState.OPEN
    w = make_world(tmp_path, db_path=tmp_path / "trade.db", legacy=env.legs)
    sprop = enter(w)
    assert deal_of(w, sprop)["state"] == DealState.OPEN
    rep = reconcile.startup(w.con, w.reg, now=w.clock())
    got = {r.deal["id"]: r for r in rep.deals}
    assert got[bprop.deal_id].check.matched is True and got[sprop.deal_id].check.matched is True
    assert got[sprop.deal_id].new == DealState.OPEN and got[bprop.deal_id].new == DealState.OPEN
    rows, matched, _ = reconcile.positions(w.con, w.reg, now=w.clock())
    assert {r["deal_id"] for r in rows} == {bprop.deal_id, sprop.deal_id} and matched is True
    ms, done = marks.run_pass(w.con, w.reg, now=w.clock())
    # C3: SOL-сделка оценивается своим путём (sol_ledger, USDC), BSC-ногами — никогда; BSC-оценка прежняя
    by_mark = {m.deal_id: m for m in ms}
    assert done and set(by_mark) == {bprop.deal_id, sprop.deal_id}
    assert by_mark[sprop.deal_id].flags.get("profile") == SOL_HL and by_mark[sprop.deal_id].flags.get("unit") == "USDC"
    assert "profile" not in by_mark[bprop.deal_id].flags and "unit" not in by_mark[bprop.deal_id].flags
    # связка SOL не собралась — BSC сверяется, SOL-сделка видна «не сверена», а не флэт
    down = RuntimeRegistry(env.legs, {SOL_HL: lambda s: (_ for _ in ()).throw(RuntimeError("нет RPC"))})
    rows, matched, prob = reconcile.positions(w.con, down, now=w.clock())
    by = {r["deal_id"]: r for r in rows}
    assert set(by) == {bprop.deal_id, sprop.deal_id} and matched is None and sprop.deal_id in prob
    assert TE.store.get_deal(env.con, bprop.deal_id)["state"] == DealState.OPEN


def test_naked_time_over_owner_limit_is_reported(tmp_path):
    """Лимит времени голой ноги (max_unhedged_ms): финальность дольше — хедж всё равно доводится, в итоге ⚠️."""
    w = make_world(tmp_path)
    w.sol.FINALITY_S = 75.0
    prop = enter(w)
    assert deal_of(w, prop)["state"] == DealState.OPEN
    ev = [json.loads(e["json"]) for e in store.events(w.con, prop.deal_id) if e["kind"] == "unhedged"]
    assert ev and ev[0]["ms"] > 60_000 and ev[0]["limit_ms"] == 60000
    assert "без хеджа" in w.hooks.reports[-1] and "дольше лимита" in w.hooks.reports[-1]


def test_sigma_model_not_called_for_non_aster_perp():
    from funding_bot.trade.engine import sigma_1s

    class P:
        venue = "hyperliquid"

        @property
        def http(self):
            raise AssertionError("свечи Aster у HL не запрашиваются")
    assert sigma_1s(P(), "para:ANSEM") is None


@pytest.mark.parametrize("dust_usdc,closed", [('"0.25"', True), ('"0.05"', False)])
def test_exit_dust_closes_only_within_both_owner_bounds(tmp_path, dust_usdc, closed):
    """U11/U13: остаток меньше шага перпа закрывает сделку только в обеих границах пыли (токены И USDC)."""
    toml = W.F.sol_toml({"limits.sol_best_hyperliquid": {"max_clip_usdc": '"150"', "max_operation_usdc": '"150"',
                                                         "max_total_position_usdc": '"150"',
                                                         "max_dust_usdc": dust_usdc}})
    w = make_world(tmp_path, toml=toml)
    did = enter(w).deal_id
    w.sol.script.append({"beh": "land", "refund": 500_000})           # роутер вернул 0.5 токена
    exit_deal(w, did)
    d = store.get_deal(w.con, did)
    bk = deal_book(w.con, did)
    assert (bk.tokens_raw, bk.short) == (500_000, D(0)) and w.venue.pos == 0
    assert d["state"] == (DealState.CLOSED if closed else DealState.OPEN)
    if closed:
        assert D(d["dust"]) == D("0.5") and "пыль" in w.hooks.reports[-1]
    else:
        assert "Осталось" in w.hooks.reports[-1]


def test_fractional_entry_residual_stays_as_carry(tmp_path):
    """Приход 903.5: шорт 903, остаток 0.5 — перенос меньше шага, не голая нога и не ноль (§4 пример)."""
    w = make_world(tmp_path)
    w.sol.outs["entry"]["jupiter_build_v2"] = 903_500_000
    did = enter(w).deal_id
    bk = deal_book(w.con, did)
    assert (bk.tokens_raw, bk.short) == (903_500_000, D(903)) and store.get_deal(w.con, did)["state"] == "OPEN"
    assert D(store.get_deal(w.con, did)["carry"]) == D("0.5")


def test_requote_outside_approved_bounds_sends_nothing(tmp_path):
    """R13: у кнопки победитель хуже одобренного минимума/стоимости — ничего не отправлено, нужен новый план."""
    w = make_world(tmp_path)
    prop = w.desk.propose_profile_entry(entry_cmd(), chat=None)
    w.sol.outs["entry"] = {"jupiter_build_v2": 880_000_000, "okx_solana_v6": 879_000_000}
    approve_run(w, prop)
    assert deal_of(w, prop)["state"] == DealState.ABORTED and w.sol.sends == [] and w.venue.calls == []
    assert w.hooks.requotes and "min_out_below_approved" in w.hooks.requotes[-1][1]
    ev = [json.loads(e["json"]) for e in store.events(w.con, prop.deal_id) if e["kind"] == "route_reselect"]
    assert ev and ev[-1]["ok"] is False


def test_sol_reserve_checked_before_swap(tmp_path):
    """S20: SOL после всех обязательных расходов меньше резерва владельца — своп не начинается (лампорты, не 10^18)."""
    w = make_world(tmp_path)
    w.sol.lamports = 20_004_000                          # < 5020 сети + 20 000 000 резерва
    with pytest.raises(Refused, match="SOL"):
        w.desk.propose_profile_entry(entry_cmd(), chat=None)
    assert w.sol.sends == []


def test_exit_hl_buy_rejected_sells_no_more_spot(tmp_path):
    """G16: продажа спота доказана, BUY reduceOnly отвергнут — дополнительный спот не продаётся, голый шорт виден."""
    w = make_world(tmp_path)
    did = enter(w).deal_id
    w.venue.script = [("error", "Insufficient margin to place order. asset=180025")]
    exit_deal(w, did)
    d = store.get_deal(w.con, did)
    bk = deal_book(w.con, did)
    assert d["state"] == DealState.PAUSED and (bk.tokens_raw, bk.short) == (0, D(903)) and len(w.sol.sends) == 2
    txt = w.hooks.reports[-1]
    assert "без хеджа" in txt and "шорта" in txt and "откупить 903" in txt
    fix = w.desk.propose_fix("rehedge", did, None)       # владелец: откуп reduceOnly на весь остаток шорта
    approve_run(w, fix)
    assert w.venue.pos == 0 and store.get_deal(w.con, did)["state"] == DealState.CLOSED


def test_common_admission_native_presend_refusal_does_not_strand_unknown(tmp_path, monkeypatch):
    from funding_bot.trade.hyperliquid_trade import HlError
    w = make_world(tmp_path)
    prop = w.desk.propose_profile_entry(entry_cmd(), chat=None)
    original = w.perp._gate
    def fail_send(what, hedge=False):
        if what == 'send' and hedge:
            raise HlError('fixture pre-send gate')
        return original(what, hedge)
    monkeypatch.setattr(w.perp, '_gate', fail_send)
    approve_run(w, prop)
    assert not w.venue.calls
    assert [o['state'] for o in orders(w, prop.deal_id)] == ['NOT_PLACED']
    assert not w.con.execute("SELECT 1 FROM hl_order_attempts WHERE kind='order'").fetchone()
    monkeypatch.setattr(w.perp, '_gate', original)
    for _ in range(2):
        w.engine.recover_sol(deal_of(w, prop))
    fix = w.desk.propose_fix('rehedge', prop.deal_id, None)
    approve_run(w, fix)
    assert len(w.venue.calls) == 1 and w.venue.pos == D(-903)


def test_crash_between_common_claim_and_native_prepare_recovers_without_send(tmp_path, monkeypatch):
    w = make_world(tmp_path)
    prop = w.desk.propose_profile_entry(entry_cmd(), chat=None)
    original = w.perp.ioc
    def crash(*args, **kw):
        raise Crash()
    monkeypatch.setattr(w.perp, 'ioc', crash)
    with pytest.raises(Crash):
        approve_run(w, prop)
    assert [o['state'] for o in orders(w, prop.deal_id)] == ['SENT']
    assert not w.venue.calls
    assert not w.con.execute("SELECT 1 FROM hl_order_attempts WHERE kind='order'").fetchone()
    monkeypatch.setattr(w.perp, 'ioc', original)
    restart(w)
    reconcile.startup(w.con, w.reg, now=w.clock())
    for _ in range(2):
        w.engine.recover_sol(deal_of(w, prop))
    assert [o['state'] for o in orders(w, prop.deal_id)] == ['NOT_PLACED']
    assert not w.venue.calls
    fix = w.desk.propose_fix('rehedge', prop.deal_id, None)
    approve_run(w, fix)
    assert len(w.venue.calls) == 1 and w.venue.pos == D(-903)


def test_no_submit_proof_fences_racing_native_prepare(tmp_path, monkeypatch):
    import threading
    from funding_bot.trade.adapters.execution import recover_not_submitted
    w = make_world(tmp_path)
    prop = w.desk.propose_profile_entry(entry_cmd(), chat=None)
    claimed, resume = threading.Event(), threading.Event()
    original = w.perp.ioc
    def hold_ioc(*a, **kw):
        claimed.set()
        assert resume.wait(5)
        return original(*a, **kw)
    monkeypatch.setattr(w.perp, 'ioc', hold_ioc)
    errors = []
    def run():
        try:
            approve_run(w, prop)
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    assert claimed.wait(5)
    con = store.connect(w.db)
    order, = store.perp_orders_unresolved(con)
    original_proof = w.perp.submission_absent
    def release_submit_inside_proof(*a, **kw):
        # Recovery already owns SQLite's write fence. A resumed native prepare
        # cannot commit ahead of NOT_PLACED, and on_signed must then refuse POST.
        assert kw['proof_con'].in_transaction
        answer = original_proof(*a, **kw)
        assert answer is True
        resume.set()
        return answer
    monkeypatch.setattr(w.perp, 'submission_absent', release_submit_inside_proof)
    assert recover_not_submitted(con, deal=store.get_deal(con, prop.deal_id), clip_id=order['clip_id'],
                                 native=w.perp, account=w.reg.for_deal(store.get_deal(con, prop.deal_id)).account_id,
                                 client_id=order['client_id'])
    worker.join(5)
    assert not worker.is_alive() and not errors
    assert not w.venue.calls and w.venue.pos == 0
    assert store.get_perp_order(con, order['client_id'])['state'] == 'NOT_PLACED'
    assert w.perp.journal.get(order['client_id'])['state'] == 'NOT_SENT'
    con.close()


def test_malformed_saved_final_never_causes_second_hedge(tmp_path, monkeypatch):
    from dataclasses import replace
    w = make_world(tmp_path)
    original = w.perp.ioc
    saved = []
    def corrupt_result(*a, **kw):
        fill = original(*a, **kw)
        saved.append(fill)
        w.perp.journal.result(fill.client_id, 'FILLED', filled=None, avg_px=None, oid=fill.order_id)
        return replace(fill, qty=D(0), quote=D(0), avg_px=D(0))
    monkeypatch.setattr(w.perp, 'ioc', corrupt_result)
    prop = enter(w)
    assert len(w.venue.calls) == 1 and w.venue.pos == D(-903)
    assert [o['state'] for o in orders(w, prop.deal_id)] == ['UNKNOWN']
    monkeypatch.setattr(w.perp, 'filters', lambda *_: pytest.fail('recovery must not request new filters'))
    for _ in range(2):
        assert w.engine.recover_sol(deal_of(w, prop))
    assert len(w.venue.calls) == 1
    # Replace the synthetic broken fixture with the originally observed proof.
    fill, = saved
    w.perp.journal.result(fill.client_id, 'FILLED', filled=fill.qty, avg_px=fill.avg_px, oid=fill.order_id)
    for _ in range(2):
        assert w.engine.recover_sol(deal_of(w, prop)) == []
    assert [o['state'] for o in orders(w, prop.deal_id)] == ['FILLED']
    assert len(w.venue.calls) == 1 and deal_book(w.con, prop.deal_id).short == D(903)


def test_negative_saved_expired_never_increases_remaining_hedge(tmp_path, monkeypatch):
    from dataclasses import replace
    w = make_world(tmp_path)
    original = w.perp.ioc
    def corrupt_result(*a, **kw):
        fill = original(*a, **kw)
        w.perp.journal.result(fill.client_id, 'EXPIRED', filled=D(-1), avg_px=D(0), oid=fill.order_id)
        return replace(fill, status='UNKNOWN', qty=D(0), quote=D(0), avg_px=D(0))
    monkeypatch.setattr(w.perp, 'ioc', corrupt_result)
    prop = enter(w)
    assert len(w.venue.calls) == 1 and w.venue.pos == D(-903)
    assert [o['state'] for o in orders(w, prop.deal_id)] == ['UNKNOWN']
    assert w.engine.recover_sol(deal_of(w, prop))
    assert len(w.venue.calls) == 1


@pytest.mark.parametrize('status', ['EXPIRED', 'CANCELED', 'CANCELLED'])
def test_positive_cancelled_fill_keeps_position_cash_flows_and_fees(tmp_path, monkeypatch, status):
    from dataclasses import replace
    from funding_bot.trade.ledger_flows import perp_quote_flows
    from funding_bot.trade.sol_ledger import ledger
    w = make_world(tmp_path)
    w.venue.script = [('partial', D(400))]
    original = w.perp.ioc
    observed = []
    def cancelled_partial(*a, **kw):
        fill = original(*a, **kw)
        observed.append(fill)
        return replace(fill, status=status) if len(observed) == 1 else fill
    monkeypatch.setattr(w.perp, 'ioc', cancelled_partial)
    prop = enter(w)
    deal = deal_of(w, prop)
    assert deal['state'] == DealState.OPEN
    assert w.venue.pos == -D(903) and deal_book(w.con, prop.deal_id).short == D(903)
    assert [o['state'] for o in orders(w, prop.deal_id)] == ['PARTIALLY_FILLED', 'FILLED']
    expected = sum((fill.quote for fill in observed), D(0))
    flows = perp_quote_flows(w.con, prop.deal_id)
    assert not flows.missing and flows.credit == expected
    projection = ledger(w.con, deal, fee_rate=D('.0005'))
    assert projection.perp_sell == expected
    assert projection.perp_fee == sum(((fill.quote * W.FEE).quantize(D('0.000001')) for fill in observed), D(0))
