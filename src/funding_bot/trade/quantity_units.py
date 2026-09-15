"""Exact quantity conversions and owned-inventory reconciliation policies."""
from dataclasses import dataclass
from decimal import Decimal


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
