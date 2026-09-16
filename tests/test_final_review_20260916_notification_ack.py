"""Regression coverage for FINAL-01 (P1) and FINAL-02 (P2), two independent notification-delivery
defects an external review (ChatGPT, GPT-6 Astra Pro, 16.09.2026, candidate 8eca9d0, REVIEW.md
sections 2-3) found and reproduced against the exact bodies of interface/runtime.py and tg/sender.py.
Both are fixed in this same change; see PATCHNOTES/final-review-01-02-notification-ack-fix-20260916.md.

FINAL-01 (interface/runtime.py: Interface.deliver_once(), kind == 'send'): the on_sent() callback
closure referenced the loop's `done` name freely instead of capturing it, so once Sender called
on_sent() back later (always async — see tg/sender.py's own docstring), Python's late binding made it
see whichever `done` the *last* iteration of the for-loop had left behind — ACKing a completely
different event's id with this event's Telegram result. A batch of two 'send' notifications was
enough to misattribute an ACK; a failed second send could even inherit the first send's message_id.

FINAL-02 (tg/sender.py: Sender.edit()/_flush_edits()/_deliver()): throttled progress edits coalesce
by key (chat_id, message_id) — Sender._edits[key] = job simply overwrites whatever job was already
waiting there, discarding its on_done, and a plain (non-throttled) edit evicting a waiting throttled
one did the same via _edits.pop(key, None). Only the *last* job's on_done ever fired. The notification
belonging to every job that got silently overwritten stayed durably unacknowledged and stuck in
Interface.inflight forever, even though the text it was superseded by really was delivered.

Both fixes are exercised here against the genuine repository modules — funding_bot.interface.runtime,
funding_bot.tg.sender, funding_bot.core.journal — not the excerpt/importlib harness the reviewer had to
use without a full checkout. Only the true system boundary is faked: the Telegram HTTP transport
(FakeSession under the real TgApi, and Clock for deterministic pacing/timeouts) exactly as
tests/test_tg.py already does for every other Sender test in this suite, and a real trade.db-shaped
SQLite database (funding_bot.trade.engine.Conns) for Journal, exactly as
tests/test_m4_progress_edit_in_place.py already does. Sender is always driven synchronously via
.drain()/._flush_edits(force=True) — the review's diagnostic note is that the existing
FakeSender.edit() in test_m4_progress_edit_in_place.py fires on_done immediately and therefore never
exercises the _edits[key] replacement path at all; every test below uses the real queueing/coalescing
so a callback is only ever delivered the way production actually delivers it: asynchronously, after
the caller (Interface.deliver_once()'s for-loop) has already moved on.

Three groups, cheapest/most isolated first:
  1. FINAL-01 — Interface.deliver_once() callback binding, real Interface + real Journal (SQLite) +
     real Sender+FakeSession, driven with sender.drain() called strictly after deliver_once() returns.
  2. FINAL-02 — Sender throttle-coalescing waiter semantics in isolation, real Sender+FakeSession only.
  3. End-to-end — Interface + Journal + Sender together, reproducing the review's own FINAL-02 scenario
     (message_id already known, two throttled edits land in one deliver_once() before the next flush).
"""
import json

from funding_bot.core.journal import Journal
from funding_bot.interface.runtime import Interface, State
from funding_bot.tg.sender import Sender
from funding_bot.trade.engine import Conns

from test_tg import Clock, FakeSession, make_api, ok, err


# --- shared real-Journal helpers (genuine Journal.emit/notifications/acknowledge; no excerpt/stub) ------------

def make_journal(tmp_path, name='trade.db'):
    return Journal(Conns(tmp_path / name))


def notification_row(journal, eid):
    return journal.conns.get().execute(
        'SELECT delivered, result FROM core_notifications WHERE id=?', (eid,)).fetchone()


def intent_row(journal, iid):
    return journal.conns.get().execute(
        'SELECT chat, msg_id FROM intents WHERE id=?', (iid,)).fetchone()


class RecordingClient:
    """Adapts a real Journal to the Client.call(method, params) shape Interface expects — the same
    shape core.service.CoreService.call dispatches 'read_notifications'/'ack_notification' to in
    production (see RpcClient in test_m4_progress_edit_in_place.py) — while also recording each fresh
    ack_notification result in a plain dict, purely so tests can assert on it directly instead of
    re-querying SQLite every time. The persistence and ACK logic themselves are 100% the real
    Journal.notifications()/acknowledge()."""

    def __init__(self, journal):
        self.journal = journal
        self.acknowledged = {}

    def call(self, method, params=None):
        params = params or {}
        if method == 'read_notifications':
            return {'events': self.journal.notifications(params.get('after', 0), params.get('limit', 20))}
        if method == 'ack_notification':
            eid, result = params['event_id'], params.get('result') or {}
            if self.journal.acknowledge(eid, result):
                self.acknowledged[eid] = result
            return {'acked': True}
        raise AssertionError(f'unexpected RPC in test: {method}')


def _real_sender(*script, **kw):
    """A genuine funding_bot.tg.sender.Sender wired to a fake Telegram HTTP transport — FakeSession
    under the real TgApi, exactly tests/test_tg.py's own boundary — with a controllable Clock so pacing
    and the edit_min_s throttle window are deterministic. Driven synchronously via .drain()/
    ._flush_edits(); never a hand-rolled Sender stand-in."""
    s = FakeSession(list(script))
    clk = Clock()
    return Sender(make_api(s), clock=clk, sleep=clk.sleep, **kw), s, clk


class SenderEditRaises:
    """Wraps a real Sender: send() delegates untouched; edit() always raises. Used only for the
    FINAL-01 remediation #2 test below, to prove a failing best-effort pending-progress follow-up edit
    never costs the primary send its own durable ACK."""

    def __init__(self, inner):
        self._inner = inner

    def send(self, *a, **kw):
        return self._inner.send(*a, **kw)

    def edit(self, *a, **kw):
        raise RuntimeError('boom: pending progress edit blew up')

    def drain(self, **kw):
        return self._inner.drain(**kw)


# === 1. FINAL-01: Interface.deliver_once() callback binding (real Interface + real Journal + real Sender) =====

def test_two_batched_sends_each_ack_their_own_event_not_each_others(tmp_path):
    journal = make_journal(tmp_path)
    e1 = journal.emit(dict(kind='send', chat_id=1, text='notification 1'))
    e2 = journal.emit(dict(kind='send', chat_id=1, text='notification 2'))
    client = RecordingClient(journal)
    snd, s, clk = _real_sender(ok({'message_id': 101}), ok({'message_id': 102}))
    ui = Interface(api=None, sender=snd, client=client, state=State(tmp_path / 'ui.json'))
    ui.deliver_once()                       # both 'send' jobs queued within ONE pass of the for-loop;
    assert ui.inflight == {e1, e2}          # neither delivered yet — genuinely batched, not sequential.
    snd.drain()                             # Telegram 'answers' both now, strictly after deliver_once()
                                             # returned — exactly the ordering FINAL-01 got wrong.
    ui.deliver_once()                       # flush the two now-pending acks into the real Journal
    assert client.acknowledged == {e1: {'message_id': 101}, e2: {'message_id': 102}}, client.acknowledged
    assert ui.inflight == set()


def test_second_send_fails_first_succeeds_second_is_retried_not_acked_with_firsts_message_id(tmp_path):
    journal = make_journal(tmp_path)
    e1 = journal.emit(dict(kind='send', chat_id=1, text='notification 1'))
    e2 = journal.emit(dict(kind='send', chat_id=1, text='notification 2 (will fail)'))
    client = RecordingClient(journal)
    snd, s, clk = _real_sender(ok({'message_id': 101}),
                                err(403, 'Forbidden: bot was blocked by the user'),
                                ok({'message_id': 202}))
    ui = Interface(api=None, sender=snd, client=client, state=State(tmp_path / 'ui.json'))
    ui.deliver_once()                       # both dispatched together
    snd.drain()                             # e1 succeeds, e2 fails — done()'s finally clears both from inflight
    ui.deliver_once()                       # flushes e1's ack; re-reads & retries the still-undelivered e2
    assert client.acknowledged == {e1: {'message_id': 101}}, client.acknowledged   # never inherits 101
    assert ui.inflight == {e2}              # e2's retry is in flight again — never silently dropped
    assert [e['id'] for e in journal.notifications()] == [e2]      # still undelivered in the Journal
    snd.drain()                             # resolves the retry
    ui.deliver_once()                       # flushes e2's ack
    assert client.acknowledged == {e1: {'message_id': 101}, e2: {'message_id': 202}}
    assert ui.inflight == set()


def test_first_send_fails_second_succeeds_first_is_retried_not_acked_with_seconds_message_id(tmp_path):
    journal = make_journal(tmp_path)
    e1 = journal.emit(dict(kind='send', chat_id=1, text='notification 1 (will fail)'))
    e2 = journal.emit(dict(kind='send', chat_id=1, text='notification 2'))
    client = RecordingClient(journal)
    snd, s, clk = _real_sender(err(403, 'Forbidden: bot was blocked by the user'),
                                ok({'message_id': 102}),
                                ok({'message_id': 201}))
    ui = Interface(api=None, sender=snd, client=client, state=State(tmp_path / 'ui.json'))
    ui.deliver_once()
    snd.drain()
    ui.deliver_once()                       # flushes e2's ack; re-reads & retries the still-undelivered e1
    assert client.acknowledged == {e2: {'message_id': 102}}, client.acknowledged   # never inherits 102
    assert ui.inflight == {e1}              # e1's retry is in flight again — never silently dropped
    assert [e['id'] for e in journal.notifications()] == [e1]
    snd.drain()                             # resolves the retry
    ui.deliver_once()                       # flushes e1's ack
    assert client.acknowledged == {e1: {'message_id': 201}, e2: {'message_id': 102}}
    assert ui.inflight == set()


def test_batch_of_20_sends_each_gets_its_own_message_id_and_inflight_drains(tmp_path):
    journal = make_journal(tmp_path)
    eids = [journal.emit(dict(kind='send', chat_id=1, text=f'notification {i}')) for i in range(20)]
    client = RecordingClient(journal)
    snd, s, clk = _real_sender(*(ok({'message_id': 1000 + i}) for i in range(20)))
    ui = Interface(api=None, sender=snd, client=client, state=State(tmp_path / 'ui.json'))
    ui.deliver_once()
    assert ui.inflight == set(eids)
    snd.drain()
    ui.deliver_once()
    assert client.acknowledged == {eid: {'message_id': 1000 + i} for i, eid in enumerate(eids)}
    assert ui.inflight == set()
    assert journal.notifications() == []               # no stale undelivered rows left behind


def test_plan_notification_next_to_ordinary_send_does_not_inherit_wrong_message_id(tmp_path):
    """core.journal.Journal.acknowledge() binds a plan's message_id into intents.msg_id only for the
    event whose payload actually carries that plan_id (FINAL-01's original, most consequential shape:
    a failed plan send must never be marked delivered/bound just because an unrelated ordinary
    notification next to it in the same batch happened to succeed)."""
    journal = make_journal(tmp_path)
    journal.conns.get().execute('INSERT INTO intents(id) VALUES (?)', ('I-PLAN',))
    e_plain = journal.emit(dict(kind='send', chat_id=1, text='ordinary notice'))
    e_plan = journal.emit(dict(kind='send', chat_id=1, text='plan that will fail to send', plan_id='I-PLAN'))
    client = RecordingClient(journal)
    snd, s, clk = _real_sender(ok({'message_id': 101}), err(403, 'Forbidden: bot was blocked by the user'))
    ui = Interface(api=None, sender=snd, client=client, state=State(tmp_path / 'ui.json'))
    ui.deliver_once()
    snd.drain()
    ui.deliver_once()                       # flushes plain's ack; re-reads & retries the still-undelivered plan
    plain_row, plan_row = notification_row(journal, e_plain), notification_row(journal, e_plan)
    plan_intent = intent_row(journal, 'I-PLAN')
    assert plain_row['delivered'] is not None and json.loads(plain_row['result'])['message_id'] == 101
    assert plan_row['delivered'] is None, dict(plan_row)                 # never acked
    assert plan_intent['msg_id'] is None, dict(plan_intent)              # never bound to a message_id
    assert ui.inflight == {e_plan}          # plan retried, never silently dropped or misattributed


def test_failing_pending_progress_edit_does_not_cost_primary_send_its_ack(tmp_path):
    """FINAL-01 remediation #2: the best-effort follow-up throttled edit for a coalesced later progress
    stage (self.sender.edit(...) inside on_sent) is wrapped in try/except specifically so that if it
    raises, the primary event's own done(res) — and therefore its durable ACK — still fires."""
    journal = make_journal(tmp_path)
    eid = journal.emit(dict(kind='send', chat_id=1, text='stage 1/2', progress_intent_id='I1'))
    client = RecordingClient(journal)
    inner, s, clk = _real_sender(ok({'message_id': 555}))
    snd = SenderEditRaises(inner)
    ui = Interface(api=None, sender=snd, client=client, state=State(tmp_path / 'ui.json'))
    # A second progress notice for the same intent_id queued while the first send is still in flight —
    # exactly how interface.presenter/ProgressTracker route a real burst (see
    # test_m4_progress_edit_in_place.py) — so resolved() below hands on_sent a pending (chat_id, text)
    # to flush as a throttled edit, and that edit is what raises here.
    ui.progress.route('I1', 1, 'stage 1/2')
    ui.progress.route('I1', 1, 'stage 2/2 (queued)')
    ui.deliver_once()
    snd.drain()                             # fires on_sent; its pending-progress sender.edit() raises
    ui.deliver_once()
    assert client.acknowledged == {eid: {'message_id': 555}}, client.acknowledged
    assert ui.inflight == set()


# === 2. FINAL-02: Sender throttle-coalescing waiter semantics in isolation (real Sender + FakeSession) ========

def test_throttle_coalescing_acks_both_superseded_and_surviving_edit_with_same_result(tmp_path):
    snd, s, clk = _real_sender(ok({'message_id': 77}))
    got1, got2 = [], []
    assert snd.edit(1, 77, 'progress 1', throttle=True, on_done=got1.append)
    assert snd.edit(1, 77, 'progress 2', throttle=True, on_done=got2.append)
    snd._flush_edits(force=True)
    assert [p['json']['text'] for p in s.posts] == ['progress 2']     # coalescing itself still works
    assert got1 == [{'message_id': 77}], got1                        # superseded job still fires
    assert got2 == [{'message_id': 77}], got2                        # surviving job fires as normal
    assert snd._edits == {} and snd._edit_waiters == {}               # nothing left dangling


def test_throttle_coalescing_of_three_edits_acks_all_three_waiters(tmp_path):
    snd, s, clk = _real_sender(ok({'message_id': 88}))
    results = [[], [], []]
    for i, bucket in enumerate(results):
        snd.edit(1, 88, f'progress {i + 1}', throttle=True, on_done=bucket.append)
    snd._flush_edits(force=True)
    assert [p['json']['text'] for p in s.posts] == ['progress 3']
    assert results == [[{'message_id': 88}], [{'message_id': 88}], [{'message_id': 88}]]
    assert snd._edit_waiters == {}


def test_unthrottled_edits_to_same_key_still_deliver_and_ack_independently(tmp_path):
    """Control (task's explicit acceptance bullet): plain, non-coalesced edits are untouched by the
    waiters mechanism — each is still delivered and acknowledged on its own."""
    snd, s, clk = _real_sender(ok({'message_id': 5}), ok({'message_id': 5}))
    got1, got2 = [], []
    snd.edit(1, 5, 'e1', on_done=got1.append)
    snd.edit(1, 5, 'e2', on_done=got2.append)
    snd.drain()
    assert [p['json']['text'] for p in s.posts] == ['e1', 'e2']      # both delivered, never coalesced
    assert got1 == [{'message_id': 5}] and got2 == [{'message_id': 5}]
    assert snd._edit_waiters == {}


def test_normal_edit_evicts_waiting_throttled_edit_but_still_acks_both(tmp_path):
    """The other supersession path named in Sender.edit()'s own docstring: 'обычная правка (закрыть
    план) вытесняет ждущий прогресс' — a plan-closed edit displacing a still-waiting throttled progress
    edit for the same message. Before the fix this silently dropped the progress edit's on_done too."""
    snd, s, clk = _real_sender(ok({'message_id': 5}))
    got_progress, got_closed = [], []
    snd.edit(1, 5, 'progress', throttle=True, on_done=got_progress.append)
    snd.edit(1, 5, 'закрыто', reply_markup=None, on_done=got_closed.append)
    snd.drain()
    assert [p['json']['text'] for p in s.posts] == ['закрыто']       # progress text never sent on its own
    assert got_progress == [{'message_id': 5}], got_progress
    assert got_closed == [{'message_id': 5}], got_closed
    assert snd._edits == {} and snd._edit_waiters == {}


def test_throttle_coalescing_on_delivery_failure_acks_all_waiters_with_none_not_silently_delivered(tmp_path):
    """Consistent retry semantics on the failure path (review's explicit requirement): when the
    surviving coalesced edit ultimately fails, every waiter — superseded and surviving alike — sees
    None, exactly like any other undelivered notification's on_done(None) (Interface.deliver_once()'s
    own `done`), so each underlying event is retried independently rather than one silently treated as
    delivered."""
    snd, s, clk = _real_sender(err(403, 'Forbidden: bot was blocked by the user'))
    got1, got2 = [], []
    snd.edit(1, 9, 'progress 1', throttle=True, on_done=got1.append)
    snd.edit(1, 9, 'progress 2', throttle=True, on_done=got2.append)
    snd._flush_edits(force=True)
    assert len(s.posts) == 1                        # one delivery attempt for the coalesced key
    assert got1 == [None] and got2 == [None]
    assert snd.dropped == 1 and snd.fails == 1
    assert snd._edit_waiters == {}


# === 3. End-to-end: Interface + real Journal (SQLite) + real Sender together ===================================

def test_end_to_end_progress_coalescing_through_real_interface_and_sender_leaves_no_orphan_ack(tmp_path):
    """The review's own FINAL-02 repro (message_id already known; two progress notices for the same
    intent land in one deliver_once() before Sender's next flush, both becoming throttle=True edits to
    the same key), reproduced with the genuine Interface + genuine Journal/SQLite + genuine Sender."""
    journal = make_journal(tmp_path)
    e1 = journal.emit(dict(kind='edit', chat_id=1, message_id=77, text='progress 1', throttle=True))
    e2 = journal.emit(dict(kind='edit', chat_id=1, message_id=77, text='progress 2', throttle=True))
    client = RecordingClient(journal)
    snd, s, clk = _real_sender(ok({'message_id': 77}))
    ui = Interface(api=None, sender=snd, client=client, state=State(tmp_path / 'ui.json'))
    ui.deliver_once()                       # both queued as throttle=True edits to the same key
    assert ui.inflight == {e1, e2}
    snd._flush_edits(force=True)            # only the coalesced (latest) text actually reaches 'Telegram'
    assert [p['json']['text'] for p in s.posts] == ['progress 2']
    ui.deliver_once()                       # flush both now-pending acks into the real Journal
    assert client.acknowledged == {e1: {'message_id': 77}, e2: {'message_id': 77}}, client.acknowledged
    assert ui.inflight == set()
    assert journal.notifications() == []
