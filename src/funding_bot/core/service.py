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
                 table_loader=None, ui_uid=None, deploy_uid=0, snapshotter=None,
                 execution_owner=None, release=None, start_drained=False, drain_release_id=None):
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
        row = conns.get().execute("SELECT version,min_reader FROM schema_version WHERE id=1").fetchone()
        if row is None:
            raise ValueError("missing trading schema version")
        self.schema_version, self.min_reader = int(row[0]), int(row[1])
        self.execution_owner = execution_owner
        self.release = release or {}
        if not hasattr(engine, "drain_evt"):
            engine.drain_evt = threading.Event()
        from .drain import Drain
        self.drain_state = Drain(conns, engine.drain_evt, start_drained=start_drained, release_id=drain_release_id)
        self.drain = self.drain_state.state["drain"]
        self.recovery = dict(complete=False, checked_at=None, evidence_revision=None)
        self.recovery_pending = False
        self.legs = legs
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
        ready = (self.ready and not self.stop.is_set() and 0 <= time.time()-self.last_loop < 5
                 and all(t.is_alive() for t in self.workers))
        return dict(ipc_version=VERSION, schema_version=self.schema_version, min_reader=self.min_reader,
                    core_version=BUILD_ID, pid=os.getpid(), boot_id=self.boot_id,
                    started_at=self.started, updated_at=self.last_loop, ready=ready,
                    drain=self.drain, drain_epoch=self.drain_state.state['drain_epoch'],
                    state_revision=self.drain_state.state['state_revision'], busy=self.engine.busy(),
                    execution_lock_held=self.execution_owner is not None and self.execution_owner.fd is not None,
                    recovery_complete=self.recovery['complete'],
                    **{k:self.release.get(k) for k in ('release_id','source_sha256','artifact_sha256',
                                                      'verification_identity_sha256')})

    def _schedule_recovery(self):
        if self.recovery_pending or self.engine.busy():
            return
        epoch = self.drain_state.state['drain_epoch']
        revision = self.drain_state.state['state_revision']
        if (self.recovery['complete'] and self.recovery['evidence_revision'] == revision
                and 0 <= time.time()-self.recovery['checked_at'] < 30):
            return
        self.recovery_pending = True
        def recover():
            result = dict(complete=False, checked_at=time.time(), evidence_revision=revision,
                          blockers=['recovery_failed'])
            try:
                if self.drain and not self.engine.busy():
                    from .recovery import check
                    blockers = check(self.conns.get(), self.legs)
                    result = dict(complete=not blockers, checked_at=time.time(),
                                  evidence_revision=revision, blockers=blockers)
            except Exception:
                log.exception('drain recovery failed')
            finally:
                with self._drain_lock:
                    if self.drain_state.state['drain_epoch'] == epoch:
                        self.recovery = result
                    self.recovery_pending = False
        if not self.jobs.put('drain-recovery', recover):
            self.recovery_pending = False

    def _drain_status(self, payload):
        if payload.get('drain_epoch') != self.drain_state.state['drain_epoch']:
            raise RpcError('stale_drain_owner')
        if self.drain:
            self._schedule_recovery()
        from .preflight import inventory
        state = inventory(self.conns.get())
        health = self.health()
        blockers = []
        if not self.drain:
            blockers.append('not_drained')
        if not health['ready']:
            blockers.append('core_not_ready')
        if not health['execution_lock_held']:
            blockers.append('execution_owner_unverified')
        if not self.release:
            blockers.append('release_identity_unverified')
        if self.engine.busy():
            blockers.append('execution_busy')
        if not state['ledger_quiet']:
            blockers.append('journal_unresolved')
        rec = self.recovery
        if (not rec['complete'] or rec['evidence_revision'] != health['state_revision']
                or rec['checked_at'] is None or not 0 <= time.time()-rec['checked_at'] < 30):
            blockers.append('fresh_recovery_required')
        counts = state['pending']
        return dict(drain=self.drain, drain_epoch=health['drain_epoch'], ready=health['ready'],
                    state_revision=health['state_revision'], recovery=dict(rec),
                    execution=dict(busy=self.engine.busy(), lock_held=health['execution_lock_held'],
                                   owner_pid=health['pid'], owner_boot_id=health['boot_id']),
                    inventory=dict(pending_requests=counts.get('requests',0), operations=counts['operations'],
                                   clips=counts['clips'], perp=counts['perp'], unresolved=sum(counts.values())),
                    journal=state, safe_to_switch=not blockers, blockers=blockers)

    def dispatch(self, method, payload, key, uid):
        if uid not in (self.ui_uid, self.deploy_uid):
            raise RpcError('unauthorized_peer')
        if method in ('begin_drain', 'get_drain_state', 'end_drain'):
            if uid != self.deploy_uid:
                raise RpcError('unauthorized_method')
            with self._drain_lock:
                if method == 'begin_drain':
                    result = self.drain_state.begin(payload)
                    self.drain = True
                    return result
                if method == 'end_drain':
                    if not self._drain_status(payload)['safe_to_switch']:
                        raise RpcError('drain_not_ready')
                    result = self.drain_state.end(payload, running_release=self.release.get('release_id'))
                    self.drain = False
                    return result
                return self._drain_status(payload)
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
