"""Exact spot cost-basis projection from the append-only generic journal.

This is deliberately a *read model*: it never changes an execution, balance or
operation state.  It is an operational weighted-average inventory basis for a
single deal and frozen spot leg, not a tax-lot report.  Values are emitted only
when every execution has a matching, complete cash receipt in the same quote
currency.  A missing receipt, an unknown fee or a third-currency cash movement
therefore makes the affected leg incomplete instead of fabricating PnL.
"""
from __future__ import annotations

import json
from decimal import Decimal as D
from typing import Any

from .leg_accounting import KIND as FACT_KIND
from .leg_cash import CASH_KIND

VERSION = 1
POLICY = "weighted_average_v1"


def _decimal(value: Any) -> D:
    # Journal writers accept only Decimal strings.  A finite check here keeps a
    # damaged row from becoming an apparently valid accounting result.
    if isinstance(value, (bool, float)):
        raise ValueError("non-exact decimal in journal")
    result = D(str(value))
    if not result.is_finite():
        raise ValueError("non-finite decimal in journal")
    return result


def _leg(fact: dict[str, Any]) -> dict[str, Any]:
    return dict(leg_id=fact["leg_id"], scope=fact["scope"], spec_hash=fact["spec_hash"],
                base_currency=fact["base_currency"], quote_currency=fact["settlement_currency"],
                quantity=D(0), basis_quote=D(0), proceeds_quote=D(0), released_basis_quote=D(0),
                realized_pnl_quote=D(0), complete=True, reasons=[])


def _fail(leg: dict[str, Any], reason: str) -> None:
    leg["complete"] = False
    if reason not in leg["reasons"]:
        leg["reasons"].append(reason)


def rebuild(con, *, deal_id: str) -> dict[str, Any]:
    """Build one deal's spot basis without mixing accounts, legs or currencies.

    The result is useful for a report only if ``complete`` is true for its leg.
    It intentionally does not value the remaining inventory at a live mark.
    """
    if not isinstance(deal_id, str) or not deal_id:
        raise ValueError("deal_id is required")
    rows = con.execute(
        "SELECT rowid, kind, json FROM exec_events WHERE deal_id=? AND kind IN (?, ?) ORDER BY rowid",
        (deal_id, FACT_KIND, CASH_KIND),
    ).fetchall()
    facts: list[tuple[int, dict[str, Any]]] = []
    cash: dict[str, list[dict[str, Any]]] = {}
    malformed = []
    for rowid, kind, raw in rows:
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("identity"), str):
                raise ValueError("invalid payload")
        except (TypeError, ValueError, json.JSONDecodeError):
            malformed.append((rowid, kind))
            continue
        if kind == FACT_KIND:
            facts.append((rowid, payload))
        else:
            cash.setdefault(payload["identity"], []).append(payload)

    legs: dict[tuple[str, str], dict[str, Any]] = {}
    # A fact with a usable leg/scope key but damaged immutable identity must
    # poison that leg.  Silently skipping it would make a previously valid
    # partial inventory look complete with a smaller quantity.  Keep pending
    # failures too: the damaged fact can precede the first valid fact for the
    # same leg in an append-only journal.
    malformed_legs: dict[tuple[str, str], set[str]] = {}
    for _, fact in facts:
        if fact.get("market_kind") != "spot":
            continue
        key = (fact.get("leg_id"), fact.get("scope"))
        if not all(isinstance(fact.get(k), str) and fact[k] for k in ("leg_id", "scope")):
            malformed.append((None, FACT_KIND))
            continue
        if (not all(isinstance(fact.get(k), str) and fact[k] for k in
                    ("spec_hash", "base_currency", "settlement_currency", "identity"))
                or fact.get("side") not in ("BUY", "SELL")):
            malformed_legs.setdefault(key, set()).add("malformed_execution_event")
            if key in legs:
                _fail(legs[key], "malformed_execution_event")
            continue
        leg = legs.setdefault(key, _leg(fact))
        for reason in malformed_legs.get(key, ()):
            _fail(leg, reason)
        if (leg["spec_hash"], leg["base_currency"], leg["quote_currency"]) != (
                fact["spec_hash"], fact["base_currency"], fact["settlement_currency"]):
            _fail(leg, "frozen_leg_changed")
            continue
        receipts = cash.get(fact["identity"], ())
        if not receipts:
            _fail(leg, "cash_receipt_missing")
            continue
        if len(receipts) != 1:
            _fail(leg, "cash_receipt_ambiguous")
            continue
        receipt = receipts[0]
        if any(receipt.get(k) != fact.get(k) for k in ("operation_id", "leg_id", "spec_hash", "scope", "native_ref")):
            _fail(leg, "cash_receipt_identity_mismatch")
            continue
        if fact.get("fees_complete") is not True or receipt.get("complete") is not True:
            _fail(leg, "fees_incomplete")
            continue
        try:
            flows = {k: _decimal(v) for k, v in receipt["cash"].items()}
        except (KeyError, AttributeError, ValueError):
            _fail(leg, "cash_receipt_invalid")
            continue
        base, quote = leg["base_currency"], leg["quote_currency"]
        if base == quote or base not in flows or quote not in flows:
            _fail(leg, "required_currency_missing")
            continue
        if any(currency not in (base, quote) and amount != 0 for currency, amount in flows.items()):
            _fail(leg, "third_currency_cash")
            continue
        base_delta, quote_delta = flows[base], flows[quote]
        if fact["side"] == "BUY":
            if base_delta <= 0 or quote_delta >= 0:
                _fail(leg, "buy_cash_direction_invalid")
                continue
            leg["quantity"] += base_delta
            leg["basis_quote"] -= quote_delta
        else:
            sold = -base_delta
            if sold <= 0 or quote_delta < 0:
                _fail(leg, "sell_cash_direction_invalid")
                continue
            if leg["quantity"] <= 0 or sold > leg["quantity"]:
                _fail(leg, "sell_exceeds_confirmed_inventory")
                continue
            released = leg["basis_quote"] * sold / leg["quantity"]
            leg["quantity"] -= sold
            leg["basis_quote"] -= released
            leg["proceeds_quote"] += quote_delta
            leg["released_basis_quote"] += released
            leg["realized_pnl_quote"] += quote_delta - released
            if leg["quantity"] == 0:
                leg["basis_quote"] = D(0)

    result = []
    for leg in legs.values():
        complete = leg["complete"] and not malformed
        average = leg["basis_quote"] / leg["quantity"] if complete and leg["quantity"] > 0 else None
        result.append({
            "leg_id": leg["leg_id"], "scope": leg["scope"], "spec_hash": leg["spec_hash"],
            "base_currency": leg["base_currency"], "quote_currency": leg["quote_currency"],
            "quantity": str(leg["quantity"]),
            "basis_quote": str(leg["basis_quote"]) if complete else None,
            "average_cost_quote": str(average) if average is not None else None,
            "proceeds_quote": str(leg["proceeds_quote"]) if complete else None,
            "released_basis_quote": str(leg["released_basis_quote"]) if complete else None,
            "realized_pnl_quote": str(leg["realized_pnl_quote"]) if complete else None,
            "complete": complete,
            "reasons": tuple(leg["reasons"] + (["malformed_execution_event"] if malformed else [])),
        })
    return {"version": VERSION, "policy": POLICY, "deal_id": deal_id, "legs": tuple(result)}


project = rebuild
