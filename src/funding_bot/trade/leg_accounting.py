"""Leg-aware accounting reconstructed from the append-only execution journal.

This module deliberately has no exchange or transport code.  Adapters translate
their public :class:`Result` and frozen :class:`LegSpec` into ``ExecutionFact``
and call :func:`record_fact`; the read model is always rebuilt from
``exec_events``.  Amounts are Decimal strings in the journal and currencies are
never converted without an explicit FX observation.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Mapping

from . import store
from .adapters.contracts import LegSpec, Result, Status
from .fees import FeeComponent

KIND = "leg_execution_fact_v1"
READER = 4


def _dec(value: Any, name: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an exact decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a finite decimal") from None
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _text(value: Any, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} must be a non-empty public string")
    return value


@dataclass(frozen=True)
class Fee:
    amount: Decimal | None
    currency: str
    known: bool = True
    inventory_debit: bool = True
    component: dict | None = None

    def __post_init__(self):
        amount = _dec(self.amount, "fee.amount", optional=not self.known)
        if amount is not None and amount < 0:
            raise ValueError("fee amount must be non-negative")
        _text(self.currency, "fee.currency")
        if type(self.known) is not bool:
            raise ValueError("fee.known must be bool")


@dataclass(frozen=True)
class ExecutionFact:
    """Public execution evidence; no keys, signed payloads, or native secrets."""

    operation_id: str
    leg_id: str
    spec_hash: str
    scope: str
    native_ref: str
    side: str
    actual_qty: Decimal
    multiplier: Decimal
    quality: str
    fees: tuple[Fee, ...] = ()
    funding: tuple[tuple[Decimal, str], ...] = ()
    base_currency: str | None = None
    settlement_currency: str | None = None
    quantity_semantics: str = "delta"
    market_kind: str = "spot"
    fees_complete: bool = False

    def __post_init__(self):
        for n in ("operation_id", "leg_id", "spec_hash", "scope", "native_ref", "quality"):
            _text(getattr(self, n), n)
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        q = _dec(self.actual_qty, "actual_qty")
        m = _dec(self.multiplier, "multiplier")
        if q < 0 or m <= 0:
            raise ValueError("quantity must be non-negative and multiplier positive")
        if self.quantity_semantics != "delta":
            raise ValueError("only a proven terminal execution per native reference is supported")
        if type(self.fees_complete) is not bool:
            raise ValueError("fees_complete must be bool")
        if self.market_kind not in {"spot", "perpetual"}:
            raise ValueError("market_kind must be spot or perpetual")
        for n in ("base_currency", "settlement_currency"):
            _text(getattr(self, n), n, optional=True)

    @property
    def durable_identity(self) -> str:
        # Native reference is scoped by operation and leg.  The same order id
        # on two accounts therefore cannot collide.
        parts = (self.operation_id, self.leg_id, self.scope, self.native_ref)
        return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))

    def payload(self) -> dict[str, Any]:
        return {
            "version": 1, "identity": self.durable_identity, "operation_id": self.operation_id,
            "leg_id": self.leg_id, "spec_hash": self.spec_hash, "scope": self.scope,
            "native_ref": self.native_ref, "side": self.side, "actual_qty": str(self.actual_qty),
            "multiplier": str(self.multiplier), "quality": self.quality,
            "quantity_semantics": self.quantity_semantics,
            "market_kind": self.market_kind, "fees_complete": self.fees_complete,
            "base_currency": self.base_currency, "settlement_currency": self.settlement_currency,
            "fees": [{"amount": None if f.amount is None else str(f.amount), "currency": f.currency, "known": f.known,
                      "inventory_debit": f.inventory_debit, "component": f.component} for f in self.fees],
            "funding": [{"amount": str(a), "currency": c} for a, c in self.funding],
        }


def _scope_key(scope: Any) -> str:
    if isinstance(scope, str):
        return scope
    if not isinstance(scope, tuple):
        raise ValueError("scope must be the frozen tuple from LegSpec/Result")
    return json.dumps(scope, ensure_ascii=False, separators=(",", ":"), default=str)


def _fee(value: Any) -> Fee:
    if isinstance(value, Fee):
        return value
    if isinstance(value, FeeComponent):
        amount = value.human()
        return Fee(amount, value.asset, known=amount is not None)
    if isinstance(value, Mapping):
        return Fee(Decimal(value["amount"]), value["currency"], bool(value.get("known", True)))
    raise ValueError("fees must be FeeComponent or Fee")


def fact_from_result(result: Result, spec: LegSpec, *, operation_id: str, side: str,
                     native_ref: str | None = None, quality: str | None = None,
                     fees: tuple[Fee, ...] = (), funding: tuple[tuple[Decimal, str], ...] = ()) -> ExecutionFact:
    """Adapt the existing adapter ``Result`` contract into a durable fact."""
    if not isinstance(result, Result) or result.version != 2:
        raise ValueError("leg accounting requires Result version 2")
    if (result.status not in {Status.SETTLED, Status.PARTIAL, Status.CANCELLED, Status.REJECTED}
            or not result.terminal or result.provisional or result.executed_quantity is None):
        raise ValueError("execution result is not proven final quantity")
    if result.status == Status.SETTLED and not result.terminal:
        raise ValueError("settled result must be terminal")
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    qty = result.executed_quantity
    result_ref = result.native_ref
    if not getattr(result_ref, "kind", None) or not getattr(result_ref, "id", None):
        raise ValueError("native_ref is required")
    ref = f"{result_ref.kind}:{result_ref.id}"
    if native_ref is not None and native_ref != ref:
        raise ValueError("native_ref differs from Result")
    if result.leg_id != spec.leg_id:
        raise ValueError("result leg_id differs from LegSpec")
    spec_hash = spec.fingerprint
    result_hash = getattr(result, "spec_hash", None)
    if result_hash != spec_hash:
        raise ValueError("result spec_hash differs from LegSpec")
    result_scope = result.scope
    expected_scope = tuple(spec.scope)
    if result_scope is None or tuple(result_scope) != expected_scope:
        raise ValueError("result scope differs from LegSpec")
    # Result.fees is the adapter's authoritative FeeComponent tuple.  An
    # explicit argument is useful for CEX results whose fee currency is public
    # but not represented by FeeComponent.
    if fees and tuple(fees) != tuple(result.fees):
        raise ValueError("fee override cannot replace Result.fees")
    fee_values = []
    for component in result.fees:
        if not isinstance(component, FeeComponent):
            raise ValueError("Result fees require authoritative FeeComponent metadata")
        fee = _fee(component)
        # Embedded swap costs already affect the proven token flows. Estimates,
        # sponsor fees and refundable deposits cannot become another base debit.
        charged = component.payer == spec.account and not component.superseded
        known = fee.known and not component.estimated and component.payer is not None
        currency = fee.currency
        if (spec.capabilities.market_kind == "spot" and spec.capabilities.venue_kind == "dex"
                and currency == spec.instrument):
            currency = spec.asset_id  # exact frozen mint/address, never a ticker alias
        fee_values.append(replace(fee, currency=currency, known=known,
            inventory_debit=charged and not component.included_in_input_output and not component.refundable,
            component=component.as_record()))
    base_currency = spec.asset_id
    settlement = spec.settlement_currency
    return ExecutionFact(operation_id=operation_id, leg_id=spec.leg_id, spec_hash=spec_hash,
                         scope=_scope_key(expected_scope), native_ref=ref, side=side,
                         actual_qty=_dec(qty, "executed_quantity"), multiplier=spec.multiplier,
                         quality=quality or str(result.finality), fees=tuple(fee_values),
                         fees_complete=result.fees_complete, quantity_semantics="delta",
                         market_kind=spec.capabilities.market_kind,
                         funding=funding, base_currency=base_currency, settlement_currency=settlement)


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record_fact(con, fact: ExecutionFact, *, deal_id: str | None = None,
                intent_id: str | None = None, clip_id: int | None = None,
                now: float | None = None) -> bool:
    """Atomically persist one fact; return False for an identical retry.

    A reused durable identity with different evidence is a hard conflict.  No
    second ledger/table is involved and the reader floor is raised in the same
    SQLite write transaction as the event.
    """
    if not isinstance(fact, ExecutionFact):
        raise TypeError("fact must be ExecutionFact")
    payload = fact.payload()
    digest = hashlib.sha256(_canonical(payload).encode()).hexdigest()
    with store.tx(con):
        store.require_reader(con, READER, now=now)
        rows = con.execute("SELECT json FROM exec_events WHERE kind=?", (KIND,)).fetchall()
        for (raw,) in rows:
            old = json.loads(raw or "{}")
            if old.get("identity") != fact.durable_identity:
                continue
            if old.get("digest") == digest or _canonical({k: v for k, v in old.items() if k != "digest"}) == _canonical(payload):
                return False
            raise ValueError(f"conflicting execution fact identity: {fact.durable_identity}")
        payload["digest"] = digest
        store.event(con, KIND, deal_id=deal_id, intent_id=intent_id, clip_id=clip_id, now=now, **payload)
    return True


def _fact(raw: str) -> ExecutionFact:
    p = json.loads(raw)
    return ExecutionFact(operation_id=p["operation_id"], leg_id=p["leg_id"], spec_hash=p["spec_hash"],
        scope=p["scope"], native_ref=p["native_ref"], side=p["side"], actual_qty=Decimal(p["actual_qty"]),
        multiplier=Decimal(p["multiplier"]), quality=p["quality"],
        quantity_semantics=p.get("quantity_semantics", "delta"), market_kind=p.get("market_kind", "spot"),
        base_currency=p.get("base_currency"), settlement_currency=p.get("settlement_currency"),
        fees_complete=p.get("fees_complete", False),
        fees=tuple(Fee(None if x.get("amount") is None else Decimal(x["amount"]), x["currency"], bool(x.get("known", True)),
                       x.get("inventory_debit", True), x.get("component")) for x in p.get("fees", [])),
        funding=tuple((Decimal(x["amount"]), x["currency"]) for x in p.get("funding", [])))


def rebuild(con, *, operation_id: str | None = None) -> dict[str, Any]:
    """Pure read-model rebuild.  Currency buckets never cross-convert."""
    query = "SELECT json FROM exec_events WHERE kind=? ORDER BY ts,rowid"
    args: tuple[Any, ...] = (KIND,)
    rows = con.execute(query, args).fetchall()
    legs = {}
    for (raw,) in rows:
        fact = _fact(raw)
        if operation_id is not None and fact.operation_id != operation_id:
            continue
        key = (fact.leg_id, fact.scope)
        leg = legs.setdefault(key, {"leg_id": fact.leg_id, "spec_hash": fact.spec_hash,
            "scope": fact.scope, "qty": Decimal(0), "executions": 0, "fees": {},
            "unknown_fees": 0, "fees_complete": True, "funding": {}})
        if leg["spec_hash"] != fact.spec_hash:
            raise ValueError("leg specification changed within accounting scope")
        leg["fees_complete"] = leg["fees_complete"] and fact.fees_complete
        leg["qty"] += fact.actual_qty * fact.multiplier * (1 if fact.side == "BUY" else -1)
        for fee in fact.fees:
            if not fee.known or fee.amount is None:
                leg["unknown_fees"] += 1
                leg["fees_complete"] = False
                continue
            component = fee.component or {}
            if component.get("superseded") or component.get("refundable"):
                continue
            if component and component.get("payer") != json.loads(fact.scope)[2]:
                continue
            leg["fees"][fee.currency] = leg["fees"].get(fee.currency, Decimal(0)) + fee.amount
            if fact.market_kind == "spot" and fee.inventory_debit and fee.currency == fact.base_currency:
                leg["qty"] -= fee.amount
        for amount, currency in fact.funding:
            leg["funding"][currency] = leg["funding"].get(currency, Decimal(0)) + amount
        leg["executions"] += 1
    return {"version": 1, "operation_id": operation_id, "legs": tuple(_encode_leg(x) for x in legs.values())}


def _encode_leg(leg):
    return {**leg, "qty": str(leg["qty"]),
            "fees": {k: str(v) for k, v in leg["fees"].items()},
            "funding": {k: str(v) for k, v in leg["funding"].items()}}


# Explicit aliases make the adapter boundary easy to discover without adding
# another persistence API.
append_fact = record_fact
project = rebuild
