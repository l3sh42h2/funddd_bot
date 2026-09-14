"""M1 failure scenarios. Synthetic SQLite and adapters, no exchange calls."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import pytest
from funding_bot.core.journal import Journal, Outbox
from funding_bot.core.service import CoreService
from funding_bot.ipc.lock import ExecutionLock
from funding_bot.ipc.protocol import Server, Client, RpcError, send, receive
from funding_bot.interface.runtime import Interface, State
from funding_bot.trade.engine import Conns
from funding_bot.trade import store

OWNER = 71234


def update(uid=1, text='стоп'):
    return {'update_id':uid, 'message': {'from':{'id':OWNER}, 'chat':{'id':OWNER,'type':'private'},
                                       'date':int(time.time()), 'text':text}}


@pytest.fixture
def conns(tmp_path):
    return Conns(tmp_path/'trade.db')


def test_accept_duplicate_changed_payload_and_restart(conns):
    j = Journal(conns)
    u = update()
    a = j.accept('tg:1', {'update':u})
    assert a['state']=='queued'
    assert j.accept('tg:1', {'update':u}) == a
    with pytest.raises(RpcError, match='key_payload_mismatch'):
        j.accept('tg:1', {'update':update(text='продолжить')})
    assert j.claim()[0] == 'tg:1'
    j.recover()
    assert j.get('tg:1')['state']=='interrupted'
    assert j.claim() is None
    assert store.tg_offset(conns.get()) == 2


def test_inbox_bound_reserves_pause(conns):
    j = Journal(conns, max_pending=1)
    j.accept('tg:1', {'update':update(1)}, 2)
    with pytest.raises(RpcError, match='busy'):
        j.accept('tg:2', {'update':update(2)}, 2)
    assert j.accept('tg:2', {'update':update(2)}, 0)['state']=='queued'
    assert j.claim(fast=True)[0]=='tg:2'


def test_offset_from_legacy_history(conns):
    con = conns.get()
    store.claim_update(con, 100, time.time(), OWNER, OWNER, 'old')
    j = Journal(conns)
    assert j.offset_floor()==101


def test_outbox_survives_restart_and_ack_is_idempotent(conns):
    j=Journal(conns); out=Outbox(j)
    out.send(OWNER, 'done')
    eid=j.notifications()[0]['id']
    out2=Outbox(Journal(conns))
    assert out2.journal.notifications()[0]['text']=='done'
    assert out2.ack(eid, {'message_id':8}) == {'acked':True}
    out2.ack(eid, {'message_id':8})
    assert j.notifications()==[]


def test_execution_lock_same_inode_no_second_process(tmp_path):
    path=tmp_path/'exec.lock'
    with ExecutionLock(path):
        ino=path.stat().st_ino
        with pytest.raises(BlockingIOError):
            with ExecutionLock(path):pass
        proc=subprocess.run([sys.executable,'-c',
            'from funding_bot.ipc.lock import ExecutionLock; import sys;\nwith ExecutionLock(sys.argv[1]): pass',str(path)],capture_output=True,text=True)
        assert proc.returncode != 0
    with ExecutionLock(path):assert path.stat().st_ino==ino


@pytest.fixture
def rpc(tmp_path):
    # macOS Unix socket paths are limited to 104 bytes.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='fb-',dir='/tmp') as d:
        path=d+'/core.sock'
        calls=[]
        srv=Server(path, lambda m,p,k,u:(calls.append((m,p,k,u)) or {'value':p}), {os.getuid()})
        thread=threading.Thread(target=srv.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        yield Client(path),srv,calls
        srv.shutdown();srv.server_close();thread.join()


def test_rpc_actual_peer_version_and_oversize(rpc):
    client,srv,calls=rpc
    assert client.call('echo', {'n':1})=={'value':{'n':1}}
    assert calls[0][-1]==os.getuid()
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
        s.connect(client.path)
        send(s,dict(protocol_version=999, request_id='x',method='echo',payload={}))
        assert receive(s)['error']=='unsupported_version'
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
        s.connect(client.path);s.sendall((1048577).to_bytes(4,'big'))
        assert receive(s)['error']=='frame_too_large'
    assert len(calls)==1


def test_rpc_denied_uid(rpc):
    client,srv,_=rpc;srv.allowed={os.getuid()+9999}
    with pytest.raises(RpcError):client.call('echo')


class Engine:
    def __init__(self):
        self.pause_evt=threading.Event();self.term=threading.Event();self.current=None;self.hooks=None
        self.busy_flag=False;self.started=False;self.submissions=[]
    def busy(self):return self.busy_flag
    def start(self):self.started=True
    def stop(self):self.started=False
    def wait_idle(self, timeout):return not self.busy_flag
    def submit(self,iid):self.submissions.append(iid)


def service(conns, desk=None):
    cfg=SimpleNamespace(owner_id=OWNER, profile_enabled=lambda _:False)
    s=CoreService(conns,desk or SimpleNamespace(),Engine(),lambda _:None,owner_loader=lambda:cfg)
    s.jobs.tick=lambda:None
    return s


def wait_for(fn, timeout=3):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        if fn():return
        time.sleep(.01)
    assert fn()


def test_core_without_telegram_stop_during_slow_plan(conns, monkeypatch):
    monkeypatch.delenv('TG_BOT_TOKEN', raising=False)
    entered=threading.Event();release=threading.Event()
    def plan(*_):
        entered.set();release.wait(3);return 'plan ready'
    s=service(conns,SimpleNamespace(propose_entry=plan))
    s.start(reconcile=False)
    try:
        assert s.engine.started
        s.dispatch('submit_user_command',{'update':update(1,'вход AIW3 okx·bsc aster 200')},'tg:1',os.getuid())
        assert entered.wait(2)
        s.dispatch('submit_user_command',{'update':update(2,'стоп')},'tg:2',os.getuid())
        wait_for(s.engine.pause_evt.is_set)
        assert s.journal.get('tg:1')['state']=='running'
        release.set()
        wait_for(lambda:s.journal.get('tg:1')['state']=='done')
        assert s.journal.notifications()  # Result retained with no interface / no Telegram.
    finally:
        release.set();s.shutdown()


def test_owner_auth_and_stale_message_in_core(conns):
    s=service(conns);s.start(reconcile=False)
    try:
        u=update();u['message']['from']['id']=99
        s.dispatch('submit_user_command',{'update':u},'tg:1',os.getuid())
        wait_for(lambda:s.journal.get('tg:1')['state']=='done')
        assert not s.engine.pause_evt.is_set()
        u=update(2);u['message']['date']=1
        s.dispatch('submit_user_command',{'update':u},'tg:2',os.getuid())
        wait_for(lambda:s.journal.get('tg:2')['state']=='done')
        assert not s.engine.pause_evt.is_set()
    finally:s.shutdown()


def test_drain_and_separate_deploy_authority(conns):
    s=service(conns);s.ready=True
    with pytest.raises(RpcError,match='unauthorized_method'):
        s.dispatch('begin_drain',{},None,os.getuid())
    s.dispatch('begin_drain',{'release_id':'fixture','expected_state_revision':0},None,0)
    with pytest.raises(RpcError,match='draining'):
        s.dispatch('submit_user_command',{'update':update(1,'продолжить')},'tg:1',os.getuid())
    assert s.dispatch('submit_user_command',{'update':update(2)},'tg:2',os.getuid())['state']=='queued'


def test_interface_timeout_does_not_advance_offset(tmp_path):
    class Remote:
        def call(self,m,p=None,**kw):
            if m=='get_offset_floor':return {'offset':3}
            raise RpcError('core_unavailable')
    api=SimpleNamespace(get_updates=lambda *a,**k:[update(3)])
    state=State(tmp_path/'state.json');ui=Interface(api,None,Remote(),state)
    with pytest.raises(RpcError):ui.poll_once()
    assert state.data['offset']==3
    assert State(state.path).data['offset']==3


def test_delivered_ack_pending_prevents_resend_on_interface_restart(tmp_path):
    j=[]
    class Remote:
        def call(self,m,p=None,**kw):
            j.append(m)
            if m=='read_notifications':return {'events':[]}
            return {'acked':True}
    state=State(tmp_path/'state.json');state.ack_pending(1,{'message_id':4})
    ui=Interface(None,None,Remote(),State(state.path));ui.deliver_once()
    assert j==['ack_notification','read_notifications']
    assert not ui.state.pending()


def test_missing_core_database_is_not_created(tmp_path,monkeypatch):
    from funding_bot.core.bootstrap import run_core
    from funding_bot.trade import tconfig
    path=tmp_path/'missing.db';monkeypatch.setattr(tconfig,'TRADE_DB_PATH',path)
    assert run_core({})==78
    assert not path.exists()


def test_callback_duplicate_nonce_ttl_and_config_guard(conns):
    from funding_bot.tg.parse import callback_data
    from funding_bot.trade import owner
    # Use real owner config value object; no live credentials or network.
    cfg=owner.loads('mode = "dry"\n[telegram]\nowner_id = 71234\n') if hasattr(owner,'loads') else None
    if cfg is None:
        cfg=SimpleNamespace(owner_id=OWNER, profile_enabled=lambda _:False, frozen=lambda:{'owner_id':OWNER})
    s=service(conns);s.bot.owner_loader=lambda:cfg;s.ready=True
    con=conns.get()
    store.create_deal(con, coin='AIW3', chain='bsc', token='0x'+'a'*40, token_dec=18,
                      perp_venue='aster', symbol='AIW3USDT', leg_usd=__import__('decimal').Decimal(200),
                      owner_json="{}", sim=True, deal_id='DTEST')
    iid,nonce=store.create_intent(con,deal_id='DTEST',kind='entry',spec={'coin':'AIW3'},plan={'n':1},now=time.time())
    s._save_plan_guard(iid)
    u={'update_id':1,'callback_query':{'id':'cb1','from':{'id':OWNER},'data':callback_data('ok',iid,nonce),
       'message':{'message_id':1,'chat':{'id':OWNER,'type':'private'}}}}
    assert s._check_plan_guard(u)
    s.bot.handle(u);s.bot.handle(u)
    assert s.engine.submissions==[iid]
    # Changing the frozen config invalidates another outstanding plan before CAS.
    s.bot.owner_loader=lambda:SimpleNamespace(frozen=lambda:{'changed':True})
    assert not s._check_plan_guard(u)


def test_real_core_process_survives_interface_exit_and_rejects_second_core(tmp_path):
    import tempfile
    from funding_bot.trade import store
    with tempfile.TemporaryDirectory(prefix='fb-process-',dir='/tmp') as d:
        root=__import__('pathlib').Path(d)
        db=store.connect(root/'trade.db');db.close()
        script=root/'boot.py'
        script.write_text('''from types import SimpleNamespace
from funding_bot.core import bootstrap
from funding_bot.core import commands
cfg=SimpleNamespace(owner_id=71234, profile_enabled=lambda _:False, frozen=lambda:{})
bootstrap.owner.load=lambda:cfg
bootstrap.build_trader_legs=lambda *a,**k:(SimpleNamespace(mode='dry'),lambda _:None,None,'dry',False)
commands.reconcile.startup=lambda *a,**k:SimpleNamespace(expired=[],deals=[],wallet_problems=[])
raise SystemExit(bootstrap.run_core())
''')
        env=dict(os.environ,FUNDING_BOT_RUNTIME=str(root),FUNDING_CORE_SOCKET=str(root/'core.sock'),
                 FUNDING_EXECUTION_LOCK=str(root/'execution.lock'),FUNDING_TRADE_DB=str(root/'trade.db'))
        env.pop('TG_BOT_TOKEN',None)
        log=open(root/'core.log','w+')
        proc=subprocess.Popen([sys.executable,str(script)],env=env,stdout=log,stderr=log)
        client=Client(root/'core.sock')
        def ready():
            try:return client.call('get_status')['ready']
            except RpcError:return False
        try:
            wait_for(ready,5)
            first=client.call('get_status')
            other=subprocess.run([sys.executable,str(script)],env=env,capture_output=True,text=True,timeout=5)
            assert other.returncode==78
            # Independent short-lived UI process submits a command and exits.
            code='from funding_bot.ipc.protocol import Client; import json; Client().call("submit_user_command", {"update":json.loads('+repr(json.dumps(update()))+')},key="tg:1")'
            subprocess.run([sys.executable,'-c',code],env=env,check=True,timeout=5)
            wait_for(lambda:client.call('get_request',{'key':'tg:1'})['state']=='done')
            after=client.call('get_status')
            assert after['pid']==first['pid'] and after['boot_id']==first['boot_id'] and proc.poll() is None
            assert client.call('read_notifications')['events']
        finally:
            proc.terminate()
            try:proc.wait(timeout=8)
            except subprocess.TimeoutExpired:proc.kill();proc.wait();raise
            log.close()
        assert proc.returncode==0


def test_core_schema_gate_before_inbox_mutation(conns):
    con=conns.get()
    con.execute('CREATE TABLE core_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    con.execute("INSERT INTO core_meta VALUES('schema_version','99')")
    with pytest.raises(RpcError,match='unsupported_core_schema'):Journal(conns)
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='core_requests'").fetchone()


def test_config_change_while_quoting_invalidates_plan(conns):
    from decimal import Decimal
    entered=threading.Event();release=threading.Event();settings={'limit':200}
    cfg=SimpleNamespace(owner_id=OWNER,profile_enabled=lambda _:False,frozen=lambda:dict(settings))
    con=conns.get()
    store.create_deal(con,coin='AIW3',chain='bsc',token='0x'+'b'*40,token_dec=18,perp_venue='aster',
                      symbol='AIW3USDT',leg_usd=Decimal(200),owner_json='{}',sim=True,deal_id='DQ')
    def plan(*a):
        entered.set();release.wait(3)
        iid,nonce=store.create_intent(conns.get(),deal_id='DQ',kind='entry',spec={'coin':'AIW3'},plan={},now=time.time())
        return SimpleNamespace(intent_id=iid,nonce=nonce,html='plan',superseded=())
    s=service(conns,SimpleNamespace(propose_entry=plan));s.bot.owner_loader=lambda:cfg
    s.start(reconcile=False)
    try:
        s.dispatch('submit_user_command',{'update':update(1,'вход AIW3 okx·bsc aster 200')},'tg:1',os.getuid())
        assert entered.wait(2)
        settings['limit']=100;release.set()
        wait_for(lambda:s.journal.get('tg:1')['state']=='done')
        r=con.execute('SELECT intent_id,fingerprint FROM core_plan_guards').fetchone()
        assert r is not None and r['fingerprint'] != s._plan_fingerprint(r['intent_id'])
        assert s.engine.submissions==[]
    finally:release.set();s.shutdown()


def test_end_drain_rpc_lost_response_retries_without_new_recovery(conns):
    s = service(conns); s.ready = True; s.release = {'release_id':'R2'}
    first = s.dispatch('begin_drain', {'release_id':'R2','expected_state_revision':0}, None, 0)
    s._drain_status = lambda _: {'safe_to_switch': True}
    payload = dict(drain_epoch=first['drain_epoch'], expected_release_id='R2')
    result = s.dispatch('end_drain', payload, None, 0)
    s._drain_status = lambda _: (_ for _ in ()).throw(AssertionError('no second recovery'))
    assert s.dispatch('end_drain', payload, None, 0) == result
    with pytest.raises(RpcError, match='stale_drain_owner'):
        s.dispatch('end_drain', dict(payload, drain_epoch='stale'), None, 0)


def test_drain_evidence_invalidated_by_later_ledger_commit(conns, monkeypatch):
    from funding_bot.core import recovery
    s=service(conns); s.ready=True; s.release={'release_id':'R2'}
    s.execution_owner=SimpleNamespace(fd=3)
    s.jobs.sync=True
    monkeypatch.setattr(recovery, 'check', lambda *_, **kwargs: [])
    try:
        begin=s.dispatch('begin_drain', {'release_id':'R2','expected_state_revision':0}, None, 0)
        payload=dict(drain_epoch=begin['drain_epoch'])
        assert s.dispatch('get_drain_state',payload,None,0)['safe_to_switch']
        # A later fill/backfill commit invalidates even if no pending attempt exists.
        conns.get().execute("INSERT INTO flags VALUES('fixture_new_fill','1')")
        monkeypatch.setattr(recovery, 'check', lambda *_, **kwargs: ['position_unverified'])
        state=s.dispatch('get_drain_state',payload,None,0)
        assert not state['safe_to_switch'] and not state['recovery']['complete']
    finally:
        s._evidence_db.close()


def test_drain_rejects_commit_inside_final_validation(conns, monkeypatch):
    from funding_bot.core import recovery
    s=service(conns);s.ready=True;s.release={'release_id':'R2'}
    s.execution_owner=SimpleNamespace(fd=3);s.jobs.sync=True
    def check(con, legs, *, resolve=True):
        if not resolve:
            con.execute("INSERT OR REPLACE INTO flags VALUES('concurrent_evidence','changed')")
        return []
    monkeypatch.setattr(recovery,'check',check)
    try:
        first=s.dispatch('begin_drain',{'release_id':'R2','expected_state_revision':0},None,0)
        result=s.dispatch('get_drain_state',{'drain_epoch':first['drain_epoch']},None,0)
        assert not result['safe_to_switch']
        assert 'evidence_changed_during_validation' in result['recovery']['blockers']
    finally:s._evidence_db.close()
