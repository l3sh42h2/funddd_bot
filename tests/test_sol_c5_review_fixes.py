"""Ревью связки SOL × HL 13.09 — регрессии подтверждённых находок (каждая проверена мутацией):
P-A агент HL проверяется до ноги Solana (вход и выход); P-B «не выставлена» по IOC доказуема после сбоя/смерти
процесса; P-C откат confirmed-свопа — доказанное неисполнение; P-D пустая сделка на паузе снимается; SEC-1 SOL
маршрута на чужой счёт — отказ до подписи; SEC-2 такой SOL — статья расходов; R1 сбой сборки ног назван причиной."""
from decimal import Decimal as D
import pytest
from funding_bot.trade import reconcile, sol_exec, sol_ledger, store
from funding_bot.trade.engine import Refused, deal_book
from funding_bot.trade.owner import SOL_HL
from funding_bot.trade.runtime import RuntimeRegistry
from funding_bot.trade.sol_exec import SwapOutcome
from funding_bot.trade.solana import NATIVE_MINT, USDC_MINT
from funding_bot.trade.solana import journal as J
from funding_bot.trade.solana import validate as V
from funding_bot.trade.store import ClipState, DealState, OpState, PerpOrderState
import hl_support as S
import sol_c2_world as W
from sol_c2_world import Crash, approve_run, enter, entry_cmd, make_world, restart

pytest.importorskip("solders")
pytest.importorskip("eth_account")

MISSING = {"role": "missing"}


def deal_of(w, did):
    return store.get_deal(w.con, did)


def op_of(w, iid):
    return store.operation_of_intent(w.con, iid)


def orders(w, did):
    return [r[0] for r in w.con.execute("SELECT state FROM perp_orders WHERE client_id LIKE ? ORDER BY id",
                                        (f"fb-{did}-%",))]


def exchange_calls(w):
    return [p for path, p in w.hl.calls if path == "/exchange"]


# --- P-A: агент HL — до свопа и до продажи спота ------------------------------------------------------------------
@pytest.mark.parametrize("how", ["revoked", "expired", "unreadable"])
def test_pa_invalid_agent_stops_entry_before_swap_even_when_isolated_already(tmp_path, how):
    w = make_world(tmp_path)
    w.venue.lev = 1                               # уже isolated 1x: setup ничего не подписывает — агент им не виден
    prop = w.desk.propose_profile_entry(entry_cmd(), chat=None)
    if how == "revoked":                          # агента отозвали между планом и кнопкой
        w.venue.agent_role = MISSING
    elif how == "expired":
        w.venue.extra_agents = [{"name": "bot", "address": S.AGENT.lower(), "validUntil": int(w.clock() * 1000) - 1}]
    else:
        w.venue.agent_role = S.Resp(500, {"error": "down"})
    approve_run(w, prop)
    d = deal_of(w, prop.deal_id)
    assert w.sol.sends == [] and exchange_calls(w) == [] and w.venue.pos == 0     # ни свопа, ни подписи HL
    assert d["state"] == DealState.ABORTED and op_of(w, prop.intent_id)["state"] == OpState.ABANDONED
    assert w.sol.usdc == 500_000_000 and "своп не начинаю" in w.hooks.reports[-1]
    assert {"revoked": "не действует (userRole missing)", "expired": "истёк",
            "unreadable": "userRole не прочитан"}[how] in w.hooks.reports[-1]


def test_pa_invalid_agent_refused_at_plan(tmp_path):
    w = make_world(tmp_path)
    w.venue.agent_role = MISSING
    with pytest.raises(Refused, match="одобрите API-кошелёк"):
        w.desk.propose_profile_entry(entry_cmd(), chat=None)
    assert w.sol.sends == []


def test_pa_invalid_agent_stops_exit_before_spot_sale(tmp_path):
    w = make_world(tmp_path)
    prop = enter(w)
    did = prop.deal_id
    assert deal_of(w, did)["state"] == DealState.OPEN and w.venue.pos == D(-903)
    ex = w.desk.propose_exit(did, None, False, chat=None)
    w.venue.agent_role = MISSING                  # отозван после плана выхода
    approve_run(w, ex)
    bk = deal_book(w.con, did)
    assert len(w.sol.sends) == 1 and w.venue.pos == D(-903)        # спот не продан — голого шорта нет
    assert (bk.tokens_raw, bk.short) == (903_000_000, D(903)) and deal_of(w, did)["state"] == DealState.PAUSED
    assert "спот не продаю" in w.hooks.reports[-1]
    with pytest.raises(Refused, match="выход не начинаю"):          # и план выхода при отозванном агенте — отказ
        w.desk.propose_exit(did, None, False, chat=None)


# --- P-B: IOC, не дошедшая до биржи, — доказуемо «не выставлена» -----------------------------------------------
def test_pb_ioc_lost_during_hl_outage_proven_not_placed_after_recovery(tmp_path):
    w = make_world(tmp_path)
    w.venue.script = ["lost"]                     # POST не дошёл до биржи
    w.venue.status_fail = True                    # и orderStatus в окне потока не читается
    prop = enter(w)
    did = prop.deal_id
    assert deal_of(w, did)["state"] == DealState.PAUSED and orders(w, did) == ["UNKNOWN"] and w.venue.orders == {}
    w.venue.status_fail = False                   # HL ожил, но позиция изменилась не нашей заявкой — не доказано
    w.venue.pos = D(-100)
    w.clock.sleep(600)
    assert w.engine.recover_sol(deal_of(w, did)) == [f"заявка fb-{did}-e01-c1-a1: исход неизвестен"]
    w.venue.pos = D(0)
    w.clock.sleep(600)
    assert w.engine.recover_sol(deal_of(w, did)) == []
    assert orders(w, did) == ["NOT_PLACED"] and op_of(w, prop.intent_id)["state"] == OpState.PAUSED_RISK
    fix = w.desk.propose_fix("rehedge", did, None)           # голый лонг теперь закрывается командой
    approve_run(w, fix)
    assert w.venue.pos == D(-903) and deal_book(w.con, did).short == D(903)


def test_pb_crash_between_signed_and_post_resolved_at_restart(tmp_path, monkeypatch):
    w = make_world(tmp_path)
    orig = w.perp.http.exchange

    def boom(payload):
        if payload["action"]["type"] == "order":
            raise Crash()                         # процесс умер между SIGNED и POST
        return orig(payload)
    monkeypatch.setattr(w.perp.http, "exchange", boom)
    prop = w.desk.propose_profile_entry(entry_cmd(), chat=None)
    with pytest.raises(Crash):
        approve_run(w, prop)
    monkeypatch.undo()
    did = prop.deal_id
    assert w.venue.calls == [] and orders(w, did) == ["SENT"]
    restart(w)
    (dr,) = reconcile.startup(w.con, w.reg, now=w.clock()).deals
    assert dr.check.matched is True and dr.new == DealState.PAUSED, dr.check.detail
    assert orders(w, did) == ["NOT_PLACED"]
    approve_run(w, w.desk.propose_fix("rehedge", did, None))
    assert w.venue.pos == D(-903) and len(w.venue.calls) == 1


# --- P-C: confirmed-своп откатился — доказанное неисполнение ------------------------------------------------------
def _fork_after_confirm(w):
    real = W.SolWorld.status

    def status(sig):
        t = w.sol.txs.get(sig)
        if t is not None and w.clock() >= t["land_t"] + 5 and not t.get("undo"):
            t["undo"] = True                      # форк: транзакция выпала из цепи, балансы вернулись
            w.sol.usdc, w.sol.ansem, w.sol.lamports = t["usdc_pre"], t["ansem_pre"], t["lam_pre"]
        if t is not None and t.get("undo"):
            return None
        return real(w.sol, sig)
    w.sol.status = status


def test_pc_rolled_back_swap_applied_as_not_executed(tmp_path):
    w = make_world(tmp_path)
    _fork_after_confirm(w)
    prop = enter(w)
    (a,) = [r[0] for r in w.con.execute("SELECT state FROM sol_tx_attempts")]
    (c,) = store.clips_of(w.con, prop.intent_id)
    op = op_of(w, prop.intent_id)
    assert a == "ROLLED_BACK" and c["state"] == ClipState.DEX_REVERTED and w.venue.calls == []
    assert (op["state"], op["reserved_raw"], op["confirmed_raw"]) == (OpState.ABANDONED, "0", "0")
    assert deal_of(w, prop.deal_id)["state"] == DealState.ABORTED and w.sol.usdc == 500_000_000
    assert "не вошёл в блок" in w.hooks.reports[-1]
    w.desk.propose_profile_entry(entry_cmd(), chat=None)          # рынок свободен


def test_pc_rolled_back_left_by_older_process_applied_by_recover(tmp_path, monkeypatch):
    w = make_world(tmp_path)
    _fork_after_confirm(w)
    orig = sol_exec.SolanaExecutor._resolve_once

    def old(self, con, aid, apply):               # прежний процесс: ROLLED_BACK записан, исход не применён
        if J.attempt(con, aid)["state"] == "ROLLED_BACK":
            return SwapOutcome("unknown", aid, None, reason="confirmed откатился — нужна сверка")
        return orig(self, con, aid, apply)
    monkeypatch.setattr(sol_exec.SolanaExecutor, "_resolve_once", old)
    prop = enter(w)
    did = prop.deal_id
    (c,) = store.clips_of(w.con, prop.intent_id)
    assert c["state"] == ClipState.DEX_UNKNOWN and deal_of(w, did)["state"] == DealState.PAUSED
    monkeypatch.undo()
    restart(w)
    assert w.engine.recover_sol(deal_of(w, did)) == []
    (c,) = store.clips_of(w.con, prop.intent_id)
    assert c["state"] == ClipState.DEX_REVERTED and op_of(w, prop.intent_id)["reserved_raw"] == "0"
    assert deal_of(w, did)["state"] == DealState.ABORTED and w.sol.sends and w.venue.calls == []


# --- P-D: пустая сделка на паузе снимается, рынок свободен ----------------------------------------------------------
def _unknown_then_expired(tmp_path):
    w = make_world(tmp_path)
    w.sol.blackhole = True                        # отправка «в никуда», второй узел молчит — исход неизвестен
    w.sol.down = {"b"}
    prop = w.desk.propose_profile_entry(entry_cmd(), chat=None)
    approve_run(w, prop)
    assert deal_of(w, prop.deal_id)["state"] == DealState.PAUSED
    w.sol.down, w.sol.blackhole = set(), False
    w.clock.sleep(120)                            # срок blockhash вышел у обоих узлов
    return w, prop


def test_pd_expired_swap_empty_paused_deal_aborted_market_free(tmp_path):
    w, prop = _unknown_then_expired(tmp_path)
    assert w.engine.recover_sol(deal_of(w, prop.deal_id)) == []
    op = op_of(w, prop.intent_id)
    assert deal_of(w, prop.deal_id)["state"] == DealState.ABORTED and (op["state"], op["reserved_raw"]) == (
        OpState.ABANDONED, "0")
    assert w.sol.usdc == 500_000_000 and w.venue.calls == []
    again = w.desk.propose_profile_entry(entry_cmd(), chat=None)  # новый вход по ANSEM возможен
    approve_run(w, again)
    assert deal_of(w, again.deal_id)["state"] == DealState.OPEN


def test_pd_exit_command_on_empty_deal_says_aborted(tmp_path):
    w, prop = _unknown_then_expired(tmp_path)
    with pytest.raises(Refused, match="сделка снята, «вход» заново"):
        w.desk.propose_exit(prop.deal_id, None, False, chat=None)
    assert deal_of(w, prop.deal_id)["state"] == DealState.ABORTED
    with pytest.raises(Refused, match="продолжать нечего"):
        w.desk.propose_resume(prop.deal_id, None)


def test_pd_startup_aborts_empty_deal(tmp_path):
    from funding_bot.tg.bot import Bot
    w, prop = _unknown_then_expired(tmp_path)
    restart(w)
    (dr,) = reconcile.startup(w.con, w.reg, now=w.clock()).deals
    assert (dr.old, dr.new, dr.check.matched) == (DealState.PAUSED, DealState.ABORTED, True)
    assert deal_of(w, prop.deal_id)["state"] == DealState.ABORTED and "снята" in Bot._check_line(dr)


def test_pd_paused_deal_with_open_long_is_not_aborted(tmp_path):
    w = make_world(tmp_path)
    w.venue.script = [("error", "Insufficient margin to place order. asset=180025")]
    prop = enter(w)
    assert w.engine.recover_sol(deal_of(w, prop.deal_id)) == []
    bk = deal_book(w.con, prop.deal_id)
    assert deal_of(w, prop.deal_id)["state"] == DealState.PAUSED and (bk.tokens_raw, bk.short) == (903_000_000, D(0))


# --- SEC-1: SOL маршрута на чужой счёт — отказ до подписи, бюджет его не покрывает ---------------------------------
def test_sec1_foreign_sol_within_budget_refused_before_sign():
    import sol_tx_helpers as H
    mv = V.ManifestValidator()
    rm = H.resolved(H.build([H.cu_limit(300_000), H.cu_price(100_000), H.route_v2()]))   # ATA уже есть
    it = H.intent(native_budget_lamports=200_000 + 2_500_000, account_rent=((H.ANSEM_ATA, 0), (H.USDC_ATA, 0)))
    plan = mv.validate(rm, it)
    assert plan.ok
    dst = H.key("route-fee-collector")
    fee = H.parsed("system", "transfer", {"source": H.WALLET, "destination": dst, "lamports": 2_400_000})
    out_pre = H.token_account(H.ANSEM_MINT, H.WALLET, 0, program=H.TOKEN_2022_PROGRAM)
    sim, pre = H.good_sim(extra_inner=[fee], drop=plan.details["native_total"] + 2_400_000, router_index=2,
                          out_pre=out_pre)
    v = mv.check_simulation(sim, it, pre, msg=rm, plan=plan)
    assert v.reasons == (f"route_foreign_sol:{dst}",) and v.details["sol_out_foreign_net"] == 2_400_000
    sim, pre = H.good_sim(extra_inner=[fee], drop=plan.details["native_total"], router_index=2, out_pre=out_pre)
    assert mv.check_simulation(sim, it, pre, msg=rm, plan=plan).ok     # вернулся в той же транзакции — не расход


# --- SEC-2: SOL, ушедший на чужие счета, — статья расходов сделки ----------------------------------------------
def test_sec2_receipt_counts_sol_sent_to_foreign_accounts(tmp_path):
    w = make_world(tmp_path)

    def leak(t):
        if t["side"] == "entry":                  # маршрут увёл 2 400 000 лампортов на чужой счёт
            t["lam_post"] -= 2_400_000
            w.sol.lamports -= 2_400_000
    w.sol.on_land = leak
    prop = enter(w)
    d = deal_of(w, prop.deal_id)
    assert d["state"] == DealState.OPEN
    got = sorted((f["kind"], f["amount_raw"]) for f in store.fee_events(w.con, deal_id=d["id"]))
    assert got == [("network_total", "5020"), ("rent_nonrefundable", "2400000")]
    assert sol_ledger.ledger(w.con, d, fee_rate=None).network_lamports == 2_405_020
    row = J.attempt(w.con, w.con.execute("SELECT attempt_id FROM sol_tx_attempts").fetchone()[0])
    again = w.spot._stored_receipt(w.con, row)            # тот же чек из журнала (рестарт) — та же статья
    assert ("rent_nonrefundable", 2_400_000) in [(f.kind, f.amount_raw) for f in again.fees]


def test_sec2_sol_pair_foreign_sol_unknown_not_zero():
    both = dict(wallet=W.WALLET, fee=5000, tip=0, deposit=0, refund=0, external=2_400_000, source="t")
    usdc = {(f.kind, f.amount_raw) for f in sol_exec._fees({"in_mint": USDC_MINT, "out_mint": "x"}, **both)}
    assert ("rent_nonrefundable", 2_400_000) in usdc
    sol = [(f.kind, f.amount_raw) for f in sol_exec._fees({"in_mint": NATIVE_MINT, "out_mint": "x"}, **both)]
    assert ("rent_nonrefundable", None) in sol            # торговый SOL в том же external — неизвестно, не 0


# --- R1: сбой сборки ног — причиной, а не «не подключена» -----------------------------------------------------------
def test_r1_failed_sol_build_named_in_restart_and_positions(tmp_path):
    import test_trade_engine as TE
    from funding_bot.tg.bot import Bot
    env = TE.sim_env(tmp_path)
    bprop = env.desk.propose_entry("AIW3", "okx·bsc", "aster", D(200), chat=None)
    TE.run_approved(env, bprop)
    w = make_world(tmp_path, db_path=tmp_path / "trade.db", legacy=env.legs)
    sprop = enter(w)

    def no_rpc(sim):
        raise RuntimeError("нет RPC")
    down = RuntimeRegistry(env.legs, {SOL_HL: no_rpc})
    got = {r.deal["id"]: r for r in reconcile.startup(w.con, down, now=w.clock()).deals}
    want = "связка Solana × Hyperliquid не собрана: RuntimeError: нет RPC — не сверена"
    assert got[sprop.deal_id].check.detail == want and got[sprop.deal_id].check.matched is None
    assert "не собрана: RuntimeError: нет RPC" in Bot._check_line(got[sprop.deal_id])
    assert got[bprop.deal_id].check.matched is True and got[bprop.deal_id].new == DealState.OPEN   # BSC прежний
    _rows, _ok, problems = reconcile.positions(w.con, down, now=w.clock())
    assert f"{sprop.deal_id}: {want}" in problems
    none = RuntimeRegistry(env.legs, {})                  # связки в процессе нет вовсе — прежний текст
    got = {r.deal["id"]: r for r in reconcile.startup(w.con, none, now=w.clock()).deals}
    assert got[sprop.deal_id].check.detail == "связка Solana × Hyperliquid не подключена — не сверена"
