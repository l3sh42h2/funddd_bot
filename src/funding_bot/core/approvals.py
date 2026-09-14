"""Operator approval decisions. No transport parsing, markup or delivery effects.

The caller authenticates the principal and checks the current plan fingerprint.
This boundary owns durable nonce/expiry/CAS/pause decisions, returning reason codes.
Only a newly applied approval returns submit=True; delivery ACK cannot call it.
"""
from dataclasses import dataclass
import re
import time
from ..trade import store
from ..trade.store import IntentStatus, StoreBusy


@dataclass(frozen=True)
class ApprovalAction:
    action: str
    intent_id: str
    nonce: str

    def __post_init__(self):
        if (self.action not in ('ok', 'no') or not isinstance(self.intent_id, str)
                or re.fullmatch(r'[A-Z0-9]{2,12}', self.intent_id) is None
                or not isinstance(self.nonce, str) or re.fullmatch(r'[0-9a-f]{8}', self.nonce) is None):
            raise ValueError('invalid approval action')


@dataclass(frozen=True)
class ApprovalDecision:
    applied: bool
    action: str
    intent_id: str
    reason: str
    submit: bool
    closed: str | None
    version: int = 1


def decide(con, action: ApprovalAction | None, *, paused=False, now=None) -> ApprovalDecision:
    t = time.time() if now is None else now
    if action is None:
        return ApprovalDecision(False, '?', '', 'unknown', False, None)
    if not isinstance(action, ApprovalAction) or type(paused) is not bool:
        raise ValueError('invalid approval input')
    if con.in_transaction:
        raise ValueError('approval cannot return submit permission before a caller transaction commits')
    iid, nonce = action.intent_id, action.nonce
    if action.action == 'no':
        if store.reject_intent(con, iid, nonce):
            return ApprovalDecision(True, 'no', iid, 'cancelled', False, 'no')
        return _why_not(con, action, t)
    if paused:
        return ApprovalDecision(False, 'ok', iid, 'paused', False, None)
    try:
        applied = store.approve_intent(con, iid, nonce, now=t)
    except StoreBusy:
        return ApprovalDecision(False, 'ok', iid, 'busy', False, None)
    if applied:
        return ApprovalDecision(True, 'ok', iid, 'accepted', True, 'ok')
    return _why_not(con, action, t)


def _why_not(con, action, t):
    iid = action.intent_id
    row = store.get_intent(con, iid)
    if row is None:
        return ApprovalDecision(False, action.action, iid, 'stale', False, None)
    if row['nonce'] != action.nonce:
        return ApprovalDecision(False, action.action, iid, 'old_button', False, None)
    st = row['status']
    if st == IntentStatus.PROPOSED and (row['expires'] or 0) <= t:
        store.expire_intents(con, now=t)
        return ApprovalDecision(False, action.action, iid, 'expired', False, 'expired')
    reason = {
        IntentStatus.APPROVED: 'already', IntentStatus.RUNNING: 'running',
        IntentStatus.EXPIRED: 'expired', IntentStatus.REJECTED: 'already_cancelled',
        IntentStatus.DONE: 'done', IntentStatus.PARTIAL: 'done', IntentStatus.FAILED: 'done',
        IntentStatus.INTERRUPTED: 'interrupted',
    }.get(st, 'stale')
    return ApprovalDecision(False, action.action, iid, reason, False,
                            'expired' if st == IntentStatus.EXPIRED else None)
