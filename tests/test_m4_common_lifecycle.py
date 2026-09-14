"""Durable lifecycle boundaries: no network and no fabricated final fills."""
from types import SimpleNamespace
from decimal import Decimal as D
import pytest
from funding_bot.trade import store
from funding_bot.trade import operation_roots as roots
from funding_bot.trade.operations import OperationController, EndDecision
from funding_bot.trade.adapters.obligations import unresolved
from test_m4_operation_roots import con, deal, plan, spec, running, APPROVAL_1, approve


def setup(con):
    d = deal(con)
    iid, nonce = roots.propose(con, deal=d, kind='entry', spec=spec('entry', APPROVAL_1),
                               plan=plan(d['id'], 'entry', 100), profile_id='bsc_okx_aster', chat=None)
    running(con, iid, nonce)
    store.set_deal_state(con, d['id'], store.DealState.ENTERING)
    return SimpleNamespace(did=d['id'], iid=iid, kind='entry', it=store.get_intent(con, iid),
                           deal=store.get_deal(con, d['id']), op_id=store.operation_of_intent(con, iid)['id'])


def pending_perp(con, run):
    clip = store.create_clip(con, run.iid, 1, 100)
    store.perp_order_intent(con, clip_id=clip, client_id=f'fb-{run.did}-e{clip:02d}-c1-a1',
                           venue='sim:aster', symbol='ROOTUSDT', side='SELL', reduce_only=False,
                           tif='IOC', price=D(1), qty=D(1))


def test_unknown_perp_with_zero_reserve_cannot_be_abandoned(con):
    run = setup(con)
    pending_perp(con, run)
    assert store.get_operation(con, run.op_id)['reserved_raw'] == '0'
    target = OperationController(con).pause(run, SimpleNamespace(reason='stop', text='stop'),
                                          progressed=False, empty=True)
    assert target == store.DealState.PAUSED
    assert store.get_operation(con, run.op_id)['state'] == store.OpState.PAUSED_UNKNOWN
    assert unresolved(con, run.deal) == ('perp_unresolved',)
    before = con.total_changes
    with pytest.raises(store.StoreError, match='unresolved execution'):
        roots.propose(con, deal=run.deal, kind='entry', spec=spec('entry', APPROVAL_1),
                      plan=plan(run.did, 'entry', 100), profile_id='bsc_okx_aster', chat=None)
    assert con.total_changes == before


def test_finish_is_atomic_when_second_transition_fails(con, monkeypatch):
    run = setup(con)
    before = (store.get_operation(con, run.op_id), store.get_intent(con, run.iid), store.get_deal(con, run.did))
    original = store.set_intent_status
    def fail(*args, **kwargs):
        raise store.StoreError('injected terminal write failure')
    monkeypatch.setattr(store, 'set_intent_status', fail)
    with pytest.raises(store.StoreError, match='injected'):
        OperationController(con).commit_end(run, EndDecision(store.DealState.OPEN, store.IntentStatus.DONE,
                                                             (store.OpState.OPEN,)))
    assert (store.get_operation(con, run.op_id), store.get_intent(con, run.iid), store.get_deal(con, run.did)) == before
    assert not con.execute("SELECT 1 FROM exec_events WHERE kind='operation_end'").fetchone()
    monkeypatch.setattr(store, 'set_intent_status', original)


def test_terminal_context_cannot_be_paused_after_report_failure(con):
    run = setup(con)
    controller = OperationController(con)
    controller.commit_end(run, EndDecision(store.DealState.OPEN, store.IntentStatus.DONE, (store.OpState.OPEN,)))
    before = (store.get_operation(con, run.op_id), store.get_intent(con, run.iid), store.get_deal(con, run.did))
    with pytest.raises(store.StoreError, match='no longer running'):
        controller.pause(run, SimpleNamespace(reason='error', text='UI failed'), progressed=True, empty=False)
    assert (store.get_operation(con, run.op_id), store.get_intent(con, run.iid), store.get_deal(con, run.did)) == before


def test_real_engine_unknown_perp_blocks_resume_before_new_plan(tmp_path):
    from test_trade_engine import live_env, run_approved, OWNER
    from funding_bot.trade.engine import Refused
    e = live_env(tmp_path, clip="300")
    e.perp.script = ["unknown_lost"]
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    run_approved(e, p)
    op = store.operation_of_intent(e.con, p.intent_id)
    assert op['state'] == store.OpState.PAUSED_UNKNOWN and op['reserved_raw'] == '0'
    before = tuple(e.con.execute("SELECT count(*) FROM " + t).fetchone()[0]
                   for t in ('intents', 'clips', 'perp_orders'))
    with pytest.raises(Refused):
        e.desk.propose_resume(p.deal_id, chat=OWNER)
    assert store.get_operation(e.con, op['id']) == op
    assert before == tuple(e.con.execute("SELECT count(*) FROM " + t).fetchone()[0]
                           for t in ('intents', 'clips', 'perp_orders'))
    assert len(e.spot.swaps) == len(e.perp.calls) == 1


def test_unknown_leverage_before_clip_preserves_active_owner(tmp_path, monkeypatch):
    import sol_c2_world as W
    w = W.make_world(tmp_path)
    original = w.venue.exchange
    def lost(payload):
        if payload['action']['type'] == 'updateLeverage':
            return W.S.Resp(503, {'error': 'unavailable'})
        return original(payload)
    monkeypatch.setattr(w.venue, 'exchange', lost)
    p = w.desk.propose_profile_entry(W.entry_cmd(), chat=None)
    W.approve_run(w, p)
    d = store.get_deal(w.con, p.deal_id)
    op = store.operation_of_intent(w.con, p.intent_id)
    assert 'perp_native_unresolved' in unresolved(w.con, d)
    assert op['state'] == store.OpState.PAUSED_UNKNOWN
    assert d['state'] == store.DealState.PAUSED
    assert op['reserved_raw'] == op['confirmed_raw'] == '0'
    assert not w.sol.sends and not w.venue.calls
    assert w.con.execute('SELECT count(*) FROM clips').fetchone()[0] == 0


def test_admission_root_failure_rolls_back_intent(con, monkeypatch):
    d = deal(con)
    iid, nonce = roots.propose(con, deal=d, kind='entry', spec=spec('entry', APPROVAL_1),
                               plan=plan(d['id'], 'entry', 100), profile_id='bsc_okx_aster', chat=None)
    approve(con, iid, nonce)
    run = SimpleNamespace(did=d['id'], iid=iid, kind='entry', it=store.get_intent(con, iid), deal=d,
                           op_id=store.operation_of_intent(con, iid)['id'], legs=SimpleNamespace(sim=True))
    def fail(*args):
        raise store.StoreError('root CAS failure')
    monkeypatch.setattr(roots, 'start_linked', fail)
    with pytest.raises(store.StoreError, match='root CAS'):
        OperationController(con).admit(run)
    assert store.get_intent(con, iid)['status'] == store.IntentStatus.APPROVED
    assert store.get_operation(con, run.op_id)['state'] == store.OpState.APPROVED
    assert not con.execute("SELECT 1 FROM exec_events WHERE kind='start'").fetchone()
