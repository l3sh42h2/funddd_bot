"""Кто может командовать ботом (trade_spec §6 шаги 1, 5–6; отчёт telegram §5–6). Чистые функции + CAS кнопки.

Правило: командует только владелец — личный чат, from.id == chat.id == telegram.owner_id из owner.toml.
- Незнакомцы игнорируются (и пишутся в лог), кроме /start: ему отвечаем ЕГО ids (чтобы владелец мог узнать свой
  id до заполнения owner.toml) — не чаще раза в 10 мин на чат и не больше START_GLOBAL_MAX за окно всего
  (бот не должен стать усилителем чужого спама). Пока owner_id пуст, командовать не может никто.
- Группа, канал, правка сообщения, сообщение от бота, без текста — игнор: команды с деньгами только из лички.
- Сообщение владельца старше STALE_CMD_S (90 с) — «устарела», не исполняется: после простоя Telegram отдаёт
  до 24 ч очереди, и вчерашний «вход» не должен сработать сегодня. Кнопки старыми не бывают по дате (у нажатия её
  нет) — их режет срок намерения (expires) в самом UPDATE.
- Нажатие незнакомца — answerCallbackQuery без текста (гасит «часики») и игнор.

press() — кнопка плана: одно атомарное UPDATE в store.approve_intent. Двойное нажатие, повтор доставки и два
устройства дают ровно один submit=True; остальные получают «уже принято» и т. п.
"""
from __future__ import annotations
import logging, threading, time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Callable
from ..trade import store, tconfig
from ..trade.store import IntentStatus, StoreBusy
from . import views
from .parse import normalize, parse_callback

log = logging.getLogger(__name__)

# вердикты classify (пишутся в tg_updates.verdict)
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


def classify(u: dict, owner_id: int | None, *, now: float | None = None, limiter: StartLimiter | None = None,
             stale_s: float = tconfig.STALE_CMD_S) -> Decision:
    """Вердикт по одному update. owner_id — из свежего owner.toml (None = владельца нет, командовать нельзя)."""
    t = time.time() if now is None else now
    uid = _int(u.get("update_id")) if isinstance(u, dict) else None
    owner = owner_id if _int(owner_id) and owner_id > 0 else None
    if not isinstance(u, dict):
        return Decision(IGNORE, None)

    cq = u.get("callback_query")
    if isinstance(cq, dict):
        frm = cq.get("from") or {}
        msg = cq.get("message") if isinstance(cq.get("message"), dict) else {}
        chat = msg.get("chat") or {}
        d = dict(update_id=uid, user_id=_int(frm.get("id")), chat_id=_int(chat.get("id")),
                 text=cq.get("data") if isinstance(cq.get("data"), str) else None,
                 callback_id=str(cq["id"]) if cq.get("id") is not None else None,
                 message_id=_int(msg.get("message_id")))
        ok = (owner is not None and d["user_id"] == owner and not frm.get("is_bot")
              # недоступное сообщение (date=0) приходит без чата — тогда хватает from.id; чат, если есть, — личка владельца
              and (not chat or (d["chat_id"] == owner and chat.get("type") == "private")))
        return Decision(OWNER_CB if ok else STRANGER_CB, **d)

    m = u.get("message")
    if not isinstance(m, dict):          # edited_message, channel_post, прочее — вне allowed_updates, но не доверяем
        return Decision(IGNORE, uid)
    frm = m.get("from") or {}
    chat = m.get("chat") or {}
    text = m.get("text") if isinstance(m.get("text"), str) else None
    d = dict(update_id=uid, user_id=_int(frm.get("id")), chat_id=_int(chat.get("id")), text=text,
             date=_int(m.get("date")))
    if text is None or chat.get("type") != "private" or frm.get("is_bot") or d["user_id"] is None \
            or d["user_id"] != d["chat_id"]:
        return Decision(IGNORE, **d)
    if owner is not None and d["user_id"] == owner:
        if d["date"] is None or t - d["date"] > stale_s:
            return Decision(STALE, **d)
        return Decision(OWNER_MSG, **d)
    toks = normalize(text)
    if toks and toks[0] == "/start":
        if limiter is not None and not limiter.allow(d["chat_id"], t):
            return Decision(STRANGER_LIMITED, **d)
        return Decision(STRANGER_START, **d)
    return Decision(IGNORE, **d)


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
    """Нажатие владельца (вердикт OWNER_CB). con — соединение ПОТОКА вызывающего. На паузе «да» не принимается:
    после «стоп» новое не начинается, снять паузу — «продолжить» со свежим планом."""
    t = time.time() if now is None else now
    cb = parse_callback(data)
    if cb is None:
        return PressResult(False, "?", "", views.CB_UNKNOWN, False, None)
    iid, nonce = cb.intent_id, cb.nonce
    if cb.action == "no":
        if store.reject_intent(con, iid, nonce):
            return PressResult(True, "no", iid, views.CB_CANCELLED, False, "no")
        return _why_not(con, cb.action, iid, nonce, t)
    if paused:
        return PressResult(False, "ok", iid, views.CB_PAUSED, False, None)
    try:
        applied = store.approve_intent(con, iid, nonce, now=t)
    except StoreBusy:
        return PressResult(False, "ok", iid, views.CB_BUSY, False, None)
    if applied:
        return PressResult(True, "ok", iid, views.CB_ACCEPTED, True, "ok")
    return _why_not(con, cb.action, iid, nonce, t)


def _why_not(con, action: str, iid: str, nonce: str, t: float) -> PressResult:
    row = store.get_intent(con, iid)
    if row is None:
        return PressResult(False, action, iid, views.CB_STALE, False, None)
    if row["nonce"] != nonce:
        return PressResult(False, action, iid, views.CB_OLD_BUTTON, False, None)
    st = row["status"]
    if st == IntentStatus.PROPOSED and (row["expires"] or 0) <= t:
        store.expire_intents(con, now=t)       # таймер ещё не прошёл — пометить сейчас и снять кнопки
        return PressResult(False, action, iid, views.CB_EXPIRED, False, "expired")
    answer = {
        IntentStatus.APPROVED: views.CB_ALREADY,
        IntentStatus.RUNNING: views.CB_RUNNING,
        IntentStatus.EXPIRED: views.CB_EXPIRED,
        IntentStatus.REJECTED: views.CB_ALREADY_CANCELLED,
        IntentStatus.DONE: views.CB_DONE,
        IntentStatus.PARTIAL: views.CB_DONE,
        IntentStatus.FAILED: views.CB_DONE,
        IntentStatus.INTERRUPTED: views.CB_INTERRUPTED,
    }.get(st, views.CB_STALE)
    return PressResult(False, action, iid, answer, False, "expired" if st == IntentStatus.EXPIRED else None)
