"""Fencing survives crash/restart and never clears an owner's separate STOP."""
import threading
import pytest
from funding_bot.core.drain import Drain
from funding_bot.core.journal import Journal
from funding_bot.ipc.protocol import RpcError
from funding_bot.trade.engine import Conns


def setup(tmp_path):
    conns = Conns(tmp_path/'trade.db')
    Journal(conns)
    return conns


def test_durable_drain_epoch_and_retry(tmp_path):
    conns = setup(tmp_path)
    event = threading.Event()
    drain = Drain(conns, event)
    request = dict(release_id='R2', expected_state_revision=0)
    first = drain.begin(request)
    assert event.is_set() and first['state_revision'] == 1
    assert drain.begin(request) == first
    restarted_event = threading.Event()
    restarted = Drain(conns, restarted_event)
    assert restarted_event.is_set() and restarted.state == first
    with pytest.raises(RpcError, match='drain_owned_by_other_release'):
        restarted.begin(dict(release_id='R3', expected_state_revision=1))
    with pytest.raises(RpcError, match='stale_drain_owner'):
        restarted.end(dict(drain_epoch='stale', expected_release_id='R2'), running_release='R2')
    owner_stop = threading.Event(); owner_stop.set()
    result = restarted.end(dict(drain_epoch=first['drain_epoch'], expected_release_id='R2'), running_release='R2')
    assert result['state_revision'] == 2 and not restarted_event.is_set() and owner_stop.is_set()
    with pytest.raises(RpcError, match='stale_state_revision'):
        restarted.begin(request)


def test_first_migration_fenced_before_executor(tmp_path):
    conns = setup(tmp_path)
    event = threading.Event()
    d = Drain(conns, event, start_drained=True, release_id='R1')
    assert event.is_set() and d.state['drain']
    # Environment bootstrap flag must not create a new epoch after normal restart.
    again = Drain(conns, threading.Event(), start_drained=True, release_id='R1')
    assert again.state == d.state


def test_failed_persistence_stays_fenced(tmp_path, monkeypatch):
    conns = setup(tmp_path); event = threading.Event(); drain = Drain(conns, event)
    def fail(*_): raise RuntimeError('disk unavailable')
    monkeypatch.setattr(drain, '_write', fail)
    with pytest.raises(RuntimeError):
        drain.begin(dict(release_id='R2', expected_state_revision=0))
    assert event.is_set()


def test_stale_controller_cannot_overwrite_epoch(tmp_path):
    conns = setup(tmp_path)
    a, b = Drain(conns, threading.Event()), Drain(conns, threading.Event())
    a.begin(dict(release_id='R2', expected_state_revision=0))
    with pytest.raises(RpcError, match='stale_drain_state'):
        b.begin(dict(release_id='R3', expected_state_revision=0))
    assert Drain(conns, threading.Event()).state['release_id'] == 'R2'


def test_native_gate_reads_deploy_fence_and_unknown_fails_closed(tmp_path):
    from funding_bot.trade import store
    conns = setup(tmp_path); con = conns.get()
    assert not store.execution_paused(con)
    d = Drain(conns, threading.Event())
    state = d.begin(dict(release_id='R2', expected_state_revision=0))
    assert store.execution_paused(con) and not store.is_paused(con)
    d.end(dict(drain_epoch=state['drain_epoch'], expected_release_id='R2'), running_release='R2')
    assert not store.execution_paused(con)
    con.execute("UPDATE core_meta SET value='broken' WHERE key='deployment_drain'")
    assert store.execution_paused(con)
