"""Перп-нога Binance USDⓈ-M Futures: подпись HMAC-SHA256 REST API, PerpLeg поверх requests (trade_spec §4).

СВЯЗКА ВЫКЛЮЧЕНА ПО УМОЛЧАНИЮ (18.09.2026, задача владельца «добавь в торговлю binance»). Этот модуль реализует
только ногу-исполнитель; ни один профиль owner.toml на неё пока не ссылается — регистрация в
adapters/registry.production_registry() не означает разрешение live (ADAPTER_GUIDE.md). Живых вызовов при
разработке не делалось; все тесты — на фейковом requests.Session (см. tests/test_binance_trade.py).

Механика API (сверено 18.09 по официальной документации Binance Futures + независимо перепроверено чтением
другого бота этого проекта — hyper/transfer_binance_futures_client.py, ТОЛЬКО идея API, не код и не секреты
Transfer — см. PATCHNOTES/binance-perp-spot-adapters-20260918.md):
- Binance USDⓈ-M — почти буквальный образец, по которому уже написан aster_trade.py (Aster — клон Binance
  Futures API: те же пути, поля, заголовок веса X-MBX-USED-WEIGHT-1M, те же отрицательные коды ошибок).
  Разница с Aster здесь — в СПОСОБЕ подписи (HMAC-SHA256 api-key/secret, а не EIP-712 агентом) и в РЕАЛЬНЫХ
  путях Binance (/fapi/v1/… и /fapi/v2/… — у Aster своя нумерация /fapi/v3/…, к настоящему Binance не относится).
- Подпись: query-string (urlencode параметров, включая timestamp и recvWindow) подписывается HMAC-SHA256
  секретом; итоговая строка — "&signature=<hex>"; заголовок аутентификации — X-MBX-APIKEY. Binance принимает
  ВСЕ параметры (включая POST/DELETE) как query-string URL, JSON-тела нет (это отличает Binance от многих REST
  API и подтверждено независимым чтением обоих файлов hyper/*binance_futures_client.py — они делают ровно так же:
  `session.request(method, url, params=query, …)`). timestamp — миллисекунды, recvWindow — допуск (по умолчанию
  5000 мс, как у большинства клиентов Binance); нет отдельного nonce, как у Aster — просто метка времени.
- Эндпоинты (v1, кроме отмеченных v2 — они современнее и это то, что использует Binance сейчас для аккаунта/
  позиции; сверено также по хинту задачи «стандартная Binance Futures API v2»):
  /fapi/v1/time, /fapi/v1/exchangeInfo, /fapi/v1/depth, /fapi/v1/premiumIndex (уже бьёт коллектор, см.
  audit_truth/_fapi.py — тот же путь), /fapi/v1/order (POST/GET/DELETE), /fapi/v1/userTrades, /fapi/v1/income,
  /fapi/v1/leverage, /fapi/v1/marginType, /fapi/v1/positionSide/dual, /fapi/v1/multiAssetsMargin,
  /fapi/v2/positionRisk, /fapi/v2/account, /fapi/v2/balance.
- Коды ошибок — тот же список, что уже проверен и обработан в aster_trade.py (Aster — клон Binance): -1021
  (часы вне recvWindow), -1022 (плохая подпись), -1003 (rate limit), -1111 (precision), -2013 (заявки нет),
  -2019 (маржи не хватает), -2022 (reduce-only отказ), -4046 (margin type уже такой — не ошибка), -4059
  (position side уже такой — не ошибка), -4164 (меньше min notional), -4168 (isolated запрещён в Multi-Assets),
  -1006/-1007/-1001 (неизвестный исход отправки, HTTP 5xx/timeout — тоже сюда). Не выдумано: числа встречаются в
  документации Binance Error Codes и совпадают с тем, что уже обрабатывает aster_trade.py для клона того же API.
- Вес: X-MBX-USED-WEIGHT-1M, 418 — бан (Retry-After секунд), 429 — сузить темп. Лимит IP — 2400/мин
  (config.EXCHANGES["binance"]["weight_limit"], уже заведён коллектором для публичных запросов).

Ключи — BINANCE_API_KEY/BINANCE_API_SECRET окружения (.env), читаются ТОЛЬКО этим модулем (как GATE_API_KEY у
Gate — HMAC api-key/secret, а не EOA-подпись, поэтому trade/keys.py её не касается). Секрет никогда не
логируется и не печатается (обёртка _Secret, как у Gate); при загрузке регистрируется в общем реестре
редактирования keys.py (_remember_secret), чтобы работал общий redact()/redact_secrets().

Один аккаунт для двух рынков: Binance выдаёт один api-key/secret с двумя НЕЗАВИСИМЫМИМИ переключателями прав
в API Management — «Enable Futures» и «Enable Spot & Margin Trading» — сверено по документации Binance
(User Data Stream / API key permissions). Один и тот же секрет подписывает запросы и к fapi.binance.com
(этот модуль), и к api.binance.com (binance_spot_trade.py) — общий низкоуровневый HMAC-код здесь и переиспользуется
там (sign/build_query/load_env_keys/_Secret), а не пишется вторым независимым способом. Если владелец создал
ключ без прав на фьючи или спот — соответствующие подписанные вызовы отклонит сама биржа (-2015 неверный ключ/
права); это ожидаемый безопасный отказ, а не баг адаптера.

Настройка позиции: one-way (positionSide/dual → false, идемпотентно, -4059 = уже так) и margin_type — ЛЮБОЙ из
ISOLATED/CROSSED, какой владелец впишет в owner.toml (perp.binance.margin_type — тот же перечень, что и у
остальных площадок, owner.py §_PERP; секция perp.binance уже существует, т.к. "binance" давно в
config.PERP_VENUES для дашборда). В отличие от gate_trade.py (там ISOLATED навязан кодом по решению владельца
для конкретной сделки FATCOIN на Gate) здесь такого сужения нет — как у aster_trade.py, разрешён тот margin_type,
что владелец явно выбрал; пустое значение — live запрещён общим правилом owner.py (ПУСТО = ЗАПРЕЩЕНО).

Исход неизвестной заявки (settle_unknown) — по образцу AsterTrade, НЕ GateTrade: у Binance (как у клона — Aster)
query() по origClientOrderId работает без ограничения по времени (не как text-id Gate, которое биржа ищет лишь
~60 с) — доки Binance не документируют исчезновение ордера из истории раньше окончания retention, поэтому
трёхкратный -2013 + неизменная позиция + отсутствие чужих userTrades в окне — достаточное доказательство
NOT_FOUND, без второго (Gate-подобного) списка «завершённых заявок». Явно зафиксировано: если это допущение
окажется неверным на живых данных, settle_unknown нужно будет усилить до gate-подобной двухсписочной проверки.
"""
from __future__ import annotations
import hashlib, hmac, json, logging, re, time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.parse import urlencode
import requests
from .. import config
from ..client import BannedError, BinanceLike, BudgetExceeded
from .aster_trade import dec_str, fmt_param   # чистые Decimal-хелперы, венда-независимые (как у Gate)
from .keys import ModeForbidden, _remember_secret, gate as mode_gate
from ..symbols import norm_symbol_factor
from .types import Book, Filters, PerpFill, PerpInstrument

log = logging.getLogger(__name__)
D = Decimal
_D0 = Decimal(0)

VENUE = "binance"
BINANCE_KEY_ENV = "BINANCE_API_KEY"
BINANCE_SECRET_ENV = "BINANCE_API_SECRET"

SIGNED_TIMEOUT = (5, 10)             # (connect, read): дольше — исход неизвестен, выясняем запросом
SOFT_READ = config.WEIGHT_SOFT_LIMIT
SOFT_ORDER = 0.95                    # путь заявки: ioc / query / positionRisk / settle
FILTERS_TTL_S = 3600
RECV_WINDOW_MS = 5000                # допуск Binance по умолчанию у большинства клиентов; максимум документирован 60000
CLOCK_SKEW_MAX_S = 2.0               # наш запас внутри recvWindow (RTT + дрейф)
DEPTH_LIMITS = (5, 10, 20, 50, 100, 500, 1000)
PAGE_LIMIT = 1000
MAX_PAGES = 16                       # income — окна по 7 суток (документированный максимум); userTrades — по fromId
WEEK_MS = 7 * 24 * 3600 * 1000
TRADES_SLACK_MS = 5000               # окно userTrades в settle_unknown раньше отправки: часы ±2 с (live) + запас
UNKNOWN_QUERIES = 3
UNKNOWN_SPAN_S = 5.0
UNKNOWN_CODES = frozenset({-1000, -1001, -1006, -1007})   # «исполнение неизвестно, могло пройти»
MULTI_ASSETS_NO_ISOLATED = -4168     # «Unable to adjust to isolated-margin mode under the Multi-Assets mode.»
NO_SUCH_ORDER = -2013
CID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,36}$")    # newClientOrderId Binance Futures: до 36 симв., буквы/цифры/._-
FINAL = frozenset({"FILLED", "PARTIALLY_FILLED", "EXPIRED", "REJECTED"})


# --- ошибки ---------------------------------------------------------------------------------------
class BinanceError(RuntimeError):
    """Отказ ноги Binance (разбор ответа, фильтры, несоответствие)."""


class BinanceApiError(BinanceError):
    """Площадка ответила кодом ошибки: http, code (отрицательный код Binance или None), msg."""

    def __init__(self, what: str, http: int, code: int | None, msg: str):
        self.http, self.code, self.msg = http, code, msg
        super().__init__(f"{what}: HTTP {http} code {code} {msg}"[:300])


class BinanceNetError(BinanceError):
    """Ответа нет (таймаут, обрыв). Для отправки — исход НЕИЗВЕСТЕН; ts — метка времени подписи (для settle_unknown)."""

    def __init__(self, method: str, path: str, ts: int, cause: BaseException):
        self.method, self.path, self.ts = method, path, ts
        super().__init__(f"{method} {path}: нет ответа ({type(cause).__name__})")


# --- секрет: не печатать -----------------------------------------------------------------------
class _Secret:
    """Обёртка BINANCE_API_SECRET — как _Secret у Gate/keys.SignerKey у Aster: печатается как <secret>, значение
    отдаёт только reveal()."""
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
        raise BinanceError(f"не число: {x!r}")
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError):
        raise BinanceError(f"не число: {x!r}") from None


def _code(body: Any) -> int | None:
    if isinstance(body, dict) and "code" in body:
        try:
            return int(body["code"])
        except (TypeError, ValueError):
            return None
    return None


def _is_error(status: int, body: Any) -> bool:
    c = _code(body)
    return status >= 400 or (c is not None and c < 0)


def _retry_after(r, default: float) -> float:
    try:
        return max(float(r.headers.get("Retry-After") or default), 0.0)
    except (TypeError, ValueError):
        return default


def _msg(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("msg") or body.get("_raw") or "")[:200]
    return ""


# --- подпись: общая для perp (этот модуль) и spot (binance_spot_trade.py) --------------------------
def build_query(params: dict) -> str:
    """Query-string, которая и подписывается, и уходит на биржу байт в байт (urlencode, порядок вставки).
    None-значения выбрасываются (Binance не принимает "None" строкой). bool → "true"/"false" (fmt_param)."""
    pairs = [(str(k), fmt_param(v)) for k, v in params.items() if v is not None]
    return urlencode(pairs)


def sign(secret: str, query: str) -> str:
    """HMAC-SHA256(secret, query) — Binance (api-key/secret), а не HMAC-SHA512 formulой Gate."""
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()


def load_env_keys(environ=None) -> tuple[str, str]:
    """BINANCE_API_KEY/BINANCE_API_SECRET из окружения — единственное место, где ЭТОТ модуль их читает (spot —
    свой load_env_keys в binance_spot_trade.py, тем же именам переменных: один аккаунт, общий секрет)."""
    import os
    env = os.environ if environ is None else environ
    key = (env.get(BINANCE_KEY_ENV) or "").strip()
    secret = (env.get(BINANCE_SECRET_ENV) or "").strip()
    if not key or not secret:
        raise BinanceError(f"нет {BINANCE_KEY_ENV}/{BINANCE_SECRET_ENV} в окружении (.env)")
    return key, secret


# --- разбор ответов -------------------------------------------------------------------------------
def parse_filters(sym: dict) -> Filters:
    """Строка exchangeInfo.symbols → Filters (PRICE_FILTER/LOT_SIZE/MARKET_LOT_SIZE/MIN_NOTIONAL — как у Aster,
    тот же формат фильтров Binance-подобного fapi)."""
    f = {x.get("filterType"): x for x in sym.get("filters") or ()}
    pf, lot = f.get("PRICE_FILTER"), f.get("LOT_SIZE")
    if not pf or not lot:
        raise BinanceError(f"{sym.get('symbol')}: нет PRICE_FILTER/LOT_SIZE")
    mlot = f.get("MARKET_LOT_SIZE") or lot
    mn = f.get("MIN_NOTIONAL") or {}
    tick, step = _d(pf["tickSize"]), _d(lot["stepSize"])
    if tick <= 0 or step <= 0:
        raise BinanceError(f"{sym.get('symbol')}: шаг цены/количества ≤ 0")
    return Filters(tick=tick, step=step, min_qty=_d(lot.get("minQty", "0")), max_qty_limit=_d(lot["maxQty"]),
                   max_qty_market=_d(mlot["maxQty"]), min_notional=_d(mn.get("notional", mn.get("minNotional", "0"))),
                   tifs=frozenset(sym.get("timeInForce") or ()))


def _pf(cid: str, status: str, *, order_id: int | None = None, qty: Decimal = _D0, avg: Decimal = _D0,
        quote: Decimal = _D0, nonce: int = 0, code: int | None = None) -> PerpFill:
    return PerpFill(client_id=cid, order_id=order_id, status=status, qty=qty, avg_px=avg, quote=quote,
                    sign_nonce=nonce, err_code=code)


def order_to_fill(cid: str, body: dict, nonce: int) -> PerpFill:
    """Ответ заявки (RESULT — newOrderRespType) → PerpFill. PARTIALLY_FILLED здесь — ФИНАЛ (IOC: остаток истёк).
    Не финальные NEW/PARTIALLY_FILLED → UNKNOWN: заявка существует, но newOrderRespType=RESULT её не отдаёт
    не-финальной для IOC (сервер решает IOC синхронно) — оставлено на случай неожиданного ответа, не должно
    происходить в норме."""
    try:
        oid = int(body["orderId"]) if body.get("orderId") is not None else None
        qty = _d(body.get("executedQty", "0"))
        avg = _d(body.get("avgPrice", "0"))
        quote = _d(body.get("cumQuote", "0"))
    except (BinanceError, TypeError, ValueError):
        return _pf(cid, "UNKNOWN", nonce=nonce)
    if oid is None or (body.get("clientOrderId") not in (None, cid)):
        return _pf(cid, "UNKNOWN", nonce=nonce)      # чужая или безымянная заявка — не доверяем
    if qty > 0 and avg <= 0 and quote > 0:
        avg = quote / qty
    if qty > 0 and quote <= 0 and avg > 0:
        quote = avg * qty
    st = str(body.get("status") or "")
    if st == "FILLED":
        out = "FILLED"
    elif st in ("EXPIRED", "CANCELED", "EXPIRED_IN_MATCH"):
        out = "PARTIALLY_FILLED" if qty > 0 else "EXPIRED"
    elif st == "REJECTED":
        out = "REJECTED"
    else:
        out = "UNKNOWN"
    return _pf(cid, out, order_id=oid, qty=qty, avg=avg, quote=quote, nonce=nonce)


def _trade_row(t: dict) -> dict:
    """userTrades → строка для store.add_perp_fills."""
    return {"trade_id": int(t["id"]), "order_id": int(t["orderId"]) if t.get("orderId") is not None else None,
            "symbol": t.get("symbol"), "side": t.get("side"), "price": _d(t["price"]), "qty": _d(t["qty"]),
            "quote_qty": _d(t["quoteQty"]) if t.get("quoteQty") is not None else None,
            "commission_abs": abs(_d(t["commission"])) if t.get("commission") is not None else None,
            "commission_asset": t.get("commissionAsset"), "maker": bool(t.get("maker")),
            "realized_pnl": _d(t["realizedPnl"]) if t.get("realizedPnl") is not None else None,
            "ts": int(t["time"])}


def _income_row(r: dict) -> dict:
    return {"tran_id": int(r["tranId"]), "symbol": r.get("symbol"), "income": _d(r["income"]),
            "asset": r.get("asset"), "info": r.get("info"), "ts": int(r["time"])}


# --- нога -------------------------------------------------------------------------------------------
from .adapters.signing_fence import JournalBoundIoc


class BinanceTrade(JournalBoundIoc):
    """PerpLeg для Binance USDⓈ-M Futures. Публичное (фильтры, стакан, фандинг, время) — в любом режиме, без
    ключей; подписанное — через call() с воротами mode_state() на КАЖДЫЙ вызов (как Aster/Gate)."""
    venue = VENUE
    ioc_partial_terminal = True  # order_to_fill maps non-final exchange partial to UNKNOWN
    BASE = config.EXCHANGES[VENUE]["base"]

    def __init__(self, key: str | None = None, secret: str | None = None, *,
                 mode_state: Callable[[], tuple[str | None, bool]] | None = None, session=None,
                 base: str | None = None, timeout=SIGNED_TIMEOUT, now: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, public_retries: int = 2,
                 recv_window_ms: int = RECV_WINDOW_MS):
        self.base = (base or self.BASE).rstrip("/")
        self.http = BinanceLike(VENUE, self.base, config.EXCHANGES[VENUE]["weight_limit"], session=session)
        self._s = self.http._s
        self._key = key
        self._secret = _Secret(secret) if secret is not None else None
        if secret:      # редактирование логов — сразу при создании, а не только у from_env (единый реестр keys.py)
            _remember_secret(secret)
            _remember_secret(secret.lower())
        self._mode_state = mode_state or (lambda: ("dry", False))
        self.timeout = timeout
        self.recv_window_ms = int(recv_window_ms)
        self._now, self._sleep = now, sleep
        self.public_retries = public_retries
        self.backoff_until = 0.0            # после 429: до этого момента подписанные вызовы не делаем
        self.last_error: str | None = None
        self._filters: dict[str, tuple[float, Filters]] = {}
        self._status: dict[str, str] = {}
        self._meta: dict[str, dict] = {}

    @classmethod
    def from_env(cls, mode_state: Callable[[], tuple[str | None, bool]], environ=None, **kw) -> "BinanceTrade":
        key, secret = load_env_keys(environ)
        return cls(key, secret, mode_state=mode_state, **kw)

    def __repr__(self) -> str:
        return f"BinanceTrade(key={self._key!r}, secret={self._secret!r})"

    # --- ворота, бюджет, HTTP --------------------------------------------------------------------
    def _gate(self, action: str, hedge: bool) -> None:
        try:
            mode, paused = self._mode_state()
        except Exception as e:
            raise ModeForbidden(f"режим/пауза не прочитаны ({type(e).__name__}): {action} запрещено") from None
        mode_gate(mode, action, paused=bool(paused), hedge=hedge)

    def _check_budget(self, path: str, critical: bool) -> None:
        now = self._now()
        if now < self.http.banned_until:
            raise BannedError(f"binance: бан (418) ещё {self.http.banned_until - now:.0f} с, {path} не отправлен")
        if now < self.backoff_until:
            raise BudgetExceeded(f"binance: пауза после 429 ещё {self.backoff_until - now:.1f} с, {path} не отправлен")
        soft = SOFT_ORDER if critical else SOFT_READ
        if self.http.budget_used() >= soft:
            raise BudgetExceeded(f"binance: вес {self.http.budget_used():.0%} ≥ {soft:.0%}, {path} не отправлен")

    def _absorb(self, r) -> tuple[int, Any]:
        self.http._read_weight(r)
        status = int(r.status_code)
        if status == 418:
            self.http.banned_until = self._now() + _retry_after(r, 120.0)
        elif status == 429:
            self.http.n_429 += 1
            self.backoff_until = self._now() + max(_retry_after(r, 0.0), 2.0)
        try:
            body = json.loads(r.content, parse_float=Decimal) if r.content else {}
        except ValueError:
            body = {"_raw": (r.text or "")[:200]}
        if status < 400:
            self.http.last_ok_ts = self._now()
        else:
            self.http.n_err += 1
        return status, body

    def call(self, method: str, path: str, params: dict | None = None, *, hedge: bool = False,
             critical: bool = False, on_signed: Callable[[int], None] | None = None) -> tuple[int, Any, int]:
        """Подписанный вызов → (HTTP-статус, JSON, ts подписи в мс). Ошибки площадки НЕ бросает — их разбирает
        вызывающий. Бросает до сети: ModeForbidden (ворота), BannedError/BudgetExceeded (бюджет); после подписи:
        BinanceNetError (ответа нет). Все параметры (включая POST/DELETE) уходят query-string в URL — Binance
        не принимает JSON-тело для подписанных вызовов (сверено по докам и независимо перечитанным транспортом
        другого бота этого проекта). on_signed(ts) — после подписи и ДО отправки (запись-до, как у Aster/Gate)."""
        m = method.upper()
        self._gate("signed_read" if m == "GET" else "send", hedge)
        if self._key is None or self._secret is None:
            raise ModeForbidden("нет ключей Binance (BINANCE_API_KEY/BINANCE_API_SECRET не загружены): "
                                "подписанный вызов запрещён")
        self._check_budget(path, critical)
        ts = int(self._now() * 1000)
        payload = dict(params or {})
        payload["recvWindow"] = self.recv_window_ms
        payload["timestamp"] = ts
        query = build_query(payload)
        sig = sign(self._secret.reveal(), query)
        full_qs = f"{query}&signature={sig}"
        if on_signed is not None:
            on_signed(ts)
        url = f"{self.base}{path}?{full_qs}"
        headers = {"X-MBX-APIKEY": self._key}
        try:
            r = self._s.request(m, url, headers=headers, timeout=self.timeout)
        except Exception as e:       # noqa: сеть/таймаут/обрыв — после подписи исход отправки неизвестен
            self.http.n_err += 1
            raise BinanceNetError(m, path, ts, e) from None
        status, body = self._absorb(r)
        return status, body, ts

    def _signed_ok(self, method: str, path: str, params: dict | None = None, what: str = "", **kw) -> Any:
        st, body, _ = self.call(method, path, params, **kw)
        if _is_error(st, body):
            raise BinanceApiError(what or f"{method} {path}", st, _code(body), _msg(body))
        return body

    def _public(self, path: str, params: dict | None = None) -> Any:
        return self.http.get(path, params, retries=self.public_retries)

    # --- публичное ---------------------------------------------------------------------------------
    def server_time_ms(self) -> int:
        return int(self._public("/fapi/v1/time")["serverTime"])

    def clock_offset_s(self) -> float:
        """Сервер − локальные часы, с поправкой на половину RTT."""
        t0 = self._now()
        ms = self.server_time_ms()
        t1 = self._now()
        return ms / 1000.0 - (t0 + t1) / 2.0

    def check_clock(self, max_s: float = CLOCK_SKEW_MAX_S) -> float:
        """live не стартует при |часы − /fapi/v1/time| > 2 с: recvWindow по умолчанию 5000 мс, запас нужен на
        RTT и дрейф (иначе граничные запросы будут отклоняться -1021 непредсказуемо)."""
        off = self.clock_offset_s()
        if abs(off) > max_s:
            raise BinanceError(f"часы расходятся с Binance на {off:+.2f} с (> {max_s} с; recvWindow "
                               f"{self.recv_window_ms} мс): проверь NTP, live не запускаю")
        return off

    def _load_filters(self) -> None:
        ei = self._public("/fapi/v1/exchangeInfo")
        t = self._now()
        for sym in ei.get("symbols") or ():
            try:
                self._filters[sym["symbol"]] = (t, parse_filters(sym))
                self._status[sym["symbol"]] = str(sym.get("status") or "")
                self._meta[sym["symbol"]] = {"base": sym.get("baseAsset"), "quote": sym.get("quoteAsset"),
                                             "ctype": sym.get("contractType")}
            except (BinanceError, KeyError):
                continue

    def filters(self, symbol: str) -> Filters:
        hit = self._filters.get(symbol)
        if hit and self._now() - hit[0] < FILTERS_TTL_S:
            return hit[1]
        try:
            self._load_filters()
        except Exception as e:
            if hit:
                log.warning("binance: exchangeInfo не обновлён (%s), беру фильтры %s из кэша", type(e).__name__, symbol)
                return hit[1]
            raise
        hit = self._filters.get(symbol)
        if not hit:
            raise BinanceError(f"{symbol}: нет в exchangeInfo Binance")
        return hit[1]

    def instrument(self, symbol: str) -> PerpInstrument:
        """Контракт по exchangeInfo (тот же кэш, что filters): m = токенов в одном контракте по baseAsset
        (symbols.norm_symbol_factor: 1000BONK → BONK ×1000, как у Aster/Binance — обычные USDT-M контракты
        Binance линейны 1:1, но некоторые тикеры несут множитель в самом имени)."""
        self.filters(symbol)
        meta = self._meta.get(symbol) or {}
        ba = meta.get("base") or None
        base, fac = norm_symbol_factor(str(ba)) if ba else (None, None)
        return PerpInstrument(symbol, ba, base, None if fac is None else Decimal(int(fac)), meta.get("quote"),
                              meta.get("ctype"))

    def status(self, symbol: str) -> str | None:
        return self._status.get(symbol)

    def book(self, symbol: str, limit: int = 20) -> Book:
        want = max(1, min(int(limit), DEPTH_LIMITS[-1]))
        lim = next(x for x in DEPTH_LIMITS if x >= want)
        b = self._public("/fapi/v1/depth", {"symbol": symbol, "limit": lim})
        bids = tuple((_d(p), _d(q)) for p, q in (b.get("bids") or ())[:want])
        asks = tuple((_d(p), _d(q)) for p, q in (b.get("asks") or ())[:want])
        return Book(bids=bids, asks=asks, ts=self._now())

    def funding(self, symbol: str) -> tuple[Decimal, Decimal, int]:
        """(марк, последняя ставка за интервал, время следующего начисления мс) из premiumIndex — тот же путь,
        что уже бьёт read-only коллектор (audit_truth/_fapi.py)."""
        p = self._public("/fapi/v1/premiumIndex", {"symbol": symbol})
        if isinstance(p, list):
            p = next((x for x in p if x.get("symbol") == symbol), None) or {}
        return _d(p["markPrice"]), _d(p["lastFundingRate"]), int(p["nextFundingTime"])

    # --- подписанные чтения ------------------------------------------------------------------------
    def position_risk(self, symbol: str | None = None) -> list[dict]:
        body = self._signed_ok("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, "positionRisk")
        if not isinstance(body, list):
            raise BinanceError("positionRisk: не список")
        return body

    def position(self, symbol: str) -> Decimal | None:
        """Знаковая позиция (< 0 — шорт). None — НЕИЗВЕСТНО: ошибка, пустой ответ, строки hedge-режима.
        Пустое чтение за «флэт» не выдаём никогда (урок: призраки позиций)."""
        try:
            st, body, _ = self.call("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, critical=True)
        except ModeForbidden:
            raise
        except Exception as e:
            log.warning("binance positionRisk %s: %s", symbol, type(e).__name__)
            return None
        if _is_error(st, body) or not isinstance(body, list):
            return None
        rows = [r for r in body if isinstance(r, dict) and r.get("symbol") == symbol]
        if not rows:
            return None
        total = _D0
        for r in rows:
            if str(r.get("positionSide") or "BOTH") != "BOTH":    # hedge-режим: не наша модель (reduceOnly нельзя)
                return None
            try:
                total += _d(r["positionAmt"])
            except (BinanceError, KeyError):
                return None
        return total

    def balances(self) -> list[dict]:
        body = self._signed_ok("GET", "/fapi/v2/balance", None, "balance")
        if not isinstance(body, list):
            raise BinanceError("balance: не список")
        return body

    def available_margin(self, asset: str = "USDT") -> Decimal | None:
        """availableBalance актива маржи; None — ошибка или актива нет в ответе (не 0)."""
        try:
            rows = self.balances()
        except ModeForbidden:
            raise
        except Exception as e:
            log.warning("binance balance: %s", type(e).__name__)
            return None
        row = next((r for r in rows if isinstance(r, dict) and r.get("asset") == asset), None)
        if row is None or row.get("availableBalance") is None:
            return None
        try:
            return _d(row["availableBalance"])
        except BinanceError:
            return None

    def dual_side(self) -> bool:
        """True — hedge-режим (нам нельзя: reduceOnly в нём не отправить)."""
        body = self._signed_ok("GET", "/fapi/v1/positionSide/dual", None, "positionSide/dual")
        v = body.get("dualSidePosition") if isinstance(body, dict) else None
        if isinstance(v, str):
            return v.strip().lower() == "true"
        if isinstance(v, bool):
            return v
        raise BinanceError(f"positionSide/dual: неожиданный ответ {body!r}"[:200])

    def multi_assets(self) -> bool:
        """True — аккаунт в режиме Multi-Assets: изолированная маржа в нём запрещена (-4168)."""
        body = self._signed_ok("GET", "/fapi/v1/multiAssetsMargin", None, "multiAssetsMargin")
        v = body.get("multiAssetsMargin") if isinstance(body, dict) else None
        if isinstance(v, str) and v.strip().lower() in ("true", "false"):
            return v.strip().lower() == "true"
        if isinstance(v, bool):
            return v
        raise BinanceError(f"multiAssetsMargin: неожиданный ответ {body!r}"[:200])

    # --- настройка (идемпотентно) ----------------------------------------------------------------
    def setup(self, symbol: str, leverage: int, margin_type: str) -> None:
        """One-way режим, тип маржи (ISOLATED или CROSSED — тот, что владелец задал в owner.toml, без сужения
        кодом — как Aster; Gate сузил до ISOLATED отдельным решением владельца именно для Gate/FATCOIN, здесь
        такого решения не было), плечо. Повтор безвреден: -4059/-4046 («уже так») — успех."""
        if isinstance(leverage, bool) or not isinstance(leverage, int) or not 1 <= leverage <= 125:
            raise ValueError(f"плечо — целое 1…125, а не {leverage!r}")
        if margin_type not in ("ISOLATED", "CROSSED"):
            raise ValueError(f"тип маржи ISOLATED|CROSSED, а не {margin_type!r}")
        self._setup_step("/fapi/v1/positionSide/dual", {"dualSidePosition": False}, ok_code=-4059)
        try:
            self._setup_step("/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type}, ok_code=-4046)
        except BinanceApiError as e:
            if e.code == MULTI_ASSETS_NO_ISOLATED:
                raise BinanceError("аккаунт Binance в режиме Multi-Assets — изолированная маржа в нём запрещена. "
                                   "Переключите в Binance: Futures → настройки → Asset Mode → Single-Asset Mode "
                                   "(или разрешите CROSSED в owner.toml). Ничего не отправлено") from None
            raise
        body = self._signed_ok("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, "leverage")
        try:
            got = int(body.get("leverage"))
        except (TypeError, ValueError, AttributeError):
            raise BinanceError(f"leverage: в ответе нет плеча: {body!r}"[:200]) from None
        if got != leverage:
            raise BinanceError(f"leverage: просили {leverage}x, площадка поставила {got}x")
        log.info("binance setup %s: one-way, %s, %sx", symbol, margin_type, leverage)

    def _setup_step(self, path: str, params: dict, ok_code: int) -> None:
        st, body, _ = self.call("POST", path, params)
        if _is_error(st, body) and _code(body) != ok_code:
            raise BinanceApiError(path.rsplit("/v1/", 1)[-1], st, _code(body), _msg(body))

    # --- заявки ------------------------------------------------------------------------------------
    def _check_order(self, symbol: str, side: str, qty: Decimal, px: Decimal, cid: str) -> None:
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side BUY|SELL, а не {side!r}")
        if not CID_RE.match(cid or ""):
            raise ValueError(f"client_id не проходит шаблон Binance (до 36 симв., буквы/цифры/._-): {cid!r}")
        for name, v in (("qty", qty), ("px_cap", px)):
            if isinstance(v, bool) or not isinstance(v, Decimal) or not v.is_finite() or v <= 0:
                raise ValueError(f"{name} — положительный Decimal, а не {v!r}")
        hit = self._filters.get(symbol)
        if hit:     # фильтры в кэше — не отправляем то, что биржа точно отклонит (и не округляем молча за движок)
            f = hit[1]
            if qty % f.step != 0 or qty < f.min_qty or qty > f.max_qty_limit:
                raise ValueError(f"{symbol}: qty {qty} не по шагу {f.step} / вне [{f.min_qty}, {f.max_qty_limit}]")
            if px % f.tick != 0:
                raise ValueError(f"{symbol}: цена {px} не кратна тику {f.tick}")

    def ioc(self, symbol: str, side: str, qty: Decimal, px_cap: Decimal, client_id: str, reduce_only: bool,
            *, hedge: bool = False, on_signed: Callable[[int], None] | None = None) -> PerpFill:
        """LIMIT IOC с ценой-ограничителем, newOrderRespType=RESULT (итог сразу, без опроса).
        Не отправлено (ворота, бюджет, параметры) — исключение. Отправлено — PerpFill:
          FILLED / PARTIALLY_FILLED (финал, остаток истёк) / EXPIRED (0 исполнено) — ответ биржи;
          REJECTED + err_code — площадка отказала (-2019, -4164, -1111, -2022 …), заявки нет;
          UNKNOWN — 5xx/-1000/-1001/-1006/-1007/обрыв или не финальный статус: НЕ ПОВТОРЯТЬ, звать settle_unknown().
        hedge=True — хедж уже исполненной ноги (разрешён и на паузе). sign_nonce — метка времени (мс) подписи."""
        on_signed = self._ioc_callback(on_signed, symbol=symbol, side=side, quantity=qty,
                                        price=px_cap, client_id=client_id, reduce_only=reduce_only)
        self._check_order(symbol, side, qty, px_cap, client_id)
        params = {"symbol": symbol, "side": side, "type": "LIMIT", "timeInForce": "IOC", "quantity": qty,
                  "price": px_cap, "newClientOrderId": client_id, "reduceOnly": bool(reduce_only),
                  "newOrderRespType": "RESULT"}
        self.last_error = None
        try:
            st, body, n = self.call("POST", "/fapi/v1/order", params, hedge=hedge, critical=True, on_signed=on_signed)
        except BinanceNetError as e:
            self.last_error = str(e)
            log.warning("binance ioc %s %s %s: нет ответа — исход неизвестен, выясняю запросом", client_id, side, qty)
            return _pf(client_id, "UNKNOWN", nonce=e.ts)
        code = _code(body)
        if code in UNKNOWN_CODES or st >= 500 or st == 408:
            self.last_error = f"HTTP {st} code {code} {_msg(body)}"
            return _pf(client_id, "UNKNOWN", nonce=n, code=code)
        if _is_error(st, body):     # 4xx/отрицательный код: отказ до исполнения (429/418/403 — тоже не принята)
            self.last_error = f"HTTP {st} code {code} {_msg(body)}"
            log.warning("binance ioc %s отклонена: %s", client_id, self.last_error)
            return _pf(client_id, "REJECTED", nonce=n, code=code)
        if not isinstance(body, dict):
            return _pf(client_id, "UNKNOWN", nonce=n)
        f = order_to_fill(client_id, body, n)
        log.info("binance ioc %s %s %s@≤%s → %s %s avg %s", client_id, side, qty, px_cap, f.status, f.qty, f.avg_px)
        return f

    def query(self, symbol: str, client_id: str) -> PerpFill:
        """GET /fapi/v1/order?origClientOrderId. -2013 → NOT_FOUND (наблюдение, не доказательство — доказательство
        даёт только settle_unknown()). sign_nonce = 0: nonce запроса — не nonce заявки, в perp_orders его писать
        нельзя."""
        if not CID_RE.match(client_id or ""):
            raise ValueError(f"client_id не проходит шаблон Binance: {client_id!r}")
        try:
            st, body, _ = self.call("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id},
                                    critical=True)
        except ModeForbidden:
            raise
        except Exception as e:      # обрыв, бюджет, бан — запрос не дал ответа: исход по-прежнему неизвестен
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            return _pf(client_id, "UNKNOWN")
        code = _code(body)
        if code == NO_SUCH_ORDER:
            return _pf(client_id, "NOT_FOUND", code=code)
        if _is_error(st, body) or not isinstance(body, dict):
            self.last_error = f"HTTP {st} code {code} {_msg(body)}"
            return _pf(client_id, "UNKNOWN", code=code)
        return order_to_fill(client_id, body, 0)

    def settle_unknown(self, symbol: str, client_id: str, *, pos_before: Decimal | None, since_ms: int,
                       known_order_ids=frozenset()) -> PerpFill:
        """Выяснить исход заявки с неизвестным итогом — без повторной отправки. По образцу AsterTrade (НЕ Gate):
        origClientOrderId у Binance/Aster не имеет короткого окна поиска, в отличие от text-id Gate (~60 с) —
        поэтому здесь нет второго (Gate-подобного) списка «завершённых заявок», достаточно -2013 трижды И
        неизменной позиции И отсутствия чужих userTrades в окне. Если живые данные покажут, что origClientOrderId
        всё же перестаёт находиться раньше окончания retention, это допущение нужно будет усилить.
          найдена с финальным статусом → её PerpFill;
          -2013 ×n И позиция == pos_before И нет чужих userTrades с since_ms → NOT_FOUND (не выставлена: движок
            пишет NOT_PLACED и может слать снова ПОД НОВОЙ попыткой);
          иначе → UNKNOWN (пауза и сверка; повторной отправки нет никогда)."""
        n = max(1, UNKNOWN_QUERIES)
        gap = UNKNOWN_SPAN_S / max(n - 1, 1)
        not_found, last = 0, None
        for i in range(n):
            if i:
                self._sleep(gap)
            f = self.query(symbol, client_id)
            if f.status in FINAL:
                return f
            if f.status == "NOT_FOUND":
                not_found += 1
            else:
                last = f
        if not_found < n:
            return last or _pf(client_id, "UNKNOWN")
        unknown = _pf(client_id, "UNKNOWN", code=NO_SUCH_ORDER)
        if pos_before is None:
            return unknown
        pos = self.position(symbol)
        if pos is None or pos != pos_before:
            log.warning("binance %s: -2013 ×%d, но позиция %s ≠ %s — исход неизвестен", client_id, n, pos, pos_before)
            return unknown
        try:
            st, body, _ = self.call("GET", "/fapi/v1/userTrades",
                                    {"symbol": symbol, "startTime": int(since_ms) - TRADES_SLACK_MS, "limit": PAGE_LIMIT},
                                    critical=True)
        except ModeForbidden:
            raise
        except Exception:
            return unknown
        if _is_error(st, body) or not isinstance(body, list):
            return unknown
        known = {int(x) for x in known_order_ids}
        foreign = [t for t in body if isinstance(t, dict) and t.get("orderId") is not None
                   and int(t["orderId"]) not in known]
        if foreign or any(isinstance(t, dict) and t.get("orderId") is None for t in body):
            log.warning("binance %s: -2013 ×%d, но в userTrades %d новых сделок — исход неизвестен",
                        client_id, n, len(foreign))
            return unknown
        return _pf(client_id, "NOT_FOUND", code=NO_SUCH_ORDER)

    # --- учёт ------------------------------------------------------------------------------------
    def history_account(self):
        from .accounting import _hash
        if not self._key:
            raise BinanceError('history account needs a loaded API key')
        return 'acct:v1:binance:' + _hash((self.base, self._key))

    def history_fills(self, symbol, from_id):
        return self.fills(symbol, from_id, _strict=True)

    def history_funding(self, symbol, start_ms):
        return self.funding_income(symbol, start_ms, _strict=True)

    def fills(self, symbol: str, from_id: int | None, *, _strict=False) -> list[dict]:
        """Сделки userTrades (строки для store.add_perp_fills). from_id=None — последняя страница; иначе все с
        fromId постранично (1000 за раз, до MAX_PAGES). По trade_id без повторов, по возрастанию."""
        out: dict[int, dict] = {}
        fid = None if from_id is None else int(from_id)
        for _ in range(MAX_PAGES):
            params = {"symbol": symbol, "fromId": fid, "limit": PAGE_LIMIT}
            body = self._signed_ok("GET", "/fapi/v1/userTrades", params, "userTrades")
            if not isinstance(body, list):
                raise BinanceError("userTrades: не список")
            if _strict:
                from .history_validation import exact_id
                if any(type(t) is not dict or not exact_id(t.get('id')) or
                       not exact_id(t.get('orderId')) for t in body):
                    raise BinanceError('history native trade/order ID is not exact')
            rows = [_trade_row(t) for t in body]
            for r in rows:
                if _strict and (r['symbol'] != symbol or
                                (r['trade_id'] in out and out[r['trade_id']] != r)):
                    raise BinanceError('history fill namespace or duplicate conflict')
                out[r["trade_id"]] = r
            if fid is None or len(body) < PAGE_LIMIT:
                return [out[k] for k in sorted(out)]
            fid = max(r["trade_id"] for r in rows) + 1
        raise BinanceError(f"userTrades {symbol}: больше {MAX_PAGES} страниц — сузь from_id")

    def funding_income(self, symbol: str, start_ms: int, *, _strict=False) -> list[dict]:
        """Начисления FUNDING_FEE с start_ms до сейчас (строки для store.add_funding_income): окна по 7 сут
        (предел документации Binance), внутри окна — страницы по времени; дедуп по tranId."""
        seen: dict[int, dict] = {}
        now_ms = int(self._now() * 1000)
        s = int(start_ms)
        for _ in range(MAX_PAGES):
            if s > now_ms:
                break
            e = min(s + WEEK_MS - 1, now_ms)
            params = {"symbol": symbol, "incomeType": "FUNDING_FEE", "startTime": s, "endTime": e, "limit": PAGE_LIMIT}
            body = self._signed_ok("GET", "/fapi/v1/income", params, "income")
            if not isinstance(body, list):
                raise BinanceError("income: не список")
            for r in body:
                if _strict and (not isinstance(r, dict) or r.get('incomeType') != 'FUNDING_FEE' or
                                r.get('symbol') != symbol):
                    raise BinanceError('history funding namespace or type is unproven')
                if isinstance(r, dict) and r.get("incomeType", "FUNDING_FEE") == "FUNDING_FEE":
                    row = _income_row(r)
                    if _strict and row['tran_id'] in seen and seen[row['tran_id']] != row:
                        raise BinanceError('history funding duplicate conflict')
                    seen[row["tran_id"]] = row
            if len(body) >= PAGE_LIMIT:
                last = max(int(r["time"]) for r in body)
                if _strict and last <= s:
                    raise BinanceError('history funding timestamp saturated; coverage gap')
                s = last if last > s else s + 1
            else:
                s = e + 1
        else:
            if s <= now_ms:
                raise BinanceError(f"income {symbol}: больше {MAX_PAGES} окон — передай start_ms ближе")
        return sorted(seen.values(), key=lambda r: (r["ts"], r["tran_id"]))

    def health(self) -> dict:
        h = self.http.health()
        h.update(backoff_until=int(self.backoff_until), key=self._key)
        return h
