"""Headless process entrypoint. No TG token/API startup dependency."""
import logging
import os
import signal
import threading
from pathlib import Path
from .commands import build_trader_legs
from .service import CoreService
from ..ipc.lock import ExecutionLock
from ..ipc.paths import execution_lock, socket_path
from ..ipc.protocol import Server
from ..trade import owner, tconfig
from ..trade.engine import Conns, CfgHolder, Desk, Engine
from ..trade.keys import install_log_redaction, KeysError, redact

log = logging.getLogger(__name__)


def run_core(environ=None):
    env = os.environ if environ is None else environ
    install_log_redaction()
    # A wrong path must never silently create an empty trading database.
    if not Path(tconfig.TRADE_DB_PATH).is_file():
        log.error('trade DB missing; initialize explicitly before starting core')
        return 78
    try:
        with ExecutionLock(execution_lock()) as lock:
            return _run(env, lock)
    except BlockingIOError:
        log.error('another trader/core owns execution lock')
        return 78
    except (owner.OwnerConfigError, KeysError) as e:
        log.error('core configuration: %s', redact(e))
        return 78


def _run(env, execution_owner=None):
    from .release import load_release
    release = load_release(env.get("FUNDING_RELEASE_MANIFEST"))
    cfg = owner.load()
    conns, holder = Conns(), CfgHolder()
    rt, legs, keys_mode, mode, legacy_on = build_trader_legs(cfg, conns, holder, env)
    ref = {}
    from ..market_snapshot import load_market_table
    desk = Desk(conns, legs, table_loader=load_market_table, keys_mode=keys_mode, busy=lambda: ref['engine'].busy())
    engine = Engine(conns, legs, desk, keys_mode=keys_mode, holder=holder, busy_path=tconfig.TRADING_BUSY)
    ref['engine'] = engine
    from .readmodel import snapshot
    from ..market_snapshot import load_market_table
    service = CoreService(conns, desk, engine, legs, rt=rt, mode=mode, table_loader=load_market_table,
                          owner_loader=owner.load, ui_uid=int(env.get('FUNDING_INTERFACE_UID', os.getuid())),
                          deploy_uid=int(env.get('FUNDING_DEPLOY_UID', 0)), snapshotter=snapshot,
                          execution_owner=execution_owner, release=release,
                          start_drained=env.get('FUNDING_START_DRAINED') == '1',
                          drain_release_id=env.get('FUNDING_DRAIN_RELEASE_ID'))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    server = None
    try:
        service.start()
        # RuntimeDirectory is initially owned by core's primary group; shared IPC group is explicit.
        ipc_group = env.get('FUNDING_IPC_GROUP')
        if ipc_group:
            import grp
            parent = socket_path().parent
            parent.mkdir(parents=True, exist_ok=True)
            os.chown(parent, -1, grp.getgrnam(ipc_group).gr_gid)
        server = Server(socket_path(), service.dispatch, {service.ui_uid, service.deploy_uid})
        if ipc_group:
            os.chown(socket_path(), -1, grp.getgrnam(ipc_group).gr_gid)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
        thread.start()
        while not stop.wait(.5):
            service.last_loop = __import__('time').time()
    finally:
        if server:
            server.shutdown()
            server.server_close()
        service.shutdown()
    return 0
