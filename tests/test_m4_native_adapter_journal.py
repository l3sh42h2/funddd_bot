from dataclasses import replace
from decimal import Decimal as D
import pytest
from funding_bot.trade import store
from funding_bot.trade.adapters.contracts import Action, Prepared, Quote, AdapterError
from funding_bot.trade.adapters.native_journal import PerpJournal
from test_migration_m3 import spec


def setup(tmp_path):
    con = store.connect(tmp_path/'trade.db')
    did = store.create_deal(con, coin='A', chain='bsc', token='0x'+'a'*40, token_dec=18,
                            perp_venue='aster', symbol='AUSDT', leg_usd=D(100), owner_json='{}', sim=True)
    iid, _ = store.create_intent(con, deal_id=did, kind='entry', spec={}, plan={})
    clip = store.create_clip(con, iid, 1, 100)
    leg = spec(instrument='AUSDT')
    cid = store.client_order_id(did, 'e', clip, 1, 1)
    action = Action(cid, leg.leg_id, 'SELL', D(2))
    quote = Quote(action, 200, D(2), D(4), leg.asset_id, 'USDC', '{"price_cap":"2"}')
    prepared = Prepared(cid, quote, leg.fingerprint)
    return con, PerpJournal(con, clip_id=clip, fill_venue='sim:aster', spec=leg), prepared


def test_native_order_is_only_submit_authority_and_repeated_claim_refuses(tmp_path):
    con, journal, prepared = setup(tmp_path)
    journal.prepare(prepared); journal.prepare(prepared)
    assert con.execute('SELECT count(*) FROM perp_orders').fetchone()[0] == 1
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='core_leg_attempts'").fetchone()
    journal.claim(prepared)
    assert not con.in_transaction
    assert store.get_perp_order(con, prepared.attempt_id)['state'] == 'SENT'
    with pytest.raises(AdapterError):
        journal.claim(prepared)
    journal.on_signed(prepared.attempt_id, 123)
    assert journal.lookup(prepared.attempt_id)['sign_nonce'] == 123
    assert journal.lookup(prepared.attempt_id)['resolved_ts'] is None
    with pytest.raises(store.StoreError):
        journal.on_signed(prepared.attempt_id, 124)


def test_reusing_native_identity_with_changed_request_refuses(tmp_path):
    con, journal, prepared = setup(tmp_path)
    journal.prepare(prepared)
    changed = replace(prepared, quote=replace(prepared.quote, expires_at=201))
    with pytest.raises(AdapterError):
        journal.prepare(changed)
    with pytest.raises(AdapterError):
        journal.claim(changed)
    assert store.get_perp_order(con, prepared.attempt_id)['state'] == 'INTENT'


@pytest.mark.parametrize('venue', ['aster', 'gate', 'hyperliquid'])
def test_native_binding_keeps_hedge_permission_and_durable_signature(tmp_path, venue):
    from types import SimpleNamespace as NS
    from funding_bot.trade.adapters.futures_bindings import bind
    from funding_bot.trade.adapters.context import AdapterContext
    from funding_bot.trade.adapters.registry import production_registry
    from funding_bot.trade.types import PerpFill, PerpInstrument, Filters
    con, old_journal, old_prepared = setup(tmp_path)
    leg = replace(old_journal.spec, venue=venue, adapter_id=venue)
    journal = PerpJournal(con, clip_id=old_journal.clip_id, fill_venue='sim:'+venue, spec=leg)
    links = dict(deal_id=journal.deal_id, intent_id=journal.intent_id, clip_id=journal.clip_id)
    seen = []
    def ioc(symbol, side, qty, price, cid, ro, **kw):
        assert not con.in_transaction
        assert journal.lookup(cid)['state'] == 'SENT'
        assert kw['hedge'] is True
        assert kw.get('links') == (links if venue == 'hyperliquid' else None)
        kw['on_signed'](42)
        seen.append(cid)
        return PerpFill(cid, 7, 'FILLED', qty, price, qty*price, 42)
    native = NS(ioc=ioc,
        instrument=lambda s: PerpInstrument(s, 'A', 'A', D(1), 'USDC', 'PERPETUAL'),
        filters=lambda s: Filters(leg.tick, leg.step, leg.step, D(100), D(100), D(1), frozenset({'IOC'})))
    context = AdapterContext()
    context.add(leg, bind(native, journal=journal, authorize=lambda *a: None,
                         attempt_lookup=journal.lookup, on_signed=journal.on_signed,
                         clock=lambda: 100, hedge=True, links=links))
    adapter = production_registry().build(leg, context)
    quote = adapter.quote(old_prepared.quote.action, {'price_cap': D(2)})
    prepared = adapter.prepare(old_prepared.attempt_id, quote)
    result = adapter.submit(prepared)
    assert result.version == 2 and result.executed_quantity == D(2)
    assert result.perp_quote.amount == D(4)
    assert journal.lookup(prepared.attempt_id)['sign_nonce'] == 42
    with pytest.raises(AdapterError):
        adapter.submit(prepared)
    assert seen == [prepared.attempt_id]


def test_hl_reduce_only_dust_preserves_native_exception(tmp_path):
    from types import SimpleNamespace as NS
    from funding_bot.trade.adapters.futures_bindings import bind
    from funding_bot.trade.types import PerpInstrument, Filters
    leg = spec('hyperliquid')
    native = NS(instrument=lambda s: PerpInstrument(s, 'A', 'A', D(1), 'USDC', 'PERPETUAL'),
                filters=lambda s: Filters(leg.tick, leg.step, leg.step, D(100), D(100), D(10), frozenset({'IOC'})))
    binding = bind(native, journal=None, authorize=None, attempt_lookup=None, on_signed=None)
    action = Action('dust', leg.leg_id, 'BUY', D(1), True)
    assert binding.quote(leg, action, {'price_cap': D(1)}).action == action
    with pytest.raises(AdapterError):
        binding.quote(leg, replace(action, reduce_only=False), {'price_cap': D(1)})
