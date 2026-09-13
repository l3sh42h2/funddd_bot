"""Telegram + HTTP process. It has no trading DB connection or signing factory."""
import json
import logging
import os
import signal
import threading
import time
from pathlib import Path
from ..ipc.paths import interface_state
from ..ipc.protocol import Client, RpcError
from ..market_snapshot import atomic_json
from ..build_info import BUILD_ID

log = logging.getLogger(__name__)


class State:
    def __init__(self, path=None):
        self.path = Path(path or interface_state())
        self.lock = threading.RLock()
        try:
            self.data = json.loads(self.path.read_text())
            if not isinstance(self.data, dict) or not isinstance(self.data.get('offset'), int):
                raise ValueError('invalid interface state')
        except FileNotFoundError:
            self.data = dict(offset=0, acks={})
        # Corrupt state must not silently reset offsets.

    def save(self):
        atomic_json(self.path, self.data, 0o600)

    def advance(self, offset):
        with self.lock:
            self.data['offset'] = max(self.data['offset'], offset)
            self.save()

    def ack_pending(self, eid, result):
        with self.lock:
            self.data.setdefault('acks', {})[str(eid)] = result
            self.save()

    def ack_done(self, eid):
        with self.lock:
            self.data['acks'].pop(str(eid), None)
            self.save()

    def pending(self):
        with self.lock:
            return dict(self.data.get('acks', {}))


class Interface:
    def __init__(self, api, sender, client=None, state=None, *, check_startup=False):
        self.api, self.sender = api, sender
        self.client, self.state = client or Client(), state or State()
        self.stop = threading.Event()
        self.started = time.time()
        self.last_renew = self.started
        self.check_startup = check_startup
        self.last_poll = None
        self.last_core = None
        self.inflight = set()
        self.inflight_lock = threading.RLock()

    def health(self):
        now = time.time()
        return dict(schema_version=1,build_id=BUILD_ID,pid=os.getpid(),updated_at=now,
                    last_poll_at=self.last_poll,last_core_at=self.last_core,
                    ready=self.last_poll is not None and 0 <= now-self.last_poll <= 105)

    def poll_once(self):
        if self.check_startup:
            self.api.get_me()
            if (self.api.get_webhook_info() or {}).get('url'):
                raise RpcError('telegram_webhook_configured')
            self.check_startup = False
        # On first split, import old offset through core (never by opening trade.db).
        floor = self.client.call('get_offset_floor')['offset']
        self.last_core = time.time()
        self.state.advance(floor)
        updates = self.api.get_updates(self.state.data['offset'], timeout=25)
        self.last_poll = time.time()
        for u in sorted((u for u in updates if isinstance(u, dict)), key=lambda u:u.get('update_id', -1)):
            uid = u.get('update_id')
            if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
                continue
            result = self.client.call('submit_user_command', {'update':u}, key=f'tg:{uid}')
            if result.get('state') not in ('queued','running','done','failed','interrupted'):
                raise RpcError('invalid_acceptance')
            self.state.advance(uid+1)  # Only after core has durably recorded this exact command.
        return len(updates)

    def poll(self):
        while not self.stop.is_set():
            try:
                self.poll_once()
            except Exception as e:
                now = time.time()
                if now - max(self.last_poll or self.started, self.last_renew) >= 105:
                    self.api.renew()
                    self.last_renew = now
                log.warning('interface poll paused: %s', type(e).__name__)
                self.stop.wait(3)

    def deliver_once(self):
        # Save successful delivery before ACK, so a disconnected core doesn't cause resend on UI restart.
        for eid, result in self.state.pending().items():
            self.client.call('ack_notification', {'event_id':int(eid), 'result':result})
            self.state.ack_done(eid)
        events = self.client.call('read_notifications', {'limit':20})['events']
        for ev in events:
            eid = ev['id']
            with self.inflight_lock:
                if eid in self.inflight:
                    continue
                self.inflight.add(eid)
            def done(res, eid=eid):
                try:
                    if res is not None:
                        self.state.ack_pending(eid, {'message_id':res.get('message_id')})
                finally:
                    with self.inflight_lock:
                        self.inflight.discard(eid)
            kind = ev['kind']
            if kind == 'answer':
                try:
                    self.api.answer_callback_query(ev['callback_id'], ev.get('text'))
                except Exception as e:
                    from ..tg.api import TgBadRequest
                    if isinstance(e, TgBadRequest):
                        # Expired callback acknowledgement is terminal UI delivery, never a trading result.
                        done({})
                    else:
                        done(None)
                    continue
                done({})
            elif kind == 'send':
                ok = self.sender.send(ev['chat_id'], ev['text'], html=ev.get('html', True),
                                      reply_markup=ev.get('reply_markup'), silent=ev.get('silent',False), on_done=done)
                if not ok:
                    done(None)
            elif kind == 'edit':
                ok = self.sender.edit(ev['chat_id'], ev['message_id'], ev['text'], html=ev.get('html',True),
                                      reply_markup=ev.get('reply_markup'), throttle=ev.get('throttle',False), on_done=done)
                if not ok:
                    done(None)
            else:
                done(None)
                raise RpcError('unsupported_notification')

    def deliver(self):
        while not self.stop.is_set():
            try:
                self.deliver_once()
            except Exception as e:
                log.warning('interface notification delivery paused: %s', type(e).__name__)
            self.stop.wait(1)


def run_interface(port=8792, host='127.0.0.1', environ=None):
    env = os.environ if environ is None else environ
    token = env.get('TG_BOT_TOKEN')
    if not token:
        log.error('TG_BOT_TOKEN missing for interface')
        return 78
    from ..tg.api import TgApi
    from ..tg.sender import Sender
    from ..ipc.lock import ExecutionLock
    from ..serve import Handler
    from http.server import ThreadingHTTPServer
    # Exactly one Telegram poller. Separate lock from execution so interface can restart independently.
    with ExecutionLock(interface_state().with_suffix('.lock')):
        api = TgApi(token)
        sender = Sender(TgApi(token)).start()
        ui = Interface(api, sender, check_startup=True)
        server = ThreadingHTTPServer((host, port), Handler)
        server.daemon_threads = True
        for target in (ui.poll, ui.deliver, server.serve_forever):
            threading.Thread(target=target, daemon=True).start()
        signal.signal(signal.SIGTERM, lambda *_:ui.stop.set())
        signal.signal(signal.SIGINT, lambda *_:ui.stop.set())
        while not ui.stop.wait(1):
            atomic_json(ui.state.path.with_name("interface_health.json"), ui.health(), 0o600)
        server.shutdown()
        server.server_close()
        sender.close(10)
    return 0
