"""Production submit port: journal admission, exact native outcome and restart safety."""
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest

from funding_bot.trade import store
from funding_bot.trade.adapters.execution import submit_ioc
from funding_bot.trade.adapters.contracts import AdapterError
from funding_bot.trade.types import Filters, InstrumentSpec, PerpFill, PerpInstrument


def world(tmp_path, venue='aster'):
    con = store.connect(tmp_path / 'trade.db')
    inst = InstrumentSpec(chain='bsc', token='0x'+'a'*40, token_dec=18,
                          perp_venue=venue, perp_symbol='TESTUSDT', units_per_contract=D(1),
                          quote_asset='USDT', ident_ev='fixture:verified', verified=True)
    did = store.create_deal(con, coin='TEST', chain='bsc', token=inst.token, token_dec=18,
                            perp_venue=venue, symbol=inst.perp_symbol, leg_usd=D(100),
                            owner_json='{}', sim=True, inst=inst)
    iid, _ = store.create_intent(con, deal_id=did, kind='entry', spec={'inst_hash': inst.inst_hash()}, plan={})
    clip = store.create_clip(con, iid, 1, 100)
    cid = store.client_order_id(did, 'e', clip, 1, 1)
    calls = []
    native = NS(venue=venue, ioc_partial_terminal=True,
                filters=lambda _: Filters(D('.01'), D('.01'), D('.01'), D(1000), D(1000), D(1), frozenset({'IOC'})),
                instrument=lambda _: PerpInstrument(inst.perp_symbol, 'TEST', 'TEST', D(1), 'USDT', 'PERPETUAL'))
    native.result = PerpFill(cid, 42, 'FILLED', D(2), D(3), D(6), 123)
    def ioc(symbol, side, qty, price, client, ro, **kw):
        assert not con.in_transaction
        assert store.get_perp_order(con, client)['state'] == 'SENT'
        kw['on_signed'](123)
        calls.append((symbol, side, qty, price, client, ro, kw))
        return native.result
    native.ioc = ioc
    args = dict(deal=store.get_deal(con, did), clip_id=clip, native=native,
                account='fixture:account', fill_venue='sim:'+venue, client_id=cid,
                side='SELL', quantity=D(2), price=D(3), reduce_only=False,
                clock=lambda: 100, authorize=lambda *a: None)
    return con, native, calls, args


@pytest.mark.parametrize('venue', ['aster', 'gate', 'hyperliquid'])
def test_actual_port_uses_one_durable_native_attempt(tmp_path, venue):
    con, native, calls, args = world(tmp_path, venue)
    result = submit_ioc(con, **args)
    assert result == native.result and len(calls) == 1
    assert calls[0][-1]['hedge'] is True
    if venue == 'hyperliquid':
        assert calls[0][-1]['links']['deal_id'] == args['deal']['id']
    assert store.get_perp_order(con, args['client_id'])['sign_nonce'] == 123
    with pytest.raises(AdapterError):
        submit_ioc(con, **args)
    assert len(calls) == 1
    con.close()
    restarted = store.connect(tmp_path / 'trade.db')
    with pytest.raises(AdapterError):
        submit_ioc(restarted, **args)
    assert len(calls) == 1


@pytest.mark.parametrize('change', [{'client_id':'another-attempt'}, {'quote':D(0)},
                                     {'qty':D(0)}, {'status':'UNKNOWN'}, {'qty':D('NaN')}])
def test_malformed_or_foreign_native_fill_is_unknown_not_retried(tmp_path, change):
    con, native, calls, args = world(tmp_path)
    native.result = replace(native.result, **change)
    assert submit_ioc(con, **args).status == 'UNKNOWN'
    assert len(calls) == 1
    with pytest.raises(AdapterError):
        submit_ioc(con, **args)
    assert len(calls) == 1


def test_authorization_refusal_precedes_claim_and_network(tmp_path):
    con, native, calls, args = world(tmp_path)
    def reject(*a):
        raise RuntimeError('readonly')
    args['authorize'] = reject
    with pytest.raises(RuntimeError):
        submit_ioc(con, **args)
    assert not calls
    assert store.get_perp_order(con, args['client_id'])['state'] == 'INTENT'


def test_changed_deal_symbol_is_not_sent_under_frozen_instrument(tmp_path):
    con, native, calls, args = world(tmp_path)
    args['deal']['symbol'] = 'OTHERUSDT'
    with pytest.raises(AdapterError):
        submit_ioc(con, **args)
    assert not calls and store.get_perp_order(con, args['client_id']) is None


@pytest.mark.parametrize('change', ['clip', 'client', 'venue', 'frozen'])
def test_foreign_scope_refused_before_quote_or_journal(tmp_path, change):
    con, native, calls, args = world(tmp_path)
    if change == 'clip':
        other = store.create_deal(con, coin='OTHER', chain='bsc', token='0x'+'b'*40, token_dec=18,
                                  perp_venue='aster', symbol='OTHERUSDT', leg_usd=D(100), owner_json='{}', sim=True)
        iid, _ = store.create_intent(con, deal_id=other, kind='entry', spec={}, plan={})
        args['clip_id'] = store.create_clip(con, iid, 1, 100)
    elif change == 'client':
        args['client_id'] = args['client_id'].replace('-e01-', '-e99-')
    elif change == 'venue':
        args['fill_venue'] = 'aster'
    else:
        args['deal']['inst_json'] = args['deal']['inst_json'].replace('fixture:verified', 'fixture:changed')
    native.filters = lambda _: pytest.fail('scope must be checked before native quote')
    with pytest.raises(AdapterError):
        submit_ioc(con, **args)
    assert not calls and con.execute('SELECT count(*) FROM perp_orders').fetchone()[0] == 0


def test_legacy_recovery_needs_no_new_metadata_or_common_prepared_event(tmp_path):
    from funding_bot.trade.adapters.execution import settle_ioc
    con, native, calls, args = world(tmp_path)
    store.perp_order_intent(con, clip_id=args['clip_id'], client_id=args['client_id'], venue='sim:aster',
                            symbol=args['deal']['symbol'], side='SELL', reduce_only=False,
                            tif='IOC', price=D(3), qty=D(2))
    store.perp_order_sent(con, args['client_id'])
    native.filters = lambda _: pytest.fail('legacy recovery needs no current trading metadata')
    native.settle_unknown = lambda *a, **kw: native.result
    assert settle_ioc(con, deal=args['deal'], native=native, account=args['account'],
                      client_id=args['client_id'], pos_before=D(0), since_ms=0) == native.result
    assert not calls


@pytest.mark.parametrize('status', ['EXPIRED', 'CANCELED', 'CANCELLED'])
def test_zero_cancelled_native_alias_is_compatible_expired(tmp_path, status):
    con, native, calls, args = world(tmp_path)
    native.result = replace(native.result, status=status, qty=D(0), avg_px=D(0), quote=D(0))
    result = submit_ioc(con, **args)
    assert result.status == 'EXPIRED' and result.qty == result.quote == D(0)


def test_rejected_with_positive_execution_is_unresolved_not_discarded(tmp_path):
    con, native, calls, args = world(tmp_path)
    native.result = replace(native.result, status='REJECTED')
    assert submit_ioc(con, **args).status == 'UNKNOWN'
    assert len(calls) == 1
