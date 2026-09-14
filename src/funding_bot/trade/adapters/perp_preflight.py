"""Venue-neutral perpetual preflight boundary.

The SOL coordinator owns when preflight runs; this module owns what the native
perpetual venue can prove before a spot swap.  Hyperliquid keeps its existing
HL-specific validators.  Aster/Gate use their common instrument/filters/book/
margin/position/setup surface and never need an HL account object.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from ..keys import redact
from ..spot_router import margin_for
from . import hl_preflight

D = Decimal


PreflightRefused = hl_preflight.PreflightRefused


@dataclass(frozen=True)
class PreflightRequest:
    """Frozen coordinator inputs; no venue-specific config is read here."""

    symbol: str
    quote_currency: str
    multiplier: D
    leverage: D | int | None
    capacity: D
    fee_rate: Callable[[], D | None] | D | None
    reserve: D | None
    expected_position: D | None
    sim: bool = False
    book_levels: int = 20
    margin_type: str = "ISOLATED"

    def __post_init__(self):
        # capacity is normalized underlying quantity; callers must apply the
        # frozen contract multiplier before constructing this request.
        for name in ('multiplier', 'capacity'):
            value = getattr(self, name)
            if not isinstance(value, D) or not value.is_finite() or value <= 0:
                raise PreflightRefused('units', f'{name}: positive finite Decimal required')
        for name in ('reserve', 'expected_position'):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, D) or not value.is_finite()):
                raise PreflightRefused('units', f'{name}: finite Decimal required')
        if self.reserve is not None and self.reserve < 0:
            raise PreflightRefused('margin', 'negative reserve')
        if not self.symbol or not self.quote_currency or type(self.sim) is not bool:
            raise PreflightRefused('identity', 'explicit symbol/currency/mode required')
        if type(self.book_levels) is not int or self.book_levels <= 0:
            raise PreflightRefused('book', 'positive depth required')


@dataclass(frozen=True)
class PreflightResult:
    step: D | None
    position: D | None
    margin_available: D | None
    margin_required: D | None
    identity: Any = None


def _fee(rate) -> D | None:
    value = rate() if callable(rate) else rate
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, D) or not value.is_finite() or value < 0:
        raise PreflightRefused("fee_unknown", "ставка комиссии перпа не подтверждена")
    return value


def _leverage(value) -> int:
    if value is None:
        raise PreflightRefused("owner_missing", "плечо перпа не задано")
    try:
        lev = int(value)
    except (TypeError, ValueError):
        raise PreflightRefused("owner_invalid", "плечо перпа должно быть целым") from None
    if isinstance(value, bool) or D(str(value)) != D(lev) or lev <= 0:
        raise PreflightRefused("owner_invalid", "плечо перпа должно быть положительным целым")
    return lev


def agent_gate(perp, *, venue: str, sim: bool, what: str) -> None:
    """Use HL agent validation only for HL; CEX auth is gated by native ioc()."""
    if venue == "hyperliquid":
        hl_preflight.agent_gate(perp, sim=sim, what=what)


def inspect(perp, request: PreflightRequest, *, inst=None, venue: str | None = None) -> PreflightResult:
    """Read-only frozen-leg proof for planning; never changes leverage/setup."""
    venue = venue or getattr(perp, "venue", None)
    if not venue:
        raise PreflightRefused("venue", "площадка перпа не задана")
    try:
        native, filt = perp.instrument(request.symbol), perp.filters(request.symbol)
    except Exception as e:
        raise PreflightRefused("meta", f"мета {venue} не прочитана: {redact(e)}") from None
    if (getattr(native, "symbol", request.symbol) != request.symbol or
            getattr(native, "quote_asset", None) != request.quote_currency or
            getattr(native, "m", None) != request.multiplier):
        raise PreflightRefused("identity", f"замороженный инструмент {request.symbol} изменился")
    step = getattr(filt, "step", None)
    if not isinstance(step, D) or not step.is_finite() or step <= 0:
        raise PreflightRefused("meta", f"шаг {request.symbol} неизвестен")
    available = None
    if venue == "hyperliquid":
        if inst is None:
            raise PreflightRefused("identity", "замороженная спецификация перпа не передана")
        try:
            ref = perp.identity()
            if ref.fullcoin != inst.perp_symbol or ref.is_delisted or (
                    inst.perp_asset_id is not None and ref.asset != inst.perp_asset_id):
                raise PreflightRefused("identity", f"рынок {request.symbol} изменился")
            margin = perp.margin()
        except PreflightRefused:
            raise
        except Exception as e:
            raise PreflightRefused("hl_meta", f"мета Hyperliquid не прочитана: {redact(e)}") from None
        if not margin.trade_supported:
            raise PreflightRefused("margin", f"режим счёта HL «{margin.mode}» первой версией не поддержан: "
                                   f"{margin.reason or ''}".strip())
        available = margin.available
    else:
        try:
            available = perp.available_margin()
        except Exception as e:
            raise PreflightRefused("margin", f"маржа {venue} не прочитана: {redact(e)}") from None
    return PreflightResult(step, None, available, None, native)


def entry(perp, request: PreflightRequest, *, inst=None, venue: str | None = None) -> PreflightResult:
    """Validate the perp leg before spot execution.

    ``venue='hyperliquid'`` delegates byte-for-byte policy to the existing HL
    validator.  Other venues must expose only the generic native futures API.
    """
    venue = venue or getattr(perp, "venue", None)
    if venue == "hyperliquid":
        if inst is None:
            raise PreflightRefused("identity", "замороженная спецификация перпа не передана")
        try:
            # Preserve existing HL behavior and messages exactly.  The caller
            # may still use the legacy expected_short representation.
            hl_preflight.entry(perp, inst, sim=request.sim, leverage=request.leverage,
                               capacity=request.capacity, fee_rate=lambda: _fee(request.fee_rate),
                               reserve=request.reserve,
                               expected_short=None if request.expected_position is None else -request.expected_position,
                               book_levels=request.book_levels)
            # ``hl_preflight.entry`` already performed all reads.  Do not
            # repeat them here: the existing HL path remains byte-for-byte
            # equivalent in call count and failure ordering.
            return PreflightResult(None, None, None, None)
        except hl_preflight.PreflightRefused:
            raise
        except Exception as e:
            raise PreflightRefused("hl_meta", f"мета Hyperliquid не прочитана: {redact(e)}") from None

    if not venue:
        raise PreflightRefused("venue", "площадка перпа не задана")
    symbol = request.symbol
    try:
        native = perp.instrument(symbol)
        filt = perp.filters(symbol)
    except Exception as e:
        raise PreflightRefused("meta", f"мета {venue} не прочитана: {redact(e)}") from None
    if getattr(native, "symbol", symbol) != symbol:
        raise PreflightRefused("identity", f"инструмент {symbol} изменился")
    if request.quote_currency and getattr(native, "quote_asset", None) != request.quote_currency:
        raise PreflightRefused("identity", f"котировка {symbol} изменилась")
    if request.multiplier is not None and getattr(native, "m", None) != request.multiplier:
        raise PreflightRefused("identity", f"множитель {symbol} изменился")
    step = getattr(filt, "step", None)
    if not isinstance(step, D) or not step.is_finite() or step <= 0:
        raise PreflightRefused("meta", f"шаг {symbol} неизвестен")
    lev = _leverage(request.leverage)
    if request.sim:
        return PreflightResult(step, None, None, None, native)
    if request.expected_position is None:
        raise PreflightRefused("position_unknown", f"ожидаемая позиция {symbol} не передана")
    try:
        book = perp.book(symbol, request.book_levels)
    except Exception as e:
        raise PreflightRefused("book_unknown", f"стакан {symbol} не прочитан: {redact(e)}") from None
    if not book.asks:
        raise PreflightRefused("margin", "маржа под шорт не посчитана (пустые аски)")
    rate = _fee(request.fee_rate)
    if rate is None:
        raise PreflightRefused("fee_unknown", "ставка комиссии перпа не подтверждена")
    try:
        need = margin_for(request.capacity, book.asks[0][0], D(lev), rate)
        available = perp.available_margin()
    except Exception as e:
        raise PreflightRefused("margin", f"маржа {venue} не прочитана: {redact(e)}") from None
    if (request.reserve is None or not isinstance(available, D) or not available.is_finite()
            or not isinstance(need, D) or not need.is_finite() or need <= 0):
        raise PreflightRefused("margin", "маржа или резерв перпа неизвестны")
    if available - request.reserve < need:
        raise PreflightRefused("margin", f"маржи {available} < нужно {need} + резерв {request.reserve}")
    try:
        position = perp.position(symbol)
    except Exception as e:
        raise PreflightRefused("position_unknown", f"позиция {symbol} не прочитана: {redact(e)}") from None
    if not isinstance(position, D) or not position.is_finite():
        raise PreflightRefused("position_unknown", f"позиция {symbol} не прочитана")
    if position != request.expected_position:
        raise PreflightRefused("position_mismatch", f"позиция {venue} {position} ≠ журнал {request.expected_position}")
    try:
        perp.setup(symbol, lev, request.margin_type)
    except Exception as e:
        raise PreflightRefused("setup", f"настройка {symbol}: {redact(e)}") from None
    return PreflightResult(step, position, available, need, native)
