"""Legacy Telegram extraction and presentation facade for core authorization."""
from __future__ import annotations
from typing import Any
from dataclasses import dataclass
from ..trade import tconfig
from ..core.authority import (Decision, StartLimiter, OWNER_MSG, OWNER_CB, STALE, STRANGER_START,
                              STRANGER_LIMITED, STRANGER_CB, IGNORE, START_GLOBAL_MAX)
from ..core.authority import classify as authorize_source
from ..ipc.source import legacy_telegram_source
from .parse import parse_callback
from . import views

def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def update_meta(u: dict) -> tuple[int | None, int | None, str | None]:
    """(user_id, chat_id, text) для журнала tg_updates — у сообщения и у нажатия кнопки."""
    if not isinstance(u, dict):
        return None, None, None
    if isinstance(u.get("callback_query"), dict):
        cq = u["callback_query"]
        msg = cq.get("message") if isinstance(cq.get("message"), dict) else {}
        return _int((cq.get("from") or {}).get("id")), _int((msg.get("chat") or {}).get("id")), cq.get("data")
    for k in ("message", "edited_message", "channel_post", "edited_channel_post"):
        m = u.get(k)
        if isinstance(m, dict):
            t = m.get("text")
            return _int((m.get("from") or {}).get("id")), _int((m.get("chat") or {}).get("id")), \
                t if isinstance(t, str) else None
    return None, None, None


def classify(u, owner_id, *, now=None, limiter=None, stale_s=tconfig.STALE_CMD_S):
    return authorize_source(legacy_telegram_source(u), owner_id, now=now, limiter=limiter, stale_s=stale_s)

# --- кнопка плана: атомарный CAS -----------------------------------------------------------------------
@dataclass(frozen=True)
class PressResult:
    applied: bool           # этот вызов перевёл намерение (approved/rejected)
    action: str             # ok | no | ?
    intent_id: str
    answer: str             # текст answerCallbackQuery (≤ 200)
    submit: bool            # отдать executor.submit(intent_id): ровно один True на намерение
    closed: str | None      # чем закрыть сообщение плана (views.plan_closed): ok | no | expired | None


def press(con, data: str | None, *, paused: bool = False, now: float | None = None) -> PressResult:
    """Legacy Telegram facade: parse the button, delegate decision, render its reason."""
    from ..core.approvals import ApprovalAction, decide
    cb = parse_callback(data)
    action = ApprovalAction(cb.action, cb.intent_id, cb.nonce) if cb else None
    result = decide(con, action, paused=paused, now=now)
    answer = getattr(views, 'CB_' + result.reason.upper())
    return PressResult(result.applied, result.action, result.intent_id, answer, result.submit, result.closed)
