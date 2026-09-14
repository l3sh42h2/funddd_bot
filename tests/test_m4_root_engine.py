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
