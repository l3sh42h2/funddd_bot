"""Additive command inbox and notification outbox, owned only by core."""
import hashlib
import json
import threading
import time
from ..ipc.protocol import RpcError

SCHEMA = '''
CREATE TABLE IF NOT EXISTS core_requests(
 key TEXT PRIMARY KEY, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
 priority INTEGER NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
 updated REAL NOT NULL, result TEXT);
CREATE TABLE IF NOT EXISTS core_notifications(
 id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL, created REAL NOT NULL,
 delivered REAL, result TEXT);
CREATE TABLE IF NOT EXISTS core_plan_guards(intent_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS core_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
'''


class Journal:
    def __init__(self, conns, max_pending=100):
        self.conns, self.max_pending = conns, max_pending
        self.lock = threading.RLock()
        con = self.conns.get()
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='core_meta'").fetchone():
            version = con.execute("SELECT value FROM core_meta WHERE key='schema_version'").fetchone()
            if version and version[0] not in ('1', '2', '3', '4'):
                raise RpcError('unsupported_core_schema')
        con.executescript(SCHEMA)
        con.execute("INSERT OR IGNORE INTO core_meta VALUES('schema_version','1')")
        self.legacy_floor = self.offset_floor()

    def accept(self, key, payload, priority=2):
        if not isinstance(key, str) or not key or len(key) > 200:
            raise RpcError('invalid_key')
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        digest = hashlib.sha256(body.encode()).hexdigest()
        with self.lock:
            con = self.conns.get()
            con.execute('BEGIN IMMEDIATE')
            try:
                row = con.execute('SELECT * FROM core_requests WHERE key=?', (key,)).fetchone()
                if row:
                    if row['payload_hash'] != digest:
                        raise RpcError('key_payload_mismatch')
                else:
                    n = con.execute("SELECT count(*) FROM core_requests WHERE state IN ('queued','running')").fetchone()[0]
                    if n >= self.max_pending and priority > 0:
                        raise RpcError('busy')
                    # A small bounded reserve for pause; never an unbounded emergency queue.
                    if n >= self.max_pending + 10:
                        raise RpcError('busy')
                    now = time.time()
                    con.execute('INSERT INTO core_requests VALUES(?,?,?,?,?,?,?,NULL)',
                                (key, body, digest, priority, 'queued', now, now))
                # Preserve the legacy offset journal as part of durable acceptance for a compatible rollback.
                from ..trade import store
                uid = payload['update']['update_id']
                store.claim_update(con, uid, time.time(), None, None, '[core inbox]')
                store.set_tg_offset(con, uid + 1)
                con.commit()
            except BaseException:
                con.rollback()
                raise
        return self.get(key)

    def get(self, key):
        r = self.conns.get().execute('SELECT key,state,result FROM core_requests WHERE key=?', (key,)).fetchone()
        if r is None:
            raise RpcError('not_found')
        return {'key': r['key'], 'state': r['state'], 'result': json.loads(r['result']) if r['result'] else None}

    def claim(self, fast=False):
        with self.lock:
            con = self.conns.get()
            op = '<' if fast else '>='
            r = con.execute(f"SELECT * FROM core_requests WHERE state='queued' AND priority {op} 2 ORDER BY priority,created LIMIT 1").fetchone()
            if r is None:
                return None
            con.execute("UPDATE core_requests SET state='running',updated=? WHERE key=? AND state='queued'", (time.time(), r['key']))
            return r['key'], json.loads(r['payload'])

    def finish(self, key, result, state='done'):
        self.conns.get().execute('UPDATE core_requests SET state=?,result=?,updated=? WHERE key=?',
                                (state, json.dumps(result), time.time(), key))

    def recover(self):
        # Neither queued approvals nor possibly handled requests are replayed across restart.
        # Legacy reconcile resolves actual approved intents / attempts; user gets explicit outcome.
        con = self.conns.get()
        con.execute("UPDATE core_requests SET state='interrupted',result=?,updated=? WHERE state IN ('queued','running')",
                    (json.dumps({'reason': 'core_restart_requires_new_plan'}), time.time()))

    def emit(self, payload):
        con = self.conns.get()
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        # A new wire event and its rollback reader fence must commit together.
        from ..trade import store
        with store.tx(con):
            if payload.get('dto_version') in (2, 3, 4):
                # Old Journal implementations already enforce this gate. A
                # previous artifact's runner must not boot an unaware core.
                version = str(payload['dto_version'])
                con.execute("UPDATE core_meta SET value=CAST(MAX(CAST(value AS INT),?) AS TEXT) WHERE key='schema_version'",
                            (int(version),))
                con.execute("INSERT INTO core_meta(key,value) VALUES('notification_dto_version',?) "
                            "ON CONFLICT(key) DO UPDATE SET value=CAST(MAX(CAST(value AS INT),CAST(excluded.value AS INT)) AS TEXT)",
                            (version,))
            cur = con.execute('INSERT INTO core_notifications(payload,created) VALUES(?,?)', (body, time.time()))
            return cur.lastrowid

    def notifications(self, after=0, limit=20):
        if not isinstance(after, int) or after < 0 or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise RpcError('invalid_cursor')
        out = []
        size = 0
        for r in self.conns.get().execute('SELECT id,payload FROM core_notifications WHERE id>? AND delivered IS NULL ORDER BY id LIMIT ?', (after, limit)):
            size += len(r['payload'].encode())
            if size > 800_000:
                break
            out.append({'id': r['id'], **json.loads(r['payload'])})
        return out

    def acknowledge(self, eid, result):
        if not isinstance(eid, int) or isinstance(eid, bool) or not isinstance(result, dict):
            raise RpcError('invalid_ack')
        # Telegram Message data is reduced to its ID; never store arbitrary API responses.
        mid = result.get('message_id')
        if mid is not None and (not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0):
            raise RpcError('invalid_message_id')
        with self.lock:
            con = self.conns.get()
            con.execute('BEGIN IMMEDIATE')
            try:
                row = con.execute('SELECT payload,delivered FROM core_notifications WHERE id=?', (eid,)).fetchone()
                if row is None:
                    raise RpcError('not_found')
                fresh = row['delivered'] is None
                if fresh:
                    payload = json.loads(row['payload'])
                    iid = payload.get('plan_id')
                    if iid and mid:
                        con.execute('UPDATE intents SET chat=?,msg_id=? WHERE id=?', (payload['chat_id'], mid, iid))
                    con.execute('UPDATE core_notifications SET delivered=?,result=? WHERE id=?',
                                (time.time(), json.dumps({'message_id': mid}), eid))
                con.commit()
            except BaseException:
                con.rollback()
                raise
        return fresh

    def offset_floor(self):
        from ..trade import store
        con = self.conns.get()
        last = con.execute('SELECT MAX(update_id) FROM tg_updates').fetchone()[0]
        return max(store.tg_offset(con), int(last) + 1 if last is not None else 0)


class Outbox:
    """Legacy Sender-compatible facade. Only writes DB; never talks to Telegram."""
    def __init__(self, journal):
        from collections import OrderedDict
        self.journal, self.callbacks = journal, OrderedDict()
        self.plan_guard = None
        self.lock = threading.RLock()

    def send(self, chat_id, text, *, html=True, reply_markup=None, silent=False, on_done=None):
        from ..operator_commands import parse_callback
        iid = None
        if reply_markup:
            for row in reply_markup.get('inline_keyboard', []):
                for button in row:
                    cb = parse_callback(button.get('callback_data'))
                    if cb:
                        iid = cb.intent_id
        if iid and self.plan_guard:
            self.plan_guard(iid)
        return self._emit(dict(kind='send', chat_id=chat_id, text=text, html=html,
                               reply_markup=reply_markup, silent=silent, plan_id=iid), on_done)

    def edit(self, chat_id, message_id, text, *, reply_markup=None, html=True, throttle=False, on_done=None):
        return self._emit(dict(kind='edit', chat_id=chat_id, message_id=message_id, text=text,
                               reply_markup=reply_markup, html=html, throttle=throttle), on_done)

    def _emit(self, payload, callback=None):
        # Bound each notification independently; don't silently truncate plans or signed intent descriptions.
        if len(json.dumps(payload).encode()) > 700_000:
            raise RpcError('notification_too_large')
        with self.lock:
            eid = self.journal.emit(payload)
            if callback:
                self.callbacks[eid] = callback
                # Durable plan/message binding is in DB; ephemeral progress hooks are bounded.
                while len(self.callbacks) > 1000:
                    self.callbacks.popitem(last=False)
        return True

    def alarm(self, chat_id, text, kind):
        return self.send(chat_id, text)

    def answer_callback_query(self, callback_id, text):
        return self._emit(dict(kind='answer', callback_id=callback_id, text=text))

    def approval_reply(self, callback_id, reason):
        from ..ipc.notifications import DTO_VERSION, APPROVAL_REASONS
        if reason not in APPROVAL_REASONS:
            raise RpcError('unsupported_approval_reason')
        return self._emit(dict(kind='approval_reply', dto_version=DTO_VERSION,
                               callback_id=callback_id, reason=reason))

    def notice(self, recipient, topic, **facts):
        from ..ipc.notifications import DTO_VERSION, validate_notice
        validate_notice(topic, facts)
        return self._emit(dict(kind='operator_notice', dto_version=DTO_VERSION,
                               chat_id=recipient, topic=topic, facts=facts))

    def status_report(self, chat_id, snapshot):
        from dataclasses import asdict
        from ..ipc.notifications import DTO_VERSION
        from ..ipc.reports import StatusView, encode
        if not isinstance(snapshot, StatusView):
            raise RpcError('invalid_status_snapshot')
        return self._emit(dict(kind='status_report', dto_version=DTO_VERSION,
                               chat_id=chat_id, snapshot=encode(asdict(snapshot))))

    def final_report(self, chat_id, snapshot):
        from dataclasses import asdict
        from ..ipc.notifications import EXECUTION_REPORT_VERSION
        from ..ipc.reports import FinalView, SolFinalView, encode
        if type(snapshot) not in (FinalView, SolFinalView):
            raise RpcError('invalid_final_snapshot')
        return self._emit(dict(kind='final_report', dto_version=EXECUTION_REPORT_VERSION,
                               chat_id=chat_id, snapshot=encode(asdict(snapshot)), solana=type(snapshot) is SolFinalView))

    def execution_notice(self, chat_id, topic, facts):
        from ..ipc.notifications import EXECUTION_NOTICE_VERSION, validate_execution_notice
        from ..ipc.reports import encode
        validate_execution_notice(topic, facts)
        return self._emit(dict(kind='execution_notice', dto_version=EXECUTION_NOTICE_VERSION,
                               chat_id=chat_id, topic=topic, facts=encode(facts)))

    def positions_report(self, chat_id, snapshots, *, at, matched, mismatch, sim):
        from dataclasses import asdict
        from ..ipc.notifications import DTO_VERSION, GENERIC_POSITION_VERSION
        from ..ipc.reports import PositionView, encode
        if not all(isinstance(s, PositionView) for s in snapshots):
            raise RpcError('invalid_positions_snapshot')
        generic = any(s.generic_legs is not None for s in snapshots)
        rows = [asdict(s) for s in snapshots]
        if not generic:
            for row in rows:
                row.pop('generic_legs', None)
        return self._emit(dict(kind='positions_report', dto_version=GENERIC_POSITION_VERSION if generic else DTO_VERSION,
                               chat_id=chat_id, snapshots=encode(rows), at=at,
                               matched=matched, mismatch=mismatch, sim=sim))

    def restart_report(self, chat_id, snapshot, *, solana=False, check_only=False):
        from dataclasses import asdict
        from ..ipc.notifications import DTO_VERSION
        from ..ipc.reports import RestartView, encode
        if not isinstance(snapshot, RestartView) or type(solana) is not bool or type(check_only) is not bool:
            raise RpcError('invalid_restart_snapshot')
        return self._emit(dict(kind='restart_report', dto_version=DTO_VERSION, chat_id=chat_id,
                               snapshot=encode(asdict(snapshot)), solana=solana, check_only=check_only))

    def plan_closed(self, chat_id, message_id, summary, action, at):
        from ..ipc.notifications import DTO_VERSION
        return self._emit(dict(kind='plan_closed', dto_version=DTO_VERSION, chat_id=chat_id,
                               message_id=message_id, summary=summary, action=action, at=at))

    def plan_proposed(self, chat_id, intent_id, nonce, summary, legacy_body, on_done=None):
        from ..ipc.notifications import DTO_VERSION
        if self.plan_guard is None:
            raise RpcError('plan_guard_missing')
        self.plan_guard(intent_id)
        return self._emit(dict(kind='plan_proposed', dto_version=DTO_VERSION, chat_id=chat_id,
                               plan_id=intent_id, nonce=nonce, summary=summary, legacy_body=legacy_body), on_done)

    def ack(self, eid, result):
        with self.lock:
            fresh = self.journal.acknowledge(eid, result)
            cb = self.callbacks.pop(eid, None)
        if fresh and cb:
            cb(result)
        return {'acked': True}

    def close(self, *_):
        return 0
