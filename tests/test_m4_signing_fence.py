"""Actual HTTP adapters: durable callback or no IOC request reaches transport."""
from decimal import Decimal as D
import pytest
from funding_bot.trade import store
from funding_bot.trade.adapters.contracts import AdapterError, Action, Quote, Prepared
from funding_bot.trade.adapters.native_journal import PerpJournal
from funding_bot.trade.adapters.mapping import map_perpetual
from funding_bot.trade.adapters.execution import recover_not_submitted
from funding_bot.trade.types import InstrumentSpec, Filters
from test_trade_aster import FakeAster, mk as aster
from test_gate_trade import FakeGate, mk as gate


def setup(tmp_path, venue):
    con = store.connect(tmp_path / 'trade.db')
    fake = FakeAster() if venue == 'aster' else FakeGate()
    if venue == 'gate': fake.account_row['user'] = 123
    native = aster(fake) if venue == 'aster' else gate(fake)
    account = native.history_account()
    symbol = 'AIW3USDT' if venue == 'aster' else 'FATCOIN_USDT'
    inst = InstrumentSpec(chain='bsc', token='0x'+'a'*40, token_dec=18, perp_venue=venue,
                          perp_symbol=symbol, quote_asset='USDT', units_per_contract=D(1),
                          ident_ev='synthetic:verified', verified=True)
    did = store.create_deal(con, coin='TEST', chain='bsc', token=inst.token, token_dec=18,
                            perp_venue=venue, symbol=symbol, leg_usd=D(100), owner_json='{}', sim=False, inst=inst)
    from funding_bot.trade.adapters.execution_scope import bind_draft
    bind_draft(con, store.get_deal(con, did), native)
    iid, _ = store.create_intent(con, deal_id=did, kind='entry', spec={'inst_hash':inst.inst_hash()}, plan={})
    clip = store.create_clip(con, iid, 1, 100)
    cid = store.client_order_id(did, 'e', clip, 1, 1)
    f = Filters(D('.01'), D(1), D(1), D(1000), D(1000), D(1), frozenset({'IOC'}))
    leg = map_perpetual(store.get_deal(con,did), account=account, filters=f, metadata_revision='test')
    barrier = native.bind_execution_journal(con, account)
    journal = PerpJournal(con, clip_id=clip, fill_venue=venue, spec=leg, send_barrier=barrier)
    action = Action(cid, leg.leg_id, 'SELL', D(2))
    prepared = Prepared(cid, Quote(action, 9999999999, D(2), D(6), leg.asset_id, 'USDT',
                                   '{"price_cap":"3"}'), leg.fingerprint)
    journal.prepare(prepared); journal.claim(prepared)
    params = (symbol, 'SELL', D(2), D(3), cid, False)
    proof = dict(deal=store.get_deal(con,did), clip_id=clip, native=native, account=account, client_id=cid)
    return con, native, fake, journal, params, proof


@pytest.mark.parametrize('venue',['aster','gate'])
@pytest.mark.parametrize('callback',['missing','noop','raises','uncommitted'])
def test_missing_or_uncommitted_signature_never_reaches_http(tmp_path,venue,callback):
    con,native,fake,journal,params,proof=setup(tmp_path,venue)
    sent=sum(r.method == 'POST' for r in fake.sent)
    def cb(n):
        if callback=='raises': raise RuntimeError('write failed')
        if callback=='uncommitted': con.execute('BEGIN IMMEDIATE')
    with pytest.raises((AdapterError,RuntimeError)):
        native.ioc(*params,on_signed=None if callback=='missing' else cb)
    assert sum(r.method == 'POST' for r in fake.sent)==sent
    if con.in_transaction: con.rollback()


@pytest.mark.parametrize('venue',['aster','gate'])
def test_claim_crash_recovers_and_delayed_signer_is_fenced(tmp_path,venue):
    con,native,fake,journal,params,proof=setup(tmp_path,venue)
    sent=sum(r.method == 'POST' for r in fake.sent)
    assert recover_not_submitted(con,**proof)
    assert store.get_perp_order(con,params[4])['state']=='NOT_PLACED'
    with pytest.raises(store.StoreError):
        native.ioc(*params,on_signed=lambda n:journal.on_signed(params[4],n))
    assert sum(r.method == 'POST' for r in fake.sent)==sent


@pytest.mark.parametrize('venue',['aster','gate'])
@pytest.mark.parametrize('nonce',[0,123])
def test_committed_signature_never_proves_absence(tmp_path,venue,nonce):
    con,native,fake,journal,params,proof=setup(tmp_path,venue)
    journal.on_signed(params[4],nonce)
    assert not recover_not_submitted(con,**proof)
    assert store.get_perp_order(con,params[4])['state']=='SENT'


@pytest.mark.parametrize('venue',['aster','gate'])
def test_valid_signature_allows_one_http_request(tmp_path,venue):
    con,native,fake,journal,params,proof=setup(tmp_path,venue)
    sent=sum(r.method == 'POST' for r in fake.sent)
    native.ioc(*params,on_signed=lambda n:journal.on_signed(params[4],n))
    assert sum(r.method == 'POST' for r in fake.sent)==sent+1
    assert store.get_perp_order(con,params[4])['sign_nonce'] is not None
    assert not recover_not_submitted(con,**proof)


@pytest.mark.parametrize('venue',['aster','gate'])
def test_foreign_account_and_db_cannot_rebind_or_prove_absence(tmp_path,venue):
    con,native,fake,journal,params,proof=setup(tmp_path,venue)
    with pytest.raises(AdapterError): native.bind_execution_journal(con,'other')
    other=store.connect(tmp_path/'other.db')
    with pytest.raises(AdapterError): native.bind_execution_journal(other,proof['account'])
    with store.tx(other):
        assert not native.submission_absent(params[0],params[4],proof['account'],proof_con=other)
    with pytest.raises(AdapterError):
        recover_not_submitted(con,**dict(proof,account='other'))
    assert store.get_perp_order(con,params[4])['state']=='SENT'


@pytest.mark.parametrize('venue', ['aster', 'gate'])
def test_connection_views_keep_fences_separate_and_transport_budget_shared(tmp_path, venue):
    con, native, fake, journal, params, proof = setup(tmp_path, venue)
    other = store.connect(tmp_path / 'trade.db')
    first = native.execution_view(con, proof['account'])
    second = native.execution_view(other, proof['account'])
    assert first._execution_fence.con is con
    assert second._execution_fence.con is other
    first.backoff_until = 12345
    assert native.backoff_until == second.backoff_until == 12345
    with pytest.raises(AdapterError):
        first.bind_execution_journal(other, proof['account'])
    assert second._execution_fence.con is other
    other.close()


@pytest.mark.parametrize('venue', ['aster', 'gate'])
def test_copied_journal_cannot_create_second_execution_authority(tmp_path, venue):
    con, native, fake, journal, params, proof = setup(tmp_path, venue)
    copied = store.connect(tmp_path / 'copy.db')
    con.backup(copied)
    before = sum(r.method == 'POST' for r in fake.sent)
    with pytest.raises(AdapterError):
        native.execution_view(copied, proof['account'])
    assert sum(r.method == 'POST' for r in fake.sent) == before
    assert store.get_perp_order(con, params[4])['sign_nonce'] is None
    assert store.get_perp_order(copied, params[4])['sign_nonce'] is None
    copied.close()


@pytest.mark.parametrize('venue',['aster','gate'])
def test_second_native_send_cannot_reuse_signing_admission(tmp_path,venue):
    con,native,fake,journal,params,proof=setup(tmp_path,venue)
    native.ioc(*params,on_signed=lambda n:journal.on_signed(params[4],n))
    posts=sum(r.method=='POST' for r in fake.sent)
    with pytest.raises(store.StoreError):
        native.ioc(*params,on_signed=lambda n:journal.on_signed(params[4],n))
    assert sum(r.method=='POST' for r in fake.sent)==posts


@pytest.mark.parametrize('venue',['aster','gate'])
@pytest.mark.parametrize('field,value',[('order_id',1),('executed_qty','0'),('cum_quote','0'),
                                      ('avg_price','0'),('resolved_ts',0),('sign_nonce',0)])
def test_any_execution_or_signature_evidence_blocks_local_absence(tmp_path,venue,field,value):
    con,native,fake,journal,params,proof=setup(tmp_path,venue)
    con.execute(f'UPDATE perp_orders SET {field}=? WHERE client_id=?',(value,params[4]))
    assert not recover_not_submitted(con,**proof)


@pytest.mark.parametrize('venue',['aster','gate'])
def test_recovery_wins_against_inflight_native_signing_callback(tmp_path,venue):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    con,native,fake,journal,params,proof=setup(tmp_path,venue)
    reached,release=Event(),Event()
    def callback(n):
        reached.set()
        assert release.wait(5)
        journal.on_signed(params[4],n)
    posts=sum(r.method=='POST' for r in fake.sent)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(native.ioc,*params,on_signed=callback)
        try:
            assert reached.wait(5)
            assert recover_not_submitted(con,**proof)
        finally:
            release.set()
        with pytest.raises(store.StoreError): pending.result(timeout=5)
    assert sum(r.method=='POST' for r in fake.sent)==posts
    assert store.get_perp_order(con,params[4])['state']=='NOT_PLACED'


@pytest.mark.parametrize('venue',['aster','gate'])
def test_restart_can_rebind_same_persisted_claim_and_prove_absence(tmp_path,venue):
    con,native,fake,journal,params,proof=setup(tmp_path,venue)
    con.close()
    reopened=store.connect(tmp_path/'trade.db')
    restored=aster(fake) if venue=='aster' else gate(fake)
    restored.bind_execution_journal(reopened,proof['account'])
    assert recover_not_submitted(reopened,**dict(proof,native=restored))
    assert store.get_perp_order(reopened,params[4])['state']=='NOT_PLACED'


def test_two_gate_signers_cannot_join_same_connection_transaction(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier,Event,local
    con,native,fake,journal,params,proof=setup(tmp_path,'gate')
    native.filters(params[0])
    checked=Barrier(2); a_ready=Event(); a_read=Event(); b_done=Event(); a_post=Event(); ctx=local()
    original_require=journal._require_autocommit
    original_get=store.get_perp_order
    original_send=fake.send
    def require():
        original_require()
        if ctx.name=='A': a_ready.set()
        checked.wait(3)
        if ctx.name=='B': assert a_read.wait(3)
    def get(c,cid):
        row=original_get(c,cid)
        assert row and row['symbol']==params[0] and row['venue']=='gate', repr(row)
        if getattr(ctx,'inside',False) and ctx.name=='A' and c.in_transaction:
            a_read.set()
            assert b_done.wait(3)
        return row
    def send(request,**kwargs):
        result=original_send(request,**kwargs)
        if request.method=='POST': a_post.set()
        return result
    monkeypatch.setattr(journal,'_require_autocommit',require)
    monkeypatch.setattr(store,'get_perp_order',get)
    monkeypatch.setattr(fake,'send',send)
    def run(name):
        ctx.name=name;ctx.inside=False
        if name=='B': assert a_ready.wait(3)
        def callback(n):
            ctx.inside=True
            try: journal.on_signed(params[4],n)
            finally:
                ctx.inside=False
                if name=='B': b_done.set()
            if name=='B': assert a_post.wait(3)
        try: return native.ioc(*params,on_signed=callback).status
        except Exception as exc: return type(exc).__name__ + ':' + str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending=[pool.submit(run,name) for name in ('A','B')]
        results=[f.result(timeout=8) for f in pending]
    assert results[0]=='FILLED' and results[1].startswith('OperationalError:'), results
    assert sum(r.method=='POST' for r in fake.sent)==1
    assert fake.position_row['size']==-2
