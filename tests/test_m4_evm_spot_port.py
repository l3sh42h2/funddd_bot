"""Common EVM port through real Desk/Engine, with native transport substituted."""
import json
import pytest
from dataclasses import replace
from decimal import Decimal as D

from funding_bot.trade import store
from funding_bot.trade.types import SwapResult
from funding_bot.trade.types import Book
from test_trade_engine import live_env, run_approved, OWNER


def events(e, kind):
    return [json.loads(r[0]) for r in e.con.execute('SELECT json FROM exec_events WHERE kind=? ORDER BY rowid', (kind,))]


def test_engine_uses_common_spot_and_futures_attempts_once(tmp_path):
    e = live_env(tmp_path)
    p = e.desk.propose_entry('AIW3', 'okx·bsc', 'aster', D(200), chat=OWNER)
    run_approved(e, p)
    assert store.get_deal(e.con, p.deal_id)['state'] == 'OPEN', e.hooks.reports
    assert len(events(e, 'adapter_spot_prepared')) == len(e.spot.swaps) == 1
    assert len(events(e, 'adapter_spot_claimed')) == 1
    assert events(e, 'adapter_spot_terminal')[0]['status'] == 'SETTLED'
    assert len(events(e, 'adapter_perp_prepared')) == len(e.perp.calls)
    e.engine.execute(p.intent_id)
    assert len(e.spot.swaps) == 1


def test_incomplete_success_never_hedges_requested_quantity(tmp_path):
    e = live_env(tmp_path)
    p = e.desk.propose_entry('AIW3', 'okx·bsc', 'aster', D(200), chat=OWNER)
    send = e.spot.swap
    def missing(*args, **kw):
        return replace(send(*args, **kw), amount_out=0)
    e.spot.swap = missing
    run_approved(e, p)
    assert len(e.spot.swaps) == 1 and not e.perp.calls
    assert not events(e, 'adapter_spot_terminal')
    op = store.operation_of_intent(e.con, p.intent_id)
    assert int(op['reserved_raw']) == 200 * 10**18
    assert store.get_deal(e.con, p.deal_id)['state'] == 'PAUSED'


def test_proven_revert_allows_one_new_attempt_and_keeps_gas_events(tmp_path):
    e = live_env(tmp_path)
    p = e.desk.propose_entry('AIW3', 'okx·bsc', 'aster', D(200), chat=OWNER)
    send, calls = e.spot.swap, []
    def reverted(*args, **kw):
        calls.append(args)
        if len(calls) == 1:
            return SwapResult('0x' + '1'*64, 'reverted', 0, 0, 123, .01, 1, 1)
        return send(*args, **kw)
    e.spot.swap = reverted
    run_approved(e, p)
    assert len(calls) == 2 and len(e.spot.swaps) == 1
    terminal = events(e, 'adapter_spot_terminal')
    assert [x['status'] for x in terminal] == ['REJECTED', 'SETTLED']
    assert len({x['attempt_id'] for x in terminal}) == 2
    assert len(events(e, 'dex')) == 2
    assert store.get_deal(e.con, p.deal_id)['state'] == 'OPEN', e.hooks.reports


def test_changed_wallet_fails_before_spot_submit(tmp_path):
    e = live_env(tmp_path)
    p = e.desk.propose_entry('AIW3', 'okx·bsc', 'aster', D(200), chat=OWNER)
    e.spot.wallet = '0x' + 'c'*40
    run_approved(e, p)
    assert not e.spot.swaps and not e.perp.calls
    assert not e.spot.approvals


def test_thin_book_exit_closes_small_reduce_only_children(tmp_path):
    e = live_env(tmp_path)
    p = e.desk.propose_entry('AIW3', 'okx·bsc', 'aster', D(100), chat=OWNER)
    run_approved(e, p)
    assert e.perp.pos < 0
    e.perp.b = Book(e.perp.b.bids, ((D('.04092'), D(100)),), e.perp.b.ts)
    x = e.desk.propose_exit(p.deal_id, None, False, chat=OWNER)
    run_approved(e, x)
    assert e.perp.pos == 0, e.hooks.reports
    assert store.get_deal(e.con, p.deal_id)['state'] == 'CLOSED'
    assert any(c['ro'] and c['qty'] * c['cap'] < 5 for c in e.perp.calls)


def test_native_mined_balance_mismatch_survives_repeated_recovery(tmp_path):
    import test_trade_evm as fx
    from types import SimpleNamespace as NS
    from funding_bot.trade.types import InstrumentSpec
    from funding_bot.trade import reconcile
    fixture = fx.env.__wrapped__(tmp_path)
    e = next(fixture)
    try:
        inst = InstrumentSpec(chain='bsc', token=fx.AIW3, token_dec=18,
                              perp_venue='aster', perp_symbol='AIW3USDT',
                              units_per_contract=D(1), quote_asset='USDT',
                              verified=True, ident_ev='fixture:verified')
        did = store.create_deal(e.con, coin='AIW3', chain='bsc', token=fx.AIW3,
                                token_dec=18, perp_venue='aster', symbol='AIW3USDT',
                                leg_usd=D(100), owner_json=e.cfg.frozen_json(), sim=False, inst=inst)
        iid, _ = store.create_intent(e.con, deal_id=did, kind='entry', spec={}, plan={})
        op = store.create_operation(e.con, deal_id=did, profile_id='fixture', inst_hash=inst.inst_hash(),
                                    mode='live', side='entry', target_kind='stable_raw_budget',
                                    target_asset=fx.USDT, target_decimals=18, target_raw=100*fx.E18)
        store.link_intent(e.con, op, iid)
        store.set_operation_state(e.con, op, store.OpState.APPROVED)
        store.set_operation_state(e.con, op, store.OpState.RUNNING)
        cid = store.create_clip(e.con, iid, 1, 100 * fx.E18)
        from funding_bot.trade.operations import OperationController
        OperationController(e.con).begin_spot(cid, operation_id=op, reserve_raw=100*fx.E18)
        fx._allow(e)
        e.chain.silent_credit = 5
        result = e.spot.swap(fx.USDT, fx.AIW3, 100 * fx.E18, str(cid))
        assert result.status == 'unknown' and result.tx_state == 'MINED_OK'
        deal, legs = store.get_deal(e.con, did), NS(sim=False, spot=e.spot)
        for _ in range(2):
            assert reconcile.resolve_clips(e.con, deal, legs)
            assert store.get_clip(e.con, cid)['state'] != 'DEX_OK'
            assert 'balanceOf' in store.get_dex_tx(e.con, result.tx_hash)['err']
            assert int(store.get_operation(e.con, op)['reserved_raw']) == 100*fx.E18
        # Missing archive state cannot erase a previously proven discrepancy.
        e.chain.no_state_below = 10**9
        assert reconcile.resolve_clips(e.con, deal, legs)
        e.chain.no_state_below = None
        e.chain.bal[(fx.AIW3, e.w)] -= 5
        assert reconcile.resolve_clips(e.con, deal, legs) == []
        assert store.get_clip(e.con, cid)['state'] == 'DEX_OK'
        assert store.get_dex_tx(e.con, result.tx_hash)['err'] == ''
        assert store.get_operation(e.con, op)['reserved_raw'] == '0'
        assert int(store.get_operation(e.con, op)['confirmed_raw']) == result.amount_in
        before = store.get_clip(e.con, cid)
        assert reconcile.resolve_clips(e.con, deal, legs) == []
        assert store.get_clip(e.con, cid) == before
    finally:
        fixture.close()


@pytest.mark.parametrize('raw', [10**28-1, 10**28+1, 2**96, 12345678901234567890123456789])
def test_raw_quantity_is_exact_through_common_spot_submit(tmp_path, raw):
    from types import SimpleNamespace as NS
    from funding_bot.trade.adapters.spot_execution import submit_evm
    from funding_bot.trade.adapters.contracts import RawAmount
    e = live_env(tmp_path)
    proposal = e.desk.propose_entry('AIW3', 'okx·bsc', 'aster', D(100), chat=OWNER)
    deal = store.get_deal(e.con, proposal.deal_id)
    iid, _ = store.create_intent(e.con, deal_id=deal['id'], kind='exit', spec={}, plan={})
    cid = store.create_clip(e.con, iid, 1, raw)
    store.set_clip_state(e.con, cid, 'DEX_SENT')
    sent = []
    e.spot.build_swap = lambda ti, to, value, **kw: NS(min_receive=1)
    def send(ti, to, value, ref, **kw):
        sent.append(value)
        return SwapResult('0x'+'8'*64, 'ok', value, 1, 1, .01, 1, 1)
    e.spot.swap = send
    from funding_bot import config
    stable, dec = config.OKX_DEX_STABLES['56']
    result = submit_evm(e.con, deal=deal, clip_id=cid, native=e.spot, stable=stable,
                        stable_dec=dec, token_in=deal['token'], token_out=stable,
                        amount_raw=raw, clock=lambda: 1, authorize=lambda *a: None)
    assert sent == [raw] and result.amount_in == raw and result.status == 'ok'
    assert RawAmount('token', raw, 18).amount.as_integer_ratio() == D(str(raw)+'e-18').as_integer_ratio()
