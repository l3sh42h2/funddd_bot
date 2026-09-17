"""Перп-нога Aster v3: подпись EIP-712 агентом (API-кошельком) и PerpLeg поверх REST (trade_spec §4, отчёт aster).

Подпись. Подписывается НЕ набор полей, а ровно та строка, что уходит на площадку: urlencode(params + nonce + [user]
+ signer) в порядке вставки, без пересортировки; её же кладём в query (GET) или в тело формы (POST/PUT/DELETE), и
`&signature=0x…` дописывается последним. Любая библиотека, «вежливо» переупорядочившая параметры или перекодировавшая
строку, ломает подпись — поэтому запрос собирается из готовой строки, а не из dict. В eth_account 0.13 нет
encode_structured_data из примеров документации (ImportError) — только encode_typed_data(full_message=…); hexbytes ≥ 1
отдаёт .hex() без 0x — префикс ставим сами.

Nonce — микросекунды, строго возрастающий, ОДИН генератор на агента в процессе: сервер помнит последние 100 nonce
агента, повтор — «дубликат», меньше минимума — «просрочен». Поэтому агент не делится ни с кем (ни с другим ботом,
ни с ручной торговлей), а timestamp/recvWindow в v3 нет вовсе — их заменяет nonce (допуск закладываем ±10 с).

Ворота режима на нижнем уровне (§8): call() спрашивает режим owner.toml и флаг паузы у движка на КАЖДЫЙ вызов.
GET — readonly/live; POST/PUT/DELETE — только live и не на паузе (кроме hedge=True: хедж уже исполненной ноги DEX
ставится и после «стоп», Q7). Ворота срабатывают ДО подписи: запрещённый вызов не тратит nonce и не уходит в сеть.

Неизвестный исход (HTTP 5xx, -1006, -1007, таймаут, обрыв): заявка могла исполниться. Здесь НИКОГДА нет повтора
отправки — ioc() возвращает UNKNOWN, а settle_unknown() выясняет исход запросом по origClientOrderId. «Не выставлена»
(NOT_FOUND) — только после -2013 трижды за ~5 с И неизменных positionRisk и userTrades; тогда движок пишет NOT_PLACED
и может послать снова под НОВЫМ номером попытки. newClientOrderId уникален у Aster лишь среди открытых заявок —
исполненный IOC им не дедуплицируется, поэтому «повторить на всякий случай» = удвоить позицию.

Вес. Заголовок X-MBX-USED-WEIGHT-1M — на IP, общий с коллектором; читается так же, как в client.BinanceLike
(тем же объектом: публичные чтения идут через BinanceLike.get). Обычные чтения пропускаются с 70 % бюджета, путь
заявки (ioc/query/позиция) — до 95 %: хедж, не отправленный из-за чужого веса, опаснее, чем лишний запрос.
"""
from __future__ import annotations
import json, logging, re, threading, time, urllib.parse
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from .. import config
from ..client import BannedError, BinanceLike, BudgetExceeded
from . import tconfig
from .keys import KeyMismatch, Keys, ModeForbidden, gate as mode_gate
from ..symbols import norm_symbol_factor
from .types import Book, Filters, PerpFill, PerpInstrument

log = logging.getLogger(__name__)
D = Decimal
_D0 = Decimal(0)

VENUE = "aster"
_TYPES = {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                           {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
          "Message": [{"name": "msg", "type": "string"}]}
_DOMAIN = {"name": "AsterSignTransaction", "version": "1", "chainId": tconfig.ASTER_EIP712_CHAIN_ID,
           "verifyingContract": "0x0000000000000000000000000000000000000000"}
_RESERVED = frozenset({"nonce", "user", "signer", "signature"})
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_CID_RE = re.compile(tconfig.ASTER_CLIENT_ID_RE)
_METHODS = frozenset({"GET", "POST", "PUT", "DELETE"})
_FORM = {"Content-Type": "application/x-www-form-urlencoded"}

SIGNED_TIMEOUT = (5, 10)            # (connect, read): дольше — исход заявки неизвестен, выясняем запросом
SOFT_READ = config.WEIGHT_SOFT_LIMIT
SOFT_ORDER = 0.95                   # путь заявки: ioc / query / positionRisk / settle
FILTERS_TTL_S = 3600                # фильтры меняются редко; устаревшие лучше, чем никаких (биржа отклонит -1111)
DEPTH_LIMITS = (5, 10, 20, 50)      # вес 2; больше 50 не берём (tconfig.ASTER_DEPTH_LIMIT)
PAGE_LIMIT = 1000
MAX_PAGES = 16                      # income стоит 30 веса: 16 окон по 7 сут ≈ 480 — дальше пусть движок сузит start
WEEK_MS = 7 * 24 * 3600 * 1000      # окно income/userTrades по документации — не больше 7 суток
TRADES_SLACK_MS = 5000              # окно userTrades в settle_unknown раньше отправки: часы ±2 с (live) + запас
UNKNOWN_CODES = frozenset({-1006, -1007})   # «исполнение неизвестно, могло пройти» [DOC 170-172]
MULTI_ASSETS_NO_ISOLATED = -4168            # «Unable to adjust to isolated-margin mode under the Multi-Assets mode» [L 12.09]
NO_SUCH_ORDER = -2013
FINAL = frozenset({"FILLED", "PARTIALLY_FILLED", "EXPIRED", "REJECTED"})


# --- ошибки ---------------------------------------------------------------------------------------
class AsterError(RuntimeError):
    """Отказ ноги Aster (разбор ответа, фильтры, несоответствие)."""


class AsterApiError(AsterError):
    """Площадка ответила кодом ошибки: http, code (отрицательный код Aster или None), msg."""

    def __init__(self, what: str, http: int, code: int | None, msg: str):
        self.http, self.code, self.msg = http, code, msg
        super().__init__(f"{what}: HTTP {http} code {code} {msg}"[:300])


class AsterNetError(AsterError):
    """Ответа нет (таймаут, обрыв). Для отправки — исход НЕИЗВЕСТЕН; nonce подписи — для noop/guarded cancel."""

    def __init__(self, method: str, path: str, nonce: int, cause: BaseException):
        self.method, self.path, self.nonce = method, path, nonce
        super().__init__(f"{method} {path}: нет ответа ({type(cause).__name__})")


# --- числа ----------------------------------------------------------------------------------------
def dec_str(d: Decimal) -> str:
    """Decimal → строка без экспоненты и хвостовых нулей ('1E+2' → '100', '0.50' → '0.5'). float не принимается:
    в состоянии денег float нет, а repr float в запросе — это другой шаг цены."""
    if isinstance(d, bool) or not isinstance(d, Decimal):
        raise TypeError(f"ожидался Decimal, получен {type(d).__name__}")
    if not d.is_finite():
        raise ValueError(f"не число: {d}")
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def fmt_param(v: Any) -> str:
    """Значение параметра запроса: bool → "true"/"false" (Python True url-кодируется как 'True'), Decimal без
    экспоненты, int как есть, str как есть. float — TypeError."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Decimal):
        return dec_str(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return v
    raise TypeError(f"параметр {type(v).__name__} не поддерживается (float запрещён — Decimal)")


def floor_step(x: Decimal, step: Decimal) -> Decimal:
    """Вниз до кратного шагу (количество: не продать больше, чем пришло с DEX)."""
    if step <= 0:
        raise ValueError("шаг ≤ 0")
    return (x // step) * step


def ceil_step(x: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("шаг ≤ 0")
    q = x // step
    return (q if q * step == x else q + 1) * step


def _d(x: Any) -> Decimal:
    if isinstance(x, bool) or x is None:
        raise AsterError(f"не число: {x!r}")
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError):
        raise AsterError(f"не число: {x!r}") from None


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
    """Retry-After в секундах (это время, не деньги). Не число (HTTP-дата, мусор) — default: исключение здесь
    вылетело бы уже ПОСЛЕ отправки заявки, и движок получил бы исключение вместо PerpFill."""
    try:
        return max(float(r.headers.get("Retry-After") or default), 0.0)
    except (TypeError, ValueError):
        return default


def _msg(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("msg") or body.get("_raw") or "")[:200]
    return ""


# --- nonce и подпись ------------------------------------------------------------------------------
class MonotonicNonce:
    """µs, строго возрастающий: max(часы, прошлый + 1). Часы, ушедшие назад, не дают повтора — счёт идёт от
    прошлого значения (сервер отбросит лишь при уходе дальше допуска, а не молча примет дубликат)."""

    def __init__(self, now_us: Callable[[], int] | None = None):
        self._now_us = now_us or (lambda: time.time_ns() // 1000)
        self._last = 0
        self._lk = threading.Lock()

    def next(self) -> int:
        with self._lk:
            self._last = max(int(self._now_us()), self._last + 1)
            return self._last

    @property
    def last(self) -> int:
        return self._last


_NONCES: dict[str, MonotonicNonce] = {}
_NONCES_LK = threading.Lock()


def nonce_for(signer: str) -> MonotonicNonce:
    """Один генератор на адрес агента в процессе: два AsterSigner одного агента не выдадут одинаковый nonce."""
    k = signer.lower()
    with _NONCES_LK:
        if k not in _NONCES:
            _NONCES[k] = MonotonicNonce()
        return _NONCES[k]


def typed_message(msg: str) -> dict:
    """EIP-712 full_message для encode_typed_data: домен AsterSignTransaction/1/1666/0x0…0, Message(string msg)."""
    return {"types": _TYPES, "primaryType": "Message", "domain": dict(_DOMAIN), "message": {"msg": msg}}


class AsterSigner:
    """Подписант v3. acct — keys.SignerKey (или LocalAccount в тестах): нужны .address и .sign_message().
    user — адрес ОСНОВНОГО аккаунта (мастер), signer — адрес агента; подписывает ключ агента."""

    def __init__(self, user: str, signer: str, acct, send_user: bool, nonces: MonotonicNonce | None = None):
        for name, a in (("user", user), ("signer", signer)):
            if not _ADDR_RE.match(str(a or "")):
                raise ValueError(f"Aster {name}: не адрес 0x…40 hex: {a!r}")
        if str(acct.address).lower() != signer.lower():
            raise KeyMismatch(f"ключ агента даёт адрес {acct.address}, а signer = {signer}")
        if signer.lower() == user.lower():
            raise KeyMismatch("signer Aster = user: это мастер-ключ основного аккаунта, нужен отдельный агент (Q3)")
        from eth_account.messages import encode_typed_data   # здесь: dry-режим работает и без extra `trade`
        self._encode = encode_typed_data
        self.user, self.signer, self.send_user = user, signer, bool(send_user)
        self._acct = acct
        self.nonces = nonces or nonce_for(signer)

    def __repr__(self) -> str:
        return f"AsterSigner(user={self.user}, signer={self.signer}, send_user={self.send_user})"

    def sign_qs(self, params: dict) -> tuple[str, int]:
        """(строка запроса с подписью последней, nonce). None-значения выбрасываются; порядок — порядок вставки."""
        bad = _RESERVED & set(params)
        if bad:
            raise ValueError(f"зарезервированные параметры ставит подписант: {sorted(bad)}")
        pairs = [(str(k), fmt_param(v)) for k, v in params.items() if v is not None]
        n = self.nonces.next()
        pairs.append(("nonce", str(n)))
        if self.send_user:
            pairs.append(("user", self.user))
        pairs.append(("signer", self.signer))
        msg = urllib.parse.urlencode(pairs)
        signed = self._acct.sign_message(self._encode(full_message=typed_message(msg)))
        return f"{msg}&signature=0x{bytes(signed.signature).hex()}", n


# --- разбор ответов -------------------------------------------------------------------------------
def parse_filters(sym: dict) -> Filters:
    """Строка exchangeInfo.symbols → Filters. MARKET_LOT_SIZE нет — берём LOT_SIZE; MIN_NOTIONAL нет — 0."""
    f = {x.get("filterType"): x for x in sym.get("filters") or ()}
    pf, lot = f.get("PRICE_FILTER"), f.get("LOT_SIZE")
    if not pf or not lot:
        raise AsterError(f"{sym.get('symbol')}: нет PRICE_FILTER/LOT_SIZE")
    mlot = f.get("MARKET_LOT_SIZE") or lot
    mn = f.get("MIN_NOTIONAL") or {}
    tick, step = _d(pf["tickSize"]), _d(lot["stepSize"])
    if tick <= 0 or step <= 0:
        raise AsterError(f"{sym.get('symbol')}: шаг цены/количества ≤ 0")
    return Filters(tick=tick, step=step, min_qty=_d(lot.get("minQty", "0")), max_qty_limit=_d(lot["maxQty"]),
                   max_qty_market=_d(mlot["maxQty"]), min_notional=_d(mn.get("notional", mn.get("minNotional", "0"))),
                   tifs=frozenset(sym.get("timeInForce") or ()))


def _pf(cid: str, status: str, *, order_id: int | None = None, qty: Decimal = _D0, avg: Decimal = _D0,
        quote: Decimal = _D0, nonce: int = 0, code: int | None = None) -> PerpFill:
    return PerpFill(client_id=cid, order_id=order_id, status=status, qty=qty, avg_px=avg, quote=quote,
                    sign_nonce=nonce, err_code=code)


def order_to_fill(cid: str, body: dict, nonce: int) -> PerpFill:
    """Ответ заявки (RESULT или запрос) → PerpFill. PARTIALLY_FILLED здесь — ФИНАЛ: часть исполнена, остаток IOC
    истёк (у Binance-подобных это EXPIRED/CANCELED с executedQty > 0). Не финальные NEW/PARTIALLY_FILLED биржи
    → UNKNOWN с order_id: заявка существует, итог ещё не известен — спросить снова, а не считать."""
    try:
        oid = int(body["orderId"]) if body.get("orderId") is not None else None
        qty = _d(body.get("executedQty", "0"))
        avg = _d(body.get("avgPrice", "0"))
        quote = _d(body.get("cumQuote", "0"))
    except (AsterError, TypeError, ValueError):
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


def conditional_to_fill(cid: str, body: dict, nonce: int) -> PerpFill:
    """Aster conditional order response.  Unlike IOC, NEW is a proven armed order, not UNKNOWN.  It has no
    execution yet and therefore carries quantity zero until a later query says FILLED/PARTIALLY_FILLED."""
    f = order_to_fill(cid, body, nonce)
    if f.order_id is not None and str(body.get("status") or "") in ("NEW", "PENDING_NEW"):
        return _pf(cid, "OPEN", order_id=f.order_id, nonce=nonce)
    return f


def _trade_row(t: dict) -> dict:
    """userTrades → строка для store.add_perp_fills. commission — модулем: в примере документации она со знаком «−»."""
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


class AsterTrade(JournalBoundIoc):
    """PerpLeg для Aster. Публичное (фильтры, стакан, фандинг, время) — в любом режиме, без ключей; подписанное —
    через call() с воротами. mode_state() → (режим owner.toml, пауза): движок читает их заново на каждый вызов;
    не задан или упал — считаем dry (закрыто)."""
    venue = VENUE
    ioc_partial_terminal = True  # order_to_fill maps non-final exchange partial to UNKNOWN
    BASE = tconfig.ASTER_BASE

    def __init__(self, signer: AsterSigner | None = None, *, keys: Keys | None = None,
                 mode_state: Callable[[], tuple[str | None, bool]] | None = None, session=None,
                 base: str | None = None, timeout=SIGNED_TIMEOUT, now: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, public_retries: int = 2):
        self.base = (base or self.BASE).rstrip("/")
        self.http = BinanceLike(VENUE, self.base, config.EXCHANGES[VENUE]["weight_limit"], session=session)
        self._s = self.http._s
        self.signer, self._keys = signer, keys
        self._mode_state = mode_state or (lambda: ("dry", False))
        self.timeout = timeout
        self._now, self._sleep = now, sleep
        self.public_retries = public_retries
        self.backoff_until = 0.0            # после 429: до этого момента подписанные вызовы не делаем
        self.order_count: dict[str, int] = {}
        self.last_error: str | None = None  # текст последнего отказа ioc/query — для err в perp_orders
        self._filters: dict[str, tuple[float, Filters]] = {}
        self._status: dict[str, str] = {}
        self._meta: dict[str, dict] = {}    # baseAsset/quoteAsset/contractType — для instrument() (ревью 13.09)

    @classmethod
    def from_keys(cls, keys: Keys, mode_state: Callable[[], tuple[str | None, bool]],
                  send_user: bool | None = None, **kw) -> "AsterTrade":
        """Боевая сборка: подписант из keys.aster (агент), ворота — по меньшему из режима загрузки и файла."""
        su = tconfig.aster_send_user() if send_user is None else send_user
        return cls(AsterSigner(keys.aster_user, keys.aster_signer, keys.aster, su), keys=keys,
                   mode_state=mode_state, **kw)

    # --- ворота, бюджет, HTTP --------------------------------------------------------------------
    def _gate(self, action: str, hedge: bool) -> None:
        try:
            mode, paused = self._mode_state()
        except Exception as e:      # не прочитали режим/паузу — закрыто
            raise ModeForbidden(f"режим/пауза не прочитаны ({type(e).__name__}): {action} запрещено") from None
        if self._keys is not None:
            self._keys.gate(mode, action, paused=bool(paused), hedge=hedge)
        else:
            mode_gate(mode, action, paused=bool(paused), hedge=hedge)

    def _check_budget(self, path: str, critical: bool) -> None:
        now = self._now()
        if now < self.http.banned_until:
            raise BannedError(f"aster: бан (418) ещё {self.http.banned_until - now:.0f} с, {path} не отправлен")
        if now < self.backoff_until:
            raise BudgetExceeded(f"aster: пауза после 429 ещё {self.backoff_until - now:.1f} с, {path} не отправлен")
        soft = SOFT_ORDER if critical else SOFT_READ
        if self.http.budget_used() >= soft:
            raise BudgetExceeded(f"aster: вес {self.http.budget_used():.0%} ≥ {soft:.0%}, {path} не отправлен")

    def _absorb(self, r) -> tuple[int, Any]:
        self.http._read_weight(r)
        for k, v in r.headers.items():
            if k.lower().startswith("x-mbx-order-count-"):
                try:
                    self.order_count[k.lower()[len("x-mbx-order-count-"):]] = int(v)
                except ValueError:
                    pass
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
        """Подписанный вызов → (HTTP-статус, JSON, nonce). Ошибки площадки НЕ бросает — их разбирает вызывающий
        (ему видно, была ли это отправка). Бросает до сети: ModeForbidden (ворота), BannedError/BudgetExceeded
        (бюджет), ValueError/TypeError (параметры); после подписи: AsterNetError (ответа нет).
        on_signed(nonce) — после подписи и ДО отправки: движок пишет nonce в perp_orders (запись-до); упал — не шлём."""
        m = method.upper()
        if m not in _METHODS:
            raise ValueError(f"метод {method}")
        self._gate("signed_read" if m == "GET" else "send", hedge)
        if self.signer is None:
            raise ModeForbidden("нет подписанта Aster (ключи не загружены): подписанный вызов запрещён")
        self._check_budget(path, critical)
        qs, n = self.signer.sign_qs(dict(params or {}))
        if on_signed is not None:
            on_signed(n)
        url = self.base + path
        try:
            if m == "GET":
                r = self._s.request("GET", f"{url}?{qs}", timeout=self.timeout)
            else:   # тело формы — ровно подписанная строка, байт в байт (requests её не перекодирует)
                r = self._s.request(m, url, data=qs.encode("ascii"), headers=_FORM, timeout=self.timeout)
        except Exception as e:      # noqa: для отправки любой обрыв = исход неизвестен
            self.http.n_err += 1
            raise AsterNetError(m, path, n, e) from None
        status, body = self._absorb(r)
        return status, body, n

    def _signed_ok(self, method: str, path: str, params: dict | None = None, what: str = "", **kw) -> Any:
        st, body, _ = self.call(method, path, params, **kw)
        if _is_error(st, body):
            raise AsterApiError(what or f"{method} {path}", st, _code(body), _msg(body))
        return body

    def _public(self, path: str, params: dict | None = None) -> Any:
        return self.http.get(path, params, retries=self.public_retries)

    # --- публичное ---------------------------------------------------------------------------------
    def server_time_ms(self) -> int:
        return int(self._public("/fapi/v3/time")["serverTime"])

    def clock_offset_s(self) -> float:
        """Сервер − локальные часы, с поправкой на половину RTT."""
        t0 = self._now()
        ms = self.server_time_ms()
        t1 = self._now()
        return ms / 1000.0 - (t0 + t1) / 2.0

    def check_clock(self, max_s: float = tconfig.ASTER_CLOCK_SKEW_MAX_S) -> float:
        """live не стартует при |часы − /fapi/v3/time| > 2 с: nonce живёт в окне ±10 с, запас нужен на RTT и дрейф."""
        off = self.clock_offset_s()
        if abs(off) > max_s:
            raise AsterError(f"часы расходятся с Aster на {off:+.2f} с (> {max_s} с): проверь NTP, live не запускаю")
        return off

    def _load_filters(self) -> None:
        ei = self._public("/fapi/v3/exchangeInfo")
        t = self._now()
        for sym in ei.get("symbols") or ():
            try:
                self._filters[sym["symbol"]] = (t, parse_filters(sym))
                self._status[sym["symbol"]] = str(sym.get("status") or "")
                self._meta[sym["symbol"]] = {"base": sym.get("baseAsset"), "quote": sym.get("quoteAsset"),
                                             "ctype": sym.get("contractType")}
            except (AsterError, KeyError):
                continue

    def filters(self, symbol: str) -> Filters:
        hit = self._filters.get(symbol)
        if hit and self._now() - hit[0] < FILTERS_TTL_S:
            return hit[1]
        try:
            self._load_filters()
        except Exception as e:
            if hit:
                log.warning("aster: exchangeInfo не обновлён (%s), беру фильтры %s из кэша", type(e).__name__, symbol)
                return hit[1]
            raise
        hit = self._filters.get(symbol)
        if not hit:
            raise AsterError(f"{symbol}: нет в exchangeInfo Aster")
        return hit[1]

    def instrument(self, symbol: str) -> PerpInstrument:
        """Контракт по exchangeInfo (тот же кэш, что filters): m = токенов в одном контракте по baseAsset
        (symbols.norm_symbol_factor: 1000BONK → BONK ×1000). У Aster в exchangeInfo нет contractSize — множитель
        живёт только в baseAsset (снимок 12.09: 12 символов «1000X», у всех symbol начинается с baseAsset).
        baseAsset нет — base/m = None: движок откажет во входе."""
        self.filters(symbol)                       # свежесть кэша и «нет в exchangeInfo» — как у фильтров
        meta = self._meta.get(symbol) or {}
        ba = meta.get("base") or None
        base, fac = norm_symbol_factor(str(ba)) if ba else (None, None)
        return PerpInstrument(symbol, ba, base, None if fac is None else Decimal(int(fac)), meta.get("quote"),
                              meta.get("ctype"))

    def status(self, symbol: str) -> str | None:
        """Статус символа из последнего exchangeInfo (TRADING/…); None — ещё не загружали."""
        return self._status.get(symbol)

    def book(self, symbol: str, limit: int = 20) -> Book:
        want = max(1, min(int(limit), tconfig.ASTER_DEPTH_LIMIT))
        lim = next(x for x in DEPTH_LIMITS if x >= want)
        b = self._public("/fapi/v3/depth", {"symbol": symbol, "limit": lim})
        bids = tuple((_d(p), _d(q)) for p, q in (b.get("bids") or ())[:want])
        asks = tuple((_d(p), _d(q)) for p, q in (b.get("asks") or ())[:want])
        return Book(bids=bids, asks=asks, ts=self._now())

    def funding(self, symbol: str) -> tuple[Decimal, Decimal, int]:
        """(марк, последняя ставка за интервал, время следующего начисления мс) из premiumIndex."""
        p = self._public("/fapi/v3/premiumIndex", {"symbol": symbol})
        if isinstance(p, list):
            p = next((x for x in p if x.get("symbol") == symbol), None) or {}
        return _d(p["markPrice"]), _d(p["lastFundingRate"]), int(p["nextFundingTime"])

    # --- подписанные чтения ------------------------------------------------------------------------
    def position_risk(self, symbol: str | None = None) -> list[dict]:
        """Сырые строки positionRisk (liquidationPrice, isolatedMargin, leverage — для отчёта)."""
        body = self._signed_ok("GET", "/fapi/v3/positionRisk", {"symbol": symbol}, "positionRisk")
        if not isinstance(body, list):
            raise AsterError("positionRisk: не список")
        return body

    def position(self, symbol: str) -> Decimal | None:
        """Знаковая позиция (< 0 — шорт). None — НЕИЗВЕСТНО: ошибка, пустой ответ, строки hedge-режима.
        Пустое чтение за «флэт» не выдаём никогда (урок: призраки позиций)."""
        try:
            st, body, _ = self.call("GET", "/fapi/v3/positionRisk", {"symbol": symbol}, critical=True)
        except ModeForbidden:
            raise
        except Exception as e:
            log.warning("aster positionRisk %s: %s", symbol, type(e).__name__)
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
            except (AsterError, KeyError):
                return None
        return total

    def balances(self) -> list[dict]:
        body = self._signed_ok("GET", "/fapi/v3/balance", None, "balance")
        if not isinstance(body, list):
            raise AsterError("balance: не список")
        return body

    def available_margin(self, asset: str = "USDT") -> Decimal | None:
        """availableBalance актива маржи; None — ошибка или актива нет в ответе (не 0)."""
        try:
            rows = self.balances()
        except ModeForbidden:
            raise
        except Exception as e:
            log.warning("aster balance: %s", type(e).__name__)
            return None
        row = next((r for r in rows if isinstance(r, dict) and r.get("asset") == asset), None)
        if row is None or row.get("availableBalance") is None:
            return None
        try:
            return _d(row["availableBalance"])
        except AsterError:
            return None

    def commission_rate(self, symbol: str) -> dict:
        return self._signed_ok("GET", "/fapi/v3/commissionRate", {"symbol": symbol}, "commissionRate")

    def leverage_bracket(self, symbol: str) -> Any:
        return self._signed_ok("GET", "/fapi/v3/leverageBracket", {"symbol": symbol}, "leverageBracket")

    def dual_side(self) -> bool:
        """True — hedge-режим (нам нельзя: reduceOnly в нём не отправить). Вес 30 — только в статус/сверку."""
        body = self._signed_ok("GET", "/fapi/v3/positionSide/dual", None, "positionSide/dual")
        v = body.get("dualSidePosition") if isinstance(body, dict) else None
        if isinstance(v, str):
            return v.strip().lower() == "true"
        if isinstance(v, bool):
            return v
        raise AsterError(f"positionSide/dual: неожиданный ответ {body!r}"[:200])

    def multi_assets(self) -> bool:
        """True — аккаунт в режиме Multi-Assets: изолированная маржа в нём запрещена (-4168; первая живая команда
        владельца 12.09 остановилась на этом до approve и свопа). Только чтение — в статус/предпроверку."""
        body = self._signed_ok("GET", "/fapi/v3/multiAssetsMargin", None, "multiAssetsMargin")
        v = body.get("multiAssetsMargin") if isinstance(body, dict) else None
        if isinstance(v, str) and v.strip().lower() in ("true", "false"):
            return v.strip().lower() == "true"
        if isinstance(v, bool):
            return v
        raise AsterError(f"multiAssetsMargin: неожиданный ответ {body!r}"[:200])

    # --- настройка (идемпотентно) ----------------------------------------------------------------
    def setup(self, symbol: str, leverage: int, margin_type: str) -> None:
        """One-way режим, тип маржи, плечо. Повтор безвреден: -4059/-4046 («уже так») — успех, плечо сверяется по
        ответу. Поэтому при обрыве (AsterNetError) движок может просто вызвать setup ещё раз."""
        if isinstance(leverage, bool) or not isinstance(leverage, int) or not 1 <= leverage <= 125:
            raise ValueError(f"плечо — целое 1…125, а не {leverage!r}")
        if margin_type not in ("ISOLATED", "CROSSED"):
            raise ValueError(f"тип маржи ISOLATED|CROSSED, а не {margin_type!r}")
        self._setup_step("/fapi/v3/positionSide/dual", {"dualSidePosition": False}, ok_code=-4059)
        try:
            self._setup_step("/fapi/v3/marginType", {"symbol": symbol, "marginType": margin_type}, ok_code=-4046)
        except AsterApiError as e:
            if e.code == MULTI_ASSETS_NO_ISOLATED:
                raise AsterError("аккаунт Aster в режиме Multi-Assets — изолированная маржа в нём запрещена. Переключите "
                                 "в Aster: Futures → настройки → Asset Mode → Single-Asset (или разрешите CROSSED в "
                                 "owner.toml). Ничего не отправлено") from None
            raise
        body = self._signed_ok("POST", "/fapi/v3/leverage", {"symbol": symbol, "leverage": leverage}, "leverage")
        try:
            got = int(body.get("leverage"))
        except (TypeError, ValueError, AttributeError):
            raise AsterError(f"leverage: в ответе нет плеча: {body!r}"[:200]) from None
        if got != leverage:
            raise AsterError(f"leverage: просили {leverage}x, площадка поставила {got}x")
        log.info("aster setup %s: one-way, %s, %sx", symbol, margin_type, leverage)

    def _setup_step(self, path: str, params: dict, ok_code: int) -> None:
        st, body, _ = self.call("POST", path, params)
        if _is_error(st, body) and _code(body) != ok_code:
            raise AsterApiError(path.rsplit("/v3/", 1)[-1], st, _code(body), _msg(body))

    # --- заявки ------------------------------------------------------------------------------------
    def _check_order(self, symbol: str, side: str, qty: Decimal, px: Decimal, cid: str) -> None:
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side BUY|SELL, а не {side!r}")
        if not _CID_RE.match(cid or ""):
            raise ValueError(f"client_id не проходит шаблон Aster: {cid!r}")
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
          UNKNOWN — 5xx/-1006/-1007/обрыв или не финальный статус: НЕ ПОВТОРЯТЬ, звать settle_unknown().
        hedge=True — хедж уже исполненной ноги DEX (разрешён и на паузе). sign_nonce — nonce подписи заявки."""
        on_signed = self._ioc_callback(on_signed, symbol=symbol, side=side, quantity=qty,
                                        price=px_cap, client_id=client_id, reduce_only=reduce_only)
        self._check_order(symbol, side, qty, px_cap, client_id)
        params = {"symbol": symbol, "side": side, "type": "LIMIT", "timeInForce": "IOC", "quantity": qty,
                  "price": px_cap, "newClientOrderId": client_id, "reduceOnly": bool(reduce_only),
                  "newOrderRespType": "RESULT"}
        self.last_error = None
        try:
            st, body, n = self.call("POST", "/fapi/v3/order", params, hedge=hedge, critical=True, on_signed=on_signed)
        except AsterNetError as e:
            self.last_error = str(e)
            log.warning("aster ioc %s %s %s: нет ответа — исход неизвестен, выясняю запросом", client_id, side, qty)
            return _pf(client_id, "UNKNOWN", nonce=e.nonce)
        code = _code(body)
        if code in UNKNOWN_CODES or st >= 500 or st == 408:
            self.last_error = f"HTTP {st} code {code} {_msg(body)}"
            return _pf(client_id, "UNKNOWN", nonce=n, code=code)
        if _is_error(st, body):     # 4xx/отрицательный код: отказ до исполнения (429/418/403 WAF — тоже не принята)
            self.last_error = f"HTTP {st} code {code} {_msg(body)}"
            log.warning("aster ioc %s отклонена: %s", client_id, self.last_error)
            return _pf(client_id, "REJECTED", nonce=n, code=code)
        if not isinstance(body, dict):
            return _pf(client_id, "UNKNOWN", nonce=n)
        f = order_to_fill(client_id, body, n)
        log.info("aster ioc %s %s %s@≤%s → %s %s avg %s", client_id, side, qty, px_cap, f.status, f.qty, f.avg_px)
        return f

    def take_profit_on_fall(self, symbol: str, qty: Decimal, stop_price: Decimal, client_id: str, *,
                            working_type: str, on_signed: Callable[[int], None] | None = None) -> PerpFill:
        """Arm the protective BUY reduce-only conditional for a spot-long / perp-short deal.

        At a falling price, the short is closed by TAKE_PROFIT_MARKET BUY.  STOP_MARKET would mean the opposite
        trigger direction for a short and is deliberately not used.  ``quantity`` is exact and frozen; closePosition
        is avoided because it could close a manual/foreign position on the same symbol.
        """
        if working_type not in ("MARK_PRICE", "CONTRACT_PRICE"):
            raise ValueError("working_type MARK_PRICE|CONTRACT_PRICE")
        self._check_order(symbol, "BUY", qty, stop_price, client_id)
        params = {"symbol": symbol, "side": "BUY", "type": "TAKE_PROFIT_MARKET", "quantity": qty,
                  "stopPrice": stop_price, "newClientOrderId": client_id, "reduceOnly": True,
                  "workingType": working_type, "newOrderRespType": "RESULT"}
        self.last_error = None
        try:
            st, body, n = self.call("POST", "/fapi/v3/order", params, critical=True, on_signed=on_signed)
        except AsterNetError as e:
            self.last_error = str(e)
            return _pf(client_id, "UNKNOWN", nonce=e.nonce)
        code = _code(body)
        if code in UNKNOWN_CODES or st >= 500 or st == 408:
            self.last_error = f"HTTP {st} code {code} {_msg(body)}"
            return _pf(client_id, "UNKNOWN", nonce=n, code=code)
        if _is_error(st, body):
            self.last_error = f"HTTP {st} code {code} {_msg(body)}"
            return _pf(client_id, "REJECTED", nonce=n, code=code)
        if not isinstance(body, dict):
            return _pf(client_id, "UNKNOWN", nonce=n)
        return conditional_to_fill(client_id, body, n)

    def query_conditional(self, symbol: str, client_id: str) -> PerpFill:
        """Read an armed conditional order without treating its NEW state as a missing outcome."""
        if not _CID_RE.match(client_id or ""):
            raise ValueError(f"client_id не проходит шаблон Aster: {client_id!r}")
        try:
            st, body, _ = self.call("GET", "/fapi/v3/order", {"symbol": symbol, "origClientOrderId": client_id},
                                    critical=True)
        except ModeForbidden:
            raise
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            return _pf(client_id, "UNKNOWN")
        code = _code(body)
        if code == NO_SUCH_ORDER:
            return _pf(client_id, "NOT_FOUND", code=code)
        if _is_error(st, body) or not isinstance(body, dict):
            self.last_error = f"HTTP {st} code {code} {_msg(body)}"
            return _pf(client_id, "UNKNOWN", code=code)
        return conditional_to_fill(client_id, body, 0)

    def query(self, symbol: str, client_id: str) -> PerpFill:
        """GET /order?origClientOrderId. Один -2013 → NOT_FOUND, но это ОДНО наблюдение, не доказательство
        (заявка могла ещё не дойти до движка биржи) — доказательство даёт только settle_unknown().
        sign_nonce = 0: nonce запроса — не nonce заявки, в perp_orders его писать нельзя."""
        if not _CID_RE.match(client_id or ""):
            raise ValueError(f"client_id не проходит шаблон Aster: {client_id!r}")
        try:
            st, body, _ = self.call("GET", "/fapi/v3/order", {"symbol": symbol, "origClientOrderId": client_id},
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
        """Выяснить исход заявки с неизвестным итогом — без повторной отправки.
          найдена с финальным статусом → её PerpFill (с order_id и исполнением);
          -2013 трижды за ~5 с И позиция == pos_before И нет чужих userTrades с since_ms → NOT_FOUND (не выставлена:
            движок пишет NOT_PLACED и может слать снова ПОД НОВОЙ попыткой);
          иначе → UNKNOWN (пауза и сверка; «повторить на всякий случай» нельзя).
        pos_before — позиция по книге движка до отправки (None — не знаем, доказать нельзя); since_ms — время
        отправки; known_order_ids — заявки, чьи сделки в окне уже учтены (предыдущие дочерние того же клипа)."""
        n = max(1, int(tconfig.ASTER_UNKNOWN_QUERIES))
        gap = tconfig.ASTER_UNKNOWN_SPAN_S / max(n - 1, 1)
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
            log.warning("aster %s: -2013 ×%d, но позиция %s ≠ %s — исход неизвестен", client_id, n, pos, pos_before)
            return unknown
        try:
            st, body, _ = self.call("GET", "/fapi/v3/userTrades",
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
            log.warning("aster %s: -2013 ×%d, но в userTrades %d новых сделок — исход неизвестен",
                        client_id, n, len(foreign))
            return unknown
        return _pf(client_id, "NOT_FOUND", code=NO_SUCH_ORDER)

    # --- учёт ------------------------------------------------------------------------------------
    def history_account(self):
        from .accounting import _hash
        if self.signer is None or not self.signer.send_user:
            raise AsterError('history account needs an explicit signed user or verified signer-owner mapping')
        return 'acct:v1:aster:' + _hash((self.base, self.signer.user.lower()))

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
            body = self._signed_ok("GET", "/fapi/v3/userTrades", params, "userTrades")
            if not isinstance(body, list):
                raise AsterError("userTrades: не список")
            if _strict:
                from .history_validation import exact_id
                if any(type(t) is not dict or not exact_id(t.get('id')) or
                       not exact_id(t.get('orderId')) for t in body):
                    raise AsterError('history native trade/order ID is not exact')
            rows = [_trade_row(t) for t in body]
            for r in rows:
                if _strict and (r['symbol'] != symbol or
                                (r['trade_id'] in out and out[r['trade_id']] != r)):
                    raise AsterError('history fill namespace or duplicate conflict')
                out[r["trade_id"]] = r
            if fid is None or len(body) < PAGE_LIMIT:
                return [out[k] for k in sorted(out)]
            fid = max(r["trade_id"] for r in rows) + 1
        raise AsterError(f"userTrades {symbol}: больше {MAX_PAGES} страниц — сузь from_id")

    def funding_income(self, symbol: str, start_ms: int, *, _strict=False) -> list[dict]:
        """Начисления FUNDING_FEE с start_ms до сейчас (строки для store.add_funding_income): окна по 7 сут
        (предел документации), внутри окна — страницы по времени; дедуп по tranId."""
        seen: dict[int, dict] = {}
        now_ms = int(self._now() * 1000)
        s = int(start_ms)
        for _ in range(MAX_PAGES):
            if s > now_ms:
                break
            e = min(s + WEEK_MS - 1, now_ms)
            params = {"symbol": symbol, "incomeType": "FUNDING_FEE", "startTime": s, "endTime": e, "limit": PAGE_LIMIT}
            body = self._signed_ok("GET", "/fapi/v3/income", params, "income")
            if not isinstance(body, list):
                raise AsterError("income: не список")
            for r in body:
                if _strict and (not isinstance(r, dict) or r.get('incomeType') != 'FUNDING_FEE' or
                                r.get('symbol') != symbol):
                    raise AsterError('history funding namespace or type is unproven')
                if isinstance(r, dict) and r.get("incomeType", "FUNDING_FEE") == "FUNDING_FEE":
                    row = _income_row(r)
                    if _strict and row['tran_id'] in seen and seen[row['tran_id']] != row:
                        raise AsterError('history funding duplicate conflict')
                    seen[row["tran_id"]] = row
            if len(body) >= PAGE_LIMIT:
                last = max(int(r["time"]) for r in body)
                if _strict and last <= s:
                    raise AsterError('history funding timestamp saturated; coverage gap')
                s = last if last > s else s + 1     # legacy pagination; strict reader refuses timestamp loss
            else:
                s = e + 1
        else:
            if s <= now_ms:
                raise AsterError(f"income {symbol}: больше {MAX_PAGES} окон — передай start_ms ближе")
        return sorted(seen.values(), key=lambda r: (r["ts"], r["tran_id"]))

    def health(self) -> dict:
        h = self.http.health()
        h.update(backoff_until=int(self.backoff_until), order_count=dict(self.order_count),
                 signer=self.signer.signer if self.signer else None)
        return h
