"""Principal authorization from neutral source facts; no Telegram or presenter imports."""
from __future__ import annotations
import threading, time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable
from ..trade import tconfig
from ..ipc.source import OperatorSource
from ..operator_commands import normalize

OWNER_MSG = "owner_msg"
OWNER_CB = "owner_cb"
STALE = "stale"
STRANGER_START = "stranger_start"
STRANGER_LIMITED = "stranger_start_limited"
STRANGER_CB = "stranger_cb"
IGNORE = "ignore"

START_GLOBAL_MAX = 20       # ответов на /start незнакомцам за окно TG_START_REPLY_GAP_S — всего, по всем чатам
_LIMITER_CHATS_MAX = 10_000  # память ограничена: старейшие чаты забываются


@dataclass(frozen=True)
class Decision:
    verdict: str
    update_id: int | None
    user_id: int | None = None
    chat_id: int | None = None
    text: str | None = None             # текст сообщения или data кнопки
    date: int | None = None             # message.date (unix, с)
    callback_id: str | None = None      # для answerCallbackQuery
    message_id: int | None = None       # сообщение с кнопкой (снять кнопки правкой)

    @property
    def is_owner(self) -> bool:
        return self.verdict in (OWNER_MSG, OWNER_CB)


class StartLimiter:
    """/start незнакомцу: один ответ на чат за gap_s и не больше global_max за gap_s всего. Потокобезопасен."""

    def __init__(self, gap_s: float = tconfig.TG_START_REPLY_GAP_S, global_max: int = START_GLOBAL_MAX,
                 clock: Callable[[], float] = time.time, max_chats: int = _LIMITER_CHATS_MAX):
        self.gap_s = gap_s
        self.global_max = global_max
        self._clock = clock
        self._max_chats = max_chats
        self._last: "OrderedDict[int, float]" = OrderedDict()
        self._recent: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self, chat_id: int, now: float | None = None) -> bool:
        t = self._clock() if now is None else now
        with self._lock:
            while self._recent and t - self._recent[0] >= self.gap_s:
                self._recent.popleft()
            prev = self._last.get(chat_id)
            if prev is not None and t - prev < self.gap_s:
                return False
            if len(self._recent) >= self.global_max:
                return False
            self._last[chat_id] = t
            self._last.move_to_end(chat_id)
            while len(self._last) > self._max_chats:
                self._last.popitem(last=False)
            self._recent.append(t)
            return True


def classify(source: OperatorSource, owner_id: int | None, *, now=None, limiter=None,
             stale_s=tconfig.STALE_CMD_S) -> Decision:
    if not isinstance(source, OperatorSource) or source.version != 1:
        raise ValueError('unsupported operator source')
    t = time.time() if now is None else now
    owner = owner_id if type(owner_id) is int and owner_id > 0 else None
    d = dict(update_id=source.update_id, user_id=source.user_id, chat_id=source.chat_id, text=source.text,
             date=source.date, callback_id=source.callback_id, message_id=source.message_id)
    if source.kind == 'action':
        ok = (owner is not None and source.user_id == owner and not source.actor_is_bot
              and (not source.has_conversation or (source.chat_id == owner and source.private)))
        return Decision(OWNER_CB if ok else STRANGER_CB, **d)
    if source.kind != 'message':
        return Decision(IGNORE, source.update_id)
    if (source.text is None or not source.private or source.actor_is_bot or source.user_id is None
            or source.user_id != source.chat_id):
        return Decision(IGNORE, **d)
    if owner is not None and source.user_id == owner:
        if source.date is None or t-source.date > stale_s:
            return Decision(STALE, **d)
        return Decision(OWNER_MSG, **d)
    toks = normalize(source.text)
    if toks and toks[0] == '/start':
        if limiter is not None and not limiter.allow(source.chat_id, t):
            return Decision(STRANGER_LIMITED, **d)
        return Decision(STRANGER_START, **d)
    return Decision(IGNORE, **d)
