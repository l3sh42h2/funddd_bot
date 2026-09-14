"""Extension matrix uses durable core coordinator and production adapter ports."""
from dataclasses import replace
from decimal import Decimal as D
import time
import pytest

from funding_bot.trade import store, leg_accounting
from funding_bot.trade.operation_plan import OperationPlan, LegBound
from funding_bot.trade.generic_operations import GenericOperationCoordinator, EventAttemptJournal
from common_adapter_fixtures import spec, registry, Transport, context


def make_plan(a, b, *, operation_id='op-entry', kind='entry'):
    bounds = {}
    for s in (a, b):
        side = 'BUY' if s.direction == 'long' else 'SELL'
        if kind == 'exit':
            side = 'SELL' if side == 'BUY' else 'BUY'
        bounds[s.leg_id] = LegBound(side, D(0), D(2) / s.multiplier, s.quote_currency, D(20),
            reduce_only=kind == 'exit' and s.capabilities.market_kind == 'perpetual',
            min_receive=D(0), min_receive_currency=s.quote_currency)
    return OperationPlan(operation_id, kind, (a, b), a.leg_id, D(2), D(2),
        time.time() + 600, 'floor', bounds, {'owner': 'fixture', 'mode': 'dry'})


def setup(tmp_path, a, b):
    con = store.connect(tmp_path / 'trade.db')
    plan = make_plan(a, b)
    did = store.create_generic_deal(con, asset_id=a.asset_id,
        position_spec={'generic_position_v1': True, 'legs': plan.to_dict()['legs']}, owner_json='{}', sim=True)
    reg = registry()
    coordinator = GenericOperationCoordinator(con, reg, None)
    iid, nonce = coordinator.propose(deal=store.get_deal(con, did), plan=plan, profile_id='fixture')
    transports = (Transport(), Transport())
    for t in transports:
        t.clock = time.time()
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=iid, operation_id=plan.operation_id, leg_id=s.leg_id)
                for s in plan.legs]
    ctx = context(plan.legs, transports, journals)
    coordinator.context = ctx
    assert coordinator.approve(iid, nonce)
    return con, did, plan, iid, coordinator, transports


@pytest.mark.parametrize('first', ['fixture_cex_spot', 'fixture_evm_spot', 'fixture_sol_spot'])
@pytest.mark.parametrize('second,venue', [('fixture_cex_perp', 'aster'), ('fixture_cex_perp', 'gate'),
                                        ('fixture_dex_perp', 'hyperliquid')])
def test_independent_spot_and_futures_entry_rebuild(tmp_path, first, second, venue):
    a, b = spec(first, 'lead', 'long'), spec(second, 'hedge', 'short', venue=venue)
    con, did, plan, iid, coordinator, transports = setup(tmp_path, a, b)
    outcome = coordinator.execute(iid)
    assert outcome.state == store.OpState.OPEN
    assert [len(x.sent) for x in transports] == [1, 1]
    assert store.get_operation(con, plan.operation_id)['reserved_raw'] == '0'
    assert [D(x['qty']) for x in leg_accounting.rebuild(con, deal_id=did)['legs']] == [D(2), D(-2)]
    con.close()
    con = store.connect(tmp_path / 'trade.db')
    assert [D(x['qty']) for x in leg_accounting.rebuild(con, deal_id=did)['legs']] == [D(2), D(-2)]


@pytest.mark.parametrize('reverse', [False, True])
def test_two_futures_opposite_directions_distinct_currencies(tmp_path, reverse):
    a = spec('fixture_cex_perp', 'lead', 'short' if reverse else 'long', quote='USDT')
    b = spec('fixture_dex_perp', 'hedge', 'long' if reverse else 'short', quote='USDC')
    con, did, plan, iid, coordinator, transports = setup(tmp_path, a, b)
    assert coordinator.execute(iid).state == store.OpState.OPEN
    quantities = [D(x['qty']) for x in leg_accounting.rebuild(con, deal_id=did)['legs']]
    assert quantities == ([D(-2), D(2)] if reverse else [D(2), D(-2)])


def test_leading_ack_loss_reopen_resolves_without_duplicate(tmp_path):
    a, b = spec('fixture_sol_spot', 'lead', 'long'), spec('fixture_cex_perp', 'hedge', 'short', venue='gate')
    con, did, plan, iid, coordinator, transports = setup(tmp_path, a, b)
    transports[0].fail_after_send = True
    assert coordinator.execute(iid).state == store.OpState.PAUSED_UNKNOWN
    assert [len(x.sent) for x in transports] == [1, 0]
    assert int(store.get_operation(con, plan.operation_id)['reserved_raw']) > 0
    con.close()
    con = store.connect(tmp_path / 'trade.db')
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=iid, operation_id=plan.operation_id, leg_id=s.leg_id)
                for s in plan.legs]
    coordinator = GenericOperationCoordinator(con, registry(), context(plan.legs, transports, journals))
    outcome = coordinator.resume(iid)
    assert [len(x.sent) for x in transports] == [1, 0]
    assert outcome.state == store.OpState.PAUSED_RISK
    assert D(leg_accounting.rebuild(con, deal_id=did)['legs'][0]['qty']) == 2


def test_base_fee_changes_hedge_actual_quantity(tmp_path):
    from funding_bot.trade.fees import FeeComponent
    a, b = spec('fixture_cex_spot', 'lead', 'long'), spec('fixture_dex_perp', 'hedge', 'short')
    con, did, plan, iid, coordinator, transports = setup(tmp_path, a, b)
    transports[0].fees = (FeeComponent('platform', a.asset_id, 6, 100000, a.account, False, False),)
    outcome = coordinator.execute(iid)
    assert transports[1].sent[0][2].quantity == D('1.9')
    assert sum(D(x['qty']) for x in leg_accounting.rebuild(con, deal_id=did)['legs']) == 0


@pytest.mark.parametrize('first,second', [('fixture_cex_spot', 'fixture_cex_perp'),
    ('fixture_evm_spot', 'fixture_dex_perp'), ('fixture_sol_spot', 'fixture_cex_perp'),
    ('fixture_cex_perp', 'fixture_dex_perp')])
def test_entry_then_exit_uses_owned_two_leg_inventory(tmp_path, first, second):
    a, b = spec(first, 'lead', 'long'), spec(second, 'hedge', 'short')
    con, did, plan, iid, coordinator, transports = setup(tmp_path, a, b)
    assert coordinator.execute(iid).state == store.OpState.OPEN
    exit_plan = make_plan(a, b, operation_id='op-exit', kind='exit')
    exit_id, nonce = coordinator.propose(deal=store.get_deal(con, did), plan=exit_plan, profile_id='fixture')
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=exit_id, operation_id=exit_plan.operation_id, leg_id=s.leg_id)
                for s in exit_plan.legs]
    coordinator.context = context(exit_plan.legs, transports, journals)
    assert coordinator.approve(exit_id, nonce)
    outcome = coordinator.execute(exit_id)
    assert outcome.state == store.OpState.CLOSED
    assert store.get_deal(con, did)['state'] == store.DealState.CLOSED
    assert all(D(x['qty']) == 0 for x in leg_accounting.rebuild(con, deal_id=did)['legs'])
    assert all(x[2].reduce_only for x in transports[1].sent if x[2].side == 'BUY')


def test_generic_dashboard_reads_two_legs_without_legacy_exchange_assumptions(tmp_path):
    from funding_bot.core.readmodel import load_deals
    from funding_bot.cabinet import deal_card
    a, b = spec('fixture_cex_perp', 'lead', 'long'), spec('fixture_dex_perp', 'hedge', 'short')
    con, did, plan, iid, coordinator, transports = setup(tmp_path, a, b)
    coordinator.execute(iid)
    view = load_deals(con)['deals'][0]
    assert len(view['legs']['legs']) == 2
    rendered = deal_card(view)
    assert 'lead' in rendered and 'hedge' in rendered and 'okx·' not in rendered
    assert 'PnL: нет подтверждённой оценки' in rendered


def test_generic_startup_and_positions_do_not_use_legacy_token_decimals(tmp_path):
    from funding_bot.trade import reconcile
    a, b = spec('fixture_cex_perp', 'lead', 'long'), spec('fixture_dex_perp', 'hedge', 'short')
    con, did, plan, iid, coordinator, transports = setup(tmp_path, a, b)
    coordinator.execute(iid)
    before = [len(x.sent) for x in transports]
    def factory(c, intent, deal, frozen):
        journals = [EventAttemptJournal(c, deal_id=did, intent_id=intent['id'],
                    operation_id=plan.operation_id, leg_id=s.leg_id) for s in plan.legs]
        return context(plan.legs, transports, journals)
    report = reconcile.startup(con, lambda sim: None, generic_registry=registry(), generic_context_factory=factory)
    assert len(report.deals) == 1 and report.deals[0].check.matched is True
    rows, matched, _ = reconcile.positions(con, lambda sim: None, now=time.time(),
        generic_registry=registry(), generic_context_factory=factory)
    assert matched is True and len(rows[0]['generic_legs']['legs']) == 2
    assert [len(x.sent) for x in transports] == before


@pytest.mark.parametrize('first', ['fixture_cex_spot', 'fixture_evm_spot', 'fixture_sol_spot', 'fixture_cex_perp'])
def test_generic_plan_runs_in_actual_engine_queue_once(tmp_path, first):
    from funding_bot.trade.engine import Engine, Conns
    from types import SimpleNamespace
    a, b = spec(first, 'lead', 'long'), spec('fixture_dex_perp', 'hedge', 'short')
    con, did, plan, iid, coordinator, transports = setup(tmp_path, a, b)
    def factory(c, intent, deal, frozen):
        journals = [EventAttemptJournal(c, deal_id=did, intent_id=intent['id'],
                    operation_id=plan.operation_id, leg_id=s.leg_id) for s in plan.legs]
        return context(plan.legs, transports, journals)
    engine = Engine(Conns(tmp_path / 'trade.db'), lambda sim: None, SimpleNamespace(),
                    generic_context_factory=factory, generic_registry=registry())
    engine.submit(iid)
    engine.submit(iid)
    engine.start()
    try:
        assert engine.wait_idle(3)
        assert store.get_intent(con, iid)['status'] == store.IntentStatus.DONE
        assert [len(x.sent) for x in transports] == [1, 1]
    finally:
        engine.stop()
        engine._thread.join(2)


def test_readonly_hedge_refuses_before_leading_submission(tmp_path):
    a, b = spec('fixture_cex_spot', 'lead', 'long'), spec('fixture_cex_perp', 'hedge', 'short')
    con, did, plan, iid, coordinator, transports = setup(tmp_path, a, b)
    transports[1].allow = False
    try:
        coordinator.execute(iid)
    except Exception:
        pass
    assert [len(x.sent) for x in transports] == [0, 0]


def test_exposure_limit_is_enforced_before_external_send(tmp_path):
    a, b = spec('fixture_cex_spot', 'lead', 'long'), spec('fixture_cex_perp', 'hedge', 'short')
    con = store.connect(tmp_path / 'trade.db')
    plan = replace(make_plan(a, b), max_unhedged_exposure=D('.001'))
    did = store.create_generic_deal(con, asset_id=a.asset_id,
        position_spec={'generic_position_v1': True, 'legs': plan.to_dict()['legs']}, owner_json='{}', sim=True)
    coordinator = GenericOperationCoordinator(con, registry(), None)
    iid, nonce = coordinator.propose(deal=store.get_deal(con, did), plan=plan, profile_id='fixture')
    transports = (Transport(), Transport())
    for t in transports:
        t.clock = time.time()
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=iid, operation_id=plan.operation_id, leg_id=s.leg_id)
                for s in plan.legs]
    coordinator.context = context(plan.legs, transports, journals)
    assert coordinator.approve(iid, nonce)
    try:
        coordinator.execute(iid)
    except Exception:
        pass
    # A reject or a clipped program are both safe; an unbounded first submit is not.
    assert all(action.quantity * a.multiplier <= plan.max_unhedged_exposure
               for _, _, action in transports[0].sent)
