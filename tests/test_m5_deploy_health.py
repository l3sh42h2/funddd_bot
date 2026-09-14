import json
import os
import time

from funding_bot import market_snapshot
from funding_bot.interface.runtime import Interface, State


IDENTITY = {'release_id': 'release-123', 'source_sha256': 's' * 64, 'artifact_sha256': 'a' * 64}


def test_collector_health_exposes_loaded_release_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(market_snapshot, 'RELEASE', dict(IDENTITY))
    target = tmp_path / 'table.json'
    table = {'schema_version': 1, 'snapshot_id': 'x', 'generated_at': time.time(),
             'tick_ts': time.time(), 'pid': os.getpid(), 'started_ts': time.time(), 'n_ff': 1, 'n_sf': 1}
    market_snapshot.publish_health(table, target)
    value = json.loads((tmp_path / 'collector_health.json').read_text())
    assert {k: value[k] for k in IDENTITY} == IDENTITY
    assert value['pid'] == os.getpid() and value['boot_id'] and value['ready']
    assert len(json.dumps(value).encode()) <= market_snapshot.HEALTH_MAX


def test_interface_health_exposes_loaded_release_identity(tmp_path, monkeypatch):
    monkeypatch.setattr('funding_bot.interface.runtime.load_release', lambda: dict(IDENTITY))
    interface = Interface(object(), object(), client=object(), state=State(tmp_path / 'state.json'))
    interface.last_poll = time.time(); interface.last_core = time.time()
    value = interface.health()
    assert {k: value[k] for k in IDENTITY} == IDENTITY
    assert value['pid'] == os.getpid() and value['boot_id'] and value['ready']
