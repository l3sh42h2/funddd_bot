"""M4 shared monetary rules: no keys, RPC, signing or mutable replay source."""
from decimal import Decimal as D, ROUND_FLOOR, ROUND_CEILING
import pytest
from funding_bot.trade import store
from funding_bot.trade.exposure import Exposure
from funding_bot.trade.operations import OperationController, SpotSettlement


@pytest.mark.parametrize('m', [D('.1'), D(1), D(1000)])
@pytest.mark.parametrize('step', [D('.01'), D(1)])
def test_historical_rounding_equivalence(m, step):
    e = Exposure(D(1), m, step)
    for tokens in map(D, ('0', '.0001', '.99', '1', '17.951', '10000.001')):
        for short in map(D, ('0', '.01', '.019', '1', '19.999')):
            delta = tokens - short * m
            floor = lambda x: (x / step).to_integral_value(ROUND_FLOOR) * step
            ceil = lambda x: (x / step).to_integral_value(ROUND_CEILING) * step
            for kind in ('entry', 'exit', 'rehedge'):
                old = (None, D(0), False)
                if kind in ('entry', 'rehedge') and delta >= step * m:
                    old = ('SELL', floor(delta / m), False)
                elif kind in ('exit', 'rehedge') and delta < 0:
                    old = ('BUY', min(ceil(-delta / m), short), True)
                got = e.decide(tokens, short, kind)
                assert (got.side, got.quantity, got.reduce_only) == old
                target = floor(tokens / m)
                got = e.decide(tokens, short, kind, rounding='target')
                if kind == 'entry':
                    assert got.quantity == max(D(0), target - short)
                elif kind == 'exit':
                    assert got.quantity == max(D(0), short - target)
                    assert got.deficit == (target > short)
                else:
                    assert got.quantity == abs(short - target)


def test_capacity_exit_and_multiplier_contract():
    e = Exposure(D(100), D(1000), D('.1'))
    assert e.target(D(21)) == D('2.1')
    assert e.token_step == D(1)
    got = e.decide(D(21), D(1), 'entry', rounding='target', capacity=D('1.5'))
    assert got.quantity == D('.5') and got.surplus
    got = e.decide(D(21), D(1), 'exit', rounding='target')
    assert got.side is None and got.deficit
    got = e.decide(D('.2'), D(5), 'exit', last_full=True)
    assert got.quantity == D(5) and got.reduce_only


@pytest.fixture
def journal(tmp_path):
    con = store.connect(tmp_path / 'trade.db')
    did = store.create_deal(con, coin='FIXTURE', chain='bsc', token='fixture', token_dec=6,
                           perp_venue='fake', symbol='FIXTURE', leg_usd=D(1), owner_json='{}', sim=True)
    iid, _ = store.create_intent(con, deal_id=did, kind='entry', spec={}, plan={})
    oid = store.create_operation(con, deal_id=did, profile_id='fixture', inst_hash='fixture-hash',
                                 mode='dry', side='entry', target_kind='stable_raw_budget',
                                 target_asset='USDC', target_decimals=6, target_raw=100)
    store.link_intent(con, oid, iid)
    store.set_operation_state(con, oid, store.OpState.APPROVED)
    store.set_operation_state(con, oid, store.OpState.RUNNING)
    cid = store.create_clip(con, iid, 1, 100)
    yield con, oid, cid
    con.close()


def test_replay_does_not_double_reserve_or_settle(journal):
    con, oid, cid = journal
    c = OperationController(con)
    c.begin_spot(cid, operation_id=oid, reserve_raw=100)
    with pytest.raises(store.StoreError):
        c.begin_spot(cid, operation_id=oid, reserve_raw=100)
    store.set_clip_state(con, cid, store.ClipState.DEX_UNKNOWN)
    assert store.operation_remaining(store.get_operation(con, oid)) == 0
    # Recovery can commit a partial spend; refund becomes available only now.
    assert c.settle_spot(cid, SpotSettlement(True, 60, 57), operation_id=oid, reserve_raw=100)
    assert not c.settle_spot(cid, SpotSettlement(True, 60, 57), operation_id=oid, reserve_raw=100)
    assert store.operation_remaining(store.get_operation(con, oid)) == 40
    with pytest.raises(store.StoreError):
        c.settle_spot(cid, SpotSettlement(True, 61, 57), operation_id=oid, reserve_raw=100)
    with pytest.raises(store.StoreError):
        c.settle_spot(cid, SpotSettlement(False), operation_id=oid, reserve_raw=100)
    assert store.get_operation(con, oid)['confirmed_raw'] == '60'


def test_failure_before_reserve_commit_restores_clip(journal):
    con, oid, cid = journal
    with pytest.raises(store.StoreError):
        OperationController(con).begin_spot(cid, operation_id=oid, reserve_raw=101)
    assert store.get_clip(con, cid)['state'] == 'PLANNED'
    assert store.get_operation(con, oid)['reserved_raw'] == '0'


def test_failure_between_clip_and_budget_rolls_back_both(journal, monkeypatch):
    con, oid, cid = journal
    c = OperationController(con)
    c.begin_spot(cid, operation_id=oid, reserve_raw=100)
    class Crash(BaseException):
        pass
    def crash(*args, **kwargs):
        raise Crash()
    monkeypatch.setattr(store, 'operation_settle', crash)
    with pytest.raises(Crash):
        c.settle_spot(cid, SpotSettlement(True, 60, 57), operation_id=oid, reserve_raw=100)
    assert store.get_clip(con, cid)['state'] == 'DEX_SENT'
    op = store.get_operation(con, oid)
    assert (op['reserved_raw'], op['confirmed_raw']) == ('100', '0')


def test_wrong_operation_and_zero_flow_proof(journal):
    con, oid, cid = journal
    c = OperationController(con)
    with pytest.raises(store.StoreError):
        c.begin_spot(cid)
    c.begin_spot(cid, operation_id=oid, reserve_raw=100)
    assert c.settle_spot(cid, SpotSettlement(False), operation_id=oid, reserve_raw=100)
    assert store.operation_remaining(store.get_operation(con, oid)) == 100
    assert not c.settle_spot(cid, SpotSettlement(False), operation_id=oid, reserve_raw=100)


def test_cash_flow_projection_partial_fill_and_unknown_amount(journal):
    from funding_bot.trade.ledger_flows import spot_quote_flows, perp_quote_flows
    con, oid, cid = journal
    c = OperationController(con)
    c.begin_spot(cid, operation_id=oid, reserve_raw=100)
    c.settle_spot(cid, SpotSettlement(True, 60, 57), operation_id=oid, reserve_raw=100)
    did = store.get_operation(con, oid)['deal_id']
    flows = spot_quote_flows(con, did, 6)
    assert flows.net == D('-.000060') and not flows.missing
    con.execute('INSERT INTO perp_orders(client_id,side,state,executed_qty,cum_quote) VALUES(?,?,?,?,?)',
                (f'fb-{did}-e01-c1-a1', 'SELL', 'PARTIALLY_FILLED', '2', '10'))
    # Distinct native order: only the proven partial quantity/quote is included.
    con.execute('INSERT INTO perp_orders(client_id,side,state,executed_qty,cum_quote) VALUES(?,?,?,?,?)',
                (f'fb-{did}-x01-c1-a1', 'BUY', 'FILLED', '1', '4'))
    assert perp_quote_flows(con, did).net == D(6)
    # Same prefix substring in another deal cannot pollute this deal.
    con.execute('INSERT INTO perp_orders(client_id,side,state,cum_quote) VALUES(?,?,?,?)',
                (f'fb-{did}OTHER-x01-c1-a1', 'BUY', 'FILLED', '9000'))
    assert perp_quote_flows(con, did).net == D(6)
    con.execute('UPDATE perp_orders SET cum_quote=NULL WHERE client_id=?', (f'fb-{did}-x01-c1-a1',))
    assert perp_quote_flows(con, did).net is None
    assert perp_quote_flows(con, did).legacy_net == D(10)


def test_common_loop_finishes_started_hedge_then_stops_next_clip(journal):
    from funding_bot.trade.operations import ClipLifecycle
    con, oid, existing = journal
    old_intent = store.get_clip(con, existing)['intent_id']
    did = store.get_operation(con, oid)['deal_id']
    iid, _ = store.create_intent(con, deal_id=did, kind='entry', spec={}, plan={})
    paused = False
    events = []
    def guard():
        if paused: raise RuntimeError('draining')
    def spot(*_):
        nonlocal paused
        assert not con.in_transaction
        events.append('spot'); paused = True
    program = ClipLifecycle(iid, [50,50], lambda *_:None, guard, lambda n,_:n,
                            lambda *_:None, spot, lambda *_:events.append('hedge'),
                            lambda *_:events.append('settled'), lambda c,d,q,t:q,
                            lambda:events.append('finished'))
    with pytest.raises(RuntimeError, match='draining'):
        OperationController(con).run_clips(program)
    assert events == ['spot','hedge','settled']
    assert con.execute('SELECT count(*) FROM clips WHERE intent_id=?',(iid,)).fetchone()[0] == 1
    # Same durable action cannot be replayed as a new clip after a crash/re-entry.
    paused = False
    with pytest.raises(Exception):
        OperationController(con).run_clips(program)
    assert events == ['spot','hedge','settled']


def test_common_loop_never_hedges_an_unknown_spot(journal):
    from funding_bot.trade.operations import ClipLifecycle
    con, oid, _ = journal
    iid, _ = store.create_intent(con, deal_id=store.get_operation(con, oid)['deal_id'],
                                 kind='exit', spec={}, plan={})
    events = []
    def unknown(*_): raise RuntimeError('unknown receipt')
    program = ClipLifecycle(iid, [20], lambda *_:None, lambda:None, lambda n,_:n,
                            lambda *_:None, unknown, lambda *_:events.append('hedge'),
                            lambda *_:events.append('settled'), lambda c,d,q,t:q,
                            lambda:events.append('finished'))
    with pytest.raises(RuntimeError, match='unknown receipt'):
        OperationController(con).run_clips(program)
    assert not events
