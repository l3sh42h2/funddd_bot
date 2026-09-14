import json
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
import test_marks as legacy
from funding_bot.trade import accounting as a, scoped_accounting as s, marks, store, reconcile
from funding_bot.core import readmodel
from funding_bot.trade.types import InstrumentSpec


@pytest.fixture
def con(tmp_path):
    db = store.connect(tmp_path/'trade.db')
    yield db
    db.close()


def bind(con, did='DQA9Q', account='A'):
    d = store.get_deal(con, did)
    inst = InstrumentSpec(chain=d['chain'], token=d['token'], token_dec=d['token_dec'],
                          perp_venue=d['perp_venue'], perp_symbol=d['symbol'], units_per_contract=D(1),
                          quote_asset='USDT', verified=True, source='fixture')
    con.execute('UPDATE deals SET inst_json=? WHERE id=?', (inst.to_json(), did))
    s.migrate(con)
    scope = s.ProvenScope('acct:v1:'+account, 'aster', d['symbol'], 'migration_manifest', 'fixture:identity')
    s.bind_deal(con, did, scope)
    return scope


def history(con, scope, *, fee=None):
    rows = [dict(r) for r in con.execute('SELECT * FROM perp_fills')]
    for row in rows:
        if fee is not None:
            row['commission_abs'] = fee
    s.add_fills(con, scope, rows)


def window(con, scope, amount='0.2'):
    d = store.get_deal(con, 'DQA9Q')
    start, end = int(d['created']*1000), int(d['updated']*1000)
    rows = (dict(tran_id=1, symbol=d['symbol'], asset='USDT', income=amount, ts=start+1000),)
    a.save_funding_window(con, 'DQA9Q', scope, a.FundingWindow(rows, start, end, True, 'fixture:complete_range'))


def test_bound_readers_hide_legacy_cache_and_never_turn_empty_into_zero(con):
    d = legacy.dqa9q(con)
    store.add_mark(con, d['id'], legacy.NOW, pnl_now=D(99), flags={})
    store.event(con, 'final', deal_id=d['id'], intent_id='EQA9Q', cost_usd=D(9))
    scope = bind(con)
    d = store.get_deal(con, d['id'])
    assert store.schema_info(con)['min_reader'] == 3
    assert a.sources(con, d['id']).fees is None
    assert a.sources(con, d['id']).funding is None
    j = marks.journal(con, d)
    assert j.accounting_revision and 'funding:coverage_unproven' in j.missing_flows
    assert marks.for_positions(con, d, None, now=legacy.NOW, fresh=False) is None
    assert readmodel._pnl_view(con, dict(d), legacy.NOW)['mark'] is None
    assert readmodel.funding_history(con, dict(d))['total'] is None
    assert reconcile._entry_costs(con, d['id']) == (None, None)
    history(con, scope)
    assert a.sources(con, d['id']).fees == D('0.0803')
    assert a.sources(con, d['id']).funding is None
    m = marks.mark_deal(con, d, legacy.legs_of(), now=legacy.NOW, fetch_funding=False)
    assert m.pnl_now is m.pnl_exit is m.funding is None
    assert not m.flags['accounting_complete']


def test_closed_projection_recovers_only_after_proven_window(con):
    legacy.dqa9q(con); legacy._close_dqa9q(con)
    store.add_mark(con, 'DQA9Q', legacy.NOW-1, pnl_now=D(99), flags={'final':True})
    scope = bind(con)
    d = store.get_deal(con, 'DQA9Q')
    history(con, scope)
    assert readmodel._pnl_view(con, dict(d), legacy.NOW)['total'] is None
    window(con, scope)
    source = a.sources(con, d['id'])
    assert not source.missing and source.funding == D('0.2')
    expected = D('205.2')-200+D('200.63886')-D('206.7144')-D('0.0803')-D('0.08269')+D('0.2')-legacy.GAS
    marks.run_pass(con, lambda sim: legacy.legs_of(), now=legacy.NOW)
    current = marks.decode(store.last_mark(con, d['id']))
    assert current['pnl_now'] == expected
    assert current['flags']['accounting_complete']
    assert readmodel._pnl_view(con, dict(d), legacy.NOW)['total'] == expected
    assert con.execute('SELECT count(*) FROM deal_marks').fetchone()[0] == 2
    assert con.execute('SELECT pnl_now FROM deal_marks ORDER BY rowid LIMIT 1').fetchone()[0] == '99'
    # A newly discovered payment invalidates the previous coverage proof/cached PnL.
    s.add_funding(con, scope, [dict(tran_id=2,income='3',ts=int(legacy.T0*1000)+1001)])
    assert readmodel._pnl_view(con, dict(d), legacy.NOW)['total'] is None


def test_different_accounts_same_native_ids_have_different_fees(con):
    legacy.dqa9q(con); legacy._close_dqa9q(con)
    sa = bind(con)
    history(con, sa)
    legacy.dqa9q(con, did='OTHER', oid=111)
    sb = bind(con, 'OTHER', 'B')
    history(con, sb, fee='9')
    assert a.sources(con, 'DQA9Q').fees == D('0.16299')
    assert a.sources(con, 'OTHER').fees == D('9')


def test_sync_uses_bound_cursor_and_refuses_identity_change(con):
    legacy.dqa9q(con)
    scope = bind(con)
    rows = [dict(r) for r in con.execute('SELECT * FROM perp_fills')]
    starts=[]
    native = SimpleNamespace(venue='aster', history_account=lambda:scope.account_scope,
                             history_fills=lambda symbol,start:(starts.append(start) or rows))
    d = store.get_deal(con,'DQA9Q')
    legs = SimpleNamespace(perp=native,fill_venue='aster')
    a.sync_fills(con,d,legs); a.sync_fills(con,d,legs)
    assert starts == [0,1111]  # Legacy venue-global last id was 1110 before first sync.
    assert len(s.deal_fills(con,'DQA9Q')) == 1
    native.history_account=lambda:'acct:v1:other'
    with pytest.raises(s.ScopedAccountingError, match='account'):
        a.sync_fills(con,d,legs)
    assert len(starts)==2


def test_funding_window_rollback_and_currency_guard(con):
    legacy.dqa9q(con); legacy._close_dqa9q(con)
    scope=bind(con)
    d=store.get_deal(con,'DQA9Q');start=int(d['created']*1000);end=int(d['updated']*1000)
    bad=a.FundingWindow((dict(tran_id=1,ts=start,income='1',asset='BTC'),),start,end,True,'fixture:range')
    with pytest.raises(s.ScopedAccountingError, match='currency'):
        a.save_funding_window(con,d['id'],scope,bad)
    assert s.deal_funding(con,d)==[]
    assert con.execute("SELECT count(*) FROM exec_events WHERE kind='accounting_funding_window'").fetchone()[0]==0


def test_cost_cache_requires_its_own_execution_revision(con):
    legacy.dqa9q(con)
    scope=bind(con); history(con,scope)
    old=dict(cost_usd='9')
    assert a.event_cost(con,'DQA9Q','EQA9Q',old) is None
    proof=a.cost_revision(con,'DQA9Q','EQA9Q')
    current=dict(old,accounting_cost_revision=proof)
    assert a.event_cost(con,'DQA9Q','EQA9Q',current)==D(9)
    # Funding updates cannot erase a correctly proven execution cost.
    s.add_funding(con,scope,[dict(tran_id=1,ts=int(legacy.T0*1000),income='1')])
    assert a.event_cost(con,'DQA9Q','EQA9Q',current)==D(9)
    # Changed execution evidence must invalidate it.
    con.execute("UPDATE perp_orders SET cum_quote='201' WHERE order_id=111")
    assert a.event_cost(con,'DQA9Q','EQA9Q',current) is None


def test_daily_stop_refuses_unknown_bound_cost_instead_of_using_zero(con):
    from funding_bot.trade.engine import Desk, Refused
    legacy.dqa9q(con); bind(con)
    store.event(con,'final',deal_id='DQA9Q',intent_id='EQA9Q',cost_usd=D(0),now=legacy.NOW)
    desk=object.__new__(Desk)
    desk.conns=SimpleNamespace(get=lambda:con);desk.clock=lambda:legacy.NOW
    cfg={'limits.daily_loss_stop_usd':D(100),'limits.daily_loss_basis':'realized_costs'}
    with pytest.raises(Refused):
        desk._daily_stop_check(cfg,False)


def test_binding_fences_reader_two_atomically(con,monkeypatch):
    legacy.dqa9q(con)
    bind(con)
    monkeypatch.setattr(store,'SCHEMA_VERSION',2)
    with pytest.raises(store.SchemaTooNew):
        store._gate(con)


@pytest.mark.parametrize('state',['EXPIRED','NOT_PLACED','REJECTED'])
def test_terminal_zero_cannot_erase_positive_scoped_fill(con,state):
    legacy.dqa9q(con);legacy._close_dqa9q(con)
    scope=bind(con);history(con,scope);window(con,scope)
    con.execute("UPDATE perp_orders SET state=?,executed_qty='0',cum_quote='0' WHERE order_id=111",(state,))
    source=a.sources(con,'DQA9Q')
    assert source.fees is None and source.missing
    m=marks.final_mark(con,store.get_deal(con,'DQA9Q'),legacy.legs_of(),now=legacy.NOW)
    assert m.pnl_now is None and not m.flags['accounting_complete']


def test_old_complete_window_cannot_certify_late_payment_it_omits(con):
    legacy.dqa9q(con);legacy._close_dqa9q(con)
    scope=bind(con);history(con,scope);window(con,scope)
    s.add_funding(con,scope,[dict(tran_id=2,ts=int(legacy.T0*1000)+1001,income='3')])
    assert a.sources(con,'DQA9Q').funding is None
    with pytest.raises(s.ScopedAccountingError,match='omits'):
        window(con,scope)
    assert a.sources(con,'DQA9Q').funding is None


def test_window_cannot_consume_generator_as_false_empty_history():
    with pytest.raises(s.ScopedAccountingError,match='materialized'):
        a.FundingWindow((r for r in [dict(tran_id=1,income='7',ts=1)]),0,2,True,'fixture:range')


def test_window_freezes_rows_before_validation_or_commit():
    row=dict(tran_id=1,income='7',ts=1,asset='USDT')
    w=a.FundingWindow((row,),0,2,True,'fixture:range')
    row['income']='0'
    assert w.rows[0]['income']=='7'
    with pytest.raises(TypeError):
        w.rows[0]['income']='0'


@pytest.mark.parametrize('fill_fraction',[D(0),D('0.5')])
def test_execution_summary_keeps_confirmed_qty_before_complete_fills(con,fill_fraction):
    legacy.dqa9q(con);scope=bind(con)
    if fill_fraction:
        row=dict(con.execute('SELECT * FROM perp_fills').fetchone())
        for key in ('qty','quote_qty','commission_abs'):
            row[key]=D(row[key])*fill_fraction
        s.add_fills(con,scope,[row])
    summary=a.execution_summary(con,'DQA9Q','EQA9Q')
    assert summary['qty']==4902 and summary['quote']==D('200.63886')
    assert summary['commission_usd'] is None


@pytest.mark.parametrize('state', ['EXPIRED', 'CANCELED', 'CANCELLED'])
def test_positive_cancel_is_incomplete_until_flow_projection_supports_it(con, state):
    legacy.dqa9q(con); legacy._close_dqa9q(con)
    scope=bind(con); history(con,scope); window(con,scope)
    con.execute('UPDATE perp_orders SET state=? WHERE order_id=111', (state,))
    m=marks.final_mark(con,store.get_deal(con,'DQA9Q'),legacy.legs_of(),now=legacy.NOW)
    assert m.pnl_now is None and not m.flags['accounting_complete']


@pytest.mark.parametrize('value', [None, '-1', 'NaN', 'Infinity'])
def test_missing_or_invalid_receipt_is_unknown_not_free(con, value):
    legacy.dqa9q(con); legacy._close_dqa9q(con)
    scope=bind(con); history(con,scope); window(con,scope)
    con.execute('UPDATE dex_txs SET gas_used=?', (value,))
    m=marks.final_mark(con,store.get_deal(con,'DQA9Q'),legacy.legs_of(),now=legacy.NOW)
    assert m.pnl_now is None and m.gas is None and not m.flags['accounting_complete']


def test_active_funding_reader_accepts_complete_window_at_explicit_cut(con):
    legacy.dqa9q(con); scope=bind(con); history(con,scope)
    con.execute('UPDATE deals SET updated=?', (legacy.NOW,))
    window(con,scope)
    result=readmodel.funding_history(con,dict(store.get_deal(con,'DQA9Q')),now=legacy.NOW)
    assert result['total']==D('0.2')


def final_run(con, monkeypatch):
    from funding_bot.trade import engine
    legs=legacy.legs_of()
    run=engine.Run(it=store.get_intent(con,'EQA9Q'),deal=store.get_deal(con,'DQA9Q'),
        kind='entry',spec={},plan=SimpleNamespace(inputs={'calib':{'p_ref':'0.0408'},
            'book_top':{'mid':'0.04093'}},est={},leg_usd=D(200)),
        legs=legs,cfg={},token=legacy.TOKEN,dec=18,symbol=legacy.SYMBOL,
        stable=legacy.STABLE,sdec=18,f=legs.perp.filters(legacy.SYMBOL),started=legacy.T0)
    worker=object.__new__(engine.Engine)
    worker.conns=SimpleNamespace(get=lambda:con)
    return worker,run


def test_actual_final_keeps_confirmed_quantity_without_fills(con,monkeypatch):
    legacy.dqa9q(con); bind(con)
    worker,run=final_run(con,monkeypatch)
    view,cost,_=worker._final(run,legacy.NOW,False)
    assert view.perp_qty==D(4902) and cost is None


def test_actual_final_receipt_change_during_input_read_invalidates_cost(con,monkeypatch):
    from funding_bot.trade import engine
    legacy.dqa9q(con); scope=bind(con); history(con,scope)
    worker,run=final_run(con,monkeypatch)
    original=engine.intent_txs
    assert worker._final(run,legacy.NOW,False)[1] is not None
    calls=0
    def racing(con,iid):
        nonlocal calls
        rows=original(con,iid)
        calls+=1
        if calls==1:
            con.execute('UPDATE dex_txs SET gas_used=gas_used*100')
        return rows
    monkeypatch.setattr(engine,'intent_txs',racing)
    _,cost,extra=worker._final(run,legacy.NOW,False)
    assert cost is None and extra['accounting_cost_revision'] is None


def test_replaced_transaction_requires_matching_mined_nonce_sibling():
    old=dict(kind='swap',state='REPLACED',chain='bsc',wallet='owner',nonce=5,
             tx_hash='old',gas_used=None,eff_gas_price=None)
    new=dict(old,state='MINED_OK',tx_hash='new',gas_used=100,eff_gas_price='200')
    assert a.gas_complete([old,new])
    for field,value in [('wallet','other'),('chain','other'),('nonce',6),('state','UNKNOWN'),('gas_used',None)]:
        assert not a.gas_complete([old,dict(new,**{field:value})])
    assert not a.gas_complete([old])


def test_mark_reads_book_after_funding_sync_in_same_snapshot(con):
    legacy.dqa9q(con); scope=bind(con); history(con,scope)
    d=store.get_deal(con,'DQA9Q'); legs=legacy.legs_of()
    def funding_window(symbol,start):
        iid=legacy._intent(con,d['id'],'entry','ERACE',legacy.T0+100)
        clip=legacy._clip(con,iid,1,5*legacy.E18,100*legacy.E18,legacy.T0+101)
        legacy._order(con,d['id'],clip,'e',2,'SELL',D(100),D(4),222,'aster',fee=D('.01'))
        store.set_intent_status(con,iid,'done'); history(con,scope)
        legs.perp.pos=D(-5002)
        return a.FundingWindow((),start,int(legacy.NOW*1000),True,'fixture:range')
    legs.perp.history_account=lambda:scope.account_scope
    legs.perp.history_funding_window=funding_window
    first=marks.mark_deal(con,d,legs,now=legacy.NOW)
    second=marks.mark_deal(con,d,legs,now=legacy.NOW,fetch_funding=False)
    assert first.flags['q_short']==second.flags['q_short']==D(5002)
    assert first.pnl_now==second.pnl_now==D('-0.9593100')
    assert first.flags['accounting_revision']==second.flags['accounting_revision']
