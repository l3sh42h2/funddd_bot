"""Перп-нога Gate USDT-фьючерсов: подпись HMAC-SHA512 REST API v4, PerpLeg поверх requests (trade_spec §4).
Открыта сделкой FATCOIN: спот OKX DEX (сеть Robinhood, OKX chainIndex 4663) × шорт GATE FATCOIN_USDT.

Только USDT-margined фьючерсы (settle="usdt", SETTLE ниже) — сделка владельца не просит других расчётных валют.

Подпись (документация Gate API v4, «Authentication»; сверено 13.09 поиском по офиц. докам и исходникам
gateapi-python/-go — воркед-примера с готовым числовым сигнатурным вектором в самой документации нет, поэтому
тесты проверяют модуль НЕЗАВИСИМЫМ расчётом HMAC-SHA512 по формуле доков, а не готовым числом «из примера»):
  sign_string = METHOD + "\n" + "/api/v4" + PATH + "\n" + QUERY + "\n" + hex(sha512(BODY)) + "\n" + TIMESTAMP
  SIGN = hex(HMAC_SHA512(secret, sign_string)); заголовки KEY (ключ), Timestamp (unix-секунды), SIGN.
PATH — путь БЕЗ хоста и БЕЗ префикса /api/v4 в URL (он у нас уже в base), но В СТРОКУ ПОДПИСИ префикс /api/v4
дописывается — это давало нам большинство ошибок «Signature mismatch» у других интеграторов Gate [D]. QUERY —
ровно та query-string, что уходит в URL (urlencode, порядок вставки). BODY — сырые байты JSON, что реально
отправлены (пусто → sha512("")); переподписывать переупорядоченный dict нельзя — байты должны совпасть 1-в-1,
как и в Aster (aster_trade.py).

В отличие от Aster (микросекундный монотонный nonce, ворота на каждый вызов): у Gate нет отдельного nonce —
Timestamp это ЦЕЛЫЕ unix-секунды, сервер допускает расхождение до 60 с [D, поиск по докам] и НЕ требует
строгого возрастания между вызовами (два запроса в одну и ту же секунду — не «дубликат», в отличие от Aster).
Из этого следует: sign_nonce в PerpFill здесь — метка времени подписи (секунды), а не уникальный счётчик;
это слабее гарантии Aster (нет доказательства «эта заявка подписана раньше той»), но так уж работает Gate —
не наша выдумка. Ворота режима — как у Aster: call() спрашивает owner.toml/паузу на КАЖДЫЙ вызов, ДО подписи.

Один клиент = один контракт за раз (сделка = одна пара нога-DEX/нога-перп): вместо коллекторского перебора всех
981 контрактов (gate_fut.py, для дашборда) здесь используется точечный GET .../contracts/{contract} — дешевле
и не требует держать в памяти весь список ради одной монеты.

IOC: POST /futures/usdt/orders, tif=ioc, size = ±контракты (отрицательный — шорт, [D] доки: «Positive for buy,
negative for sell»), text = "t-" + client_id (документация: префикс "t-", ≤28 байт после него, символы
0-9A-Za-z_-.; наш CID_RE). ОДНА попытка отправки без повтора — как у Aster (неизвестный исход выясняется
запросом, а не второй отправкой, чтобы не удвоить позицию).

Округление цены к тику (order_price_round) — задача этого модуля, а НЕ отказ (в отличие от Aster, который
считает несовпадение тика ошибкой вызывающего): SELL — вверх (ceil), BUY — вниз (floor). Оба направления не
делают исполнение хуже относительно исходного px_cap планировщика: SELL с более высокой ценой продать не легче,
но никогда не продаст дешевле исходного кэпа; BUY с более низкой ценой купить не легче, но никогда не купит
дороже исходного кэпа — биржа либо не исполнит, либо исполнит не хуже запрошенного. Количество (контракты)
ВСЕГДА целое — Gate size:int — round не делаем, не то количество - отказ (ValueError), как у Aster.

GET по text-id ограничен окном: «Operations based on custom ID can only be checked when the order is in the
orderbook or within 60 seconds after the order ends; after that, only the order ID is accepted» [D, поиск по
докам gate.com]. settle_unknown() поэтому не доверяет «не найдена по text» безоговорочно позже TEXT_ID_SAFE_MS
после отправки — это тоньше, чем у Aster (там -2013 не протухает по времени), и намеренно строже: без числового
order_id (мы теряем его при обрыве связи ДО получения ответа) поздний «404» ничего не доказывает.

dual_mode (режим двух позиций) НИКОГДА не включается этим кодом ни при каких условиях — только проверяется и,
если включён, ОДИН раз пытаемся его аккуратно выключить (Gate: переключение требует отсутствия открытых позиций
и заявок [D, поиск по исходникам gateapi-go] — если не получилось, отказ явно просит владельца переключить
Futures → Settings → Position Mode → Single Position вручную, ничего не отправляем дальше). Плечо — ТОЛЬКО
изолированное (leverage>0): в owner.toml плечо для Gate — обязательный ключ (perp.gate.leverage), пусто = отказ
(owner.py §_PERP, как у всех площадок); CROSSED этот модуль не поддерживает (owner 07.09: «новые шорты 1x
изолированно» — политика владельца, не наше решение).

instrument(): m (units_per_contract, InstrumentSpec фазы 1) берём из quanto_multiplier контракта НАПРЯМУЮ —
это явное числовое поле API, а не эвристика по имени символа (в отличие от Aster, где baseAsset — единственный
сигнал). Коллекторская нормализация имени/фактора gate_fut.py (perp_base, CANON, FACTOR_OVERRIDE, MBABYDOGE и
т.п.) решает ДРУГУЮ задачу — сведение идентичности индексов для дашборда по ИМЕНИ монеты; здесь она не годится
и не применяется: m обязан быть тем числом, что реально умножает размер контракта на бирже.

Ключи — GATE_API_KEY/GATE_API_SECRET окружения (.env), читаются ТОЛЬКО этим модулем (в отличие от EVM/Aster —
там единственное место trade/keys.py; здесь другая модель авторизации — HMAC api-key/secret, а не EOA-подпись,
поэтому keys.py её не касается). Секрет никогда не логируется и не печатается (обёртка _Secret ниже, как
keys.SignerKey у Aster); при загрузке регистрируется в общем реестре редактирования keys.py (_remember_secret),
чтобы работал тот же redact()/redact_secrets(), что и для Aster/EVM ключей — единый механизм на проект, а не
второй параллельный. API KEY (не секрет, публичный идентификатор аккаунта, как aster_user) печатается как есть.

Оценка модуля Gate (12.09, тестировщик funding_bot): рейтинг C.
"""
from __future__ import annotations
import math, hashlib, hmac, json, logging, os, re, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable
from urllib.parse import quote as _urlquote
import requests
from .. import config
from .aster_trade import ceil_step, dec_str, floor_step, fmt_param   # чистые Decimal-хелперы, венда-независимые
from .keys import ModeForbidden, _remember_secret, gate as mode_gate
from ..client import BannedError, BudgetExceeded
from ..symbols import norm_symbol_factor
from .types import Book, Filters, PerpFill, PerpInstrument

log = logging.getLogger(__name__)
D = Decimal
_D0 = Decimal(0)

VENUE = "gate"
SETTLE = "usdt"
GATE_BASE = "https://api.gateio.ws/api/v4"          # тот же хост, что у spot.GateSpot/gate_fut.GateFut (коллектор)
SIGN_PREFIX = "/api/v4"                              # входит в строку подписи, но не в URL (он уже в GATE_BASE)
GATE_KEY_ENV = "GATE_API_KEY"
GATE_SECRET_ENV = "GATE_API_SECRET"

SIGNED_TIMEOUT = (5, 10)             # (connect, read): дольше — исход неизвестен, выясняем запросом
CLOCK_SKEW_MAX_S = 5.0               # наш запас; сервер по докам допускает Timestamp ±60 с [D]
RATE_SOFT_READ = 0.70                # как config.WEIGHT_SOFT_LIMIT — общий порог проекта
RATE_SOFT_ORDER = 0.95               # путь заявки (ioc/query/position/settle) — до 95 %
RATE_MIN_PAUSE_S = 1.0
RATE_MAX_PAUSE_S = 60.0              # потолок паузы после 429 при любых единицах заголовка (ревью Fable 14.09, P1-2)
FILTERS_TTL_S = 3600
CID_RE = re.compile(r"^[0-9A-Za-z_.\-]{1,28}$")      # доки: text — "t-" + ≤28 байт, 0-9A-Za-z_-.
TEXT_PREFIX = "t-"
PAGE_LIMIT = 1000
MAX_PAGES = 10
UNKNOWN_QUERIES = 3
UNKNOWN_SPAN_S = 5.0
TEXT_ID_WINDOW_S = 60                # доки: text-id ищется в стакане/≤60 с после финиша [D]
TEXT_ID_SAFE_MS = 45_000             # наш запас под RTT/дрейф — меньше документированных 60 с
ORDERS_PAGE = 100                    # страница списка завершённых заявок (settle_unknown: поиск по text)
TRADES_SLACK_MS = 5_000              # окно settle_unknown вокруг since_ms — как TRADES_SLACK_MS у Aster

# доки/поиск 13.09: полный список finish_as — filled/cancelled/liquidated/ioc/auto_deleveraged/reduce_only/
# position_closed/reduce_out/stp [D]. Какие из них считать REJECTED (заявка не дошла до площадки как задумано,
# а не «частично исполнена, остаток снят») — единственно однозначно НАШ случай: cancelled/reduce_only/
# position_closed/reduce_out/stp — заявку сняли ДО (или вместо) исполнения; liquidated/auto_deleveraged здесь
# в принципе не должны прийти для нового IOC (это исходы существовавших заявок при ликвидации/ADL) — UNKNOWN,
# если такое всё же пришло: разбираться руками, а не молча классифицировать.
REJECT_FINISH = frozenset({"cancelled", "reduce_only", "position_closed", "reduce_out", "stp"})
FINAL = frozenset({"FILLED", "PARTIALLY_FILLED", "EXPIRED", "REJECTED"})
NOT_FOUND_LABEL = "ORDER_NOT_FOUND"
# [A] предположение — какие label считать «площадка сама не справилась, исход не известен» (аналог -1006/-1007/5xx
# у Aster). Списка таких меток нет ни в одном найденном источнике доки/SDK 13.09 — списка живых ответов тоже нет
# (ключей нет). Пересмотреть по первым живым отказам (см. итоговое сообщение — «ждёт живых данных»).
SERVER_LABELS = frozenset({"SERVER_ERROR", "INTERNAL", "TOO_BUSY", "SERVICE_UNAVAILABLE"})


# --- ошибки ---------------------------------------------------------------------------------------
class GateError(RuntimeError):
    """Отказ ноги Gate (разбор ответа, фильтры, несоответствие)."""


class GateApiError(GateError):
    """Площадка ответила кодом ошибки: http, label (строка Gate, не число — в отличие от Aster), msg."""

    def __init__(self, what: str, http: int, label: str | None, msg: str):
        self.http, self.label, self.msg = http, label, msg
        super().__init__(f"{what}: HTTP {http} label {label} {msg}"[:300])


class GateNetError(GateError):
    """Ответа нет (таймаут, обрыв). Для отправки — исход НЕИЗВЕСТЕН; ts — метка времени подписи (для settle_unknown)."""

    def __init__(self, method: str, path: str, ts: int, cause: BaseException):
        self.method, self.path, self.ts = method, path, ts
        super().__init__(f"{method} {path}: нет ответа ({type(cause).__name__})")


# --- секрет: не печатать -----------------------------------------------------------------------
class _Secret:
    """Обёртка GATE_API_SECRET — как keys.SignerKey у Aster: печатается как <secret>, значение отдаёт только reveal()."""
    __slots__ = ("_v",)

    def __init__(self, v: str):
        self._v = v

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "<secret>"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "<secret>"


# --- числа -----------------------------------------------------------------------------------------
def _d(x: Any) -> Decimal:
    if isinstance(x, bool) or x is None:
        raise GateError(f"не число: {x!r}")
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError):
        raise GateError(f"не число: {x!r}") from None


def _reset_pause(header: Any, now_s: float) -> float:
    """Пауза после 429 по X-Gate-RateLimit-Reset-Timestamp. Единицам не доверяем (секунды или мс: больше 10¹¹ —
    мс) и пауза всегда в [RATE_MIN_PAUSE_S, RATE_MAX_PAUSE_S]: иначе один 429 с заголовком в мс запрещал бы все
    вызовы Gate — и хедж уже купленной ноги — до перезапуска службы (ревью Fable 14.09, P1-2)."""
    try:
        v = float(header)
    except (TypeError, ValueError):
        return RATE_MIN_PAUSE_S
    if not math.isfinite(v):
        return RATE_MIN_PAUSE_S
    if v > 1e11:
        v /= 1000.0
    return min(max(v - now_s + 1.0, RATE_MIN_PAUSE_S), RATE_MAX_PAUSE_S)


def _ms(x: Any) -> int:
    """Секунды (Decimal/str/float/int) → целые миллисекунды, округление к ближайшему (доли секунды у create_time)."""
    return int((_d(x) * 1000).to_integral_value(ROUND_HALF_UP))


def _label(body: Any) -> str | None:
    return body.get("label") if isinstance(body, dict) else None


def _msg(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("message") or body.get("_raw") or "")[:200]
    return ""


def _is_error(status: int) -> bool:
    return status >= 400            # Gate: успех всегда 2xx, ошибка — код + {"label","message"} (доки/поиск 13.09)


def _query_string(params: dict) -> str:
    """Query-string КАК ОНА УЙДЁТ на биржу — участвует и в подписи, и в URL, байт в байт (urlencode, порядок вставки)."""
    pairs = [(str(k), fmt_param(v)) for k, v in params.items() if v is not None]
    return "&".join(f"{_urlquote(k, safe='')}={_urlquote(v, safe='')}" for k, v in pairs)


_EMPTY_BODY_HASH = hashlib.sha512(b"").hexdigest()


def build_sign_string(method: str, path: str, query: str, body: bytes, ts: int) -> str:
    """METHOD\\n/api/v4+PATH\\nQUERY\\nhex(sha512(BODY))\\nTIMESTAMP — ровно формула доков Gate (см. докстринг файла)."""
    body_hash = hashlib.sha512(body).hexdigest() if body else _EMPTY_BODY_HASH
    return f"{method.upper()}\n{SIGN_PREFIX}{path}\n{query}\n{body_hash}\n{ts}"


def sign(secret: str, s: str) -> str:
    return hmac.new(secret.encode("utf-8"), s.encode("utf-8"), hashlib.sha512).hexdigest()


def load_env_keys(environ=None) -> tuple[str, str]:
    """GATE_API_KEY/GATE_API_SECRET из окружения — единственное место, где этот модуль их читает. Регистрация в
    общем реестре редактирования (trade/keys.py) происходит в GateTrade.__init__ (для любого способа создания —
    и from_env, и прямого конструктора с реальным секретом), здесь — только чтение и проверка непустоты."""
    env = os.environ if environ is None else environ
    key = (env.get(GATE_KEY_ENV) or "").strip()
    secret = (env.get(GATE_SECRET_ENV) or "").strip()
    if not key or not secret:
        raise GateError(f"нет {GATE_KEY_ENV}/{GATE_SECRET_ENV} в окружении (.env)")
    return key, secret


def _pf(cid: str, status: str, *, order_id: int | None = None, qty: Decimal = _D0, avg: Decimal = _D0,
        quote: Decimal = _D0, nonce: int = 0, code: str | None = None) -> PerpFill:
    """code — СТРОКА (label Gate), а не число: PerpFill.err_code типизирован int|None (общий для площадок в
    types.py, который эта задача не трогает — типы не проверяются в рантайме). Разбор err_code венда-специфичен
    уже сейчас (у Aster — int, у нас — str); интеграция фазы 2 должна принять это в types.py (см. итоговое
    сообщение — «ждёт интеграции»), а не считать, что err_code всегда int."""
    return PerpFill(client_id=cid, order_id=order_id, status=status, qty=qty, avg_px=avg, quote=quote,
                    sign_nonce=nonce, err_code=code)


def order_to_fill(cid: str, body: dict, ts: int, m: Decimal = D(1)) -> PerpFill:
    """Ответ FuturesOrder → PerpFill. Правило — как у Aster: реально исполненное количество (size−left) решает
    статус ПЕРВЫМ, finish_as — только когда исполнения нет (0 < |size|−|left| никогда не даёт REJECTED/EXPIRED —
    это всегда FILLED/PARTIALLY_FILLED, даже если finish_as неожиданный).

    m (quanto_multiplier) — ИСПРАВЛЕНО (GATE-2/GATE-4, 13.09): fill_price, который отдаёт Gate, это цена ОДНОГО
    ТОКЕНА (проверено живым снимком книги/фандинга — см. gate_trade.py:funding/book), а соглашение проекта
    (planner.py basis_bps, engine.py px_tok — см. докстринг book()/funding() ниже) — цена КОНТРАКТА (цена·m),
    как её и так уже отдаёт Aster (avgPrice за «пачку» m токенов). Раньше avg_px/quote копировали fill_price как
    есть без домножения — PerpFill.quote (→ store.perp_orders.cum_quote/clips.perp_quote) был в m раз МЕНЬШЕ
    настоящего нотионала (1000 контрактов по 0.00167 при m=100 давали quote=1.67 вместо 167 USDT), а avg_px не
    совпадал по масштабу с book()/funding().mark, которые эта же задача переводит в цену контракта ниже."""
    try:
        oid = int(body["id"]) if body.get("id") is not None else None
        size, left = _d(body.get("size", 0)), _d(body.get("left", 0))
    except (GateError, TypeError, ValueError, KeyError):
        return _pf(cid, "UNKNOWN", nonce=ts)
    text = str(body.get("text") or "")
    if oid is None or text not in (f"{TEXT_PREFIX}{cid}", ""):   # чужая/безымянная заявка — не доверяем (как Aster)
        return _pf(cid, "UNKNOWN", nonce=ts)
    filled = abs(size) - abs(left)
    if filled < 0:
        return _pf(cid, "UNKNOWN", nonce=ts)                     # мусор: left больше size
    avg = _d(body.get("fill_price") or 0) * m                    # цена контракта = цена токена · m (GATE-2/GATE-4)
    quote_amt = avg * filled if filled else _D0                  # = fill_price·m·filled — настоящий USDT-нотионал
    status_raw = str(body.get("status") or "")
    if status_raw != "finished":
        return _pf(cid, "UNKNOWN", order_id=oid, qty=filled, avg=avg, quote=quote_amt, nonce=ts)   # open — не финал
    finish_as = str(body.get("finish_as") or "")
    if filled > 0:
        out, code = ("FILLED" if left == 0 else "PARTIALLY_FILLED"), None
    elif finish_as == "ioc":
        out, code = "EXPIRED", None
    elif finish_as in REJECT_FINISH:
        out, code = "REJECTED", finish_as
    else:
        out, code = "UNKNOWN", finish_as or None
    return _pf(cid, out, order_id=oid, qty=filled, avg=avg, quote=quote_amt, nonce=ts, code=code)


# --- нога -------------------------------------------------------------------------------------------
class GateTrade:
    """PerpLeg для Gate (USDT-фьючерсы). Публичное — без ключей и без ворот режима (как у Aster); подписанное —
    через call() с воротами mode_state() на КАЖДЫЙ вызов."""
    venue = VENUE
    ioc_partial_terminal = True  # only status=finished produces native PARTIALLY_FILLED
    SETTLE = SETTLE
    BASE = GATE_BASE

    def __init__(self, key: str | None = None, secret: str | None = None, *,
                 mode_state: Callable[[], tuple[str | None, bool]] | None = None, session=None,
                 base: str | None = None, timeout=SIGNED_TIMEOUT, now: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, public_retries: int = 2):
        self.base = (base or self.BASE).rstrip("/")
        self._s = session or requests.Session()
        self._s.headers["user-agent"] = config.USER_AGENT
        self._key = key
        self._secret = _Secret(secret) if secret is not None else None
        if secret:      # редактирование логов — сразу при создании, а не только у from_env (единый реестр keys.py)
            _remember_secret(secret)
            _remember_secret(secret.lower())
        self._mode_state = mode_state or (lambda: ("dry", False))
        self.timeout = timeout
        self._now, self._sleep = now, sleep
        self.public_retries = public_retries
        self.used = 0
        self.budget = 0.0
        self.last_ok_ts = 0.0
        self.n_429 = 0
        self.n_err = 0
        self.banned_until = 0.0
        self.backoff_until = 0.0
        self.last_error: str | None = None
        self._filters: dict[str, tuple[float, Filters]] = {}
        self._meta: dict[str, dict] = {}       # сырой Contract — для instrument()/status()/quanto_multiplier
        self._status: dict[str, str] = {}

    @classmethod
    def from_env(cls, mode_state: Callable[[], tuple[str | None, bool]], environ=None, **kw) -> "GateTrade":
        key, secret = load_env_keys(environ)
        return cls(key, secret, mode_state=mode_state, **kw)

    def __repr__(self) -> str:
        return f"GateTrade(key={self._key!r}, secret={self._secret!r})"

    # --- ворота, бюджет, HTTP --------------------------------------------------------------------
    def _gate(self, action: str, hedge: bool) -> None:
        try:
            mode, paused = self._mode_state()
        except Exception as e:
            raise ModeForbidden(f"режим/пауза не прочитаны ({type(e).__name__}): {action} запрещено") from None
        mode_gate(mode, action, paused=bool(paused), hedge=hedge)

    def _check_budget(self, path: str, critical: bool) -> None:
        now = self._now()
        if now < self.banned_until:
            raise BannedError(f"gate: бан ещё {self.banned_until - now:.0f} с, {path} не отправлен")
        if now < self.backoff_until:
            raise BudgetExceeded(f"gate: пауза после 429 ещё {self.backoff_until - now:.1f} с, {path} не отправлен")
        soft = RATE_SOFT_ORDER if critical else RATE_SOFT_READ
        if self.budget >= soft:
            raise BudgetExceeded(f"gate: доля лимита {self.budget:.0%} ≥ {soft:.0%}, {path} не отправлен")

    def _read_usage(self, r) -> None:
        try:
            remain = int(r.headers.get("x-gate-ratelimit-requests-remain"))
            limit = int(r.headers.get("x-gate-ratelimit-limit"))
            self.used = max(limit - remain, 0)
            self.budget = self.used / limit if limit else 0.0
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    def _absorb(self, r) -> tuple[int, Any]:
        self._read_usage(r)
        status = int(r.status_code)
        if status == 429:
            self.n_429 += 1
            now = self._now()
            self.backoff_until = now + _reset_pause(r.headers.get("x-gate-ratelimit-reset-timestamp"), now)
        try:
            body = json.loads(r.content, parse_float=Decimal) if r.content else {}
        except ValueError:
            body = {"_raw": (r.text or "")[:200]}
        if status < 400:
            self.last_ok_ts = self._now()
        else:
            self.n_err += 1
        return status, body

    def call(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
             hedge: bool = False, critical: bool = False,
             on_signed: Callable[[int], None] | None = None) -> tuple[int, Any, int]:
        """Подписанный вызов → (HTTP-статус, JSON, ts подписи). Ошибки площадки НЕ бросает — разбирает вызывающий.
        Бросает до сети: ModeForbidden (ворота), BannedError/BudgetExceeded (бюджет); после подписи: GateNetError.
        on_signed(ts) — после подписи и ДО отправки (запись-до, как у Aster; ts здесь — секунды, не уникальный nonce
        — см. докстринг файла)."""
        m = method.upper()
        self._gate("signed_read" if m == "GET" else "send", hedge)
        if self._key is None or self._secret is None:
            raise ModeForbidden("нет ключей Gate (GATE_API_KEY/GATE_API_SECRET не загружены): подписанный вызов запрещён")
        self._check_budget(path, critical)
        query = _query_string(params or {})
        body_bytes = b"" if not body else json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ts = int(self._now())
        s = build_sign_string(m, path, query, body_bytes, ts)
        sig = sign(self._secret.reveal(), s)
        headers = {"KEY": self._key, "Timestamp": str(ts), "SIGN": sig, "Accept": "application/json"}
        if body_bytes:
            headers["Content-Type"] = "application/json"
        if on_signed is not None:
            on_signed(ts)
        url = self.base + path + (f"?{query}" if query else "")
        try:
            r = self._s.request(m, url, data=body_bytes or None, headers=headers, timeout=self.timeout)
        except Exception as e:       # noqa: сеть/таймаут/обрыв — после подписи исход отправки неизвестен
            self.n_err += 1
            raise GateNetError(m, path, ts, e) from None
        status, parsed = self._absorb(r)
        return status, parsed, ts

    def _signed_ok(self, method: str, path: str, params: dict | None = None, what: str = "", **kw) -> Any:
        st, body, _ = self.call(method, path, params=params, **kw)
        if _is_error(st):
            raise GateApiError(what or f"{method} {path}", st, _label(body), _msg(body))
        return body

    def _public(self, path: str, params: dict | None = None, retries: int | None = None) -> Any:
        """Публичный GET — БЕЗ ворот режима (доступен в dry), но с той же проверкой лимита/бана, что и подписанные
        чтения: лимит Gate (200/10с) общий на IP для всех эндпоинтов площадки, включая публичные — отдельного
        бюджета для них нет."""
        retries = self.public_retries if retries is None else retries
        now = self._now()
        if now < self.banned_until:
            raise BannedError(f"gate: пауза после 429 ещё {self.banned_until - now:.0f} с, {path} не отправлен")
        if now < self.backoff_until:
            raise BudgetExceeded(f"gate: пауза после 429 ещё {self.backoff_until - now:.1f} с, {path} не отправлен")
        if self.budget >= RATE_SOFT_READ:
            raise BudgetExceeded(f"gate: доля лимита {self.budget:.0%} ≥ {RATE_SOFT_READ:.0%}, {path} не отправлен")
        query = _query_string(params or {})
        url = self.base + path + (f"?{query}" if query else "")
        last: BaseException | None = None
        for i in range(retries):
            try:
                r = self._s.get(url, timeout=self.timeout[1] if isinstance(self.timeout, tuple) else self.timeout)
            except Exception as e:   # noqa: сеть/5xx — повторяем (публичное чтение идемпотентно)
                last = e; self.n_err += 1
                if i + 1 < retries:
                    self._sleep(1.0 * (i + 1)); continue
                raise GateNetError("GET", path, 0, e) from None
            status, body = self._absorb(r)
            if status == 429:
                if i + 1 < retries:
                    self._sleep(max(self.backoff_until - self._now(), RATE_MIN_PAUSE_S)); continue
                raise BannedError(f"gate: 429 на {path}, пауза до {self.backoff_until:.0f}")
            if _is_error(status):
                raise GateApiError(path, status, _label(body), _msg(body))
            return body
        raise GateNetError("GET", path, 0, last or RuntimeError("public retries exhausted"))

    # --- инструмент (точечный GET, не весь список) -------------------------------------------------
    def _load_contract(self, symbol: str) -> dict:
        c = self._public(f"/futures/{self.SETTLE}/contracts/{_urlquote(symbol, safe='')}")
        if not isinstance(c, dict) or not c.get("name"):
            raise GateError(f"{symbol}: контракт не найден на Gate")
        return c

    @staticmethod
    def _parse_filters(c: dict) -> Filters:
        # tick — ИСПРАВЛЕНО (GATE-4, 13.09): order_price_round Gate — шаг цены ОДНОГО ТОКЕНА; f.tick, как и
        # book()/funding().mark/avg_px ниже, отдаём в масштабе цены КОНТРАКТА (·m) — соглашение проекта. Нативный
        # (токенный) шаг для реального округления перед отправкой на биржу ioc() берёт из meta отдельно (self._m()
        # кэширует и то, и другое через один и тот же сырой Contract).
        native_tick = _d(c.get("order_price_round"))
        if native_tick <= 0:
            raise GateError(f"{c.get('name')}: order_price_round ≤ 0")
        tick = native_tick * _d(c.get("quanto_multiplier", 1))
        if c.get("enable_decimal"):
            # [A]: контракты с дробным шагом контрактов (enable_decimal=true) — у FATCOIN_USDT его нет (проверено
            # живым снимком 13.09: enable_decimal=false), какое поле несёт дробный шаг — не подтверждено доками;
            # изобретать не будем — явный отказ вместо тихого «шаг=1» на контракте, где это может быть неверно.
            raise GateError(f"{c.get('name')}: enable_decimal контракты не поддержаны (нет проверенного поля шага "
                            "контрактов) — доработка нужна перед торговлей таким инструментом")
        min_qty = _d(c.get("order_size_min", 0))
        max_qty = _d(c.get("order_size_max", 0))
        return Filters(tick=tick, step=D(1), min_qty=min_qty, max_qty_limit=max_qty, max_qty_market=max_qty,
                       min_notional=D(0), tifs=frozenset({"gtc", "ioc", "poc", "fok"}))

    def filters(self, symbol: str) -> Filters:
        hit = self._filters.get(symbol)
        if hit and self._now() - hit[0] < FILTERS_TTL_S:
            return hit[1]
        try:
            c = self._load_contract(symbol)
        except Exception:
            if hit:
                log.warning("gate: контракт %s не обновлён, беру фильтры из кэша", symbol)
                return hit[1]
            raise
        f = self._parse_filters(c)
        t = self._now()
        self._filters[symbol] = (t, f)
        self._meta[symbol] = c
        self._status[symbol] = str(c.get("status") or "")
        return f

    def status(self, symbol: str) -> str | None:
        return self._status.get(symbol)

    def _m(self, symbol: str) -> Decimal:
        """quanto_multiplier контракта, с прогревом кэша meta (filters()) — единая точка, где адаптер знает
        множитель для перевода цены ТОКЕНА Gate в цену КОНТРАКТА (book/funding.mark/avg_px/fills.price — GATE-4)
        и обратно (ioc() перед округлением к нативному order_price_round — GATE-4)."""
        self.filters(symbol)
        return _d((self._meta.get(symbol) or {}).get("quanto_multiplier", 1))

    def instrument(self, symbol: str) -> PerpInstrument:
        """m = quanto_multiplier контракта (авторитетно, не эвристика по имени — см. докстринг файла)."""
        self.filters(symbol)
        c = self._meta.get(symbol) or {}
        raw_base = str(c.get("name") or symbol).rsplit("_", 1)[0]
        base, _name_factor = norm_symbol_factor(raw_base)     # только читаемое имя; m ниже — не отсюда
        m = _d(c.get("quanto_multiplier", 1))
        # contract_type ("" / stocks / indices / commodities / metals / forex — как у gate_fut.asset_class) и type
        # ("direct" / "inverse" — линейный/обратный контракт) — РАЗНЫЕ оси Gate; берём contract_type буквально,
        # "" для крипто-перпа (FATCOIN_USDT) — не эвристика, а факт живого снимка 13.09.
        return PerpInstrument(symbol=symbol, base_asset=raw_base, base=base, m=m, quote_asset=self.SETTLE.upper(),
                              contract_type=c.get("contract_type"))

    # --- публичное -----------------------------------------------------------------------------------
    def server_time_ms(self) -> int:
        return int(self._public("/spot/time")["server_time"])     # общая точка времени всех рынков Gate [D]

    def clock_offset_s(self) -> float:
        t0 = self._now()
        ms = self.server_time_ms()
        t1 = self._now()
        return ms / 1000.0 - (t0 + t1) / 2.0

    def check_clock(self, max_s: float = CLOCK_SKEW_MAX_S) -> float:
        off = self.clock_offset_s()
        if abs(off) > max_s:
            raise GateError(f"часы расходятся с Gate на {off:+.2f} с (> {max_s} с; сервер по докам допускает "
                            "Timestamp ±60 с): проверь NTP, live не запускаю")
        return off

    def book(self, symbol: str, limit: int = 20) -> Book:
        """Стакан. ИСПРАВЛЕНО (GATE-4, 13.09): цены order_book Gate — за ОДИН ТОКЕН (живой снимок 13.09: bid
        0.00167 при контракте = 100 токенов); соглашение проекта (planner.py: basis_bps = best/upc/dex_px − 1,
        engine.py: px_tok = мид/m) — цена КОНТРАКТА, как её без пересчёта уже отдаёт Aster. Домножаем на m —
        без этого planner получал цену в 100 раз ниже настоящей и basis уходил в −9900 бп на ровном месте."""
        want = max(1, min(int(limit), 50))
        m = self._m(symbol)
        b = self._public(f"/futures/{self.SETTLE}/order_book", {"contract": symbol, "limit": want})
        bids = tuple((_d(x["p"]) * m, _d(x["s"])) for x in (b.get("bids") or [])[:want])
        asks = tuple((_d(x["p"]) * m, _d(x["s"])) for x in (b.get("asks") or [])[:want])
        return Book(bids=bids, asks=asks, ts=self._now())

    def funding(self, symbol: str) -> tuple[Decimal, Decimal, int]:
        """(марк — цена КОНТРАКТА [·m, см. book()], ставка ЗА ИНТЕРВАЛ funding_interval, время следующего
        списания мс). ИСПРАВЛЕНО (GATE-3, 13.09): раньше модуль сам делил ставку на funding_interval/3600 и отдавал
        «ставку за час» — но движок (engine.py: funding_h = rate/pair.period_h) делит ЕЩЁ раз на период пары, как
        и для Aster.funding(), которая отдаёт ставку КАК ЕСТЬ за интервал биржи (aster_trade.py:503-508). Двойное
        деление занижало фандинг FATCOIN (интервал 4 ч) в 4 раза. funding_interval отсутствует/≤0 — GateError, а
        не молчаливые 8 ч (то было [A]-допущение «как у большинства площадок», не число с биржи)."""
        c = self._public(f"/futures/{self.SETTLE}/contracts/{_urlquote(symbol, safe='')}")
        iv_s = int(c.get("funding_interval") or 0)
        if iv_s <= 0:
            raise GateError(f"{symbol}: funding_interval пуст/≤0 ({c.get('funding_interval')!r}) — период "
                            "фандинга не известен из API, часовую ставку не выдумываем (было [A]-допущение 8 ч)")
        m = _d(c.get("quanto_multiplier", 1))
        rate_per_interval = _d(c.get("funding_rate", 0))
        mark = _d(c.get("mark_price", 0)) * m
        next_ms = int(_d(c.get("funding_next_apply", 0)) * 1000)
        return mark, rate_per_interval, next_ms

    def sigma_1s(self, symbol: str) -> Decimal | None:
        """σ доходности цены за 1 с по минутным свечам Gate (σ₁ₘ/√60) — та же мера, что engine.sigma_1s у Aster (план
        оценивает риск голой ноги). Мало свечей — None (неизвестно, а не 0); цена токена или контракта — доходности
        одинаковы."""
        rows = self._public(f"/futures/{self.SETTLE}/candlesticks", {"contract": symbol, "interval": "1m", "limit": 61})
        closes = [_d(r["c"]) for r in rows if isinstance(r, dict) and r.get("c") is not None] \
            if isinstance(rows, list) else []
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1] > 0]
        if len(rets) < 10:
            return None
        mean = sum(rets, _D0) / len(rets)
        var = sum(((x - mean) ** 2 for x in rets), _D0) / (len(rets) - 1)
        return var.sqrt() / Decimal(60).sqrt()

    # --- подписанные чтения ------------------------------------------------------------------------
    def account(self) -> dict:
        body = self._signed_ok("GET", f"/futures/{self.SETTLE}/accounts", None, "accounts")
        if not isinstance(body, dict):
            raise GateError("accounts: не объект")
        return body

    def available_margin(self) -> Decimal | None:
        """available изолированного/классического аккаунта; None — ошибка или поле отсутствует (не 0)."""
        try:
            acc = self.account()
        except ModeForbidden:
            raise
        except Exception as e:
            log.warning("gate accounts: %s", type(e).__name__)
            return None
        if acc.get("available") is None:
            return None
        try:
            return _d(acc["available"])
        except GateError:
            return None

    def in_dual_mode(self) -> bool | None:
        try:
            acc = self.account()
        except ModeForbidden:
            raise
        except Exception as e:
            log.warning("gate accounts (in_dual_mode): %s", type(e).__name__)
            return None
        v = acc.get("in_dual_mode")
        return bool(v) if isinstance(v, bool) else None

    def position(self, symbol: str) -> Decimal | None:
        """Знаковая позиция в КОНТРАКТАХ (не в токенах — перевод через m делает движок, как у Aster/1000BONK).
        Флэт — size:0 в объекте позиции ИЛИ HTTP 400 label POSITION_NOT_FOUND: позиции по контракту ещё не было
        (живой ответ 14.09 по FATCOIN_USDT — объект позиции есть НЕ всегда). None — только при прочей ошибке/обрыве
        чтения (урок «призраки позиций» — не путать «не прочитали» с «флэт»)."""
        try:
            st, body, _ = self.call("GET", f"/futures/{self.SETTLE}/positions/{_urlquote(symbol, safe='')}",
                                    critical=True)
        except ModeForbidden:
            raise
        except Exception as e:
            log.warning("gate positions %s: %s", symbol, type(e).__name__)
            return None
        if st in (400, 404) and _label(body) == "POSITION_NOT_FOUND":
            return _D0                         # позиции по контракту не было — флэт, а не «не прочитали»
        if _is_error(st) or not isinstance(body, dict):
            return None
        try:
            return _d(body["size"])
        except (GateError, KeyError):
            return None

    # --- настройка (идемпотентно, ISOLATED только) --------------------------------------------------
    def _set_dual_mode(self, v: bool) -> None:
        if v:
            raise ValueError("gate_trade: dual_mode=True запрещён кодом — политика владельца «одна позиция»")
        self._signed_ok("POST", f"/futures/{self.SETTLE}/dual_mode", {"dual_mode": False}, "dual_mode")

    def setup(self, symbol: str, leverage: int, margin_type: str) -> None:
        """ISOLATED-only (владелец 07.09): leverage>0 обязателен, CROSSED этот модуль не отправляет вовсе. dual_mode
        проверяется и, если включён, ОДИН раз пытаемся аккуратно выключить (никогда не включаем)."""
        if isinstance(leverage, bool) or not isinstance(leverage, int) or not 1 <= leverage <= 125:
            raise ValueError(f"плечо — целое 1…125, а не {leverage!r}")
        if margin_type != "ISOLATED":
            raise GateError(f"Gate: разрешён только ISOLATED (leverage>0) — margin_type={margin_type!r} отклонён "
                            "(владелец 07.09: новые шорты только 1x/изолированно, кросс не отправляем)")
        dual = self.in_dual_mode()
        if dual is None:
            raise GateError("Gate: не удалось прочитать in_dual_mode — настройка отменена (не гадаем)")
        if dual:
            try:
                self._set_dual_mode(False)
            except GateApiError as e:
                raise GateError("Gate: аккаунт в режиме двух позиций (dual_mode), выключить не удалось "
                                f"({e}) — вероятно, есть открытые позиции/заявки. Переключите вручную: Futures → "
                                "Settings → Position Mode → Single Position. Ничего не отправлено") from None
            if self.in_dual_mode():
                raise GateError("Gate: аккаунт в режиме двух позиций (dual_mode) — переключить не удалось "
                                "(вероятно, есть открытые позиции/заявки). Переключите вручную: Futures → "
                                "Settings → Position Mode → Single Position. Ничего не отправлено")
        body = self._signed_ok("POST", f"/futures/{self.SETTLE}/positions/{_urlquote(symbol, safe='')}/leverage",
                               {"leverage": str(int(leverage))}, "leverage")
        try:
            got = int(_d(body.get("leverage")) if isinstance(body, dict) else None)
        except (GateError, TypeError, ValueError):
            raise GateError(f"leverage: в ответе нет плеча: {body!r}"[:200]) from None
        if got != leverage:
            raise GateError(f"leverage: просили {leverage}x, площадка поставила {got}x")
        log.info("gate setup %s: isolated, %sx, single-position", symbol, leverage)

    # --- заявки --------------------------------------------------------------------------------------
    def _round_price(self, side: str, px: Decimal, tick: Decimal) -> Decimal:
        return ceil_step(px, tick) if side == "SELL" else floor_step(px, tick)

    def _check_order(self, symbol: str, side: str, qty: Decimal, px: Decimal, client_id: str, f: Filters) -> None:
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side BUY|SELL, а не {side!r}")
        if not CID_RE.match(client_id or ""):
            raise ValueError(f"client_id не проходит шаблон Gate (после '{TEXT_PREFIX}', ≤28 байт): {client_id!r}")
        for name, v in (("qty", qty), ("px_cap", px)):
            if isinstance(v, bool) or not isinstance(v, Decimal) or not v.is_finite() or v <= 0:
                raise ValueError(f"{name} — положительный Decimal, а не {v!r}")
        if qty != qty.to_integral_value():
            raise ValueError(f"{symbol}: qty {qty} не целое число контрактов (Gate size — int)")
        if qty < f.min_qty or (f.max_qty_limit > 0 and qty > f.max_qty_limit):
            raise ValueError(f"{symbol}: qty {qty} вне [{f.min_qty}, {f.max_qty_limit}] контрактов")

    def ioc(self, symbol: str, side: str, qty: Decimal, px_cap: Decimal, client_id: str, reduce_only: bool,
            *, hedge: bool = False, on_signed: Callable[[int], None] | None = None) -> PerpFill:
        """POST /futures/usdt/orders, tif=ioc, size со знаком (SELL — отрицательный). px_cap — цена КОНТРАКТА
        (соглашение проекта, как book()/funding().mark — ИСПРАВЛЕНО GATE-4, 13.09): Gate на бирже принимает и
        отдаёт цену ОДНОГО ТОКЕНА, поэтому здесь кэп переводится в токен (/m) ДО округления к нативному тику
        order_price_round (SELL вверх, BUY вниз — см. докстринг файла) и обратно в цену контракта (·m) для
        PerpFill.avg_px (в order_to_fill). Количество (контракты) НЕ округляется, не то число — отказ.
        Одна отправка без повтора: FILLED / PARTIALLY_FILLED (финал) / EXPIRED (0 исполнено) — ответ площадки;
        REJECTED + код=finish_as или label — площадка отказала/сняла заявку без исполнения;
        UNKNOWN — 5xx/SERVER_LABELS/обрыв/не финал: НЕ ПОВТОРЯТЬ, звать settle_unknown()."""
        f = self.filters(symbol)
        self._check_order(symbol, side, qty, px_cap, client_id, f)
        c = self._meta.get(symbol) or {}
        m = _d(c.get("quanto_multiplier", 1))
        native_tick = _d(c.get("order_price_round"))
        px_cap_native = px_cap / m                     # кэп пришёл ценой контракта — на биржу шлём цену токена
        px_native = self._round_price(side, px_cap_native, native_tick)
        if px_native <= 0:
            raise ValueError(f"{symbol}: цена после округления к тику ≤ 0 (px_cap={px_cap}, tick={f.tick})")
        size = -int(qty) if side == "SELL" else int(qty)
        text = f"{TEXT_PREFIX}{client_id}"
        body = {"contract": symbol, "size": size, "price": dec_str(px_native), "tif": "ioc", "text": text,
                "reduce_only": bool(reduce_only)}
        self.last_error = None
        try:
            st, resp, ts = self.call("POST", f"/futures/{self.SETTLE}/orders", body=body, hedge=hedge,
                                     critical=True, on_signed=on_signed)
        except GateNetError as e:
            self.last_error = str(e)
            log.warning("gate ioc %s %s %s: нет ответа — исход неизвестен, выясняю запросом", client_id, side, qty)
            return _pf(client_id, "UNKNOWN", nonce=e.ts)
        label = _label(resp)
        if label in SERVER_LABELS or st >= 500 or st == 408:
            self.last_error = f"HTTP {st} {label} {_msg(resp)}"
            return _pf(client_id, "UNKNOWN", nonce=ts, code=label)
        if _is_error(st):
            self.last_error = f"HTTP {st} {label} {_msg(resp)}"
            log.warning("gate ioc %s отклонена: %s", client_id, self.last_error)
            return _pf(client_id, "REJECTED", nonce=ts, code=label)
        if not isinstance(resp, dict):
            return _pf(client_id, "UNKNOWN", nonce=ts)
        fres = order_to_fill(client_id, resp, ts, m)
        log.info("gate ioc %s %s %s@≤%s(токен)/≤%s(контракт) → %s %s avg %s",
                 client_id, side, qty, px_native, px_cap, fres.status, fres.qty, fres.avg_px)
        return fres

    def query(self, symbol: str, client_id: str) -> PerpFill:
        """GET /futures/usdt/orders/{text}. ВНИМАНИЕ (доки, см. файл): по text ищет только пока заявка в стакане
        или ≤60 с после финиша — settle_unknown() отдельно защищается от позднего вызова этого метода."""
        if not CID_RE.match(client_id or ""):
            raise ValueError(f"client_id не проходит шаблон Gate: {client_id!r}")
        text = f"{TEXT_PREFIX}{client_id}"
        try:
            st, body, _ = self.call("GET", f"/futures/{self.SETTLE}/orders/{_urlquote(text, safe='')}", critical=True)
        except ModeForbidden:
            raise
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            return _pf(client_id, "UNKNOWN")
        if st == 404:
            return _pf(client_id, "NOT_FOUND", code=_label(body) or NOT_FOUND_LABEL)
        if _is_error(st) or not isinstance(body, dict):
            self.last_error = f"HTTP {st} {_label(body)} {_msg(body)}"
            return _pf(client_id, "UNKNOWN", code=_label(body))
        return order_to_fill(client_id, body, 0, self._m(symbol))   # m — цена контракта в avg_px (GATE-2/GATE-4)

    def settle_unknown(self, symbol: str, client_id: str, *, pos_before: Decimal | None, since_ms: int,
                       known_order_ids=frozenset()) -> PerpFill:
        """Выяснить исход заявки с неизвестным итогом — без повторной отправки (как у Aster). Дополнительная
        защита Gate: text-id ищется биржей лишь ~60 с (доки) — после TEXT_ID_SAFE_MS с отправки «не нашли по
        text» больше не доказательство NOT_FOUND, а просто «нечем проверить», исход остаётся UNKNOWN."""
        n = max(1, UNKNOWN_QUERIES)
        gap = UNKNOWN_SPAN_S / max(n - 1, 1)
        not_found, last = 0, None
        for i in range(n):
            if i:
                self._sleep(gap)
            g = self.query(symbol, client_id)
            if g.status in FINAL:
                return g
            if g.status == "NOT_FOUND":
                not_found += 1
            else:
                last = g
        if not_found < n:
            return last or _pf(client_id, "UNKNOWN")
        # 404 ×n. По text Gate ищет заявку только в стакане и ≤60 с после финиша (доки) — позже 404 ничего не
        # доказывает, и прежняя проверка оставляла заявку UNKNOWN навсегда: голая нога, которую бот не дохеджирует
        # (ревью Fable 14.09, P1-1). Исход — по спискам, где text остаётся: завершённые заявки контракта (нашли —
        # это и есть итог) и my_trades окна (исполнения без строки сделки не бывает). «Не выставлена» — только если
        # оба списка покрыли окно с отправки, заявки в них нет, чужих сделок в окне нет и позиция (если известна)
        # не сдвинулась. Иначе UNKNOWN; повторной отправки нет никогда.
        unknown = _pf(client_id, "UNKNOWN", code=NOT_FOUND_LABEL)
        text = f"{TEXT_PREFIX}{client_id}"
        cutoff = since_ms - TRADES_SLACK_MS
        try:
            row, covered = self._finished_by_text(symbol, text, cutoff)
            if row is not None:
                return order_to_fill(client_id, row, 0, self._m(symbol))
            if not covered:
                log.warning("gate %s: 404 ×%d, список завершённых заявок не дошёл до отправки — исход неизвестен",
                            client_id, n)
                return unknown
            if pos_before is not None:
                pos = self.position(symbol)
                if pos is None or pos != pos_before:
                    log.warning("gate %s: 404 ×%d, но позиция %s ≠ %s — исход неизвестен", client_id, n, pos, pos_before)
                    return unknown
            trades, t_covered = self._trades_since(symbol, cutoff)
        except ModeForbidden:
            raise
        except Exception as e:                 # noqa — список не прочитан: исход неизвестен, ничего не меняем
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            return unknown
        if not t_covered:
            log.warning("gate %s: 404 ×%d, my_trades не дошли до отправки — исход неизвестен", client_id, n)
            return unknown
        known = {int(x) for x in known_order_ids}
        foreign = []
        for t in trades:
            try:
                oid = int(t["order_id"]) if t.get("order_id") is not None else None
            except (TypeError, ValueError):
                oid = None
            if oid is None or oid not in known or str(t.get("text") or "") == text:
                foreign.append(t)
        if foreign:
            log.warning("gate %s: 404 ×%d, но в my_trades %d новых сделок в окне — исход неизвестен",
                        client_id, n, len(foreign))
            return unknown
        return _pf(client_id, "NOT_FOUND", code=NOT_FOUND_LABEL)

    def _finished_by_text(self, symbol: str, text: str, cutoff_ms: int) -> tuple[dict | None, bool]:
        """(заявка с этим text или None, покрыт ли список до cutoff_ms). Список завершённых заявок контракта — новые
        первыми, страницами по offset; text в нём остаётся и после окна text-id. Не покрыт (упёрлись в MAX_PAGES) —
        отсутствие ничего не доказывает."""
        offset = 0
        for _ in range(MAX_PAGES):
            body = self._signed_ok("GET", f"/futures/{self.SETTLE}/orders",
                                   {"contract": symbol, "status": "finished", "limit": ORDERS_PAGE, "offset": offset},
                                   "orders finished", critical=True)
            if not isinstance(body, list):
                raise GateError("orders: не список")
            oldest = None
            for o in body:
                if not isinstance(o, dict):
                    continue
                if str(o.get("text") or "") == text:
                    return o, True
                try:
                    ts = _ms(o.get("create_time", 0))
                except (GateError, TypeError, ValueError):
                    continue
                oldest = ts if oldest is None else min(oldest, ts)
            if len(body) < ORDERS_PAGE or (oldest is not None and oldest < cutoff_ms):
                return None, True
            offset += len(body)
        return None, False

    def _trades_since(self, symbol: str, cutoff_ms: int) -> tuple[list[dict], bool]:
        """(строки my_trades не старше cutoff_ms, покрыто ли окно). Листание назад по last_id (как fills());
        строка с нечитаемым временем — в окне (считается «чужой», а не пропускается)."""
        out: list[dict] = []
        lid: int | None = None
        for _ in range(MAX_PAGES):
            params = {"contract": symbol, "limit": PAGE_LIMIT}
            if lid is not None:
                params["last_id"] = str(lid)
            body = self._signed_ok("GET", f"/futures/{self.SETTLE}/my_trades", params, "my_trades", critical=True)
            if not isinstance(body, list):
                raise GateError("my_trades: не список")
            oldest, ids = None, []
            for t in body:
                if not isinstance(t, dict):
                    continue
                try:
                    ts = _ms(t.get("create_time", 0))
                except (GateError, TypeError, ValueError):
                    out.append(t)
                    continue
                oldest = ts if oldest is None else min(oldest, ts)
                if ts >= cutoff_ms:
                    out.append(t)
                try:
                    ids.append(int(t["id"]))
                except (KeyError, TypeError, ValueError):
                    pass
            if len(body) < PAGE_LIMIT or (oldest is not None and oldest < cutoff_ms):
                return out, True
            if not ids:
                return out, False
            lid = min(ids)
        return out, False

    # --- учёт ------------------------------------------------------------------------------------
    def _trade_row(self, symbol: str, t: dict, m: Decimal) -> dict:
        # price — ИСПРАВЛЕНО (GATE-2/GATE-4, 13.09): t["price"] у Gate my_trades — цена ОДНОГО ТОКЕНА; строка
        # store.perp_fills.price хранится в масштабе цены КОНТРАКТА (·m) — как и Aster t["price"] (avgPrice за
        # «пачку» m токенов), и как book()/order_to_fill() этой же задачи. quote_qty дальше — просто price·qty,
        # без отдельного ·m: он уже внутри price (было query price·qty·m — то же число, но раньше price=quote_qty/qty
        # было в m раз меньше настоящей цены контракта и не совпадало по масштабу с avg_px из order_to_fill/book()).
        price, size = _d(t.get("price", 0)) * m, _d(t.get("size", 0))
        qty = abs(size)
        oid = None
        try:
            if t.get("order_id") is not None:
                oid = int(t["order_id"])
        except (TypeError, ValueError):
            oid = None
        fee = t.get("fee")
        return {"trade_id": int(t["id"]), "order_id": oid, "symbol": symbol, "side": "SELL" if size < 0 else "BUY",
                "price": price, "qty": qty, "quote_qty": price * qty if m else None,
                "commission_abs": None if fee is None else abs(_d(fee)), "commission_asset": self.SETTLE.upper(),
                "maker": str(t.get("role") or "").lower() == "maker", "realized_pnl": None,   # Gate my_trades его не даёт
                "ts": _ms(t.get("create_time", 0))}

    def history_account(self):
        from .accounting import _hash
        uid = self.account().get('user')
        if type(uid) is not int or uid <= 0:
            raise GateError('authenticated futures account user ID is missing')
        return 'acct:v1:gate:' + _hash((self.base, self.SETTLE, uid))

    def history_fills(self, symbol, from_id):
        return self.fills(symbol, from_id, _strict=True)

    def history_funding(self, symbol, start_ms):
        return self.funding_income(symbol, start_ms, _strict=True)

    def fills(self, symbol: str, from_id: int | None, *, _strict=False) -> list[dict]:
        """Сделки my_trades (строки для store.add_perp_fills). ИСПРАВЛЕНО (GATE-1, 13.09): live-проба публичного
        /trades 13.09 показала реальную семантику Gate — last_id листает СТРОГО НАЗАД (id < last_id, новые
        сделки первыми), это НЕ курсор «после X», как fromId у Aster. Раньше модуль слал last_id=from_id−1 и шёл
        max(id) вперёд — получал старые сделки (id < from_id) вместо новых, либо на большой истории упирался в
        MAX_PAGES, гоняя last_id по чужому направлению. Теперь идём от САМОЙ СВЕЖЕЙ страницы (без last_id) назад,
        last_id очередной страницы = min(id) предыдущей, пока страница не окажется неполной (упёрлись в начало
        истории аккаунта) или её min(id) не опустится до from_id включительно (дальше уже собрали все нужные id —
        более старые не нужны); из накопленного отбрасываем id < from_id. from_id=None — как раньше, только
        первая (самая свежая) страница без фильтра."""
        m = self._m(symbol)                                # прогреть кэш meta заодно с множителем
        out: dict[int, dict] = {}
        lid: int | None = None
        for _ in range(MAX_PAGES):
            params = {"contract": symbol, "limit": PAGE_LIMIT}
            if lid is not None:
                params["last_id"] = str(lid)
            body = self._signed_ok("GET", f"/futures/{self.SETTLE}/my_trades", params, "my_trades")
            if not isinstance(body, list):
                raise GateError("my_trades: не список")
            if _strict and any(not isinstance(t, dict) or t.get('contract') != symbol or
                               any(t.get(k) is None for k in ('id','order_id','price','size','create_time')) for t in body):
                raise GateError('history fill identity or execution fields are missing')
            rows = [self._trade_row(symbol, t, m) for t in body if isinstance(t, dict)]
            if _strict:
                for raw, parsed in zip(body, rows):
                    # point_fee is a separate currency; never silently call fee=0 complete.
                    if raw.get('point_fee') is None or _d(raw['point_fee']) != 0:
                        parsed['commission_abs'] = None
            for r in rows:
                if _strict and r['trade_id'] in out and out[r['trade_id']] != r:
                    raise GateError('history fill duplicate conflict')
                out[r["trade_id"]] = r
            if from_id is None:
                return [out[k] for k in sorted(out)]
            page_min = min((r["trade_id"] for r in rows), default=None)
            if len(body) < PAGE_LIMIT or page_min is None or page_min <= from_id:
                return [out[k] for k in sorted(out) if k >= from_id]
            lid = page_min
        raise GateError(f"my_trades {symbol}: больше {MAX_PAGES} страниц — сузь from_id")

    def funding_income(self, symbol: str, start_ms: int, *, _strict=False) -> list[dict]:
        """account_book?type=fund с start_ms (строки для store.add_funding_income); дедуп по id. Пагинация —
        offset (доки не документируют лимит окна времени, в отличие от Aster/7 суток — не выдумываем предел,
        останавливаемся по MAX_PAGES, как везде в проекте)."""
        out: dict[int, dict] = {}
        start_s = int(start_ms) // 1000
        now_s = int(self._now())
        offset = 0
        for _ in range(MAX_PAGES):
            params = {"contract": symbol, "type": "fund", "from": start_s, "to": now_s, "limit": PAGE_LIMIT,
                      "offset": offset}
            body = self._signed_ok("GET", f"/futures/{self.SETTLE}/account_book", params, "account_book")
            if not isinstance(body, list):
                raise GateError("account_book: не список")
            for r in body:
                if _strict and (not isinstance(r, dict) or r.get('type') != 'fund' or
                                r.get('contract') != symbol or
                                any(r.get(k) is None for k in ('id','time','change'))):
                    raise GateError('history funding identity or money fields are missing')
                if not isinstance(r, dict) or r.get("type") != "fund":
                    continue        # защита в глубину: type=fund уже в запросе, но не доверяем фильтру площадки вслепую
                try:
                    tid = int(r["id"])
                    t_s = int(_d(r.get("time", 0)))
                except (KeyError, GateError, TypeError, ValueError):
                    if _strict:
                        raise GateError('history funding id or time is invalid') from None
                    continue
                if _strict:
                    row = {'tran_id': tid, 'symbol': r['contract'], 'income': _d(r['change']), 'ts': _ms(r['time']),
                           'asset': self.SETTLE.upper()}
                    if row['ts'] < start_ms:
                        continue
                    if tid in out and out[tid] != row:
                        raise GateError('history funding duplicate conflict')
                    out[tid] = row
                    continue
                if t_s < start_s:      # секундная точность account_book — сравниваем в секундах (как в запросе from=
                    continue           # start_s), а не в мс: округление start_ms→сек не должно отсекать свою же границу
                out[tid] = {"tran_id": tid, "symbol": r.get("contract") or symbol, "income": _d(r.get("change", 0)),
                           "ts": t_s * 1000}
            if len(body) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT
        else:
            raise GateError(f"account_book {symbol}: больше {MAX_PAGES} страниц — сузь start_ms")
        return sorted(out.values(), key=lambda r: (r["ts"], r["tran_id"]))

    def health(self) -> dict:
        return {"exchange": self.venue, "budget": round(self.budget, 3), "last_ok_ts": int(self.last_ok_ts),
                "n_429": self.n_429, "n_err": self.n_err, "banned_until": int(self.banned_until),
                "backoff_until": int(self.backoff_until), "key": self._key}
