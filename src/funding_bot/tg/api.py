"""Тонкий клиент Telegram Bot API на requests (trade_spec §6, отчёт telegram §2, §4).

Почему свой, а не библиотека: нужны ровно пять методов, а главное — управляемые таймауты и живость. Опрос TWAP
04.09 умер молча на 12 ч внутри чужого клиента; здесь каждый вызов ограничен (connect, read), и зависнуть
навсегда не может ни getUpdates (5, 35), ни отправка (5, 15).

Токен:
- лежит только в приватном URL (_url); repr/str клиента его не показывают;
- текст исключения requests содержит ПОЛНЫЙ URL c /bot<token>/ — поэтому сетевые ошибки заворачиваются в
  TgNetwork с замаскированным текстом и поднимаются ВНЕ блока except: у нового исключения нет __context__,
  и трассировка в логе не потащит за собой исходное сообщение с токеном.

Ошибки (тело `{ok:false, error_code, description, parameters:{retry_after}}`):
  429 → TgRetryAfter(seconds)  — ждать retry_after и повторить то же самое;
  409 → TgConflict              — токен опрашивает другой процесс (или стоит вебхук);
  400 → TgBadRequest            — битая разметка/кнопка; отправитель откатывается в простой текст;
  403 → TgForbidden             — владелец заблокировал бота: записать и жить дальше;
  прочее → TgError; сеть/таймаут → TgNetwork.
"""
from __future__ import annotations
import logging, re, threading
from typing import Any, Callable
import requests
from .. import config
from ..trade import tconfig
from ..trade.keys import redact_secrets

log = logging.getLogger(__name__)

BASE = "https://api.telegram.org"
SEND_TIMEOUT = (5, 15)                  # обычные методы: connect 5 с, ответ 15 с
_TOKEN_RE = re.compile(r"^\d+:[\w-]+$")


class TgError(RuntimeError):
    """Ошибка Bot API. Текст всегда без токена."""

    def __init__(self, text: str, code: int | None = None, method: str | None = None):
        super().__init__(redact_secrets(text)[:500])
        self.code = code
        self.method = method


class TgRetryAfter(TgError):
    """429: Telegram просит подождать seconds. Сообщение не бросаем — повторяем после паузы."""

    def __init__(self, seconds: int, method: str | None = None, text: str = ""):
        super().__init__(f"{method}: 429 retry_after={seconds} {text}".strip(), 429, method)
        self.seconds = int(seconds)


class TgConflict(TgError):
    """409: getUpdates этого токена уже держит другой процесс (или у токена вебхук)."""


class TgBadRequest(TgError):
    """400: разметка, кнопка, «message is not modified» и т. п. — повтор тем же запросом не поможет."""


class TgForbidden(TgError):
    """403: бот заблокирован пользователем или не может писать в чат."""


class TgNetwork(TgError):
    """Сеть/таймаут/не-JSON ответ: исход вызова не известен (для sendMessage — сообщение могло и уйти)."""


def redact(text: Any) -> str:
    """Для логов модуля: имя типа исключения + текст, токен замаскирован, длина ограничена."""
    s = f"{type(text).__name__}: {text}" if isinstance(text, BaseException) else str(text)
    return redact_secrets(s)[:300]


def mask_url(url: str | None) -> str:
    """Чужой адрес вебхука в тревоге — только схема и хост (в пути вебхука бывает секрет)."""
    m = re.match(r"^([a-z]+://[^/?#]+)", str(url or ""), re.I)
    return m.group(1) if m else "?"


class TgApi:
    """Один экземпляр на поток: опрос и отправитель держат РАЗНЫЕ сессии (отчёт §4: сторож может обновить сессию
    опроса, не задевая отправку тревоги). renew() безопасен из другого потока — идущий вызов доживает на старой
    сессии (его ограничивает read-таймаут), следующий идёт по новой."""

    def __init__(self, token: str, session_factory: Callable[[], Any] | None = None, base: str = BASE):
        token = (token or "").strip()
        if not _TOKEN_RE.match(token):
            raise ValueError("TG_BOT_TOKEN не похож на токен бота (<id>:<секрет>)")   # сам токен не печатаем
        self._url = f"{base}/bot{token}/"
        self._factory = session_factory or requests.Session
        self._lock = threading.Lock()
        self._s = None
        self.renewals = 0
        self.renew()
        self.renewals = 0

    def __repr__(self) -> str:
        return f"<TgApi renewals={self.renewals}>"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("TgApi не сериализуется (в нём токен)")

    def renew(self) -> None:
        """Новая HTTP-сессия. Старая закрывается: свободные соединения её пула рвутся сразу, занятое — по таймауту."""
        s = self._factory()
        try:
            s.headers["user-agent"] = config.USER_AGENT
        except (AttributeError, TypeError):
            pass
        with self._lock:
            old, self._s = self._s, s
            self.renewals += 1
        if old is not None:
            try:
                old.close()
            except Exception as e:                      # закрытие — best effort, но молча не глотаем
                log.debug("tg: закрытие старой сессии: %s", redact(e))

    def call(self, method: str, _to: tuple[float, float] = SEND_TIMEOUT, **params) -> Any:
        """POST JSON → result. Параметры None не отправляются (Telegram трактует null не как «нет»)."""
        body = {k: v for k, v in params.items() if v is not None}
        with self._lock:
            s = self._s
        err: TgError | None = None
        r = None
        try:
            r = s.post(self._url + method, json=body, timeout=_to)
        except requests.RequestException as e:
            err = TgNetwork(f"{method}: {redact(e)}", None, method)
        except Exception as e:                          # фейк/адаптер кинул не-requests — тоже без токена
            err = TgNetwork(f"{method}: {redact(e)}", None, method)
        if err is not None:
            raise err                                   # вне except: без __context__ с URL и токеном
        status = getattr(r, "status_code", None)
        try:
            d = r.json()
        except ValueError:
            d = None
        if not isinstance(d, dict):
            if status == 429:
                raise TgRetryAfter(5, method)
            raise TgNetwork(f"{method}: HTTP {status} без JSON", status, method)
        if d.get("ok"):
            return d.get("result")
        code = d.get("error_code") or status
        desc = str(d.get("description") or "")
        params_ = d.get("parameters") or {}
        if code == 429:
            try:
                ra = int(params_.get("retry_after") or 5)
            except (TypeError, ValueError):
                ra = 5
            raise TgRetryAfter(max(ra, 1), method, desc)
        if code == 409:
            raise TgConflict(f"{method}: 409 {desc}", 409, method)
        if code == 400:
            raise TgBadRequest(f"{method}: 400 {desc}", 400, method)
        if code == 403:
            raise TgForbidden(f"{method}: 403 {desc}", 403, method)
        raise TgError(f"{method}: {code} {desc}", code, method)

    # --- методы, которые нужны боту -------------------------------------------------------------------
    def get_me(self) -> dict:
        return self.call("getMe")

    def get_webhook_info(self) -> dict:
        return self.call("getWebhookInfo")

    def get_updates(self, offset: int, timeout: int = tconfig.POLL_S, limit: int = 100) -> list[dict]:
        """Long-poll. HTTP read-таймаут (35 с) больше удержания (25 с): полуоткрытый сокет умирает, а не висит.
        edited_message в allowed_updates НЕТ: правка старого «вход» не должна исполниться второй раз."""
        return self.call("getUpdates", _to=tconfig.POLL_TIMEOUT, offset=int(offset), timeout=int(timeout),
                         limit=int(limit), allowed_updates=["message", "callback_query"]) or []

    def send_message(self, chat_id: int, text: str, *, html: bool = True, reply_markup: dict | None = None,
                     silent: bool = False) -> dict:
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML" if html else None,
                         reply_markup=reply_markup, disable_notification=True if silent else None,
                         link_preview_options={"is_disabled": True})

    def edit_message_text(self, chat_id: int, message_id: int, text: str, *, html: bool = True,
                          reply_markup: dict | None = None) -> Any:
        """reply_markup=None снимает кнопки (Telegram без поля reply_markup убирает клавиатуру при правке)."""
        return self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                         parse_mode="HTML" if html else None, reply_markup=reply_markup,
                         link_preview_options={"is_disabled": True})

    def edit_message_reply_markup(self, chat_id: int, message_id: int, reply_markup: dict | None = None) -> Any:
        return self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                         reply_markup=reply_markup or {"inline_keyboard": []})

    def answer_callback_query(self, callback_query_id: str, text: str | None = None, show_alert: bool = False) -> Any:
        """Обязателен на каждое нажатие (иначе у владельца крутится «часики»). text ≤ 200 символов."""
        return self.call("answerCallbackQuery", callback_query_id=callback_query_id,
                         text=(text[:200] if text else None), show_alert=True if show_alert else None)
