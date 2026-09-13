"""Сухой прогон на живых публичных данных (trade_spec §8): SimSpot и SimPerp — те же протоколы SpotLeg/PerpLeg,
что у боевых ног, поэтому движок, запись-до, кнопки, прогресс и итог работают в dry ровно тем же кодом, что в live.

Что берётся вживую и что симулируется:
- SimSpot: котировки, decimals, цена пула и балансы кошелька — у настоящей ноги только для чтения (OkxEvmSpot без
  отправителя; ключ OKX DEX — тот же, что у коллектора, ключ кошелька не нужен). Исполнение — по toTokenAmount
  свежей котировки, газ = tradeFee котировки. Перед «исполнением» может вызвать /swap и прогнать ВСЕ гарды боевой
  ноги (без eth_estimateGas, пока allowance 0 — симуляция заведомо откатилась бы): владелец видит в dry, на каком
  гарде live отказал бы.
- SimPerp: фильтры, стакан и фандинг — публичные Aster; IOC «исполняется» проходом по свежему стакану не глубже
  цены-ограничителя, комиссия — FEES_TAKER площадки. Позиция — виртуальная (в симуляции чужих позиций нет).
  reduceOnly не может увеличить позицию — как у биржи (-2022).

Балансы симуляции — реальные балансы кошелька плюс виртуальные изменения (ledger): «куплено в dry» не появляется в
цепи, а отчёт должен показывать то, что было бы. Неизвестное остаётся None (не 0).
После рестарта виртуальное состояние восстанавливает движок по журналу сделок (seed_*): журнал — единственная
правда и для симуляции, иначе «сверка» dry сверяла бы с пустотой.
"""
from __future__ import annotations
import logging, threading, time
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Callable
from .. import config
from .keys import ModeForbidden, redact
from .types import Book, DexQuote, Filters, PerpFill, SwapResult

log = logging.getLogger(__name__)
D = Decimal
ZERO = D(0)
WEI = D(10) ** 18


def _lc(a: str | None) -> str:
    return (a or "").strip().lower()


class SimGuardNote(RuntimeError):
    """Не гард, а сбой самой проверки (сеть OKX): в dry — заметка в журнале, а не отказ исполнения."""


class SimSpot:
    """SpotLeg симуляции поверх ноги только для чтения (inner: quote/balances/pool_price/decimals[/build_swap]).
    native_px() — цена газовой монеты в $ (для gas_wei из tradeFee); None — газ только в $."""

    def __init__(self, inner, *, native_px: Callable[[], D | None] | None = None, exercise_guards: bool = True,
                 wallet_known: bool = True, clock: Callable[[], float] = time.time):
        self.inner = inner
        self.chain = inner.chain
        self.wallet = inner.wallet
        self.stable = getattr(inner, "stable", None)
        self.stable_dec = getattr(inner, "stable_dec", None)
        self.native_px = native_px
        self.exercise_guards = exercise_guards
        self.wallet_known = wallet_known          # False — кошелёк владельца не задан: балансы неизвестны
        self._clock = clock
        self._lock = threading.Lock()
        self.ledger: dict[str, int] = {}          # адрес токена → виртуальное изменение, сырые единицы
        self.native_spent_wei = 0
        self.swaps = 0
        self.guard_notes: list[str] = []

    def __repr__(self) -> str:
        return f"SimSpot({self.chain}, {self.wallet})"

    # --- чтение (вживую) ---
    def decimals(self, token: str, hint: Any = None) -> int:
        fn = getattr(self.inner, "decimals", None)
        if fn is None:
            raise AttributeError("у ноги нет decimals()")
        return int(fn(token, hint)) if hint is not None else int(fn(token))

    def quote(self, token_in: str, token_out: str, amount_units: int) -> DexQuote:
        return self.inner.quote(token_in, token_out, int(amount_units))

    def pool_price(self, token: str) -> D | None:
        return self.inner.pool_price(token)

    def balances(self, token: str) -> dict[str, int | None]:
        """Реальный кошелёк + виртуальные изменения симуляции. Не прочитано (или кошелька нет) — None."""
        if not self.wallet_known:
            return {"stable": None, "token": None, "native": None}
        real = self.inner.balances(token)
        out: dict[str, int | None] = {}
        with self._lock:
            for k, addr in (("stable", self.stable), ("token", token)):
                v = real.get(k)
                out[k] = None if v is None else int(v) + self.ledger.get(_lc(addr), 0)
            n = real.get("native")
            out["native"] = None if n is None else int(n) - self.native_spent_wei
        return out

    def seed(self, token: str, units: int) -> None:
        """Восстановить виртуальный остаток токена по журналу (рестарт): токены симуляции в цепи не лежат."""
        with self._lock:
            self.ledger[_lc(token)] = int(units)

    # --- «исполнение» ---
    def ensure_allowance(self, token: str, need: int, clip_ref: str) -> SwapResult | None:
        """В симуляции approve не нужен (газ апрува учтён в плане строкой approve)."""
        return None

    def _exercise(self, token_in: str, token_out: str, amount: int) -> None:
        fn = getattr(self.inner, "build_swap", None)
        if not self.exercise_guards or fn is None or not self.wallet_known:
            return
        from .evm_swap import GuardError
        try:
            fn(token_in, token_out, amount, preflight=None)
        except GuardError:
            raise                                  # гард live отказал бы — и dry отказывает, с тем же именем
        except Exception as e:                     # noqa — сеть OKX: проверка не состоялась, это заметка
            note = f"гарды /swap не прогнаны: {redact(e)}"[:200]
            self.guard_notes.append(note)
            log.warning("sim: %s", note)

    def swap(self, token_in: str, token_out: str, amount_units: int, clip_ref: str) -> SwapResult:
        amount = int(amount_units)
        if amount <= 0:
            raise ValueError("сумма свопа ≤ 0")
        self._exercise(token_in, token_out, amount)
        q = self.inner.quote(token_in, token_out, amount)
        if q.honeypot:
            from .evm_swap import GuardError
            raise GuardError("honeypot", "OKX помечает токен как honeypot")
        if q.amount_out <= 0:
            raise RuntimeError("котировка без выхода: маршрута нет")
        gas_usd = None if q.gas_usd is None else float(q.gas_usd)
        px = self.native_px() if self.native_px is not None else None
        gas_wei = 0
        if gas_usd is not None and px:
            gas_wei = int((D(str(gas_usd)) / D(str(px)) * WEI).to_integral_value(ROUND_FLOOR))
        with self._lock:
            self.swaps += 1
            n = self.swaps
            self.ledger[_lc(token_in)] = self.ledger.get(_lc(token_in), 0) - amount
            self.ledger[_lc(token_out)] = self.ledger.get(_lc(token_out), 0) + int(q.amount_out)
            self.native_spent_wei += gas_wei
        return SwapResult(tx_hash=f"sim-{clip_ref or 'x'}-{n}", status="ok", amount_in=amount,
                          amount_out=int(q.amount_out), gas_wei=gas_wei, gas_usd=gas_usd, block=0, nonce=-1)

    def resolve(self, tx_row: dict) -> SwapResult:
        """У симуляции нет транзакций в цепи: сверять нечего — исход неизвестен (движок до этого не доходит)."""
        return SwapResult(tx_hash=str(tx_row.get("tx_hash") or ""), status="unknown", amount_in=0, amount_out=0,
                          gas_wei=0, gas_usd=None, block=0, nonce=int(tx_row.get("nonce") or -1))


class SimPerp:
    """PerpLeg симуляции поверх публичной ноги (inner: filters/book/funding[/available_margin]). Заявки не уходят
    никуда: IOC исполняется по свежему стакану не глубже px_cap, остаток «истекает», как у биржи."""

    def __init__(self, inner, *, fee_taker: D | None = None, clock: Callable[[], float] = time.time):
        self.inner = inner
        self.venue = inner.venue
        self.fee_taker = fee_taker if fee_taker is not None else D(str(config.FEES_TAKER[self.venue]))
        self._clock = clock
        self._lock = threading.Lock()
        self.pos: dict[str, D] = {}
        self.orders: dict[str, PerpFill] = {}
        self.trades: list[dict] = []
        self.setups: list[tuple[str, int, str]] = []
        # номера сделок/заявок уникальны и после рестарта (perp_fills: PRIMARY KEY(venue, trade_id))
        self._seq = int(clock() * 1000) * 100
        self.last_error: str | None = None
        self.ioc_calls = 0

    def __repr__(self) -> str:
        return f"SimPerp({self.venue})"

    @property
    def http(self):
        return getattr(self.inner, "http", None)

    # --- публичное (вживую) ---
    def filters(self, symbol: str) -> Filters:
        return self.inner.filters(symbol)

    def book(self, symbol: str, limit: int = 20) -> Book:
        return self.inner.book(symbol, limit)

    def funding(self, symbol: str) -> tuple[D, D, int]:
        return self.inner.funding(symbol)

    def instrument(self, symbol: str):
        """Контракт публичной ноги (exchangeInfo: множитель в baseAsset). Нет у неё метода — None: вход отказан."""
        fn = getattr(self.inner, "instrument", None)
        return fn(symbol) if fn is not None else None

    # --- «подписанное» ---
    def position(self, symbol: str) -> D | None:
        with self._lock:
            return self.pos.get(symbol, ZERO)

    def seed(self, symbol: str, position: D) -> None:
        with self._lock:
            self.pos[symbol] = D(position)

    def available_margin(self) -> D | None:
        """Реальная маржа, если режим позволяет подписанное чтение (readonly); в dry — неизвестно (None)."""
        fn = getattr(self.inner, "available_margin", None)
        if fn is None:
            return None
        try:
            return fn()
        except ModeForbidden:
            return None
        except Exception as e:                     # noqa
            log.warning("sim: маржа не прочитана: %s", redact(e))
            return None

    def setup(self, symbol: str, leverage: int, margin_type: str) -> None:
        self.setups.append((symbol, int(leverage), str(margin_type)))

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def ioc(self, symbol: str, side: str, qty: D, px_cap: D, client_id: str, reduce_only: bool,
            *, hedge: bool = False, on_signed: Callable[[int], None] | None = None) -> PerpFill:
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side BUY|SELL, а не {side!r}")
        for name, v in (("qty", qty), ("px_cap", px_cap)):
            if isinstance(v, bool) or not isinstance(v, D) or not v.is_finite() or v <= 0:
                raise ValueError(f"{name} — положительный Decimal, а не {v!r}")
        if on_signed is not None:
            on_signed(0)                           # запись-до — тем же путём, что в live (nonce подписи нет)
        self.ioc_calls += 1
        book = self.inner.book(symbol, 50)
        levels = book.bids if side == "SELL" else book.asks
        with self._lock:
            cur = self.pos.get(symbol, ZERO)
            want = qty
            if reduce_only:
                room = -cur if side == "BUY" else cur       # BUY уменьшает шорт, SELL — лонг
                if room <= 0:
                    self.last_error = "code -2022 ReduceOnly Order is rejected"
                    f = PerpFill(client_id, None, "REJECTED", ZERO, ZERO, ZERO, 0, -2022)
                    self.orders[client_id] = f
                    return f
                want = min(qty, room)
            left, got, quote = want, ZERO, ZERO
            for px, q in levels:
                if left <= 0:
                    break
                if (px < px_cap) if side == "SELL" else (px > px_cap):
                    break
                take = min(q, left)
                got += take
                quote += take * px
                left -= take
            oid = self._next()
            status = "FILLED" if got == qty else ("PARTIALLY_FILLED" if got > 0 else "EXPIRED")
            avg = (quote / got) if got > 0 else ZERO
            if got > 0:
                self.pos[symbol] = cur - got if side == "SELL" else cur + got
                self.trades.append({"trade_id": self._next(), "order_id": oid, "symbol": symbol, "side": side,
                                    "price": avg, "qty": got, "quote_qty": quote,
                                    "commission_abs": quote * self.fee_taker, "commission_asset": "USDT",
                                    "maker": False, "realized_pnl": ZERO, "ts": int(self._clock() * 1000)})
            f = PerpFill(client_id, oid, status, got, avg, quote, 0)
            self.orders[client_id] = f
            return f

    def query(self, symbol: str, client_id: str) -> PerpFill:
        with self._lock:
            f = self.orders.get(client_id)
        return f if f is not None else PerpFill(client_id, None, "NOT_FOUND", ZERO, ZERO, ZERO, 0, -2013)

    def settle_unknown(self, symbol: str, client_id: str, *, pos_before: D | None, since_ms: int,
                       known_order_ids=frozenset()) -> PerpFill:
        return self.query(symbol, client_id)

    def fills(self, symbol: str, from_id: int | None) -> list[dict]:
        with self._lock:
            return [dict(t) for t in self.trades
                    if t["symbol"] == symbol and (from_id is None or t["trade_id"] >= int(from_id))]

    def funding_income(self, symbol: str, start_ms: int) -> list[dict]:
        return []                                  # в симуляции фандинг не начисляется
