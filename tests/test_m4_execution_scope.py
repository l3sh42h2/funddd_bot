from types import SimpleNamespace as NS
from decimal import Decimal as D

import pytest

from funding_bot.trade import store
from funding_bot.trade.adapters.execution_scope import account_for, bind_draft
from funding_bot.trade.adapters.contracts import AdapterError
from test_m4_execution_port import world


def draft(tmp_path):
    con, _, _, args = world(tmp_path)
    template = args['deal']
    did = store.create_deal(con, coin='TEST', chain=template['chain'], token=template['token'],
                            token_dec=template['token_dec'], perp_venue='aster', symbol=template['symbol'],
                            leg_usd=100, owner_json='{}', sim=False,
                            inst=store.InstrumentSpec.from_json(template['inst_json']))
    native = NS(venue='aster', history_account=lambda: 'acct:v1:aster:test')
    return con, store.get_deal(con, did), native


def test_new_draft_binding_is_durable_without_switching_accounting(tmp_path):
    con, deal, native = draft(tmp_path)
    before = deal['inst_json']
    with pytest.raises(AdapterError):
        account_for(con, deal, native)
    assert bind_draft(con, deal, native) == native.history_account()
    assert bind_draft(con, deal, native) == native.history_account()
    assert con.execute("SELECT count(*) FROM exec_events WHERE kind='execution_account_binding_v1'").fetchone()[0] == 1
    assert store.get_deal(con, deal['id'])['inst_json'] == before
    from funding_bot.trade.accounting import is_bound
    assert not is_bound(con, deal['id'])
    con.close()
    con = store.connect(tmp_path / 'trade.db')
    assert account_for(con, deal, native) == native.history_account()
    native.history_account = lambda: 'acct:v1:aster:other'
    with pytest.raises(AdapterError):
        account_for(con, deal, native)
    with pytest.raises(AdapterError):
        bind_draft(con, deal, native)


def test_current_credentials_cannot_adopt_existing_intent(tmp_path):
    con, deal, native = draft(tmp_path)
    store.create_intent(con, deal_id=deal['id'], kind='entry', spec={}, plan={})
    with pytest.raises(AdapterError):
        bind_draft(con, deal, native)
    assert con.execute("SELECT count(*) FROM exec_events WHERE kind='execution_account_binding_v1'").fetchone()[0] == 0


def test_binding_rejects_changed_deal_before_event(tmp_path):
    con, deal, native = draft(tmp_path)
    deal['symbol'] = 'OTHER'
    with pytest.raises(AdapterError):
        bind_draft(con, deal, native)


def test_binding_cannot_join_uncommitted_caller(tmp_path):
    con, deal, native = draft(tmp_path)
    con.execute('BEGIN IMMEDIATE')
    with pytest.raises(Exception):
        bind_draft(con, deal, native)
    assert con.in_transaction
    con.rollback()


def legacy(tmp_path):
    con, deal, native = draft(tmp_path)
    iid, _ = store.create_intent(con, deal_id=deal['id'], kind='entry', spec={}, plan={})
    clip = store.create_clip(con, iid, 1, 100)
    cid = store.client_order_id(deal['id'], 'e', clip, 1, 1)
    store.perp_order_intent(con, clip_id=clip, client_id=cid, venue='aster', symbol=deal['symbol'],
                            side='SELL', reduce_only=False, tif='IOC', price=D(3), qty=D(2))
    store.perp_order_sent(con, cid, sign_nonce=1)
    body = dict(clientOrderId=cid, symbol=deal['symbol'], side='SELL', origQty='2', price='3',
                reduceOnly=False, orderId=123, executedQty='2', cumQuote='6')
    reads = []
    def read(*args, **kw):
        assert not con.in_transaction
        reads.append(args)
        return body
    native._signed_ok = read
    return con, deal, native, body, reads


def test_legacy_binding_proves_orders_without_resolving_or_attributing_them(tmp_path):
    from funding_bot.trade.adapters.execution_scope import bind_legacy
    con, deal, native, body, reads = legacy(tmp_path)
    before = dict(con.execute('SELECT * FROM perp_orders').fetchone())
    assert bind_legacy(con, deal, native) == native.history_account()
    assert dict(con.execute('SELECT * FROM perp_orders').fetchone()) == before
    assert account_for(con, deal, native) == native.history_account()
    assert bind_legacy(con, deal, native) == native.history_account()
    assert len(reads) == 1
    assert store.get_deal(con, deal['id']) == deal
    from funding_bot.trade.accounting import is_bound
    assert not is_bound(con, deal['id'])
    assert con.execute('SELECT min_reader FROM schema_version').fetchone()[0] == 4


@pytest.mark.parametrize('field,value', [('clientOrderId', None), ('symbol', 'OTHER'),
                                        ('side', 'BUY'), ('origQty', '3'), ('reduceOnly', True),
                                        ('executedQty', 'NaN'), ('orderId', None), ('orderId', True),
                                        ('orderId', -1), ('orderId', 'garbage')])
def test_legacy_raw_identity_mismatch_never_creates_binding(tmp_path, field, value):
    from funding_bot.trade.adapters.execution_scope import bind_legacy
    con, deal, native, body, reads = legacy(tmp_path)
    body[field] = value
    with pytest.raises(AdapterError):
        bind_legacy(con, deal, native)
    assert con.execute("SELECT count(*) FROM exec_events WHERE kind='execution_account_binding_v1'").fetchone()[0] == 0


def test_legacy_snapshot_change_during_network_read_refuses_binding(tmp_path):
    from funding_bot.trade.adapters.execution_scope import bind_legacy
    con, deal, native, body, reads = legacy(tmp_path)
    def changed(*args, **kw):
        con.execute('UPDATE perp_orders SET sign_nonce=2')
        return body
    native._signed_ok = changed
    with pytest.raises(AdapterError):
        bind_legacy(con, deal, native)
    assert con.execute("SELECT count(*) FROM exec_events WHERE kind='execution_account_binding_v1'").fetchone()[0] == 0


def active_legacy_set(tmp_path, *, second_valid=False):
    import json
    from funding_bot.trade.adapters.execution_scope import KIND
    con, first, native, body, reads = legacy(tmp_path)
    wallet = '0x' + 'b'*40
    con.execute('UPDATE deals SET state=?,owner_json=? WHERE id=?',
                ('OPEN', json.dumps({'values': {'wallets.bsc': wallet}}), first['id']))
    first = store.get_deal(con, first['id'])
    inst = store.InstrumentSpec.from_json(first['inst_json'])
    from dataclasses import replace
    second = replace(inst, token='0x'+'c'*40, perp_symbol='SECONDUSDT')
    did = store.create_deal(con, coin='SECOND', chain=second.chain, token=second.token,
                            token_dec=second.token_dec, perp_venue='aster', symbol=second.perp_symbol,
                            leg_usd=100, owner_json=first['owner_json'], sim=False, inst=second)
    con.execute("UPDATE deals SET state='OPEN' WHERE id=?", (did,))
    # No native order history for second position: credentials alone cannot adopt it.
    legs = NS(sim=False, spot=NS(chain=inst.chain, wallet=wallet), perp=native)
    return con, first, did, legs, reads


def test_failed_second_active_proof_keeps_all_bindings_and_reader_floor(tmp_path):
    from funding_bot.trade.adapters.execution_scope import prepare_active_accounts, KIND
    con, first, second, legs, reads = active_legacy_set(tmp_path)
    before = store.schema_info(con)
    with pytest.raises(AdapterError, match=second):
        prepare_active_accounts(con, lambda sim: legs)
    assert reads  # First account was actually authenticated before second failed.
    assert store.schema_info(con) == before
    assert not con.execute('SELECT 1 FROM exec_events WHERE kind=?', (KIND,)).fetchone()


def test_genuine_unverified_legacy_blocks_activation_without_floor_or_binding(tmp_path):
    from funding_bot.trade.adapters.execution_scope import prepare_active_accounts, KIND
    con, first, second, legs, reads = active_legacy_set(tmp_path)
    con.execute("UPDATE deals SET state='CLOSED' WHERE id=?", (first['id'],))
    con.execute('UPDATE deals SET inst_json=NULL WHERE id=?', (second,))
    before = store.schema_info(con)
    with pytest.raises(AdapterError, match=second):
        prepare_active_accounts(con, lambda sim: legs)
    assert not reads
    assert store.schema_info(con) == before
    assert not con.execute('SELECT 1 FROM exec_events WHERE kind=?', (KIND,)).fetchone()


def test_existing_scoped_account_also_activates_reader_floor(tmp_path):
    import json
    from dataclasses import replace
    from funding_bot.trade.adapters.execution_scope import prepare_active_accounts, KIND
    con, deal, native = draft(tmp_path)
    inst = store.InstrumentSpec.from_json(deal['inst_json'])
    from funding_bot.trade import scoped_accounting as scoped
    scoped.migrate(con)
    scoped.bind_deal(con, deal['id'], scoped.ProvenScope(native.history_account(), 'aster',
                     deal['symbol'], 'migration_manifest', 'fixture:identity'))
    wallet = '0x'+'b'*40
    con.execute("UPDATE deals SET state='OPEN',inst_json=?,owner_json=? WHERE id=?",
                (inst.to_json(), json.dumps({'values': {'wallets.bsc': wallet}}), deal['id']))
    legs = NS(sim=False, spot=NS(chain=inst.chain, wallet=wallet), perp=native)
    assert store.schema_info(con)['min_reader'] < 4
    prepare_active_accounts(con, lambda sim: legs)
    assert store.schema_info(con)['min_reader'] == 4
    assert not con.execute('SELECT 1 FROM exec_events WHERE kind=?', (KIND,)).fetchone()


def test_core_startup_calls_real_migration_gate_and_preserves_backfill(tmp_path):
    from funding_bot.core.commands import Bot
    from test_phase1_instrument import _dqa9q_env
    import test_marks as tm
    from test_trade_engine import sends
    e = _dqa9q_env(tmp_path)
    bot = object.__new__(Bot)
    bot.conns, bot.legs, bot.clock = e.conns, e.legs, lambda: tm.NOW
    bot.chat = lambda: None
    rep = bot.startup()
    assert [did for did, _ in rep.inst_backfill] == ['DQA9Q']
    assert sends(e) == (0, 0)
    assert store.schema_info(e.con)['min_reader'] == 4
    assert store.get_deal(e.con, 'DQA9Q')['state'] == 'OPEN'
    assert bot.startup().inst_backfill == []
