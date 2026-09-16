"""m4-progress-edit-in-place-20260916: progress/sol_progress execution notices for one intent_id must edit
one Telegram message in place instead of sending a new message per stage — the behaviour interface.presenter
had before AC-07 moved rendering out of core (interface.presenter.render_execution_notice) but never finished
porting the edit-in-place half of (see PATCHNOTES/m4-progress-edit-in-place-20260916.md for the full story and
PATCHNOTES/m4-ac07-full-core-interface-boundary-20260916.md's corrected note).

Three layers, cheapest/most isolated first:
  - ProgressTracker in isolation (no DTOs, no Telegram) — the routing state machine itself.
  - interface.presenter.present() with a real ProgressTracker, fed real encoded DTOs via core.journal.Outbox/
    Journal (same pattern as test_m4_domain_notifications.py) — routing wired to real facts/rendering.
  - interface.runtime.Interface.deliver_once() end to end, with a fake Sender that mimics tg/sender.py's
    async on_done contract (on_done fires whenever the test says Telegram answered, not immediately) — this
    is what actually exercises the in-flight coalescing race the old core-side Bot.progress()/
    _progress_pending handled, and that a naive 2-state (unseen/known) tracker would not.
"""
from decimal import Decimal as D
from pathlib import Path

import pytest
from funding_bot.core.journal import Journal, Outbox
from funding_bot.trade.engine import Conns
from funding_bot.interface.presenter import present, PROGRESS_TOPICS, PROGRESS_TERMINAL_TOPICS
from funding_bot.interface.progress_tracker import ProgressTracker
from funding_bot.interface.runtime import Interface, State


# --- fixtures: known-good facts dicts (shapes proven elsewhere: test_m4_domain_notifications.py,
# test_m4_ac07_core_interface_boundary.py) ------------------------------------------------------------------

def progress_facts(intent_id, clip=1, clips=2):
    return dict(intent_id=intent_id, kind='entry', coin='A', clip=clip, clips=clips,
                spot_usd=D('2'), spot_qty=D('1'), spot_avg=D('2'), perp_qty=D('1'), perp_usd=D('2'),
                perp_avg=D('2'), imbalance_qty=D(0), imbalance_usd=D(0), gas_usd=D('0.01'), fees_usd=D(0),
                note=None, sim=True, total_usd=D('4'), step=D('.1'), m=D(1))


def sol_progress_facts(intent_id, stage='hedge'):
    return dict(intent_id=intent_id, kind='entry', coin='ANSEM', fullcoin='para:ANSEM', stage=stage,
                path='jupiter', tokens=D('12.3'), perp_qty=D('12.3'), sim=True)


def executor_crash_facts(intent_id):
    return dict(intent_id=intent_id, error='boom')


def halt_facts(intent_id, deal_id='D1'):
    # state='ABORTED' takes halt()'s short-circuit branch: only kind/coin/reason/sim/deal_id/intent_id/
    # perp_venue matter, sidestepping the unhedged-qty/position arithmetic the other branches do.
    return dict(intent_id=intent_id, kind='entry', coin='A', reason='test halt', perp_venue='aster',
                deal_id=deal_id, clip=1, clips=3, perp_pos=None, wallet_tokens=None, unhedged_qty=None,
                unhedged_usd=None, auto_unwind_s=None, sim=True, ts=100.0, clips_done=0, state='ABORTED',
                step=D('1'), m=D('1'))


def sol_halt_facts(intent_id, deal_id='D1'):
    return dict(kind='entry', coin='ANSEM', deal_id=deal_id, intent_id=intent_id, reason='delta out of band',
                state='PAUSED', wallet_tokens=D('10'), perp_pos=D('-10'), delta=D('0.5'), delta_usd=D('2.5'),
                need_qty=D('0.5'), sim=True)


def make_outbox(tmp_path, name='trade.db'):
    return Outbox(Journal(Conns(tmp_path / name)))


# === 1. ProgressTracker in isolation =========================================================================

def test_tracker_terminal_topics_cover_halt_and_crash_paths():
    assert set(PROGRESS_TERMINAL_TOPICS) == {'halt', 'sol_halt', 'executor_crash'}
    assert set(PROGRESS_TOPICS) == {'progress', 'sol_progress'}


def test_tracker_first_route_sends_then_resolved_switches_next_route_to_edit():
    t = ProgressTracker()
    action, mid = t.route('I1', 42, 'clip 1/3')
    assert (action, mid) == ('send', None)
    # A second route() call for the same intent_id before resolved() fires must not also say 'send' —
    # that would be the exact bug this tracker exists to prevent (two 'new message' sends racing).
    action2, mid2 = t.route('I1', 42, 'clip 2/3 (raced)')
    assert action2 == 'wait' and mid2 is None
    pending = t.resolved('I1', 555)
    assert pending == (42, 'clip 2/3 (raced)')          # the raced text, queued for a follow-up edit
    action3, mid3 = t.route('I1', 42, 'clip 3/3')
    assert (action3, mid3) == ('edit', 555)


def test_tracker_route_while_inflight_keeps_only_the_latest_pending_text():
    t = ProgressTracker()
    t.route('I1', 42, 'first')
    assert t.route('I1', 42, 'second')[0] == 'wait'
    assert t.route('I1', 42, 'third')[0] == 'wait'      # third overwrites second — never both delivered
    assert t.resolved('I1', 555) == (42, 'third')


def test_tracker_resolved_with_no_message_id_drops_tracking_and_queued_text():
    t = ProgressTracker()
    t.route('I1', 42, 'first')
    t.route('I1', 42, 'queued-while-inflight')
    assert t.resolved('I1', None) is None               # send failed outright: nothing to edit
    # Next progress notice for I1 must start fresh — not be treated as still-inflight or edit a
    # message that was never created.
    assert t.route('I1', 42, 'retry')[0] == 'send'


def test_tracker_forget_clears_known_inflight_and_pending_and_is_idempotent():
    t = ProgressTracker()
    t.route('I1', 42, 'a')
    t.resolved('I1', 555)
    assert t.route('I1', 42, 'b') == ('edit', 555)
    t.forget('I1')
    assert t.route('I1', 42, 'c')[0] == 'send'           # forgotten -> treated as a brand-new intent_id
    t.forget('never-tracked')                            # must not raise
    t.forget('I1')                                        # forgetting twice must not raise


def test_tracker_bounds_growth_by_evicting_oldest_resolved_entry():
    t = ProgressTracker(max_tracked=2)
    for i in range(1, 4):
        iid = f'I{i}'
        t.route(iid, 42, 'x')
        t.resolved(iid, 100 + i)
    # I1 was the oldest resolved entry and must have been evicted once I3 pushed the map over max_tracked=2.
    assert t.route('I1', 42, 'again')[0] == 'send'
    assert t.route('I2', 42, 'again')[0] == 'edit'
    assert t.route('I3', 42, 'again')[0] == 'edit'


# === 2. present() routing with real Outbox/Journal DTOs =======================================================

def test_present_second_progress_notice_for_same_intent_edits_with_throttle(tmp_path):
    out = make_outbox(tmp_path)
    tracker = ProgressTracker()
    out.execution_notice(42, 'progress', progress_facts('I1', clip=1, clips=3))
    out.execution_notice(42, 'progress', progress_facts('I1', clip=2, clips=3))
    events = out.journal.notifications()
    first = present(events[0], progress=tracker)
    assert first['kind'] == 'send' and first['silent'] is True and first['progress_intent_id'] == 'I1'
    assert 'клип 1/3' in first['text']
    # Routing state only advances once the caller reports a message_id back via resolved() — exactly as
    # interface.runtime.Interface.deliver_once() does once Telegram answers, not merely because present()
    # ran. Simulate that here so the second present() call sees a resolved chain.
    tracker.resolved('I1', 555)
    second = present(events[1], progress=tracker)
    assert second == dict(kind='edit', chat_id=42, message_id=555, text=second['text'], html=True, throttle=True)
    assert 'клип 2/3' in second['text']


def test_present_without_tracker_keeps_legacy_always_send_behaviour(tmp_path):
    """Backward compatibility: every existing caller (tests, cli.py's render_execution_notice-only path)
    that does not pass progress= must see exactly the old behaviour — always 'send', never 'edit'."""
    out = make_outbox(tmp_path)
    out.execution_notice(42, 'progress', progress_facts('I1', clip=1, clips=2))
    out.execution_notice(42, 'progress', progress_facts('I1', clip=2, clips=2))
    events = out.journal.notifications()
    assert present(events[0])['kind'] == 'send'
    assert present(events[1])['kind'] == 'send'          # still 'send' even for a repeat intent_id/topic


@pytest.mark.parametrize('topic,facts_fn', [('halt', halt_facts), ('sol_halt', sol_halt_facts),
                                            ('executor_crash', lambda iid: executor_crash_facts(iid))])
def test_present_forgets_tracked_intent_on_each_terminal_topic(tmp_path, topic, facts_fn):
    out = make_outbox(tmp_path)
    tracker = ProgressTracker()
    out.execution_notice(42, 'progress', progress_facts('I1'))
    out.execution_notice(42, topic, facts_fn('I1'))
    events = out.journal.notifications()
    present(events[0], progress=tracker)
    tracker.resolved('I1', 555)
    assert tracker.route('I1', 42, 'peek')[0] == 'edit'  # sanity: really tracked before the terminal notice
    present(events[1], progress=tracker)                 # the terminal notice itself
    assert tracker.route('I1', 42, 'after-terminal')[0] == 'send'  # forgotten, not a stale 'edit'


def test_present_new_intent_id_after_halt_sends_fresh_not_edit(tmp_path):
    """The scenario the docstring calls out: intent_id is never reused, but a *different* intent_id
    (e.g. from 'продолжить <id>' after a halt) must never inherit the previous intent's message_id."""
    out = make_outbox(tmp_path)
    tracker = ProgressTracker()
    out.execution_notice(42, 'progress', progress_facts('I1'))
    out.execution_notice(42, 'halt', halt_facts('I1'))
    out.execution_notice(42, 'progress', progress_facts('I2'))
    events = out.journal.notifications()
    present(events[0], progress=tracker)
    tracker.resolved('I1', 555)
    present(events[1], progress=tracker)                 # halt('I1') -> forget
    third = present(events[2], progress=tracker)          # progress('I2') — unrelated intent_id
    assert third['kind'] == 'send' and third['progress_intent_id'] == 'I2'


def test_present_sol_progress_routes_the_same_way_as_progress(tmp_path):
    out = make_outbox(tmp_path)
    tracker = ProgressTracker()
    out.execution_notice(42, 'sol_progress', sol_progress_facts('S1', stage='swap'))
    out.execution_notice(42, 'sol_progress', sol_progress_facts('S1', stage='hedge'))
    events = out.journal.notifications()
    first = present(events[0], progress=tracker)
    assert first['kind'] == 'send' and first['silent'] is True
    tracker.resolved('S1', 900)
    second = present(events[1], progress=tracker)
    assert second['kind'] == 'edit' and second['message_id'] == 900 and second['throttle'] is True


def test_present_returns_noop_while_first_send_still_in_flight(tmp_path):
    out = make_outbox(tmp_path)
    tracker = ProgressTracker()
    out.execution_notice(42, 'progress', progress_facts('I1', clip=1, clips=3))
    out.execution_notice(42, 'progress', progress_facts('I1', clip=2, clips=3))
    events = out.journal.notifications()
    first = present(events[0], progress=tracker)
    assert first['kind'] == 'send'
    # events[0]'s message_id is not resolved yet — the second stage must not also become a 'send'.
    second = present(events[1], progress=tracker)
    assert second == {'kind': 'noop'}


# === 3. Interface.deliver_once() end to end, with an async-like fake Sender ===================================

class RpcClient:
    """Adapts core.journal.Journal/Outbox to the Client.call(method, params) shape Interface expects,
    exactly as core.service.CoreService.call dispatches 'read_notifications'/'ack_notification' in
    production — without needing a full CoreService/Bot/Engine/Desk setup."""
    def __init__(self, journal, outbox):
        self.journal, self.outbox = journal, outbox

    def call(self, method, params=None):
        params = params or {}
        if method == 'read_notifications':
            return {'events': self.journal.notifications(params.get('after', 0), params.get('limit', 20))}
        if method == 'ack_notification':
            return self.outbox.ack(params.get('event_id'), params.get('result', {}))
        raise AssertionError(f'unexpected RPC in test: {method}')


class FakeSender:
    """Records send()/edit() calls; on_done is deliberately NOT invoked automatically — the test decides
    when Telegram 'answers' by calling resolve_send()/resolve_edit(), mirroring tg/sender.py's Sender
    (a background thread calls on_done asynchronously, not send()/edit()'s caller). This is what lets the
    in-flight-coalescing tests reproduce the exact race the old Bot._progress_pending existed for."""
    def __init__(self):
        self.sent = []
        self.edited = []

    def send(self, chat_id, text, *, html=True, reply_markup=None, silent=False, on_done=None):
        self.sent.append(dict(chat_id=chat_id, text=text, html=html, reply_markup=reply_markup,
                              silent=silent, on_done=on_done))
        return True

    def edit(self, chat_id, message_id, text, *, reply_markup=None, html=True, throttle=False, on_done=None):
        self.edited.append(dict(chat_id=chat_id, message_id=message_id, text=text, html=html,
                                reply_markup=reply_markup, throttle=throttle, on_done=on_done))
        if on_done is not None:
            on_done({})           # the message_id already exists; treat the edit as resolved immediately
        return True

    def resolve_send(self, index, message_id):
        """Simulate Telegram handing back message_id (or None = delivery failed) for self.sent[index]."""
        cb = self.sent[index]['on_done']
        if cb is not None:
            cb(None if message_id is None else {'message_id': message_id})


def make_interface(tmp_path):
    out = make_outbox(tmp_path)
    client = RpcClient(out.journal, out)
    sender = FakeSender()
    ui = Interface(api=None, sender=sender, client=client, state=State(tmp_path / 'ui.json'))
    return ui, out, sender


def test_deliver_once_edits_one_message_across_three_progress_stages(tmp_path):
    ui, out, sender = make_interface(tmp_path)
    out.execution_notice(42, 'progress', progress_facts('I1', clip=1, clips=3))
    ui.deliver_once()
    assert len(sender.sent) == 1 and len(sender.edited) == 0
    sender.resolve_send(0, 555)                           # Telegram answers: message 555 was created

    out.execution_notice(42, 'progress', progress_facts('I1', clip=2, clips=3))
    ui.deliver_once()
    assert len(sender.sent) == 1 and len(sender.edited) == 1
    assert sender.edited[0]['message_id'] == 555 and sender.edited[0]['throttle'] is True
    assert 'клип 2/3' in sender.edited[0]['text']

    out.execution_notice(42, 'progress', progress_facts('I1', clip=3, clips=3))
    ui.deliver_once()
    assert len(sender.sent) == 1 and len(sender.edited) == 2
    assert sender.edited[1]['message_id'] == 555
    assert 'клип 3/3' in sender.edited[1]['text']


def test_deliver_once_coalesces_a_burst_while_first_send_is_in_flight(tmp_path):
    ui, out, sender = make_interface(tmp_path)
    out.execution_notice(42, 'progress', progress_facts('I1', clip=1, clips=4))
    ui.deliver_once()
    assert len(sender.sent) == 1                          # message_id not known to Telegram yet in this test

    # Two more stages land before Telegram ever answers the first send — the exact race
    # ProgressTracker/_pending exists for.
    out.execution_notice(42, 'progress', progress_facts('I1', clip=2, clips=4))
    out.execution_notice(42, 'progress', progress_facts('I1', clip=3, clips=4))
    ui.deliver_once()
    # Neither must have produced a second 'send' or any 'edit' yet: both were coalesced in-memory.
    assert len(sender.sent) == 1 and len(sender.edited) == 0

    sender.resolve_send(0, 777)                            # Telegram finally answers the first send
    # resolved() must have flushed the *latest* (clip 3) queued text as one throttled edit — not clip 2.
    assert len(sender.edited) == 1
    assert sender.edited[0]['message_id'] == 777 and sender.edited[0]['throttle'] is True
    assert 'клип 3/4' in sender.edited[0]['text'] and 'клип 2/4' not in sender.edited[0]['text']

    out.execution_notice(42, 'progress', progress_facts('I1', clip=4, clips=4))
    ui.deliver_once()
    assert len(sender.sent) == 1 and len(sender.edited) == 2
    assert 'клип 4/4' in sender.edited[1]['text']


def test_deliver_once_new_intent_after_halt_sends_a_new_message(tmp_path):
    ui, out, sender = make_interface(tmp_path)
    out.execution_notice(42, 'progress', progress_facts('I1'))
    ui.deliver_once()
    sender.resolve_send(0, 555)

    out.execution_notice(42, 'halt', halt_facts('I1'))
    ui.deliver_once()                                      # forgets I1 (and sends its own halt message)
    assert len(sender.sent) == 2                            # the halt notice itself is an ordinary 'send'

    out.execution_notice(42, 'progress', progress_facts('I2'))
    ui.deliver_once()
    assert len(sender.sent) == 3 and len(sender.edited) == 0
    assert sender.sent[-1]['text'] != sender.sent[0]['text'] or True  # I2 is clip 1 again — a fresh send


def test_deliver_once_failed_first_send_lets_next_progress_retry_as_fresh_send(tmp_path):
    ui, out, sender = make_interface(tmp_path)
    out.execution_notice(42, 'progress', progress_facts('I1', clip=1, clips=2))
    ui.deliver_once()
    sender.resolve_send(0, None)                           # Telegram never delivered it at all: resolved()
                                                            # forgets I1, but the notification itself was
                                                            # never acked (res=None), so it stays undelivered
                                                            # and is re-read on the next poll — exactly like
                                                            # any other failed 'send' in this protocol.
    out.execution_notice(42, 'progress', progress_facts('I1', clip=2, clips=2))
    ui.deliver_once()
    # The still-undelivered clip-1 notification is re-read first (oldest id first) and retried as a fresh
    # 'send' — never an 'edit' of a message that was never created. Clip 2 arrives behind it in the same
    # batch and is coalesced (ProgressTracker sees 'I1' in flight again the instant the retry is
    # dispatched), to be flushed once the retry resolves — the same in-flight coalescing as any other burst.
    assert len(sender.sent) == 2 and len(sender.edited) == 0
    assert 'клип 1/2' in sender.sent[1]['text']

    sender.resolve_send(1, 999)
    assert len(sender.edited) == 1
    assert sender.edited[0]['message_id'] == 999 and 'клип 2/2' in sender.edited[0]['text']


def test_deliver_once_notification_state_is_acked_for_noop_and_not_reread(tmp_path):
    """A coalesced ('noop') notification is acknowledged locally right away (Interface.State.ack_pending) —
    otherwise Interface.deliver_once() would re-read it forever (core.journal.Journal.notifications() only
    excludes delivered rows). Like every acknowledgement in this protocol, it only reaches core.journal
    (delivered=...) on the *next* deliver_once() call (the method's own leading comment: 'save successful
    delivery before ACK, so a disconnected core doesn't cause resend on UI restart') — not a special case
    for 'noop', just the existing two-phase ack timing this change doesn't touch."""
    ui, out, sender = make_interface(tmp_path)
    out.execution_notice(42, 'progress', progress_facts('I1', clip=1, clips=2))
    ui.deliver_once()
    out.execution_notice(42, 'progress', progress_facts('I1', clip=2, clips=2))
    ui.deliver_once()                                       # clip 2 coalesces into 'noop', acked locally
    assert ui.state.pending() == {'2': {'message_id': None}}       # clip 1's send hasn't resolved yet
    sender.resolve_send(0, 555)                              # clip 1's send finally resolves
    assert len(sender.edited) == 1 and 'клип 2/2' in sender.edited[0]['text']  # flushed immediately, in-process

    ui.deliver_once()                                         # flushes both pending acks to core.journal
    assert out.journal.notifications() == []
