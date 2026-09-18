"""Спот-нога Binance (api.binance.com): первая боевая реализация CEX-spot адаптера (docs/migration/ADAPTER_GUIDE.md,
examples/cex_spot.py — тот пример никогда не был зарегистрирован в production_registry(); этот модуль — не пример,
а настоящий сетевой клиент, всё ещё НЕ включённый ни в один профиль owner.toml).

СВЯЗКА ВЫКЛЮЧЕНА ПО УМОЛЧАНИЮ. Регистрация адаптера в production_registry() (trade/adapters/registry.py) не
разрешает live — нужен профиль, лимиты и ключи владельца (ADAPTER_GUIDE.md). Живых вызовов не делалось.

Один аккаунт, общая подпись с перп-ногой (binance_trade.py): Binance выдаёт один api-key/secret с двумя
независимыми переключателями прав («Enable Spot & Margin Trading», «Enable Futures») — сверено по документации
Binance API Management, не предположено тихо. Низкоуровневая механика подписи (HMAC-SHA256 над query-string,
заголовок X-MBX-APIKEY, ВСЕ параметры — query-string, включая POST/DELETE) — ОБЩАЯ с фьючерсной ногой: этот модуль
переиспользует binance_trade.sign/build_query/_Secret/load_env_keys, а не пишет второй параллельный HMAC.
Имена переменных окружения те же — BINANCE_API_KEY/BINANCE_API_SECRET (task 18.09: «один набор ключей... даёт
доступ и к Spot, и к Futures API»): загружаются ОДИН раз общим credentials.CredentialProvider.binance(), эта нога
получает уже раскрытые значения, не второй читатель окружения (как GATE_API_KEY у Gate — единственное место чтения
здесь — сам этот факт: если владелец создал ключ без прав на Spot, подписанные вызовы отклонит сама биржа -2015,
это ожидаемый безопасный отказ).

Эндпоинты (api.binance.com, сверено по официальной документации Binance Spot API; базовый URL и лимит веса уже
заведены коллектором в config.EXCHANGES["binance_spot"] = base "https://api.binance.com", weight_limit=6000):
  /api/v3/time, /api/v3/exchangeInfo, /api/v3/depth, /api/v3/account (GET, подписан — балансы free/locked),
  /api/v3/order (POST — MARKET/LIMIT, GET — запрос по orderId/origClientOrderId, DELETE — отмена),
  /api/v3/myTrades (GET, подписан, пагинация fromId).

Ордер. Два вида, оба доступны на этом клиенте (BinanceSpotTrade.order): MARKET (quoteOrderQty — потратить ровно
эту сумму котировки, БЕЗ гарантированного пола получаемого актива: биржа исполнит по текущему стакану, границы
нет) и LIMIT+IOC (quantity + price — цена-ограничитель, как у перпа). Общий адаптерный биндинг (cex_bindings.py)
использует ТОЛЬКО LIMIT+IOC: контракт Quote (max_spend/min_receive) требует границу, известную ДО отправки, а
чистый MARKET её не даёт (сравни с EVM-свопом spot_bindings.evm — там граница из on-chain квоты ДО подписи).
MARKET оставлен как метод клиента на будущее (не вызывается адаптерным слоем) — вызывающий, который явно готов
принять неограниченное проскальзывание, должен сам на это решиться, код тут ничего не решает за него.

Исход заявки — как у Binance Futures (order_to_fill в binance_trade.py): FILLED/EXPIRED/CANCELED/REJECTED —
те же строки статуса, что и у fapi (сверено по документации Binance Spot Order status); IOC, не исполненный
целиком, спот отдаёт как EXPIRED (частичное исполнение — как FILLED-частично, финал, как у Futures IOC).
settle_unknown — по образцу binance_trade.BinanceTrade (не Gate): origClientOrderId ищется без короткого окна.
"""
from __future__ import annotations
import json, logging, re, time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from dataclasses import dataclass
from .. import config
from ..client import BannedError, BinanceLike, BudgetExceeded
from .aster_trade import fmt_param
from .binance_trade import (BinanceError, BinanceNetError, _Secret, _code, _is_error, _msg, _retry_after,
                            build_query, sign)
from .keys import ModeForbidden, _remember_secret, gate as mode_gate

log = logging.getLogger(__name__)
D = Decimal
_D0 = Decimal(0)

VENUE = "binance_spot"
BINANCE_KEY_ENV = "BINANCE_API_KEY"          # тот же ключ, что у перп-ноги (один аккаунт Binance) — не второй набор
BINANCE_SECRET_ENV = "BINANCE_API_SECRET"

SIGNED_TIMEOUT = (5, 10)
SOFT_READ = config.WEIGHT_SOFT_LIMIT
SOFT_ORDER = 0.95
FILTERS_TTL_S = 3600
RECV_WINDOW_MS = 5000
CLOCK_SKEW_MAX_S = 2.0
PAGE_LIMIT = 1000
MAX_PAGES = 16
TRADES_SLACK_MS = 5000
UNKNOWN_QUERIES = 3
UNKNOWN_SPAN_S = 5.0
UNKNOWN_CODES = frozenset({-1000, -1001, -1006, -1007})
NO_SUCH_ORDER = -2013
CID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,36}$")
FINAL = frozenset({"FILLED", "PARTIALLY_FILLED", "EXPIRED", "REJECTED"})


class SpotOrderError(BinanceError):
    """Отказ ноги Binance Spot (разбор ответа, фильтры, несоответствие)."""


@dataclass
class SpotFill:
    """Итог дочернего ордера спота. status: FILLED | PARTIALLY_FILLED | EXPIRED | REJECTED | UNKNOWN | NOT_FOUND.
    base_qty/quote_qty — исполненные количества (база/квота), avg_px = quote_qty/base_qty. Формой намеренно похож
    на types.PerpFill (тот же смысл полей), но не переиспользует его: спот не несёт позицию/маржу перпа, чужое
    имя типа было бы обманчивым."""
    client_id: str
    order_id: int | None
    status: str
    base_qty: Decimal
    quote_qty: Decimal
    avg_px: Decimal
    sign_nonce: int
    err_code: int | None = None


def load_env_keys(environ=None) -> tuple[str, str]:
    """BINANCE_API_KEY/BINANCE_API_SECRET — те же переменные, что у перп-ноги (один аккаунт). Отдельная функция —
    для симметрии с остальными venue-модулями (from_env), значения совпадают с binance_trade.load_env_keys."""
    from .binance_trade import load_env_keys as _load
    return _load(environ)


def parse_filters(sym: dict) -> "SpotFilters":
    f = {x.get("filterType"): x for x in sym.get("filters") or ()}
    pf, lot = f.get("PRICE_FILTER"), f.get("LOT_SIZE")
    if not pf or not lot:
        raise SpotOrderError(f"{sym.get('symbol')}: нет PRICE_FILTER/LOT_SIZE")
    # Binance переименовала MIN_NOTIONAL -> NOTIONAL (минимум теперь minNotional внутри NOTIONAL); принимаем оба.
    notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
    tick, step = _d(pf["tickSize"]), _d(lot["stepSize"])
    if tick <= 0 or step <= 0:
        raise SpotOrderError(f"{sym.get('symbol')}: шаг цены/количества ≤ 0")
    base_dec = sym.get("baseAssetPrecision")
    quote_dec = sym.get("quoteAssetPrecision") if sym.get("quoteAssetPrecision") is not None else sym.get("quotePrecision")
    if not isinstance(base_dec, int) or not isinstance(quote_dec, int):
        raise SpotOrderError(f"{sym.get('symbol')}: нет baseAssetPrecision/quoteAssetPrecision")
    return SpotFilters(tick=tick, step=step, min_qty=_d(lot.get("minQty", "0")), max_qty=_d(lot.get("maxQty", "0")),
                       min_notional=_d(notional.get("minNotional", notional.get("notional", "0"))),
                       base_asset=sym.get("baseAsset"), quote_asset=sym.get("quoteAsset"),
                       base_decimals=base_dec, quote_decimals=quote_dec)


@dataclass(frozen=True)
class SpotFilters:
    tick: Decimal
    step: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    base_asset: str | None
    quote_asset: str | None
    base_decimals: int
    quote_decimals: int


def _d(x: Any) -> Decimal:
    if isinstance(x, bool) or x is None:
        raise SpotOrderError(f"не число: {x!r}")
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError):
        raise SpotOrderError(f"не число: {x!r}") from None


def _sf(cid: str, status: str, *, order_id: int | None = None, base: Decimal = _D0, quote: Decimal = _D0,
        avg: Decimal = _D0, nonce: int = 0, code: int | None = None) -> SpotFill:
    return SpotFill(client_id=cid, order_id=order_id, status=status, base_qty=base, quote_qty=quote, avg_px=avg,
                    sign_nonce=nonce, err_code=code)


def order_to_fill(cid: str, body: dict, nonce: int) -> SpotFill:
    """Ответ ордера/запроса → SpotFill. По образцу binance_trade.order_to_fill (Binance Futures): те же строки
    статуса, IOC не исполненный целиком — EXPIRED (0) или PARTIALLY_FILLED (>0, финал)."""
    try:
        oid = int(body["orderId"]) if body.get("orderId") is not None else None
        base = _d(body.get("executedQty", "0"))
        quote = _d(body.get("cummulativeQuoteQty", "0"))
    except (SpotOrderError, TypeError, ValueError):
        return _sf(cid, "UNKNOWN", nonce=nonce)
    if oid is None or (body.get("clientOrderId") not in (None, cid)):
        return _sf(cid, "UNKNOWN", nonce=nonce)
    avg = quote / base if base > 0 else _D0
    st = str(body.get("status") or "")
    if st == "FILLED":
        out = "FILLED"
    elif st in ("EXPIRED", "CANCELED", "EXPIRED_IN_MATCH"):
        out = "PARTIALLY_FILLED" if base > 0 else "EXPIRED"
    elif st == "REJECTED":
        out = "REJECTED"
    else:
        out = "UNKNOWN"
    return _sf(cid, out, order_id=oid, base=base, quote=quote, avg=avg, nonce=nonce)


def _trade_row(t: dict) -> dict:
    return {"trade_id": int(t["id"]), "order_id": int(t["orderId"]) if t.get("orderId") is not None else None,
            "symbol": t.get("symbol"), "side": "BUY" if t.get("isBuyer") else "SELL", "price": _d(t["price"]),
            "qty": _d(t["qty"]), "quote_qty": _d(t["quoteQty"]) if t.get("quoteQty") is not None else None,
            "commission_abs": abs(_d(t["commission"])) if t.get("commission") is not None else None,
            "commission_asset": t.get("commissionAsset"), "maker": bool(t.get("isMaker")),
            "realized_pnl": None,      # спот не несёт реализованный PnL перпа
            "ts": int(t["time"])}


class BinanceSpotTrade:
    """SpotLeg для Binance (api.binance.com). Публичное (фильтры, стакан, время) — в любом режиме, без ключей;
    подписанное — через call() с воротами mode_state() на КАЖДЫЙ вызов (как перп-нога)."""
    venue = VENUE
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
        if secret:
            _remember_secret(secret)
            _remember_secret(secret.lower())
        self._mode_state = mode_state or (lambda: ("dry", False))
        self.timeout = timeout
        self.recv_window_ms = int(recv_window_ms)
        self._now, self._sleep = now, sleep
        self.public_retries = public_retries
        self.backoff_until = 0.0
        self.last_error: str | None = None
        self._filters: dict[str, tuple[float, SpotFilters]] = {}
        self._status: dict[str, str] = {}

    @classmethod
    def from_env(cls, mode_state: Callable[[], tuple[str | None, bool]], environ=None, **kw) -> "BinanceSpotTrade":
        key, secret = load_env_keys(environ)
        return cls(key, secret, mode_state=mode_state, **kw)

    def __repr__(self) -> str:
        return f"BinanceSpotTrade(key={self._key!r}, secret={self._secret!r})"

    # --- ворота, бюджет, HTTP (симметрично binance_trade.BinanceTrade) ----------------------------
    def _gate(self, action: str, hedge: bool) -> None:
        try:
            mode, paused = self._mode_state()
        except Exception as e:
            raise ModeForbidden(f"режим/пауза не прочитаны ({type(e).__name__}): {action} запрещено") from None
        mode_gate(mode, action, paused=bool(paused), hedge=hedge)

    def _check_budget(self, path: str, critical: bool) -> None:
        now = self._now()
        if now < self.http.banned_until:
            raise BannedError(f"binance_spot: бан (418) ещё {self.http.banned_until - now:.0f} с, {path} не отправлен")
        if now < self.backoff_until:
            raise BudgetExceeded(f"binance_spot: пауза после 429 ещё {self.backoff_until - now:.1f} с, {path} не отправлен")
        soft = SOFT_ORDER if critical else SOFT_READ
        if self.http.budget_used() >= soft:
            raise BudgetExceeded(f"binance_spot: вес {self.http.budget_used():.0%} ≥ {soft:.0%}, {path} не отправлен")

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
        """Как binance_trade.BinanceTrade.call: все параметры — query-string, подпись HMAC-SHA256 общей sign()."""
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
        except Exception as e:
            self.http.n_err += 1
            raise BinanceNetError(m, path, ts, e) from None
        status, body = self._absorb(r)
        return status, body, ts

    def _signed_ok(self, method: str, path: str, params: dict | None = None, what: str = "", **kw) -> Any:
        st, body, _ = self.call(method, path, params, **kw)
        if _is_error(st, body):
            raise SpotOrderError(f"{what or f'{method} {path}'}: HTTP {st} code {_code(body)} {_msg(body)}"[:300])
        return body

    def _public(self, path: str, params: dict | None = None) -> Any:
        return self.http.get(path, params, retries=self.public_retries)

    # --- публичное ---------------------------------------------------------------------------------
    def server_time_ms(self) -> int:
        return int(self._public("/api/v3/time")["serverTime"])

    def clock_offset_s(self) -> float:
        t0 = self._now()
        ms = self.server_time_ms()
        t1 = self._now()
        return ms / 1000.0 - (t0 + t1) / 2.0

    def check_clock(self, max_s: float = CLOCK_SKEW_MAX_S) -> float:
        off = self.clock_offset_s()
        if abs(off) > max_s:
            raise SpotOrderError(f"часы расходятся с Binance Spot на {off:+.2f} с (> {max_s} с; recvWindow "
                                 f"{self.recv_window_ms} мс): проверь NTP, live не запускаю")
        return off

    def _load_filters(self, symbol: str) -> None:
        ei = self._public("/api/v3/exchangeInfo", {"symbol": symbol})
        t = self._now()
        syms = ei.get("symbols") or ()
        row = next((s for s in syms if s.get("symbol") == symbol), None)
        if row is None:
            raise SpotOrderError(f"{symbol}: нет в exchangeInfo Binance Spot")
        self._filters[symbol] = (t, parse_filters(row))
        self._status[symbol] = str(row.get("status") or "")

    def filters(self, symbol: str) -> SpotFilters:
        hit = self._filters.get(symbol)
        if hit and self._now() - hit[0] < FILTERS_TTL_S:
            return hit[1]
        try:
            self._load_filters(symbol)
        except Exception as e:
            if hit:
                log.warning("binance_spot: exchangeInfo не обновлён (%s), беру фильтры %s из кэша", type(e).__name__, symbol)
                return hit[1]
            raise
        return self._filters[symbol][1]

    def status(self, symbol: str) -> str | None:
        return self._status.get(symbol)

    def book(self, symbol: str, limit: int = 20) -> tuple:
        want = max(1, min(int(limit), 5000))
        b = self._public("/api/v3/depth", {"symbol": symbol, "limit": want})
        bids = tuple((_d(p), _d(q)) for p, q in (b.get("bids") or ())[:want])
        asks = tuple((_d(p), _d(q)) for p, q in (b.get("asks") or ())[:want])
        return bids, asks, self._now()

    # --- подписанные чтения ------------------------------------------------------------------------
    def account(self) -> dict:
        body = self._signed_ok("GET", "/api/v3/account", None, "account")
        if not isinstance(body, dict):
            raise SpotOrderError("account: не объект")
        return body

    def balances(self) -> list[dict]:
        rows = self.account().get("balances")
        if not isinstance(rows, list):
            raise SpotOrderError("account: нет balances")
        return rows

    def balance(self, asset: str) -> Decimal | None:
        """free (доступно для новой заявки) актива; None — ошибка или актива нет (не 0)."""
        try:
            rows = self.balances()
        except ModeForbidden:
            raise
        except Exception as e:
            log.warning("binance_spot account: %s", type(e).__name__)
            return None
        row = next((r for r in rows if isinstance(r, dict) and r.get("asset") == asset), None)
        if row is None or row.get("free") is None:
            return None
        try:
            return _d(row["free"])
        except SpotOrderError:
            return None

    # --- заявки ------------------------------------------------------------------------------------
    def _check_order(self, symbol: str, side: str, qty: Decimal, px: Decimal | None, cid: str) -> None:
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side BUY|SELL, а не {side!r}")
        if not CID_RE.match(cid or ""):
            raise ValueError(f"client_id не проходит шаблон Binance: {cid!r}")
        if isinstance(qty, bool) or not isinstance(qty, Decimal) or not qty.is_finite() or qty <= 0:
            raise ValueError(f"qty — положительный Decimal, а не {qty!r}")
        if px is not None and (isinstance(px, bool) or not isinstance(px, Decimal) or not px.is_finite() or px <= 0):
            raise ValueError(f"px_cap — положительный Decimal, а не {px!r}")
        hit = self._filters.get(symbol)
        if hit:
            f = hit[1]
            if qty % f.step != 0 or qty < f.min_qty or (f.max_qty > 0 and qty > f.max_qty):
                raise ValueError(f"{symbol}: qty {qty} не по шагу {f.step} / вне [{f.min_qty}, {f.max_qty}]")
            if px is not None and px % f.tick != 0:
                raise ValueError(f"{symbol}: цена {px} не кратна тику {f.tick}")

    def order(self, symbol: str, side: str, qty: Decimal, client_id: str, *, price: Decimal | None = None,
             hedge: bool = False, on_signed: Callable[[int], None] | None = None) -> SpotFill:
        """LIMIT+IOC при price задан (цена-ограничитель — единственный режим, которым пользуется адаптерный слой,
        cex_bindings.py: контракту нужна граница ДО отправки, MARKET её не даёт). MARKET (price=None) — оставлен
        методом клиента, адаптер его не зовёт (см. докстринг файла): qty — количество базового актива к покупке/
        продаже по рынку, без ограничения проскальзывания. Не отправлено (ворота/бюджет/параметры) — исключение.
        Отправлено — SpotFill: FILLED/PARTIALLY_FILLED(финал)/EXPIRED/REJECTED/UNKNOWN — как binance_trade.ioc."""
        on_signed_cb = on_signed
        self._check_order(symbol, side, qty, price, client_id)
        params = {"symbol": symbol, "side": side, "newClientOrderId": client_id, "newOrderRespType": "FULL"}
        if price is not None:
            params.update(type="LIMIT", timeInForce="IOC", quantity=qty, price=price)
        else:
            params.update(type="MARKET", quantity=qty)
        self.last_error = None
        try:
            st, body, n = self.call("POST", "/api/v3/order", params, hedge=hedge, critical=True, on_signed=on_signed_cb)
        except BinanceNetError as e:
            self.last_error = str(e)
            log.warning("binance_spot order %s %s %s: нет ответа — исход неизвестен, выясняю запросом", client_id, side, qty)
            return _sf(client_id, "UNKNOWN", nonce=e.ts)
        code = _code(body)
        if code in UNKNOWN_CODES or st >= 500 or st == 408:
            self.last_error = f"HTTP {st} code {code} {_msg(body)}"
            return _sf(client_id, "UNKNOWN", nonce=n, code=code)
        if _is_error(st, body):
            self.last_error = f"HTTP {st} code {code} {_msg(body)}"
            log.warning("binance_spot order %s отклонён: %s", client_id, self.last_error)
            return _sf(client_id, "REJECTED", nonce=n, code=code)
        if not isinstance(body, dict):
            return _sf(client_id, "UNKNOWN", nonce=n)
        f = order_to_fill(client_id, body, n)
        log.info("binance_spot order %s %s %s@%s → %s %s avg %s", client_id, side, qty, price, f.status, f.base_qty, f.avg_px)
        return f

    def query(self, symbol: str, client_id: str) -> SpotFill:
        if not CID_RE.match(client_id or ""):
            raise ValueError(f"client_id не проходит шаблон Binance: {client_id!r}")
        try:
            st, body, _ = self.call("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_id},
                                    critical=True)
        except ModeForbidden:
            raise
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            return _sf(client_id, "UNKNOWN")
        code = _code(body)
        if code == NO_SUCH_ORDER:
            return _sf(client_id, "NOT_FOUND", code=code)
        if _is_error(st, body) or not isinstance(body, dict):
            self.last_error = f"HTTP {st} code {code} {_msg(body)}"
            return _sf(client_id, "UNKNOWN", code=code)
        return order_to_fill(client_id, body, 0)

    def cancel(self, symbol: str, client_id: str) -> SpotFill:
        """DELETE /api/v3/order по origClientOrderId. Для нормального IOC-потока не нужен (IOC самоограничивается
        по времени) — оставлен для GTC/LIMIT-заявок и полноты клиента (задача явно просит отмену)."""
        if not CID_RE.match(client_id or ""):
            raise ValueError(f"client_id не проходит шаблон Binance: {client_id!r}")
        try:
            st, body, n = self.call("DELETE", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_id},
                                    critical=True)
        except BinanceNetError as e:
            self.last_error = str(e)
            return _sf(client_id, "UNKNOWN", nonce=e.ts)
        except ModeForbidden:
            raise
        code = _code(body)
        if code == NO_SUCH_ORDER:
            return _sf(client_id, "NOT_FOUND", code=code)
        if code in UNKNOWN_CODES or st >= 500:
            self.last_error = f"HTTP {st} code {code} {_msg(body)}"
            return _sf(client_id, "UNKNOWN", nonce=n, code=code)
        if _is_error(st, body) or not isinstance(body, dict):
            self.last_error = f"HTTP {st} code {code} {_msg(body)}"
            return _sf(client_id, "REJECTED", nonce=n, code=code)
        return order_to_fill(client_id, body, n)

    def settle_unknown(self, symbol: str, client_id: str, *, base_before: Decimal | None, since_ms: int,
                       known_order_ids=frozenset()) -> SpotFill:
        """Выяснить исход ордера с неизвестным итогом — без повторной отправки (по образцу BinanceTrade.settle_unknown,
        origClientOrderId без короткого окна поиска). base_before — баланс базового актива до отправки (сравнение
        доказывает «не выставлена», а не «выставлена, но не нашли»); since_ms — время отправки."""
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
            return last or _sf(client_id, "UNKNOWN")
        unknown = _sf(client_id, "UNKNOWN", code=NO_SUCH_ORDER)
        if base_before is None:
            return unknown
        base_asset = self._filters.get(symbol)
        bal = self.balance(base_asset[1].base_asset) if base_asset else None
        if bal is None or bal != base_before:
            log.warning("binance_spot %s: -2013 ×%d, но баланс %s ≠ %s — исход неизвестен", client_id, n, bal, base_before)
            return unknown
        try:
            st, body, _ = self.call("GET", "/api/v3/myTrades",
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
            log.warning("binance_spot %s: -2013 ×%d, но в myTrades %d новых сделок — исход неизвестен",
                        client_id, n, len(foreign))
            return unknown
        return _sf(client_id, "NOT_FOUND", code=NO_SUCH_ORDER)

    # --- учёт ------------------------------------------------------------------------------------
    def history_account(self):
        from .accounting import _hash
        if not self._key:
            raise SpotOrderError('history account needs a loaded API key')
        return 'acct:v1:binance_spot:' + _hash((self.base, self._key))

    def history_fills(self, symbol, from_id):
        return self.fills(symbol, from_id, _strict=True)

    def fills(self, symbol: str, from_id: int | None, *, _strict=False) -> list[dict]:
        out: dict[int, dict] = {}
        fid = None if from_id is None else int(from_id)
        for _ in range(MAX_PAGES):
            params = {"symbol": symbol, "fromId": fid, "limit": PAGE_LIMIT}
            body = self._signed_ok("GET", "/api/v3/myTrades", params, "myTrades")
            if not isinstance(body, list):
                raise SpotOrderError("myTrades: не список")
            if _strict:
                from .history_validation import exact_id
                if any(type(t) is not dict or not exact_id(t.get('id')) or
                       not exact_id(t.get('orderId')) for t in body):
                    raise SpotOrderError('history native trade/order ID is not exact')
            rows = [_trade_row(t) for t in body]
            for r in rows:
                if _strict and (r['symbol'] != symbol or (r['trade_id'] in out and out[r['trade_id']] != r)):
                    raise SpotOrderError('history fill namespace or duplicate conflict')
                out[r["trade_id"]] = r
            if fid is None or len(body) < PAGE_LIMIT:
                return [out[k] for k in sorted(out)]
            fid = max(r["trade_id"] for r in rows) + 1
        raise SpotOrderError(f"myTrades {symbol}: больше {MAX_PAGES} страниц — сузь from_id")

    def health(self) -> dict:
        h = self.http.health()
        h.update(backoff_until=int(self.backoff_until), key=self._key)
        return h
