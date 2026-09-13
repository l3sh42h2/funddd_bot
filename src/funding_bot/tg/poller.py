"""Long-poll getUpdates и сторож живости (trade_spec §6 шаг 1 и «Telegram robustness», отчёт telegram §4).

Порядок на каждый update (почему именно так):
  claim_update (INSERT OR IGNORE в tg_updates) → handle → set_tg_offset(update_id + 1).
  Падение после handle, но до смещения — Telegram отдаст update снова, claim вернёт False, и команда не
  исполнится второй раз. Падение между claim и handle теряет команду — это и есть «не больше одного раза»:
  для команд с деньгами потерять (владелец повторит) лучше, чем выполнить дважды. Смещение только растёт.
  Ошибка БД на claim — смещение не двигается, тот же update придёт в следующем опросе.

Живость = время последнего УСПЕШНОГО возврата getUpdates, а не «поток жив»: 04.09 клиент TWAP молчал 12 ч
при живом потоке и 13 483 ошибках. Сторож каждые ~10 с:
  - нет успешного опроса дольше TG_RENEW_AFTER_S (~105 с) — api.renew() (новая сессия; зависший вызов доживает
    на старой до своего read-таймаута 35 с), в лог tg_reconnected;
  - поток опроса умер — перезапустить;
  - больше TG_RENEWS_PER_H_MAX обновлений за час и исполнитель свободен — «exit» (бот выходит с кодом 3, systemd
    перезапустит); пока идёт исполнение — только тревога, сам не выходит;
  - пишет runtime/tg_health.json (last_ok, pid, reconnects_24h) для remote_verify.sh.

При старте: getMe и getWebhookInfo. Непустой url вебхука — тревога и отказ опрашивать (getUpdates с вебхуком не
работает). deleteWebhook НЕ вызываем: вебхук может быть чужим. 409 (второй опрашивающий) — тревога и отступ.
"""
from __future__ import annotations
import json, logging, os, threading, time
from collections import deque
from pathlib import Path
from typing import Any, Callable
from ..trade import store, tconfig
from . import views
from .api import TgConflict, TgRetryAfter, mask_url, redact
from .auth import update_meta

log = logging.getLogger(__name__)

CONFLICT_BACKOFF_S = (30, 60, 120, 300)    # 409: отступ растёт, пока другой процесс держит токен
FAIL_BACKOFF_MAX_S = 30                    # сеть/прочее: 2, 4, 8 … 30 с
ALARM_REPEAT_S = 3600                      # одна и та же тревога — не чаще раза в час
WATCHDOG_EVERY_S = 10


class WebhookSet(RuntimeError):
    """У токена стоит вебхук: getUpdates не работает, опрашивать нельзя."""

    def __init__(self, host: str):
        super().__init__(f"у токена стоит вебхук ({host})")
        self.host = host


def startup_check(api, on_alarm: Callable[[str, str], Any] | None = None) -> dict:
    """getMe + getWebhookInfo. Вебхук — тревога и WebhookSet (чужой вебхук не снимаем)."""
    me = api.get_me() or {}
    info = api.get_webhook_info() or {}
    url = info.get("url") if isinstance(info, dict) else None
    if url:
        host = mask_url(url)
        log.error("tg: у токена вебхук %s — опрос не запускаю", host)
        if on_alarm is not None:
            on_alarm("webhook", views.webhook_alarm(host))
        raise WebhookSet(host)
    log.info("tg: бот @%s, ожидающих обновлений %s", me.get("username"), info.get("pending_update_count"))
    return {"username": me.get("username"), "id": me.get("id"), "pending": info.get("pending_update_count")}


class Poller:
    """Цикл getUpdates. handle(update) -> вердикт|None — диспетчер бота (auth.classify → parse → ответ); он не
    должен двигать деньги сам (вход только строит план, деньги — после кнопки). con — соединение ЭТОГО потока
    с trade.db; тот же con виден диспетчеру как poller.con (CAS кнопки выполняется в потоке опроса)."""

    def __init__(self, api, con, handle: Callable[[dict], str | None], *,
                 on_alarm: Callable[[str, str], Any] | None = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Any] | None = None, poll_s: int = tconfig.POLL_S):
        self.api = api
        self.con = con
        self.handle = handle
        self.on_alarm = on_alarm
        self.poll_s = poll_s
        self._clock = clock
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._alarmed: dict[str, float] = {}
        self.started = clock()
        self.last_ok: float | None = None       # время последнего успешного getUpdates — это и есть живость
        self.last_contact: float | None = None  # последний 409: сеть и сессия живы, опрашивает другой процесс
        self.fails = 0                          # подряд, кроме 409
        self.conflicts = 0                      # 409 подряд
        self.handled = 0
        self.skipped = 0                        # повторно доставленные (claim вернул False)

    # --- один опрос ----------------------------------------------------------------------------------
    def poll_once(self) -> int:
        ups = self.api.get_updates(store.tg_offset(self.con), timeout=self.poll_s)
        self.last_ok = self._clock()
        self.fails = 0
        if self.conflicts:
            self.conflicts = 0
            self._alarmed.pop("conflict", None)     # следующий конфликт — снова тревога
        n = 0
        for u in sorted((x for x in ups if isinstance(x, dict)), key=lambda x: x.get("update_id") or 0):
            uid = u.get("update_id")
            if not isinstance(uid, int) or isinstance(uid, bool):
                log.warning("tg: update без update_id пропущен")
                continue
            user_id, chat_id, text = update_meta(u)
            if store.claim_update(self.con, uid, self._clock(), user_id, chat_id, text):
                verdict = self._dispatch(u)
                if verdict:
                    try:
                        store.set_update_verdict(self.con, uid, str(verdict)[:64])
                    except Exception as e:          # журнал вердикта — не повод повторять команду
                        log.warning("tg: вердикт update %s не записан: %s", uid, redact(e))
                n += 1
                self.handled += 1
            else:
                self.skipped += 1
                log.info("tg: update %s уже разобран — пропуск", uid)
            store.set_tg_offset(self.con, uid + 1)
        return n

    def _dispatch(self, u: dict) -> str | None:
        try:
            return self.handle(u)
        except Exception as e:                       # диспетчер «никогда не падает»; если упал — не повторяем
            log.error("tg: обработка update %s упала: %s", u.get("update_id"), redact(e))
            return f"error:{type(e).__name__}"

    def step(self) -> float:
        """Один шаг цикла; возвращает, сколько секунд подождать перед следующим (0 — сразу)."""
        try:
            self.poll_once()
            return 0.0
        except TgRetryAfter as e:
            log.warning("tg: getUpdates 429, жду %d с", e.seconds + 1)
            return float(e.seconds + 1)
        except TgConflict as e:
            self.last_contact = self._clock()
            self.conflicts += 1
            wait = CONFLICT_BACKOFF_S[min(self.conflicts, len(CONFLICT_BACKOFF_S)) - 1]
            log.error("tg: 409 — токен опрашивает другой процесс (%s); отступ %d с", redact(e), wait)
            self._alarm("conflict", views.conflict_alarm())
            return float(wait)
        except Exception as e:
            self.fails += 1
            wait = float(min(FAIL_BACKOFF_MAX_S, 2 ** self.fails))
            log.warning("tg: опрос: %s (подряд %d), пауза %.0f с", redact(e), self.fails, wait)
            return wait

    def _alarm(self, kind: str, text: str) -> None:
        now = self._clock()
        last = self._alarmed.get(kind)
        if last is not None and now - last < ALARM_REPEAT_S:
            return
        self._alarmed[kind] = now
        if self.on_alarm is not None:
            try:
                self.on_alarm(kind, text)
            except Exception as e:
                log.error("tg: тревога %s не передана: %s", kind, redact(e))

    # --- поток -------------------------------------------------------------------------------------------
    def run(self) -> None:
        while not self._stop.is_set():
            wait = self.step()
            if wait > 0 and not self._stop.is_set():
                self._sleep(wait)

    def stop(self) -> None:
        """Идущий getUpdates доживает до своего таймаута (≤ 35 с); новых не будет."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def last_ok_age(self, now: float | None = None) -> float | None:
        if self.last_ok is None:
            return None
        return (self._clock() if now is None else now) - self.last_ok


class Watchdog:
    """Сторож опроса. check() — один проход (тесты зовут напрямую с поддельными часами), run() — поток."""

    def __init__(self, api, poller: Poller, *, executor_busy: Callable[[], bool] = lambda: False,
                 poller_alive: Callable[[], bool] | None = None, restart_poller: Callable[[], Any] | None = None,
                 on_alarm: Callable[[str, str], Any] | None = None, clock: Callable[[], float] = time.time,
                 health_path: Path | str | None = None, renew_after_s: float = tconfig.TG_RENEW_AFTER_S,
                 max_per_h: int = tconfig.TG_RENEWS_PER_H_MAX):
        self.api = api
        self.poller = poller
        self.executor_busy = executor_busy
        self.poller_alive = poller_alive
        self.restart_poller = restart_poller
        self.on_alarm = on_alarm
        self._clock = clock
        self.health_path = Path(health_path) if health_path else None
        self.renew_after_s = renew_after_s
        self.max_per_h = max_per_h
        self.renewals: deque[float] = deque()       # времена обновлений сессии за сутки
        self.last_renew: float | None = None
        self.restarts = 0
        self._busy_alarm_at: float | None = None

    def renewals_within(self, window_s: float, now: float | None = None) -> int:
        t = self._clock() if now is None else now
        return sum(1 for x in self.renewals if t - x < window_s)

    def check(self) -> str | None:
        """None | "restarted" | "renewed" | "exit". «exit» — бот выходит с кодом 3 (только при свободном
        исполнителе)."""
        now = self._clock()
        action: str | None = None
        if self.poller_alive is not None and self.restart_poller is not None and not self.poller.stopping \
                and not self.poller_alive():
            log.error("tg: поток опроса умер — перезапускаю")
            self.restart_poller()
            self.restarts += 1
            action = "restarted"
        # 409 — тоже ответ Telegram: новая сессия его не лечит, а выход с кодом 3 крутил бы рестарты каждые ~20 мин
        # (тревога о конфликте — в Poller, раз в час)
        base = max(self.poller.last_ok if self.poller.last_ok is not None else self.poller.started,
                   self.poller.last_contact if self.poller.last_contact is not None else float("-inf"),
                   self.last_renew if self.last_renew is not None else float("-inf"))
        if now - base > self.renew_after_s:
            self.api.renew()
            self.last_renew = now
            self.renewals.append(now)
            log.warning("tg_reconnected: успешного опроса не было %.0f с — новая сессия", now - base)
            action = "renewed"
        while self.renewals and now - self.renewals[0] >= 86400:
            self.renewals.popleft()
        per_h = self.renewals_within(3600, now)
        if per_h > self.max_per_h:
            if not self.executor_busy():
                log.error("tg: %d переподключений за час — выход (код 3), systemd перезапустит", per_h)
                self._write_health(now)
                return "exit"
            if self._busy_alarm_at is None or now - self._busy_alarm_at >= ALARM_REPEAT_S:
                self._busy_alarm_at = now
                if self.on_alarm is not None:
                    try:
                        self.on_alarm("tg_watchdog", views.watchdog_alarm(per_h, True))
                    except Exception as e:
                        log.error("tg: тревога сторожа не передана: %s", redact(e))
        self._write_health(now)
        return action

    def health(self, now: float | None = None) -> dict:
        t = self._clock() if now is None else now
        return {"ts": t, "pid": os.getpid(), "last_ok": self.poller.last_ok, "started": self.poller.started,
                "reconnects_24h": self.renewals_within(86400, t), "reconnects_1h": self.renewals_within(3600, t),
                "poll_fails": self.poller.fails, "conflicts": self.poller.conflicts}

    def _write_health(self, now: float) -> None:
        if self.health_path is not None:
            write_health(self.health_path, self.health(now))

    def run(self, stop: threading.Event, on_exit: Callable[[], Any], every_s: float = WATCHDOG_EVERY_S) -> None:
        """Поток сторожа. on_exit — бот сам решает, как выйти с кодом 3 (sys.exit в потоке завершит лишь поток)."""
        while not stop.wait(every_s):
            try:
                if self.check() == "exit":
                    on_exit()
                    return
            except Exception as e:
                log.error("tg: сторож: %s", redact(e))


def write_health(path: Path | str, data: dict) -> bool:
    """Атомарно (tmp + replace): remote_verify.sh не прочитает половину файла."""
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except OSError as e:
        log.warning("tg: tg_health.json не записан: %s", redact(e))
        return False
