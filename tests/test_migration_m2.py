"""M2 process boundaries, read model parity, freshness and secret isolation."""
import importlib.util
import json
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import pytest
from funding_bot import cabinet, config, market_snapshot
from funding_bot.core import readmodel
from funding_bot.ipc.values import encode, decode
from funding_bot.interface.projections import fetch_positions
from funding_bot.ipc.protocol import RpcError


def test_decimal_dto_preserves_numbers_and_numeric_symbols():
    a={'coin':'4','symbol':'1000X','amount':Decimal('0.000000000000000123'),'fee':Decimal('-0.0002')}
    wire=json.loads(json.dumps(encode(a)))
    assert wire['amount']=={'$decimal':'1.23E-16'}
    assert decode(wire)==a


def test_interface_import_has_no_trading_store_engine_or_keys_factory():
    code='''import sys
from funding_bot.interface.runtime import Interface
from funding_bot import cabinet
assert not any(n in sys.modules for n in ('funding_bot.trade.store','funding_bot.trade.engine',
'funding_bot.trade.marks','funding_bot.trade.runtime','funding_bot.core.readmodel'))
'''
    subprocess.run([sys.executable,'-c',code],check=True)


def test_headless_import_without_telegram_network_or_web():
    code='''import sys
from funding_bot.core.bootstrap import run_core
assert 'funding_bot.tg.api' not in sys.modules
assert 'funding_bot.serve' not in sys.modules
assert 'funding_bot.cabinet' not in sys.modules
'''
    subprocess.run([sys.executable,'-c',code],check=True)


def test_cabinet_default_reads_ipc_not_database(monkeypatch):
    from funding_bot.interface import projections
    calls=[]
    monkeypatch.setattr(projections,'fetch_positions',lambda **kw:(calls.append(kw) or dict(now=123,deals=[],drafts=0,err=None)))
    assert cabinet.Cabinet({}).snapshot()['now']==123
    assert calls


def test_snapshot_failure_never_claims_empty_current_positions():
    class C:
        def call(self,*a,**k):raise RpcError('core_unavailable')
    s=fetch_positions(C(),now=100)
    assert s['err'] and 'не получены' in s['err']
    assert 'не получены' in cabinet.deals_fragment(s)


def test_pagination_same_revision_and_stale_rejection():
    class C:
        def call(self,m,p=None):
            if m=='get_status':return {'ready':True}
            offset=p['offset']
            if offset:assert p['revision']=='r1'
            return dict(as_of=100,now=100,revision='r1',schema_version=1,deals=[{'id':str(offset)}],
                        drafts=0,err=None,next_offset=10 if not offset else None)
    assert len(fetch_positions(C(),now=110)['deals'])==2
    assert fetch_positions(C(),now=200)['err']


def test_replay_cabinet_dto_matches_existing_evm_fixture(tmp_path,monkeypatch):
    from test_marks import dqa9q
    from funding_bot.trade import store
    con=store.connect(tmp_path/'trade.db');dqa9q(con)
    monkeypatch.setattr(config,'TABLE_PATH',tmp_path/'missing.json')
    at=1800000000
    legacy=readmodel.load_deals(con,now=at)
    dto=decode(encode(readmodel.snapshot(con,now=at)))
    # JSON naturally turns tuples into lists; money must remain Decimal.
    norm=lambda x:decode(json.loads(json.dumps(encode(x))))
    assert norm(dto['deals'])==norm(legacy['deals'])
    assert cabinet.deals_fragment(dto)==cabinet.deals_fragment(dict(now=at,err=None,**legacy))
    con.close()


def test_market_health_bounded_stale_future_missing_and_unknown(tmp_path):
    p=tmp_path/'table.json'
    t=dict(schema_version=1,snapshot_id='abc',tick_ts=100,pid=42,sf_rows=[{'private':'not copied'}],
           notes={'secret':'never in health'},src_age={'a':0})
    market_snapshot.publish_health(t,p)
    h=market_snapshot.health_path(p)
    assert h.stat().st_size<16384
    assert b'private' not in h.read_bytes() and b'secret' not in h.read_bytes()
    assert market_snapshot.load_health(h,now=101)['ready']
    assert not market_snapshot.load_health(h,now=100000)['ready']
    assert not market_snapshot.load_health(h,now=1)['ready']
    h.write_text('{')
    assert not market_snapshot.load_health(h)['ready']
    p.write_text(json.dumps(dict(schema_version=999,sf_rows=[{}])))
    assert market_snapshot.load_market_table(p)=={}


def test_health_never_reads_table_and_latency(tmp_path,monkeypatch):
    import statistics
    p=tmp_path/'collector_health.json'
    p.write_text(json.dumps(dict(schema_version=1,tick_ts=time.time(),pid=42)))
    times=[]
    for _ in range(200):
        start=time.perf_counter();h=market_snapshot.load_health(p);times.append(time.perf_counter()-start)
        assert h['ready']
    assert sorted(times)[189]<.2


def layout_module():
    spec=importlib.util.spec_from_file_location('prepare_layout',Path(__file__).parents[1]/'deploy/migration/prepare_layout.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod


def test_env_split_uses_allowlist_and_preserves_literal_values(tmp_path):
    mod=layout_module()
    src=tmp_path/'source.env';src.write_text('TG_BOT_TOKEN=fake-tg\nDEX_EVM_KEY=fake-evm\nCUSTOM_HL_KEY=fake-hl\nOKX_DEX_SECRET="fake-$literal"\n')
    cab=tmp_path/'cab.env';cab.write_text('CABINET_LOGIN=fake-owner\nCABINET_PASS_HASH="fake-$hash"\n')
    root=tmp_path/'stage';mod.prepare(root,src,cab,interface_uid=123)
    ui=(root/'secrets/interface.env').read_text();core=(root/'secrets/core.env').read_text();col=(root/'secrets/collector.env').read_text()
    assert 'fake-evm' not in ui+col and 'fake-hl' not in ui+col
    assert 'fake-tg' not in core+col and 'fake-$hash' not in core+col
    assert 'fake-$literal' in core and 'fake-$literal' in col and 'fake-$literal' not in ui
    assert 'FUNDING_TABLE_PATH="/var/lib/funding-bot/collector/public/table.json"' in ui
    assert 'FUNDING_OKX_PACE="/var/lib/funding-bot/shared/okxdex.pace"' in core and 'FUNDING_OKX_PACE=' in col
    assert (root/'secrets/core.env').stat().st_mode & 0o777==0o600
    with pytest.raises(ValueError):mod.prepare(root,src,interface_uid=123)


def test_unknown_cabinet_keys_refused(tmp_path):
    mod=layout_module();src=tmp_path/'a';src.write_text('TG_BOT_TOKEN=fake\n')
    cab=tmp_path/'b';cab.write_text('DEX_EVM_KEY=fake\n')
    with pytest.raises(ValueError):mod.prepare(tmp_path/'stage',src,cab,interface_uid=123)


def test_units_have_separate_credentials_and_users():
    root=Path(__file__).parents[1]/'deploy/migration'
    for role in ('core','collector','interface'):
        text=(root/f'funding_bot-{role}.service').read_text()
        assert f'User=funding-{role}' in text
        assert f'EnvironmentFile=/var/lib/funding-bot/secrets/{role}.env' in text
        assert 'NoNewPrivileges=true' in text and 'ProtectSystem=strict' in text
    ui=(root/'funding_bot-interface.service').read_text()
    assert 'InaccessiblePaths=/var/lib/funding-bot/core' in ui
    assert 'Requires=funding_bot-core' not in ui


def test_split_okx_never_falls_back_to_independent_quota(tmp_path,monkeypatch):
    from funding_bot.okxdex import OkxDex
    monkeypatch.setenv('FUNDING_PROCESS','core')
    client=OkxDex(key='fake',secret='fake',passphrase='fake',pace_path=tmp_path/'pace')
    monkeypatch.setattr(client,'_shared_slot',lambda *a:(_ for _ in ()).throw(PermissionError('synthetic')))
    with pytest.raises(PermissionError):client._reserve(time.time())
    assert client.pace_path is not None


def test_health_under_large_dashboard_load(tmp_path,monkeypatch):
    import concurrent.futures
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from funding_bot import serve
    p=tmp_path/'table.json'
    t=dict(schema_version=1,snapshot_id='load-test',tick_ts=time.time(),pid=42,
           n_ff=0,n_sf=0,ff_rows=[],sf_rows=[],padding='x'*(44*1024*1024))
    p.write_text(json.dumps(t));market_snapshot.publish_health(t,p)
    monkeypatch.setattr(config,'TABLE_PATH',p)
    server=ThreadingHTTPServer(('127.0.0.1',0),serve.Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url='http://127.0.0.1:'+str(server.server_address[1])
    stop=threading.Event()
    def dashboard():
        for _ in range(3):
            with urllib.request.urlopen(url+'/data.json',timeout=10) as r:
                while r.read(65536):pass
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            jobs=[pool.submit(dashboard) for _ in range(3)]
            samples=[]
            for _ in range(60):
                start=time.perf_counter()
                with urllib.request.urlopen(url+'/status',timeout=5) as r:body=r.read()
                samples.append(time.perf_counter()-start)
                assert len(body)<=16384 and json.loads(body)['ready']
            for j in jobs:j.result()
        p95=sorted(samples)[56]
        print(json.dumps(dict(table_bytes=p.stat().st_size, dashboard_clients=3, dashboard_downloads=9,
                              status_requests=60,p95_ms=p95*1000,max_ms=max(samples)*1000)))
        assert p95<.2
    finally:
        server.shutdown();server.server_close();thread.join()
