"""M1 headless runtime: trading keeps running independently of the interface."""
import hashlib
import json
import logging
import os
import threading
import time
from .journal import Journal, Outbox
from .commands import Bot, Jobs
from ..ipc.protocol import RpcError, VERSION
from ..trade import tconfig
from ..build_info import BUILD_ID

log = logging.getLogger(__name__)


class CommandJobs(Jobs):
    """Track asynchronous plan work so inbox DONE never precedes the plan job."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.local = threading.local()
        self.last_tick_done = time.time()

    def put(self, name, fn):
        pending = getattr(self.local, 'pending', None)
        done = threading.Event()
        try:
            plan_config = self.config_loader()
        except Exception:
            plan_config = None
        def run():
            self.local.plan_config = plan_config
            try:
                fn()
            finally:
                self.local.plan_config = None
                done.set()
        ok = super().put(name, run)
        if ok and pending is not None:
            pending.append(done)
        return ok


class CoreService:
    def __init__(self, conns, desk, engine, legs, *, rt=None, mode='dry', owner_loader=None,
                 table_loader=None, ui_uid=None, deploy_uid=0, snapshotter=None):
        self.conns, self.engine = conns, engine
        self.ui_uid = os.getuid() if ui_uid is None else ui_uid
        self.deploy_uid = deploy_uid
        self.journal = Journal(conns)
        self.outbox = Outbox(self.journal)
        self.jobs = CommandJobs()
        kwargs = {'owner_loader': owner_loader} if owner_loader else {}
        self.bot = Bot(conns=conns, desk=desk, engine=engine, sender=self.outbox, legs=legs,
                       rt=rt, mode=mode, jobs=self.jobs, poll_api=self.outbox, table_loader=table_loader, **kwargs)
        self.jobs.config_loader = lambda: self.bot.owner_loader().frozen()
        self.outbox.plan_guard = self._save_plan_guard
        self.stop = threading.Event()
        self.ready = False
        self.drain = False
        self.started = time.time()
        self.last_loop = self.started
        self.boot_id = os.urandom(12).hex()
        self.workers = []
        self.snapshotter = snapshotter
        self.snapshot = None
        self.snapshot_lock = threading.Lock()
        self._drain_lock = threading.RLock()

    def start(self, reconcile=True):
        self.journal.recover()
        if reconcile:
            self.bot.startup()  # If reconciliation raises, never start executor / accept orders.
        self.engine.start()
        self.jobs.start()
        self.ready = True
        for name, target in [('commands', lambda: self._worker(False)), ('controls', lambda: self._worker(True)),
                             ('projections', self._project)]:
            t = threading.Thread(target=target, name='core-'+name, daemon=True)
            t.start()
            self.workers.append(t)
        self.bot.say('♻️ Торговое ядро запущено. Прерванные команды автоматически не повторяются; нужен свежий план.')

    def health(self):
        return dict(schema_version=VERSION, core_version=BUILD_ID, pid=os.getpid(), boot_id=self.boot_id,
                    started_at=self.started, updated_at=self.last_loop, ready=self.ready and not self.stop.is_set() and 0 <= time.time()-self.last_loop < 5
                    and all(t.is_alive() for t in self.workers),
                    drain=self.drain, busy=self.engine.busy())

    def dispatch(self, method, payload, key, uid):
        if uid not in (self.ui_uid, self.deploy_uid):
            raise RpcError('unauthorized_peer')
        if method in ('begin_drain', 'get_drain_state'):
            if uid != self.deploy_uid:
                raise RpcError('unauthorized_method')
            with self._drain_lock:
                if method == 'begin_drain':
                    self.drain = True
                from .preflight import inventory
                state = inventory(self.conns.get())
                return dict(drain=self.drain, pending_requests=state['pending'].get('requests', 0),
                            busy=self.engine.busy(), ready=self.ready, journal=state,
                            safe_to_switch=False, switch_gate='M5_full_recovery_and_schema_gate_required')
        if uid != self.ui_uid:
            if method == 'get_status':
                return self.health()
            raise RpcError('unauthorized_method')
        if method == 'get_status':
            return self.health()
        if method == 'get_request':
            return self.journal.get(payload.get('key'))
        if method == 'get_offset_floor':
            return {'offset': self.journal.offset_floor()}
        if method == 'read_notifications':
            return {'events': self.journal.notifications(payload.get('after', 0), payload.get('limit', 20))}
        if method == 'ack_notification':
            return self.outbox.ack(payload.get('event_id'), payload.get('result', {}))
        if method == 'list_positions':
            return self._positions(payload)
        if method == 'submit_user_command':
            return self._submit(payload, key)
        raise RpcError('unsupported_method')

    def _submit(self, payload, key):
        if set(payload) != {'update'} or not isinstance(payload['update'], dict):
            raise RpcError('invalid_update')
        u = payload['update']
        uid = u.get('update_id')
        if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0 or key != f'tg:{uid}':
            raise RpcError('invalid_source_key')
        if len(json.dumps(u).encode()) > 65536:
            raise RpcError('update_too_large')
        # STOP remains prompt while a slow plan is being quoted. Callbacks are also only local CAS/outbox work.
        from ..tg import parse
        name = parse.parse((u.get('message') or {}).get('text') or '').name
        priority = 0 if name == 'stop' else (1 if 'callback_query' in u else 2)
        with self._drain_lock:
            # A timed-out accepted request is always retrievable, including after drain began.
            try:
                self.journal.get(key)
            except RpcError as e:
                if e.code != 'not_found':
                    raise
                if not self.ready:
                    raise RpcError('core_not_ready')
                if self.drain and name not in ('stop', 'help', 'positions', 'status'):
                    raise RpcError('draining')
            return self.journal.accept(key, payload, priority)

    def _plan_fingerprint(self, iid, config=None):
        cfg = self.bot.owner_loader()
        con = self.conns.get()
        row = con.execute("SELECT i.spec_json,i.plan_json,i.nonce,d.state,d.updated FROM intents i JOIN deals d ON d.id=i.deal_id WHERE i.id=?", (iid,)).fetchone()
        if row is None:
            raise RpcError('plan_missing')
        body = json.dumps({'intent':list(row), 'config':cfg.frozen() if config is None else config}, sort_keys=True, default=str)
        return hashlib.sha256(body.encode()).hexdigest()

    def _save_plan_guard(self, iid):
        fp = self._plan_fingerprint(iid, getattr(self.jobs.local, "plan_config", None))
        self.conns.get().execute('INSERT OR REPLACE INTO core_plan_guards VALUES(?,?)', (iid,fp))

    def _check_plan_guard(self, update):
        from ..tg.parse import parse_callback
        cq = update.get('callback_query')
        if not isinstance(cq, dict):
            return True
        cb = parse_callback(cq.get('data'))
        if not cb or cb.action != 'ok':
            return True
        r = self.conns.get().execute('SELECT fingerprint FROM core_plan_guards WHERE intent_id=?', (cb.intent_id,)).fetchone()
        try:
            return r is not None and r[0] == self._plan_fingerprint(cb.intent_id)
        except Exception:
            return False

    def _worker(self, fast):
        while not self.stop.is_set():
            key = None
            try:
                item = self.journal.claim(fast)
                if item is None:
                    self.stop.wait(.1)
                    continue
                key, payload = item
                u = payload['update']
                if u['update_id'] < self.journal.legacy_floor:
                    self.journal.finish(key, {'verdict': 'legacy_already_processed'})
                    continue
                self.jobs.local.pending = []
                # classify rechecks owner/chat/date; auth.press rechecks intent nonce/TTL/CAS and pause.
                # Drain may begin after acceptance but before dispatch; old queued approvals cannot slip through.
                from ..tg.parse import parse
                name = parse((u.get('message') or {}).get('text') or '').name
                if self.drain and name not in ('stop', 'help', 'positions', 'status'):
                    self.journal.finish(key, {'reason':'draining'}, 'interrupted')
                    continue
                if not self._check_plan_guard(u):
                    self.bot._answer((u.get('callback_query') or {}).get('id'), 'План устарел — запросите новый')
                    self.journal.finish(key, {'verdict':'stale_plan'})
                    continue
                verdict = self.bot.handle(u)
                for done in self.jobs.local.pending:
                    while not done.wait(.1):
                        if self.stop.is_set():
                            self.journal.finish(key, {'reason': 'shutdown'}, 'interrupted')
                            break
                if not self.stop.is_set():
                    self.journal.finish(key, {'verdict': verdict})
            except Exception:
                log.exception('core command failed')
                if key is not None:
                    self.journal.finish(key, {'reason': 'command_failed'}, 'failed')
                    self.bot.say('⚠️ Команда не завершена из-за ошибки ядра. Проверьте статус; повтор входа требует нового плана.')
            finally:
                self.jobs.local.pending = None

    def _project(self):
        while not self.stop.is_set():
            if self.snapshotter is not None:
                try:
                    snapshot = self.snapshotter(self.conns.get())
                    from ..ipc.values import encode
                    snapshot = encode(snapshot)
                    with self.snapshot_lock:
                        self.snapshot = snapshot
                except Exception:
                    log.exception('projection failed; old as_of retained')
            self.stop.wait(5)

    def _positions(self, payload):
        after, limit = payload.get('offset', 0), payload.get('limit', 10)
        if not isinstance(after, int) or after < 0 or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise RpcError('invalid_page')
        with self.snapshot_lock:
            snap = self.snapshot
        if snap is None:
            raise RpcError('projection_unavailable')
        rev = snap['revision']
        if payload.get('revision') not in (None, rev):
            raise RpcError('snapshot_changed')
        return {**snap, 'deals': snap['deals'][after:after+limit],
                'next_offset': after+limit if after+limit < len(snap['deals']) else None}

    def shutdown(self):
        self.ready = False
        self.drain = True
        self.stop.set()
        self.engine.term.set()
        self.jobs.stop()
        # Do not release singleton while any signing/execution thread is still working.
        while not self.engine.wait_idle(1):
            pass
        self.engine.stop()
        if getattr(self.engine, "_thread", None) is not None:
            self.engine._thread.join()
        if self.jobs._thread is not None:
            self.jobs._thread.join()
        for t in self.workers:
            t.join(timeout=2)
