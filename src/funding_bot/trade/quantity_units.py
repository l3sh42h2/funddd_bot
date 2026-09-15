"""Exact quantity conversions and owned-inventory reconciliation policies."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import json


def _finite(value: Decimal, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or (positive and value <= 0):
        raise ValueError(f"{name} must be a finite Decimal" + (" greater than zero" if positive else ""))
    return value


def native_to_base(qty_native: Decimal, multiplier: Decimal) -> Decimal:
    """Convert signed native contracts/tokens to signed underlying exposure."""
    qty = _finite(qty_native, "qty_native")
    unit = _finite(multiplier, "multiplier", positive=True)
    q, m = qty.as_tuple(), unit.as_tuple()
    q_coefficient = int(''.join(str(digit) for digit in q.digits))
    m_coefficient = int(''.join(str(digit) for digit in m.digits))
    coefficient = q_coefficient * m_coefficient
    digits = tuple(int(digit) for digit in str(coefficient)) if coefficient else (0,)
    return Decimal((q.sign ^ m.sign, digits, q.exponent + m.exponent))


def copy_abs(value: Decimal, name: str = "quantity") -> Decimal:
    """Remove a Decimal sign without applying the active arithmetic context."""
    return _finite(value, name).copy_abs()


def copy_negate(value: Decimal, name: str = "quantity") -> Decimal:
    """Invert a Decimal sign without applying the active arithmetic context."""
    return _finite(value, name).copy_negate()


def native_to_raw(quantity: Decimal, decimals: int) -> int:
    """Convert an exactly representable native quantity to its integer root units."""
    value = _finite(quantity, "quantity")
    if value < 0:
        raise ValueError("quantity must be non-negative")
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 255:
        raise ValueError("decimals must be an integer from 0 through 255")
    numerator, denominator = value.as_integer_ratio()
    raw, remainder = divmod(numerator * 10 ** decimals, denominator)
    if remainder:
        raise ValueError("quantity is not exactly representable at the requested scale")
    return raw


def exact_sum(values) -> Decimal:
    """Add finite Decimals by aligned integer coefficients, independent of context."""
    items = tuple(_finite(value, "sum item") for value in values)
    if not items:
        return Decimal(0)
    exponent = min(value.as_tuple().exponent for value in items)
    total = 0
    for value in items:
        parts = value.as_tuple()
        coefficient = int(''.join(str(digit) for digit in parts.digits))
        if parts.sign:
            coefficient = -coefficient
        total += coefficient * 10 ** (parts.exponent - exponent)
    digits = tuple(int(digit) for digit in str(abs(total))) if total else (0,)
    return Decimal((int(total < 0), digits, exponent))


def _decimal_values(value):
    """Yield exact decimal strings from a public accounting event payload."""
    if isinstance(value, dict):
        for item in value.values():
            yield from _decimal_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _decimal_values(item)
    elif isinstance(value, str):
        try:
            item = Decimal(value)
        except (InvalidOperation, ValueError):
            return
        if item.is_finite():
            yield item


def exact_leg_rebuild(con, *, deal_id: str | None = None, operation_id: str | None = None):
    """Run the existing accounting rules with enough local precision for their full input.

    The conservative bound sums every coefficient width and exponent span in
    the selected public facts.  It therefore exceeds the width of any product,
    aligned sum or subtraction performed by ``leg_accounting.rebuild`` without
    changing the process-wide Decimal context or duplicating accounting rules.
    """
    from . import leg_accounting
    query = "SELECT json FROM exec_events WHERE kind=?"
    args = [leg_accounting.KIND]
    if deal_id is not None:
        query += " AND deal_id=?"
        args.append(deal_id)
    values = []
    for (raw,) in con.execute(query, tuple(args)).fetchall():
        payload = json.loads(raw)
        if operation_id is not None and payload.get("operation_id") != operation_id:
            continue
        values.extend(_decimal_values(payload))
    precision = 32 + sum(len(value.as_tuple().digits) + abs(value.as_tuple().exponent) + 2
                         for value in values)
    with localcontext() as decimal_context:
        decimal_context.prec = max(32, precision)
        return leg_accounting.rebuild(con, deal_id=deal_id, operation_id=operation_id)


def base_to_native_floor(exposure_base: Decimal, multiplier: Decimal, step_native: Decimal) -> Decimal:
    """Return the largest valid native quantity that does not exceed base exposure."""
    exposure = _finite(exposure_base, "exposure_base")
    if exposure < 0:
        raise ValueError("exposure_base must be non-negative")
    unit = _finite(multiplier, "multiplier", positive=True)
    step = _finite(step_native, "step_native", positive=True)
    exposure_numerator, exposure_denominator = exposure.as_integer_ratio()
    multiplier_numerator, multiplier_denominator = unit.as_integer_ratio()
    step_numerator, step_denominator = step.as_integer_ratio()
    steps = (exposure_numerator * multiplier_denominator * step_denominator //
             (exposure_denominator * multiplier_numerator * step_numerator))
    step_tuple = step.as_tuple()
    step_coefficient = int(''.join(str(digit) for digit in step_tuple.digits))
    coefficient = step_coefficient * steps
    digits = tuple(int(digit) for digit in str(coefficient)) if coefficient else (0,)
    return Decimal((0, digits, step_tuple.exponent))


@dataclass(frozen=True)
class InventoryCoverage:
    matched: bool
    observed_base: Decimal
    personal_surplus_base: Decimal


def reconcile_owned_inventory(*, market_kind: str, observed_qty_native: Decimal,
                              owned_exposure_base: Decimal, multiplier: Decimal) -> InventoryCoverage:
    """Spot wallet may cover owned inventory plus surplus; perpetual scope stays exact."""
    observed = native_to_base(observed_qty_native, multiplier)
    owned = _finite(owned_exposure_base, "owned_exposure_base")
    if market_kind == "spot":
        matched = owned >= 0 and observed >= owned
        surplus = observed - owned if matched else Decimal(0)
        return InventoryCoverage(matched, observed, surplus)
    if market_kind == "perpetual":
        return InventoryCoverage(observed == owned, observed, Decimal(0))
    raise ValueError("market_kind must be spot or perpetual")
