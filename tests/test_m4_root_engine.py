"""Root budget invariants through the real EVM Desk/Engine with fake venues."""
from decimal import Decimal as D
import pytest
from funding_bot.trade import store
from test_trade_engine import live_env, sim_env, run_approved, OWNER


def test_stopped_entry_resumes_same_root_only_after_fresh_approval(tmp_path):
    e = live_env(tmp_path, clip="300")
    first = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    original = store.operation_of_intent(e.con, first.intent_id)
    e.spot.on_swap = lambda: store.set_paused(e.con2, True)
    run_approved(e, first)
    stopped = store.get_operation(e.con, original['id'])
    assert stopped['state'] == store.OpState.STOPPED
    assert int(stopped['confirmed_raw']) > 0 and stopped['reserved_raw'] == '0'
    remaining = store.operation_remaining(stopped)
    store.set_paused(e.con, False)
    e.spot.on_swap = None
    resumed = e.desk.propose_resume(first.deal_id, chat=OWNER)
    assert store.operation_of_intent(e.con, resumed.intent_id)['id'] == original['id']
    assert sum(c.dex_in_units for c in resumed.plan.clips) == remaining
    assert len(e.spot.swaps) == 1
    assert store.get_operation(e.con, original['id']) == stopped
    assert store.reject_intent(e.con, resumed.intent_id, resumed.nonce)
    resumed = e.desk.propose_resume(first.deal_id, chat=OWNER)
    assert store.operation_of_intent(e.con, resumed.intent_id)['id'] == original['id']
    run_approved(e, resumed)
    final = store.get_operation(e.con, original['id'])
    assert final['state'] == store.OpState.OPEN, e.hooks.reports
    assert final['confirmed_raw'] == final['target_raw'] == original['target_raw']
    assert final['reserved_raw'] == '0'
    assert sum(s[2] for s in e.spot.swaps) == int(original['target_raw'])
    assert final['approval_version'] == 2


def test_filter_failure_releases_unstarted_approved_root(tmp_path):
    e = sim_env(tmp_path)
    prop = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(200), chat=OWNER)
    assert store.approve_intent(e.con, prop.intent_id, prop.nonce)
    def fail(*args):
        raise RuntimeError('offline')
    e.legs_sim.perp.filters = fail
    e.engine.execute(prop.intent_id)
    op = store.operation_of_intent(e.con, prop.intent_id)
    assert op['state'] == store.OpState.ABANDONED
    assert op['confirmed_raw'] == op['reserved_raw'] == '0'
    assert store.get_intent(e.con, prop.intent_id)['status'] == store.IntentStatus.FAILED


def test_requoted_draft_resumes_latest_root_not_abandoned_first(tmp_path):
    e = live_env(tmp_path, clip="300")
    first = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    original_filters = e.perp.filters
    def fail(*args):
        raise RuntimeError('offline')
    e.perp.filters = fail
    run_approved(e, first)
    abandoned = store.operation_of_intent(e.con, first.intent_id)
    assert abandoned['state'] == store.OpState.ABANDONED
    e.perp.filters = original_filters
    second = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER,
                                  deal_id=first.deal_id)
    e.spot.on_swap = lambda: store.set_paused(e.con2, True)
    run_approved(e, second)
    active = store.operation_of_intent(e.con, second.intent_id)
    assert active['state'] == store.OpState.STOPPED
    store.set_paused(e.con, False)
    e.spot.on_swap = None
    resumed = e.desk.propose_resume(first.deal_id, chat=OWNER)
    assert store.operation_of_intent(e.con, resumed.intent_id)['id'] == active['id']
    run_approved(e, resumed)
    assert store.get_operation(e.con, active['id'])['state'] == store.OpState.OPEN
    assert store.get_operation(e.con, abandoned['id']) == abandoned


def test_sol_exit_refund_resume_keeps_original_root(tmp_path, monkeypatch):
    from sol_c2_world import make_world, enter, approve_run
    w = make_world(tmp_path)
    did = enter(w).deal_id
    w.sol.script.append({'beh': 'land', 'refund': 103_000_000})
    first = w.desk.propose_exit(did, None, False, chat=None)
    approve_run(w, first)
    original = store.operation_of_intent(w.con, first.intent_id)
    assert original['state'] == store.OpState.PARTIAL
    sends = len(w.sol.sends)
    resumed = w.desk.propose_resume(did, chat=None)
    assert store.operation_of_intent(w.con, resumed.intent_id)['id'] == original['id']
    assert len(w.sol.sends) == sends
    assert sum(c.dex_in_units for c in resumed.plan.clips) == store.operation_remaining(original)
    from funding_bot.trade import sol_flow
    original_step = sol_flow._step
    def offline(*args):
        raise RuntimeError('metadata offline')
    monkeypatch.setattr(sol_flow, '_step', offline)
    approve_run(w, resumed)
    stopped = store.get_operation(w.con, original['id'])
    assert stopped['state'] == store.OpState.STOPPED
    assert stopped['confirmed_raw'] == original['confirmed_raw']
    monkeypatch.setattr(sol_flow, '_step', original_step)
    resumed = w.desk.propose_resume(did, chat=None)
    approve_run(w, resumed)
    final = store.get_operation(w.con, original['id'])
    assert final['state'] == store.OpState.CLOSED, w.hooks.reports
    assert final['confirmed_raw'] == original['target_raw']
    assert final['reserved_raw'] == '0'


@pytest.mark.parametrize('family', ['evm', 'solana'])
def test_restart_after_approval_releases_unstarted_draft_root(tmp_path, family):
    from funding_bot.trade import reconcile
    if family == 'evm':
        e = sim_env(tmp_path)
        prop = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(200), chat=OWNER)
        con, legs = e.con, e.legs
        assert store.approve_intent(con, prop.intent_id, prop.nonce)
    else:
        from sol_c2_world import make_world, entry_cmd
        e = make_world(tmp_path)
        prop = e.desk.propose_profile_entry(entry_cmd(), chat=None)
        con, legs = e.con, e.engine.legs
        assert store.approve_intent(con, prop.intent_id, prop.nonce, now=e.clock())
    reconcile.startup(con, legs)
    op = store.operation_of_intent(con, prop.intent_id)
    assert op['state'] == store.OpState.ABANDONED
    assert op['confirmed_raw'] == op['reserved_raw'] == '0'
    assert store.get_deal(con, prop.deal_id)['state'] == store.DealState.ABORTED


def test_pause_before_first_action_releases_root(tmp_path):
    e = sim_env(tmp_path)
    prop = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(200), chat=OWNER)
    assert store.approve_intent(e.con, prop.intent_id, prop.nonce)
    store.set_paused(e.con, True)
    e.engine.execute(prop.intent_id)
    op = store.operation_of_intent(e.con, prop.intent_id)
    assert op['state'] == store.OpState.ABANDONED, e.hooks.reports
    assert op['reserved_raw'] == op['confirmed_raw'] == '0'
