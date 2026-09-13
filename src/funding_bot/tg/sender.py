"""Очередь отправки в Telegram (trade_spec §6 «Telegram robustness», отчёт telegram §1, §8).

Почему очередь, а не прямой вызов: исполнение никогда не ждёт Telegram. 19.08 синхронная отправка ослепила
сигбот на 14 с — здесь send()/edit() только кладут задание (put_nowait) и сразу возвращаются, а сеть, темп,
429 и откаты живут в одном рабочем потоке со СВОЕЙ сессией TgApi (сторож может обновить сессию опроса,
не задевая отправку тревоги).

Поведение (отчёт §8):
- темп: не чаще раза в TG_SEND_GAP_S (1.05 с) на любой вызов — лимит Telegram ~1 сообщение/с на чат;
- 429: ждать retry_after + 1 и повторить ТО ЖЕ сообщение; не выбрасывать (отчёт исполнения важнее скорости);
- 400 «can't parse entities»: повтор простым текстом без parse_mode (теги сняты, сущности раскрыты);
  «message is not modified» у правки — не ошибка;
- 403 (владелец заблокировал бота): записать и жить дальше;
- сеть/таймаут: до NET_RETRIES попыток с паузой; исход sendMessage неизвестен — дубль сообщения безвреден,
  потеря отчёта — нет;
- длинное: режем по строкам на куски ≤ TG_SPLIT_CHARS (4000 при лимите 4096) в единицах UTF-16 — так считает
  Telegram, эмодзи весит 2; кнопки — только у последнего куска;
- прогресс: правка одного сообщения не чаще TG_EDIT_MIN_S; более новый текст вытесняет ждущий;
- тревоги инфраструктуры сворачиваются по виду за ALARM_WINDOW_S (цифры в виде → «#»), с числом повторов;
  отчёты исполнения не сворачиваются никогда.

escape() — для КАЖДОЙ динамической строки в HTML (текст ошибки биржи бывает с «<»); шаблоны — views.py.
Ссылка — только tx_link(): адрес по белому списку (bscscan, хэш ровно 0x + 64 hex), экранирован с quote=True; иначе
вместо ссылки простой текст. html_ok() — та же мера для проверки готового текста: наши теги, парные, и никаких
других <a>.
"""
from __future__ import annotations
import html, logging, queue, re, threading, time
from dataclasses import dataclass
from typing import Any, Callable
from ..trade import tconfig
from .api import TgBadRequest, TgError, TgForbidden, TgNetwork, TgRetryAfter, redact

log = logging.getLogger(__name__)

QUEUE_MAX = 200             # больше — Telegram всё равно лежит; новое отбрасывается с записью в лог
NET_RETRIES = 3             # сеть/таймаут: столько попыток на одно сообщение
ALARM_WINDOW_S = 300        # тревоги одного вида — не чаще раза в 5 мин (со счётчиком повторов)
FAIL_ALARM_AT = 3           # подряд столько неудач — «Telegram недоступен» в статусе и логе
TG_TEXT_MAX = 4096          # лимит sendMessage после разбора сущностей


# --- чистые функции: экранирование, простой текст, резка ---------------------------------------------
def escape(v: Any) -> str:
    """HTML-экранирование динамической строки (&, <, >). None → пусто. Кавычки не трогаем: в атрибуты не пишем."""
    return html.escape("" if v is None else str(v), quote=False)


# Теги, которые понимает Telegram (parse_mode=HTML). Снимаем только их: «a < b» из текста ошибки остаётся текстом.
_TAG_RE = re.compile(r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|a|tg-spoiler|tg-emoji|span|blockquote)"
                     r"(?:\s[^<>]*)?>", re.I)


def to_plain(text: str) -> str:
    """HTML → простой текст для отката после 400: теги сняты, &lt; и прочие сущности раскрыты."""
    return html.unescape(_TAG_RE.sub("", text or ""))


# Белый список ссылок: только страница транзакции в обозревателе сети. Хэш — ровно 0x + 64 hex: ни кавычка, ни «>»,
# ни чужой домен в href не пройдут, даже если строка пришла из данных.
TX_URLS = {"bsc": "https://bscscan.com/tx/", "sol": "https://solscan.io/tx/", "solana": "https://solscan.io/tx/",
           "hyperliquid": "https://app.hyperliquid.xyz/explorer/tx/"}
_TX_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")
_SOL_SIG_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{64,88}")    # подпись Solana: base58 64 байт (регистр значим)
_TX_ID_RE = {"bsc": _TX_HASH_RE, "hyperliquid": _TX_HASH_RE, "sol": _SOL_SIG_RE, "solana": _SOL_SIG_RE}
_LINK_RE = re.compile(r'<a href="(?:https://bscscan\.com/tx/0x[0-9a-fA-F]{64}'
                      r'|https://app\.hyperliquid\.xyz/explorer/tx/0x[0-9a-fA-F]{64}'
                      r'|https://solscan\.io/tx/[1-9A-HJ-NP-Za-km-z]{64,88})">')
_PAIRED = ("b", "i", "u", "s", "code", "pre")


def tx_link(tx_hash: str | None, chain: str = "bsc", text: str | None = None) -> str:
    """<a href="https://bscscan.com/tx/0x…">текст</a> (Solana — solscan.io по подписи, Hyperliquid — обозреватель HL по
    хэшу 0x…); id не прошёл белый список сети или сеть неизвестна — простой текст (экранирован). Подпись по умолчанию —
    короткий id «0x4b5e…1764»."""
    h = str(tx_hash or "")
    label = escape(text if text is not None else (f"{h[:6]}…{h[-4:]}" if len(h) > 12 else h))
    c = str(chain or "")
    base, rx = TX_URLS.get(c), _TX_ID_RE.get(c)
    if base is None or rx is None or not rx.fullmatch(h):
        return label
    return f'<a href="{html.escape(base + h, quote=True)}">{label}</a>'


def html_ok(text: str) -> bool:
    """Готовый HTML безопасен для parse_mode=HTML: только наши теги, парные, ссылки — только tx_link(); любой «<» и
    «>» из данных экранирован."""
    s = text or ""
    links = len(_LINK_RE.findall(s))
    if links != s.count("</a>"):
        return False
    rest = re.sub(r"</?(?:" + "|".join(_PAIRED) + r")>", "", _LINK_RE.sub("", s).replace("</a>", ""))
    balanced = all(s.count(f"<{t}>") == s.count(f"</{t}>") for t in _PAIRED)
    return "<" not in rest and ">" not in rest and balanced


def u16len(s: str) -> int:
    """Длина в единицах UTF-16 — так Telegram меряет 4096 (эмодзи вне BMP = 2)."""
    return len((s or "").encode("utf-16-le")) // 2


def _cut_index(s: str, limit: int) -> int:
    """Сколько символов s влезает в limit единиц UTF-16, с отступом назад, чтобы не резать внутри
    сущности «&…;» или тега «<…>» (иначе оба куска получат 400) и по возможности — по пробелу."""
    n = 0
    cut = len(s)
    for i, ch in enumerate(s):
        n += 2 if ord(ch) > 0xFFFF else 1
        if n > limit:
            cut = i
            break
    else:
        return len(s)
    head = s[:cut]
    amp = head.rfind("&")
    if amp > 0 and ";" not in head[amp:] and cut - amp <= 12:
        cut = amp
    lt = s[:cut].rfind("<")
    if lt > 0 and ">" not in s[lt:cut] and cut - lt <= 256:
        cut = lt
    # парный тег (ссылка, жирный, код) не рвём между открытием и закрытием: иначе оба куска получат 400
    for tag in ("a", "b", "code"):
        head = s[:cut]
        op = max(head.rfind(f"<{tag}>"), head.rfind(f"<{tag} "))
        if 0 < op and head.rfind(f"</{tag}>") < op and cut - op <= 512:
            cut = op
    sp = s[:cut].rfind(" ")
    if sp > 0 and cut - sp <= 200 and s.rfind("<", 0, sp) <= s.rfind(">", 0, sp):    # пробел не внутри тега
        cut = sp + 1
    return max(cut, 1)


def split_text(text: str, limit: int = tconfig.TG_SPLIT_CHARS) -> list[str]:
    """Куски ≤ limit (UTF-16) по границам строк; строка длиннее limit режется внутри (см. _cut_index).
    Пустые куски выброшены: Telegram отвечает 400 на пустой текст. "\\n".join(куски) == text, если резать
    внутри строк не пришлось."""
    text = text or ""
    if u16len(text) <= limit:
        return [text] if text.strip() else []
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in text.split("\n"):
        pieces: list[str] = []
        rest = line
        while u16len(rest) > limit:
            k = _cut_index(rest, limit)
            pieces.append(rest[:k])
            rest = rest[k:]
        pieces.append(rest)
        for p in pieces:
            pl = u16len(p)
            add = pl + (1 if cur else 0)
            if cur and cur_len + add > limit:
                chunks.append("\n".join(cur))
                cur, cur_len, add = [], 0, pl
            cur.append(p)
            cur_len += add
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()]


def fit_one(text: str, limit: int = tconfig.TG_SPLIT_CHARS) -> str:
    """Для правки (её не разрежешь на два сообщения): первый кусок + «…»."""
    if u16len(text) <= limit:
        return text
    first = split_text(text, limit - 2)
    return (first[0] if first else "") + "\n…"


# --- очередь -----------------------------------------------------------------------------------------
@dataclass
class Job:
    kind: str                                   # send | edit
    chat_id: int
    text: str
    html: bool = True
    reply_markup: dict | None = None
    silent: bool = False
    message_id: int | None = None
    on_done: Callable[[dict | None], None] | None = None   # в потоке отправителя; None — не доставлено


_FAILED = object()


class Sender:
    """Один рабочий поток. Производители (опрос, задания, исполнитель) только кладут задания.

    on_done(message) вызывается в потоке отправителя с объектом Message последнего куска (у него кнопки —
    из него message_id плана для intents.msg_id) или с None, если доставить не удалось. Своё соединение с БД
    там — забота вызывающего (одно соединение на поток)."""

    def __init__(self, api, *, gap_s: float = tconfig.TG_SEND_GAP_S, edit_min_s: float = tconfig.TG_EDIT_MIN_S,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Any] | None = None,
                 qmax: int = QUEUE_MAX, alarm_window_s: float = ALARM_WINDOW_S):
        self.api = api
        self.gap_s = gap_s
        self.edit_min_s = edit_min_s
        self.alarm_window_s = alarm_window_s
        self._clock = clock
        self._q: queue.Queue[Job] = queue.Queue(maxsize=qmax)
        self._stop = threading.Event()
        self._abort = threading.Event()          # жёсткое закрытие: сон прерывается, недоставленное бросается
        self._sleep = sleep or self._abort.wait
        self._lock = threading.Lock()
        self._edits: dict[tuple[int, int], Job] = {}
        self._last_edit: dict[tuple[int, int], float] = {}
        self._alarms: dict[str, list] = {}       # вид → [t0, повторов, chat_id]
        self._last_call: float | None = None
        self._thread: threading.Thread | None = None
        # счётчики для «статус» и tg_health.json
        self.sent = 0
        self.dropped = 0
        self.retries_429 = 0
        self.fails = 0                           # подряд
        self.last_ok: float | None = None        # time.time() последней успешной доставки

    # --- производители -------------------------------------------------------------------------------
    def send(self, chat_id: int, text: str, *, html: bool = True, reply_markup: dict | None = None,
             silent: bool = False, on_done: Callable[[dict | None], None] | None = None) -> bool:
        """Новое сообщение (с уведомлением, если не silent). False — очередь полна, сообщение не принято."""
        return self._put(Job("send", int(chat_id), text, html, reply_markup, silent, None, on_done))

    def edit(self, chat_id: int, message_id: int, text: str, *, reply_markup: dict | None = None, html: bool = True,
             throttle: bool = False, on_done: Callable[[dict | None], None] | None = None) -> bool:
        """Правка сообщения. reply_markup=None снимает кнопки. throttle=True — прогресс: не чаще edit_min_s,
        ждущий текст заменяется более новым. Обычная правка (закрыть план) вытесняет ждущий прогресс."""
        job = Job("edit", int(chat_id), text, html, reply_markup, False, int(message_id), on_done)
        key = (job.chat_id, job.message_id)
        with self._lock:
            if throttle:
                self._edits[key] = job
                return True
            self._edits.pop(key, None)
        return self._put(job)

    def alarm(self, chat_id: int, text: str, kind: str) -> bool:
        """Тревога инфраструктуры: первая за окно уходит, повторы того же вида считаются и приходят сводкой."""
        k = _alarm_kind(kind)
        now = self._clock()
        with self._lock:
            st = self._alarms.get(k)
            if st is not None and now - st[0] < self.alarm_window_s:
                st[1] += 1
                return False
            extra = st[1] if st is not None else 0
            self._alarms[k] = [now, 0, int(chat_id)]
        if extra:
            text = f"{text}\n{_repeats(extra, self.alarm_window_s)}"
        return self.send(chat_id, text)

    def _put(self, job: Job) -> bool:
        if self._abort.is_set():
            return False
        try:
            self._q.put_nowait(job)
            return True
        except queue.Full:
            self.dropped += 1
            log.error("tg: очередь отправки полна (%d) — сообщение отброшено: %s", self._q.maxsize,
                      redact(to_plain(job.text)[:80]))
            return False

    @property
    def qsize(self) -> int:
        return self._q.qsize()

    @property
    def healthy(self) -> bool:
        return self.fails < FAIL_ALARM_AT

    # --- поток -------------------------------------------------------------------------------------------
    def start(self) -> "Sender":
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self.run, name="tg-sender", daemon=True)
            self._thread.start()
        return self

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run(self) -> None:
        while True:
            if self._stop.is_set() and self._q.empty():
                self._flush_edits(force=True)
                break
            try:
                job = self._q.get(timeout=0.2)
            except queue.Empty:
                job = None
            try:
                if job is not None:
                    self._deliver(job)
                self._flush_edits(force=self._stop.is_set())
                self._flush_alarms()
            except Exception as e:                   # поток отправки не должен умереть от одного сообщения
                log.error("tg: отправитель: %s", redact(e))

    def close(self, timeout: float = 10.0) -> int:
        """SIGTERM: дослать очередь (в т.ч. «⏹ служба останавливается») за timeout, потом бросить остаток.
        Возвращает число недоставленных заданий."""
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout)
            if t.is_alive():
                self._abort.set()
                t.join(2.0)
        left = self._q.qsize() + len(self._edits)
        if left:
            log.error("tg: при закрытии не доставлено %d сообщений", left)
        return left

    def drain(self, force_edits: bool = True) -> None:
        """Синхронно разобрать очередь в текущем потоке (тесты; поток при этом не запущен)."""
        self._flush_alarms()                     # сводка повторов встаёт в очередь и уходит в этом же проходе
        while True:
            try:
                job = self._q.get_nowait()
            except queue.Empty:
                break
            self._deliver(job)
        self._flush_edits(force=force_edits)

    # --- доставка ----------------------------------------------------------------------------------------
    def _deliver(self, job: Job) -> None:
        res: Any = _FAILED
        if job.kind == "send":
            chunks = split_text(job.text)
            if not chunks:
                log.warning("tg: пустое сообщение не отправляется")
            for i, chunk in enumerate(chunks):
                markup = job.reply_markup if i == len(chunks) - 1 else None
                res = self._call(lambda h, t, m=markup: self.api.send_message(job.chat_id, t, html=h, reply_markup=m,
                                                                             silent=job.silent), chunk, job.html)
                if res is _FAILED:
                    break
        else:
            res = self._call(lambda h, t: self.api.edit_message_text(job.chat_id, job.message_id, t, html=h,
                                                                     reply_markup=job.reply_markup),
                             fit_one(job.text), job.html)
            with self._lock:
                self._last_edit[(job.chat_id, job.message_id)] = self._clock()
        if job.on_done is not None:
            try:
                # успех без объекта Message («not modified», True у правки) — пустой dict, а не None (= не доставлено)
                job.on_done(None if res is _FAILED else (res if isinstance(res, dict) else {}))
            except Exception as e:
                log.error("tg: on_done: %s", redact(e))

    def _pace(self) -> None:
        if self._last_call is not None:
            wait = self._last_call + self.gap_s - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def _call(self, fn: Callable[[bool, str], Any], text: str, use_html: bool) -> Any:
        net = 0
        while True:
            if self._abort.is_set():
                return self._fail("закрытие", text)
            self._pace()
            try:
                r = fn(use_html, text)
            except TgRetryAfter as e:
                self.retries_429 += 1
                log.warning("tg: 429, жду %d с и повторяю то же сообщение", e.seconds + 1)
                self._sleep(e.seconds + 1)
                continue
            except TgBadRequest as e:
                s = str(e).lower()
                if "not modified" in s:
                    return self._ok(None)            # правка тем же текстом — уже доставлено
                if use_html:
                    log.warning("tg: 400 на HTML (%s) — повтор простым текстом", redact(e))
                    use_html, text = False, to_plain(text)
                    continue
                return self._fail(redact(e), text)
            except TgForbidden as e:
                return self._fail(f"403, бот заблокирован: {redact(e)}", text)
            except TgNetwork as e:
                net += 1
                if net >= NET_RETRIES:
                    return self._fail(redact(e), text)
                self._sleep(2 ** net)
                continue
            except TgError as e:
                return self._fail(redact(e), text)
            except Exception as e:
                return self._fail(redact(e), text)
            return self._ok(r)

    def _ok(self, r: Any) -> Any:
        self.sent += 1
        self.fails = 0
        self.last_ok = time.time()
        return r

    def _fail(self, why: str, text: str) -> Any:
        self.dropped += 1
        self.fails += 1
        lvl = logging.ERROR if self.fails >= FAIL_ALARM_AT else logging.WARNING
        log.log(lvl, "tg: не доставлено (%s; подряд %d): %s", why, self.fails, redact(to_plain(text)[:120]))
        return _FAILED

    def _flush_edits(self, force: bool = False) -> None:
        now = self._clock()
        with self._lock:
            due = [k for k in self._edits
                   if force or now - self._last_edit.get(k, float("-inf")) >= self.edit_min_s]
            jobs = [self._edits.pop(k) for k in due]
        for job in jobs:
            self._deliver(job)

    def _flush_alarms(self) -> None:
        now = self._clock()
        out = []
        with self._lock:
            for k, st in list(self._alarms.items()):
                if now - st[0] >= self.alarm_window_s:
                    if st[1] > 0:
                        out.append((st[2], k, st[1]))
                    del self._alarms[k]
        for chat, k, n in out:
            self.send(chat, f"⚠️ {escape(ALARM_LABELS.get(k, k))} — {_repeats(n, self.alarm_window_s)}")


# Вид тревоги — внутренний ключ; владельцу — по-русски (ключ «conflict» ему ничего не говорит)
ALARM_LABELS = {"conflict": "Telegram 409", "webhook": "Вебхук на токене", "tg_watchdog": "Обрывы Telegram"}


def _repeats(n: int, window_s: float) -> str:
    """«ещё 3 раза за 5 мин» — сводка повторов одной тревоги."""
    n = int(n)
    word = "раз" if (n % 10 in (0, 1) or n % 10 >= 5 or 11 <= n % 100 <= 14) else "раза"
    return f"ещё {n} {word} за {int(window_s // 60)} мин"


def _alarm_kind(kind: str) -> str:
    return re.sub(r"\d+", "#", str(kind or "?"))[:80]
