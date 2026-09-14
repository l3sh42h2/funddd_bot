"""Immutable, venue-neutral authorization plan for two independent legs."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Mapping

from .adapters.contracts import Capabilities, LegSpec


def _d(v: Any, name: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if not isinstance(v, Decimal):
        raise ValueError(f"{name} must be an exact finite Decimal")
    try:
        x = v if isinstance(v, Decimal) else Decimal(str(v))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be an exact finite Decimal") from None
    if not x.is_finite() or (positive and x <= 0) or (nonnegative and x < 0):
        raise ValueError(f"{name} has invalid value")
    return x


def _text(v: Any, name: str) -> str:
    if not isinstance(v, str) or not v or any(ord(c) < 32 for c in v):
        raise ValueError(f"{name} must be a non-empty public string")
    return v


def _invalid_constant(value):
    raise ValueError("nonfinite JSON number")


def _json_decimal(value: Any, name: str) -> Decimal:
    """Canonical plan money is JSON text; accepting float reintroduces drift."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a decimal string")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a decimal string") from None
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _unique(items):
    result = {}
    for key, value in items:
        if key in result: raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _freeze(value):
    if isinstance(value, Mapping):
        if any(not isinstance(k, str) for k in value): raise ValueError("authorization key must be string")
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (tuple, list)): return tuple(_freeze(v) for v in value)
    if isinstance(value, float) and not math.isfinite(value): raise ValueError("nonfinite authorization")
    if value is None or isinstance(value, (str, bool, int, float)): return value
    raise ValueError("authorization must be immutable JSON data")


@dataclass(frozen=True)
class LegBound:
    side: str
    min_qty: Decimal
    max_qty: Decimal
    quote_currency: str
    max_spend: Decimal
    reduce_only: bool = False
    # ``max_spend`` is an approved quote-currency budget.  A SELL action is
    # additionally bounded by max_qty; it must have an explicit minimum quote
    # receive because a native Quote.max_spend may be denominated in base.
    min_receive: Decimal = Decimal(0)
    min_receive_currency: str | None = None

    def __post_init__(self):
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("bound side must be BUY or SELL")
        lo, hi = _d(self.min_qty, "min_qty", nonnegative=True), _d(self.max_qty, "max_qty", positive=True)
        if lo > hi:
            raise ValueError("min_qty exceeds max_qty")
        _text(self.quote_currency, "quote_currency")
        _d(self.max_spend, "max_spend", nonnegative=True)
        if type(self.reduce_only) is not bool:
            raise ValueError("reduce_only must be bool")
        _d(self.min_receive, "min_receive", nonnegative=True)
        if self.min_receive_currency is not None:
            _text(self.min_receive_currency, "min_receive_currency")


@dataclass(frozen=True)
class OperationPlan:
    operation_id: str
    kind: str
    legs: tuple[LegSpec, LegSpec]
    leading_leg_id: str
    target_exposure: Decimal
    max_unhedged_exposure: Decimal
    expires_at: float
    rounding_policy: str
    bounds: Mapping[str, LegBound]
    authorization: Mapping[str, Any]

    def __post_init__(self):
        _text(self.operation_id, "operation_id")
        if self.kind not in {"entry", "exit", "rehedge"}:
            raise ValueError("unknown operation kind")
        if type(self.legs) is not tuple or len(self.legs) != 2 or not all(isinstance(x, LegSpec) for x in self.legs):
            raise ValueError("exactly two LegSpec legs are required")
        a, b = self.legs
        if a.leg_id == b.leg_id or a.scope == b.scope:
            raise ValueError("legs must have distinct ids and scopes")
        if a.asset_id != b.asset_id:
            raise ValueError("legs must prove the same asset identity")
        if a.direction == b.direction:
            raise ValueError("legs must have opposite directions")
        if self.leading_leg_id not in {a.leg_id, b.leg_id}:
            raise ValueError("leading leg must be one of the two legs")
        _d(self.target_exposure, "target_exposure", positive=True)
        _d(self.max_unhedged_exposure, "max_unhedged_exposure", nonnegative=True)
        if not isinstance(self.expires_at, (int, float)) or isinstance(self.expires_at, bool) or not math.isfinite(self.expires_at):
            raise ValueError("expires_at must be finite")
        _text(self.rounding_policy, "rounding_policy")
        if self.rounding_policy not in {"floor", "ceil", "nearest"}:
            raise ValueError("unknown rounding policy")
        if set(self.bounds) != {a.leg_id, b.leg_id} or any(not isinstance(v, LegBound) for v in self.bounds.values()):
            raise ValueError("explicit bounds required for both legs")
        for leg in self.legs:
            bound = self.bounds[leg.leg_id]
            if bound.quote_currency != leg.quote_currency:
                raise ValueError("bound currency differs from LegSpec")
            if leg.capabilities.market_kind == "spot" and bound.side == "SELL" and leg.direction == "short":
                raise ValueError("spot short is unsupported")
            if self.kind == "exit" and leg.capabilities.market_kind == "perpetual" and not bound.reduce_only:
                raise ValueError("perpetual close requires reduce_only")
            if bound.reduce_only and not leg.capabilities.reduce_only:
                raise ValueError("reduce_only capability unavailable")
            if self.kind in {"entry", "exit"}:
                expected = "BUY" if leg.direction == "long" else "SELL"
                if self.kind == "exit": expected = "SELL" if expected == "BUY" else "BUY"
                if bound.side != expected:
                    raise ValueError("action direction differs from operation")
            if bound.max_qty * leg.multiplier > self.target_exposure:
                raise ValueError("leg bound exceeds approved exposure")
        if not isinstance(self.authorization, Mapping) or not self.authorization:
            raise ValueError("explicit authorization fields required")
        object.__setattr__(self, "bounds", MappingProxyType(dict(self.bounds)))
        object.__setattr__(self, "authorization", _freeze(self.authorization))

    def to_dict(self) -> dict[str, Any]:
        def conv(v):
            if isinstance(v, Decimal): return format(v, "f")
            if isinstance(v, Mapping): return {k: conv(v[k]) for k in sorted(v)}
            if isinstance(v, (tuple, list)): return [conv(x) for x in v]
            return v
        return conv({"version": 1, "operation_id": self.operation_id, "kind": self.kind,
            "legs": [asdict(x) for x in self.legs], "leading_leg_id": self.leading_leg_id,
            "target_exposure": self.target_exposure, "max_unhedged_exposure": self.max_unhedged_exposure,
            "expires_at": self.expires_at, "rounding_policy": self.rounding_policy,
            "bounds": {k: asdict(v) for k, v in self.bounds.items()}, "authorization": self.authorization})

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)

    @classmethod
    def from_json(cls, raw: str) -> "OperationPlan":
        try: data = json.loads(raw, object_pairs_hook=_unique, parse_constant=_invalid_constant)
        except (TypeError, json.JSONDecodeError) as exc: raise ValueError("invalid operation plan JSON") from exc
        required = {"version", "operation_id", "kind", "legs", "leading_leg_id", "target_exposure",
                    "max_unhedged_exposure", "expires_at", "rounding_policy", "bounds", "authorization"}
        if not isinstance(data, dict) or set(data) != required or data["version"] != 1 or len(data["legs"]) != 2:
            raise ValueError("unknown or missing operation plan fields")
        leg_fields = set(LegSpec.__dataclass_fields__)
        legs = []
        for item in data["legs"]:
            if set(item) != leg_fields:
                raise ValueError("unknown or missing LegSpec fields")
            item = dict(item)
            item["capabilities"] = Capabilities(**item["capabilities"])
            item["fee_currencies"] = tuple(item["fee_currencies"])
            for key in ("multiplier", "step", "tick"):
                item[key] = _json_decimal(item[key], key)
            legs.append(LegSpec(**item))
        bound_fields = set(LegBound.__dataclass_fields__)
        legacy_bound_fields = bound_fields - {"min_receive", "min_receive_currency"}
        bounds = {}
        for key, item in data["bounds"].items():
            if set(item) not in (bound_fields, legacy_bound_fields): raise ValueError("unknown or missing bound fields")
            item = dict(item)
            item.setdefault("min_receive", "0")
            item.setdefault("min_receive_currency", None)
            for n in ("min_qty", "max_qty", "max_spend", "min_receive"):
                item[n] = _json_decimal(item[n], n)
            bounds[key] = LegBound(**item)
        return cls(operation_id=data["operation_id"], kind=data["kind"], legs=tuple(legs),
                   leading_leg_id=data["leading_leg_id"], target_exposure=_json_decimal(data["target_exposure"], "target_exposure"),
                   max_unhedged_exposure=_json_decimal(data["max_unhedged_exposure"], "max_unhedged_exposure"), expires_at=data["expires_at"],
                   rounding_policy=data["rounding_policy"], bounds=bounds, authorization=data["authorization"])

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()
