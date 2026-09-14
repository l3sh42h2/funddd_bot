#!/usr/bin/env python3
"""Offline M4 accounting replay against synthetic execution-journal fixtures.

The reference projection below is intentionally independent from the production
accounting helpers.  Its rules are frozen to ``95354c4``.  A target checkout is
materialized into a temporary SQLite database and read through the production
projections.  Input fixtures are read-only; all database writes go to a new
temporary database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from decimal import Decimal as D
from pathlib import Path
from typing import Any, Iterable

from funding_bot import config
from funding_bot.trade import engine, ledger_flows, marks, sol_ledger, store

ORACLE_BASE_SHA = "95354c4"
TARGET_ROOT = Path(store.__file__).resolve().parents[3]
FIXTURE_VERSION = 1
FLOW_STATES = {"DEX_OK", "PERP_SENT", "BALANCED", "HEDGE_DEFICIT"}
FILLED_STATES = {"FILLED", "PARTIALLY_FILLED"}
OPEN_PERP_STATES = {"INTENT", "SENT", "UNKNOWN"}
ACTIVE_OP_STATES = {"APPROVED", "RUNNING", "PARTIAL", "STOPPED", "PAUSED_RISK", "PAUSED_UNKNOWN"}
STABLE_ASSETS = {"USDT", "USDC", "USD", "USD1"}
SOL_NONREFUNDABLE = {"network_total", "network_base", "network_priority", "tip", "rent_nonrefundable"}
ALLOWED_TABLES = {
    "deals", "intents", "clips", "dex_txs", "perp_orders", "operations", "operation_intents",
    "hl_order_attempts", "sol_tx_attempts", "fee_events", "ingest_cursors", "core_notifications",
}
V1_IDENTITY = ("schema", "chain", "token", "token_dec", "spot_units_per_token", "perp_venue", "perp_symbol",
               "units_per_contract")
V2_IDENTITY = (
    "schema", "profile_id", "instrument_id", "chain", "network", "genesis_hash", "token", "token_dec",
    "token_program", "token_extensions", "quote_mint", "quote_dec", "quote_program", "spot_units_per_token",
    "perp_venue", "perp_network", "perp_dex", "perp_symbol", "perp_account", "perp_collateral",
    "units_per_contract",
)


class ReplayError(RuntimeError):
    pass


@dataclass(frozen=True)
class Mismatch:
    fixture: str
    path: str
    expected: Any
    actual: Any
    classification: str = "verified_known_exact"


@dataclass
class ReplayResult:
    fixture: str
    fixture_sha256: str
    target_revision: str | None
    oracle_base_sha: str
    classification: str
    oracle: dict
    actual: dict
    mismatches: list[Mismatch]
    gaps: list[str]
    ingest_counts: dict[str, list[int]]

    @property
    def passed(self) -> bool:
        return not self.mismatches and self.classification == "verified_known_exact"

    def as_dict(self) -> dict:
        out = asdict(self)
        out["passed"] = self.passed
        return out


def _d(v: Any) -> D | None:
    if v is None or v == "":
        return None
    try:
        x = D(str(v))
    except (ArithmeticError, ValueError):
        return None
    return x if x.is_finite() else None


def _ds(v: Any) -> str | None:
    x = _d(v)
    if x is None:
        return None
    if x == 0:
        return "0"
    return format(x.normalize(), "f")


def _div(v: D, scale: int) -> D:
    return v / (D(10) ** int(scale))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_fixture(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        raise ReplayError(f"{path}: invalid JSON: {exc}") from exc
    if obj.get("fixture_version") != FIXTURE_VERSION:
        raise ReplayError(f"{path}: fixture_version must be {FIXTURE_VERSION}")
    if obj.get("oracle_base_sha") != ORACLE_BASE_SHA:
        raise ReplayError(f"{path}: oracle_base_sha must be {ORACLE_BASE_SHA}")
    if obj.get("family") not in {"evm", "solana"}:
        raise ReplayError(f"{path}: family must be evm or solana")
    if not obj.get("synthetic"):
        raise ReplayError(f"{path}: fixture must explicitly declare synthetic=true")
    if not isinstance(obj.get("expected_oracle_sha256"), str) or len(obj["expected_oracle_sha256"]) != 64:
        raise ReplayError(f"{path}: expected_oracle_sha256 is required")
    return obj, _sha(raw)


def _insert_rows(con: sqlite3.Connection, table: str, rows: Iterable[dict]) -> None:
    if table not in ALLOWED_TABLES:
        raise ReplayError(f"fixture table {table!r} is not allowed")
    for row in rows:
        if not row:
            raise ReplayError(f"empty row for {table}")
        cols = tuple(row)
        known = {x[1] for x in con.execute(f"PRAGMA table_info({table})")}
        bad = set(cols) - known
        if bad:
            raise ReplayError(f"{table}: unknown columns {sorted(bad)}")
        con.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
                    tuple(row[c] for c in cols))


def materialize_fixture(fixture: dict, path: Path) -> tuple[sqlite3.Connection, dict[str, list[int]]]:
    """Create an isolated replay database. ``path`` must be a disposable path."""
    con = store.connect(path)
    from funding_bot.core.journal import SCHEMA as CORE_SCHEMA
    con.executescript(CORE_SCHEMA)
    with store.tx(con):
        for table, rows in fixture.get("tables", {}).items():
            _insert_rows(con, table, rows)
    counts: dict[str, list[int]] = {}
    batches = fixture.get("ingest_batches", {})
    for batch in batches.get("perp_fills", []):
        counts.setdefault("perp_fills", []).append(store.add_perp_fills(con, batch["venue"], batch["rows"]))
    for batch in batches.get("funding_income", []):
        counts.setdefault("funding_income", []).append(store.add_funding_income(con, batch["venue"], batch["rows"]))
    for batch in batches.get("hl_fills", []):
        counts.setdefault("hl_fills", []).append(store.add_hl_fills(con, batch["rows"], now=fixture["as_of_ms"] / 1000))
    for batch in batches.get("hl_funding", []):
        counts.setdefault("hl_funding", []).append(store.add_hl_funding(con, batch["rows"], now=fixture["as_of_ms"] / 1000))
    return con, counts


def _dedup_fixture_rows(fixture: dict, kind: str) -> list[dict]:
    out: list[dict] = [dict(x) for x in fixture.get("tables", {}).get(kind, [])]
    batches = fixture.get("ingest_batches", {}).get(kind, [])
    if kind == "perp_fills":
        key = lambda r: (r["venue"], int(r["trade_id"]))
        for batch in batches:
            out.extend({**r, "venue": batch["venue"]} for r in batch["rows"])
    elif kind == "funding_income":
        key = lambda r: (r["venue"], int(r["tran_id"]))
        for batch in batches:
            out.extend({**r, "venue": batch["venue"]} for r in batch["rows"])
    elif kind == "hl_fills":
        key = lambda r: (r["network"], str(r["account"]).lower(), r["coin"], int(r["time"]), int(r["tid"]))
        for batch in batches:
            out.extend(dict(r) for r in batch["rows"])
    elif kind == "hl_funding":
        key = lambda r: (r["network"], str(r["account"]).lower(), r["coin"], int(r["time"]), str(r.get("hash") or ""))
        for batch in batches:
            out.extend(dict(r) for r in batch["rows"])
    else:
        return out
    unique: dict[tuple, dict] = {}
    for row in out:
        if kind in {"hl_fills", "hl_funding"}:
            row = {**row, "account": str(row["account"]).lower()}
        k = key(row)
        if k in unique:
            # Exact overlap is a duplicate page. Conflicting rows are invalid evidence.
            if {a: str(b) for a, b in unique[k].items()} != {a: str(b) for a, b in row.items()}:
                raise ReplayError(f"{kind}: conflicting duplicate key {k}")
            continue
        unique[k] = row
    return list(unique.values())


def _inst_hash(inst: dict) -> str | None:
    try:
        fields = V1_IDENTITY if int(inst.get("schema", 1)) == 1 else V2_IDENTITY
        ident = {k: inst[k] for k in fields}
    except (KeyError, TypeError, ValueError):
        return None
    if isinstance(ident.get("token_extensions"), list):
        ident["token_extensions"] = sorted(ident["token_extensions"])
    for k in ("units_per_contract", "spot_units_per_token"):
        ident[k] = _ds(ident[k])
    raw = json.dumps(ident, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _fixture_rows(fixture: dict, table: str) -> list[dict]:
    if table in {"perp_fills", "funding_income", "hl_fills", "hl_funding"}:
        return _dedup_fixture_rows(fixture, table)
    return [dict(x) for x in fixture.get("tables", {}).get(table, [])]


def _deal_rows(fixture: dict) -> tuple[dict, list[dict], list[dict], list[dict]]:
    deals = _fixture_rows(fixture, "deals")
    if len(deals) != 1:
        raise ReplayError("each fixture must contain exactly one deal")
    deal = deals[0]
    intents = [x for x in _fixture_rows(fixture, "intents") if x.get("deal_id") == deal["id"]]
    intent_ids = {x["id"] for x in intents}
    clips = [x for x in _fixture_rows(fixture, "clips") if x.get("intent_id") in intent_ids]
    clip_ids = {int(x["id"]) for x in clips}
    orders = [x for x in _fixture_rows(fixture, "perp_orders") if int(x.get("clip_id") or -1) in clip_ids or
              str(x.get("client_id", "")).startswith(f"fb-{deal['id']}-")]
    return deal, intents, clips, orders


def _strict_flows(clips: list[dict], intents: list[dict], orders: list[dict], quote_dec: int,
                  exit_kinds: set[str]) -> tuple[dict, list[str]]:
    kinds = {x["id"]: x["kind"] for x in intents}
    sin = sout = D(0)
    missing: list[str] = []
    for c in sorted(clips, key=lambda x: int(x["id"])):
        if c.get("state") in {"DEX_SENT", "DEX_UNKNOWN"}:
            missing.append(f"clip:{c['id']}:outcome")
            continue
        if c.get("state") not in FLOW_STATES:
            continue
        kind = kinds[c["intent_id"]]
        if kind == "entry":
            amount, side = c.get("dex_in"), "in"
        elif kind in exit_kinds:
            amount, side = c.get("dex_out"), "out"
        else:
            continue
        if amount is None:
            missing.append(f"clip:{c['id']}:quote")
        value = _div(D(int(amount or 0)), quote_dec)
        if side == "in":
            sin += value
        else:
            sout += value
    psell = pbuy = D(0)
    for o in sorted(orders, key=lambda x: x["client_id"]):
        if o.get("state") in OPEN_PERP_STATES:
            missing.append(f"order:{o['client_id']}:outcome")
            continue
        if o.get("state") not in FILLED_STATES:
            continue
        if o.get("cum_quote") is None:
            missing.append(f"order:{o['client_id']}:quote")
        q = _d(o.get("cum_quote")) or D(0)
        if o["side"] == "SELL":
            psell += q
        else:
            pbuy += q
    out = {"spot_debit": _ds(sin), "spot_credit": _ds(sout),
           "spot_net": None if any(x.startswith("clip:") for x in missing) else _ds(sout - sin),
           "legacy_spot_net": _ds(sout - sin), "perp_debit": _ds(pbuy), "perp_credit": _ds(psell),
           "perp_net": None if any(x.startswith("order:") for x in missing) else _ds(psell - pbuy),
           "legacy_perp_net": _ds(psell - pbuy)}
    return out, missing


def _positions(deal: dict, clips: list[dict], intents: list[dict], orders: list[dict], inst: dict) -> dict:
    kinds = {x["id"]: x["kind"] for x in intents}
    token_raw: int | None = 0
    why: list[str] = []
    for c in sorted(clips, key=lambda x: int(x["id"])):
        if c.get("state") in {"DEX_SENT", "DEX_UNKNOWN"}:
            token_raw = None
            why.append(f"clip {c['id']}: DEX unknown")
            continue
        if c.get("state") in {"PLANNED", "DEX_REVERTED"} or token_raw is None:
            continue
        if kinds[c["intent_id"]] == "entry":
            token_raw += int(c.get("dex_out") or 0)
        elif kinds[c["intent_id"]] in {"exit", "undo"}:
            token_raw -= int(c.get("dex_in") or 0)
    short: D | None = D(0)
    for o in orders:
        if o.get("state") in OPEN_PERP_STATES:
            short = None
            why.append(f"order {o['client_id']}: outcome unknown")
            continue
        if short is None:
            continue
        qty = _d(o.get("executed_qty")) or D(0)
        short += qty if o.get("side") == "SELL" else -qty
    token_dec = int(deal["token_dec"])
    fs = _d(inst.get("spot_units_per_token")) if inst else None
    fp = _d(inst.get("units_per_contract")) if inst else None
    spot_tokens = None if token_raw is None else _div(D(token_raw), token_dec)
    spot_base = None if spot_tokens is None or fs is None else spot_tokens * fs
    perp_base = None if short is None or fp is None else short * fp
    return {"spot_raw": token_raw, "perp_contracts": _ds(short), "spot_base_units": _ds(spot_base),
            "perp_base_units": _ds(perp_base),
            "delta_base_units": _ds(spot_base - perp_base) if spot_base is not None and perp_base is not None else None,
            "known": token_raw is not None and short is not None and fs is not None and fp is not None,
            "unknown_reasons": why}


def _cost_basis(clips: list[dict], intents: list[dict], orders: list[dict], token_dec: int, quote_dec: int) -> dict:
    kinds = {x["id"]: x["kind"] for x in intents}
    all_entry = [c for c in clips if kinds[c["intent_id"]] == "entry"]
    entry_clips = [c for c in all_entry if c.get("state") in FLOW_STATES]
    spot_unknown = any(c.get("state") in {"DEX_SENT", "DEX_UNKNOWN"} or
                       (c.get("state") in FLOW_STATES and None in (c.get("dex_in"), c.get("dex_out")))
                       for c in all_entry)
    spot_raw = None if spot_unknown else sum(int(c["dex_out"]) for c in entry_clips)
    spot_quote = None if spot_unknown else sum((_div(D(int(c["dex_in"])), quote_dec) for c in entry_clips), D(0))
    entry_ids = {int(c["id"]) for c in all_entry}
    entry_orders = [o for o in orders if int(o.get("clip_id") or -1) in entry_ids and o.get("side") == "SELL"]
    perp_unknown = any(o.get("state") in OPEN_PERP_STATES or
                       (o.get("state") in FILLED_STATES and None in (o.get("executed_qty"), o.get("cum_quote")))
                       for o in entry_orders)
    opens = [o for o in entry_orders if o.get("state") in FILLED_STATES]
    contracts = None if perp_unknown else sum((_d(o["executed_qty"]) for o in opens), D(0))
    quote = None if perp_unknown else sum((_d(o["cum_quote"]) for o in opens), D(0))
    tokens = None if spot_raw is None else _div(D(spot_raw), token_dec)
    complete = not spot_unknown and not perp_unknown
    return {"spot_acquired_raw": spot_raw, "spot_quote_cost": _ds(spot_quote),
            "spot_avg_entry_price": _ds(spot_quote / tokens) if spot_quote is not None and tokens else None,
            "perp_opened_contracts": _ds(contracts), "perp_quote_credit": _ds(quote),
            "perp_avg_entry_price": _ds(quote / contracts) if quote is not None and contracts else None,
            "complete": complete}


def _operation_state(fixture: dict, deal_id: str) -> dict:
    ops = [x for x in _fixture_rows(fixture, "operations") if x.get("deal_id") == deal_id]
    active = []
    for o in sorted(ops, key=lambda x: x["id"]):
        if o.get("state") in ACTIVE_OP_STATES:
            target, confirmed, reserved = (int(o[k]) for k in ("target_raw", "confirmed_raw", "reserved_raw"))
            active.append({"id": o["id"], "state": o["state"], "target_raw": target,
                           "confirmed_raw": confirmed, "reserved_raw": reserved,
                           "remaining_unreserved_raw": target - confirmed - reserved,
                           "manual_resume_required": o["state"] in {"PARTIAL", "STOPPED", "PAUSED_RISK",
                                                                                "PAUSED_UNKNOWN"}})
    notifications = _fixture_rows(fixture, "core_notifications")
    groups: dict[str, int] = {}
    for row in notifications:
        payload = row.get("payload")
        try:
            payload = json.loads(payload) if isinstance(payload, str) else payload
        except ValueError:
            payload = {"malformed": True}
        digest = _sha(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        groups[digest] = groups.get(digest, 0) + 1
    dex_unknown = sorted(str(x.get("tx_hash")) for x in _fixture_rows(fixture, "dex_txs")
                         if x.get("state") in {"SIGNED", "SENT", "UNKNOWN"})
    sol_unknown = sorted(f"{x.get('network')}:{x.get('wallet')}:{x.get('attempt_id')}"
                         for x in _fixture_rows(fixture, "sol_tx_attempts")
                         if x.get("state") in {"BROADCAST_ATTEMPTED", "SIGNED_DURABLE", "UNKNOWN"})
    hl_unknown = sorted(f"{x.get('network')}:{str(x.get('account')).lower()}:{x.get('client_id')}"
                        for x in _fixture_rows(fixture, "hl_order_attempts")
                        if x.get("state") in {"SIGNED", "SENT", "UNKNOWN"})
    return {"deal_state": next(x["state"] for x in _fixture_rows(fixture, "deals") if x["id"] == deal_id),
            "intent_states": {x["id"]: x["status"] for x in _fixture_rows(fixture, "intents")
                              if x.get("deal_id") == deal_id},
            "clip_states": {str(x["id"]): x["state"] for x in _fixture_rows(fixture, "clips")},
            "active_operations": active, "unresolved_evm": dex_unknown, "unresolved_solana": sol_unknown,
            "unresolved_hl": hl_unknown, "notification_payload_multiplicity": sorted(groups.values()),
            "notification_trade_actions": 0}


def _oracle_evm(fixture: dict, deal: dict, intents: list[dict], clips: list[dict], orders: list[dict], inst: dict) -> dict:
    qdec = int(fixture["quote_decimals"])
    flows, missing = _strict_flows(clips, intents, orders, qdec, {"exit", "undo"})
    fills = _fixture_rows(fixture, "perp_fills")
    by_order: dict[tuple, list[dict]] = {}
    for f in fills:
        by_order.setdefault((f["venue"], int(f["order_id"])), []).append(f)
    fees, fees_est = D(0), False
    for o in orders:
        if o.get("state") not in FILLED_STATES:
            continue
        q = _d(o.get("cum_quote")) or D(0)
        paid = covered = D(0)
        trust = False
        for f in by_order.get((o.get("venue"), int(o["order_id"])), []):
            if str(f.get("commission_asset") or "USDT").upper() not in STABLE_ASSETS:
                continue
            paid += abs(_d(f.get("commission_abs")) or D(0))
            fq = _d(f.get("quote_qty"))
            if fq is None:
                px, qty = _d(f.get("price")), _d(f.get("qty"))
                fq = px * qty if px is not None and qty is not None else None
            if fq is None:
                trust = True
            else:
                covered += fq
        fees += paid
        if not trust and q - covered > D("0.000001"):
            fees += (q - covered) * _d(fixture["fee_rate"])
            fees_est = True
    start, end = int(float(deal["created"]) * 1000), int(float(deal["updated"]) * 1000)
    funds = [x for x in _fixture_rows(fixture, "funding_income") if x["venue"] == deal["perp_venue"] and
             x["symbol"] == deal["symbol"] and start <= int(x["ts"]) <= end]
    funding = sum((_d(x["income"]) or D(0) for x in funds), D(0))
    gas_native = D(0)
    for tx in _fixture_rows(fixture, "dex_txs"):
        if tx.get("status") == 1 and tx.get("gas_used") is not None and tx.get("eff_gas_price") is not None:
            gas_native += D(int(tx["gas_used"])) * D(int(tx["eff_gas_price"])) / D(10) ** 18
    native_px = _d(fixture.get("native_price_quote"))
    gas_quote = None if gas_native and native_px is None else gas_native * (native_px or D(0))
    pnl = None
    if flows["spot_net"] is not None and flows["perp_net"] is not None and gas_quote is not None:
        pnl = _d(flows["spot_net"]) + _d(flows["perp_net"]) - fees + funding - gas_quote
    return {"flows": flows, "missing_monetary_evidence": missing,
            "fees": {"perp_quote": _ds(fees), "estimated": fees_est, "network_native": _ds(gas_native),
                     "network_quote": _ds(gas_quote)},
            "funding": {"quote": _ds(funding), "events": len(funds), "complete": True},
            "realized_pnl_quote": _ds(pnl),
            "accounting_complete": not missing and not fees_est and pnl is not None}


def _oracle_sol(fixture: dict, deal: dict, intents: list[dict], clips: list[dict], orders: list[dict], inst: dict) -> dict:
    qdec = int(inst["quote_dec"])
    flows, missing = _strict_flows(clips, intents, orders, qdec, {"exit"})
    sc_parts = str(inst.get("perp_account") or "").split(":")
    scope = (sc_parts[1], sc_parts[3].lower(), inst["perp_symbol"]) if len(sc_parts) == 5 else None
    attempts = {x["client_id"]: x for x in _fixture_rows(fixture, "hl_order_attempts") if x.get("deal_id") == deal["id"]}
    fills = _fixture_rows(fixture, "hl_fills")
    fees, fees_est = D(0), False
    for o in orders:
        if o.get("state") not in FILLED_STATES:
            continue
        q = _d(o.get("cum_quote")) or D(0)
        attempt = attempts.get(o["client_id"])
        cloid = attempt.get("cloid") if attempt else None
        rows = [f for f in fills if scope and (f["network"], str(f["account"]).lower(), f["coin"]) == scope and
                f.get("cloid") == cloid]
        covered, paid = D(0), D(0)
        bad = False
        for f in rows:
            px, sz, fee = _d(f.get("px")), _d(f.get("sz")), _d(f.get("fee"))
            if None in (px, sz, fee) or str(f.get("fee_token") or "").upper() != "USDC":
                bad = True
                break
            covered += px * sz
            paid += abs(fee)
        rest = q if bad else q - covered
        if rest > D("0.000001"):
            fees_est = True
            fees += (D(0) if bad else paid) + rest * _d(fixture["fee_rate"])
        else:
            fees += paid
    network: int | None = 0
    rent: int | None = 0
    spot_external = D(0)
    other: list[str] = []
    for f in _fixture_rows(fixture, "fee_events"):
        if f.get("deal_id") != deal["id"] or f.get("included") or f.get("superseded"):
            continue
        amount = None if f.get("amount_raw") is None else int(f["amount_raw"])
        if f.get("refundable"):
            rent = None if amount is None or rent is None else rent + (amount if f["kind"] == "rent_deposit" else -amount)
        elif f["asset"] == "native:solana" and f["kind"] in SOL_NONREFUNDABLE:
            network = None if amount is None or network is None else network + amount
        elif f["asset"] == inst["quote_mint"]:
            if amount is None:
                other.append(f["kind"])
            else:
                spot_external += _div(D(amount), int(f["decimals"]))
        else:
            other.append(f"{f['kind']}:{f['asset']}")
    start, end = int(float(deal["created"]) * 1000), int(float(deal["updated"]) * 1000)
    funds = [x for x in _fixture_rows(fixture, "hl_funding") if scope and
             (x["network"], str(x["account"]).lower(), x["coin"]) == scope and start <= int(x["time"]) <= end]
    funding = sum((_d(x["usdc"]) or D(0) for x in funds), D(0)) if scope else None
    native_px = _d(fixture.get("native_price_quote"))
    network_quote = None if network is None or (network and native_px is None) else _div(D(network or 0), 9) * (native_px or D(0))
    pnl = None
    if not missing and not other and funding is not None and network_quote is not None:
        pnl = _d(flows["spot_net"]) + _d(flows["perp_net"]) - fees - spot_external - network_quote + funding
    cursors = {x["scope"]: x for x in _fixture_rows(fixture, "ingest_cursors")}
    complete = bool(scope) and all(bool(cursors.get(f"hl:{scope[0]}:{scope[1]}:{scope[2]}:{kind}", {}).get("complete"))
                                   for kind in ("fills", "funding"))
    return {"flows": flows, "missing_monetary_evidence": missing,
            "fees": {"perp_quote": _ds(fees), "estimated": fees_est, "network_lamports": network,
                     "network_quote": _ds(network_quote), "rent_locked_lamports": rent,
                     "spot_external_quote": _ds(spot_external), "other_unknown": other},
            "funding": {"quote": _ds(funding), "events": len(funds), "complete": complete},
            "realized_pnl_quote": _ds(pnl),
            "accounting_complete": not missing and not fees_est and not other and complete and pnl is not None}


def oracle_projection(fixture: dict) -> dict:
    deal, intents, clips, orders = _deal_rows(fixture)
    raw_inst = deal.get("inst_json")
    try:
        inst = json.loads(raw_inst) if raw_inst else {}
    except ValueError:
        inst = {}
    identity = {"inst_json_exact": raw_inst, "inst_json_sha256": _sha((raw_inst or "").encode()),
                "inst_hash": _inst_hash(inst), "operation_inst_hashes": sorted(
                    x["inst_hash"] for x in _fixture_rows(fixture, "operations") if x.get("deal_id") == deal["id"]),
                "token": deal["token"], "perp_scope": deal.get("perp_scope")}
    body = _oracle_evm(fixture, deal, intents, clips, orders, inst) if fixture["family"] == "evm" else \
        _oracle_sol(fixture, deal, intents, clips, orders, inst)
    return {"deal_id": deal["id"], "family": fixture["family"], "as_of_ms": int(fixture["as_of_ms"]),
            "identity": identity, "positions": _positions(deal, clips, intents, orders, inst),
            "cost_basis": _cost_basis(clips, intents, orders, int(deal["token_dec"]),
                                      int(fixture["quote_decimals"])),
            "accounting": body, "state": _operation_state(fixture, deal["id"])}


def _rows_from_db(con: sqlite3.Connection, table: str) -> list[dict]:
    return [dict(x) for x in con.execute(f"SELECT * FROM {table}")]


def current_projection(con: sqlite3.Connection, fixture: dict) -> dict:
    deal = dict(con.execute("SELECT * FROM deals").fetchone())
    did = deal["id"]
    intents = _rows_from_db(con, "intents")
    clips = _rows_from_db(con, "clips")
    orders = [x for x in _rows_from_db(con, "perp_orders") if str(x.get("client_id", "")).startswith(f"fb-{did}-")]
    raw_inst = deal.get("inst_json")
    parsed = engine.deal_instrument(con, deal)
    identity = {"inst_json_exact": raw_inst, "inst_json_sha256": _sha((raw_inst or "").encode()),
                "inst_hash": parsed.inst_hash() if parsed.source != "unreadable" else None,
                "operation_inst_hashes": sorted(x[0] for x in con.execute(
                    "SELECT inst_hash FROM operations WHERE deal_id=?", (did,))),
                "token": deal["token"], "perp_scope": deal.get("perp_scope")}
    bk = engine.deal_book(con, did)
    position_reasons = [f"clip {x['id']}: DEX unknown" for x in clips
                        if x.get("state") in {"DEX_SENT", "DEX_UNKNOWN"}]
    position_reasons.extend(f"order {x['client_id']}: outcome unknown" for x in orders
                            if x.get("state") in OPEN_PERP_STATES)
    positions = {"spot_raw": bk.tokens_raw, "perp_contracts": _ds(bk.short),
                 "spot_base_units": _ds(bk.tokens(int(deal["token_dec"])) * parsed.fs) if bk.tokens_raw is not None else None,
                 "perp_base_units": _ds(bk.short * parsed.fp) if bk.short is not None else None,
                 "delta_base_units": _ds(bk.delta(int(deal["token_dec"]))) if bk.known else None,
                 "known": bk.known, "unknown_reasons": position_reasons}
    sf = ledger_flows.spot_quote_flows(con, did, int(fixture["quote_decimals"]),
                                       exit_kinds=("exit", "undo") if fixture["family"] == "evm" else ("exit",))
    pf = ledger_flows.perp_quote_flows(con, did)
    clip_missing = [f"clip:{x['id']}:outcome" for x in sorted(clips, key=lambda x: int(x["id"]))
                    if x.get("state") in {"DEX_SENT", "DEX_UNKNOWN"}]
    order_missing = [f"order:{x['client_id']}:outcome" for x in sorted(orders, key=lambda x: x["client_id"])
                     if x.get("state") in OPEN_PERP_STATES]
    flows = {"spot_debit": _ds(sf.debit), "spot_credit": _ds(sf.credit),
             "spot_net": None if clip_missing or sf.missing else _ds(sf.net),
             "legacy_spot_net": _ds(sf.legacy_net), "perp_debit": _ds(pf.debit), "perp_credit": _ds(pf.credit),
             "perp_net": None if order_missing or pf.missing else _ds(pf.net),
             "legacy_perp_net": _ds(pf.legacy_net)}
    missing = [*clip_missing, *sf.missing, *order_missing, *pf.missing]
    if fixture["family"] == "evm":
        j = marks.journal(con, deal, until_ms=int(fixture["as_of_ms"]))
        native_px = _d(fixture.get("native_price_quote"))
        gas_quote = j.gas_usd(native_px)
        pnl = None if missing or gas_quote is None else j.spot_flow + j.perp_flow - j.fees + (j.funding or D(0)) - gas_quote
        body = {"flows": flows, "missing_monetary_evidence": missing,
                "fees": {"perp_quote": _ds(j.fees), "estimated": bool(j.fees_est),
                         "network_native": _ds(j.gas_native), "network_quote": _ds(gas_quote)},
                "funding": {"quote": _ds(j.funding), "events": con.execute(
                    "SELECT count(*) FROM funding_income WHERE venue=? AND symbol=? AND ts>=? AND ts<=?",
                    (deal["perp_venue"], deal["symbol"], int(float(deal["created"]) * 1000),
                     int(fixture["as_of_ms"]))).fetchone()[0], "complete": True},
                "realized_pnl_quote": _ds(pnl),
                "accounting_complete": not missing and not j.fees_est and pnl is not None}
    else:
        L = sol_ledger.ledger(con, deal, fee_rate=_d(fixture["fee_rate"]))
        native_px = _d(fixture.get("native_price_quote"))
        pnl = None if missing else L.base(native_px)
        body = {"flows": flows, "missing_monetary_evidence": missing,
                "fees": {"perp_quote": _ds(L.perp_fee), "estimated": bool(L.fees_est),
                         "network_lamports": L.network_lamports, "network_quote": _ds(L.network_usdc(native_px)),
                         "rent_locked_lamports": L.rent_locked_lamports, "spot_external_quote": _ds(L.spot_ext_usdc),
                         "other_unknown": list(L.other_unknown)},
                "funding": {"quote": _ds(L.funding), "events": L.funding_n,
                            "complete": bool(L.funding_complete)}, "realized_pnl_quote": _ds(pnl),
                "accounting_complete": not missing and bool(L.complete) and L.network_usdc(native_px) is not None and
                                       not L.other_unknown and pnl is not None}
    tables = {name: _rows_from_db(con, name) for name in ("deals", "intents", "clips", "dex_txs", "operations",
                                                                          "core_notifications", "sol_tx_attempts",
                                                                          "hl_order_attempts")}
    shadow = {**fixture, "tables": {**fixture.get("tables", {}), **tables}}
    return {"deal_id": did, "family": fixture["family"], "as_of_ms": int(fixture["as_of_ms"]),
            "identity": identity, "positions": positions,
            "cost_basis": _cost_basis(clips, intents, orders, int(deal["token_dec"]),
                                      int(fixture["quote_decimals"])),
            "accounting": body, "state": _operation_state(shadow, did)}


def _compare(expected: Any, actual: Any, fixture: str, path: str = "") -> list[Mismatch]:
    out: list[Mismatch] = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            p = f"{path}.{key}" if path else key
            if key not in expected:
                out.append(Mismatch(fixture, p, "<absent>", actual[key]))
            elif key not in actual:
                out.append(Mismatch(fixture, p, expected[key], "<absent>"))
            else:
                out.extend(_compare(expected[key], actual[key], fixture, p))
    elif isinstance(expected, list) and isinstance(actual, list):
        if expected != actual:
            out.append(Mismatch(fixture, path, expected, actual))
    elif expected != actual:
        out.append(Mismatch(fixture, path, expected, actual))
    return out


def target_revision(root: Path) -> str | None:
    try:
        rev = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True,
                                      stderr=subprocess.DEVNULL).strip()
        status = subprocess.check_output(["git", "status", "--porcelain", "--", "src/funding_bot"], cwd=root)
        if status:
            diff = subprocess.check_output(["git", "diff", "--binary", "HEAD", "--", "src/funding_bot"], cwd=root)
            rev += "+dirty:" + _sha(status + b"\0" + diff)[:12]
        return rev
    except (OSError, subprocess.CalledProcessError):
        return None


def replay_fixture(path: Path, *, root: Path | None = None) -> ReplayResult:
    fixture, digest = load_fixture(path)
    oracle = oracle_projection(fixture)
    oracle_digest = _sha(json.dumps(oracle, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())
    if oracle_digest != fixture["expected_oracle_sha256"]:
        raise ReplayError(f"{path}: oracle digest changed: expected {fixture['expected_oracle_sha256']}, "
                          f"actual {oracle_digest}")
    expected = fixture.get("expected")
    if expected is not None:
        drift = _compare(expected, oracle, fixture["name"], "expected")
        if drift:
            raise ReplayError(f"{path}: frozen expected output differs from oracle: {drift[0]}")
    with tempfile.TemporaryDirectory(prefix="funding-m4-replay-") as td:
        con, counts = materialize_fixture(fixture, Path(td) / "trade.db")
        try:
            actual = current_projection(con, fixture)
        finally:
            con.close()
    mismatches = _compare(oracle, actual, fixture["name"])
    classification = "verified_known_exact"
    gaps = list(fixture.get("known_gaps", []))
    if oracle["accounting"]["missing_monetary_evidence"]:
        classification = "unsafe_legacy_fallback"
        gaps.append("missing monetary evidence: strict projection is unknown; legacy numeric fallback is not accepted")
    expected_counts = fixture.get("expected_ingest_counts", {})
    mismatches.extend(_compare(expected_counts, counts, fixture["name"], "ingest_counts"))
    return ReplayResult(fixture["name"], digest, target_revision(root or TARGET_ROOT), ORACLE_BASE_SHA,
                        classification, oracle, actual, mismatches, gaps, counts)


def replay_paths(paths: Iterable[Path], *, root: Path | None = None) -> list[ReplayResult]:
    return [replay_fixture(path, root=root) for path in sorted(paths)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("fixtures", nargs="*", type=Path, help="fixture JSON files")
    ap.add_argument("--fixture-dir", type=Path, default=Path("tests/replay_fixtures"))
    ap.add_argument("--json", action="store_true", help="emit complete JSON report")
    ap.add_argument("--allow-unsafe", action="store_true",
                    help="return success when only explicit unsafe legacy fixtures are non-passing")
    ns = ap.parse_args(argv)
    paths = ns.fixtures or list(ns.fixture_dir.glob("m4_*.json"))
    if not paths:
        ap.error("no fixtures found")
    try:
        results = replay_paths(paths, root=TARGET_ROOT)
    except (ReplayError, sqlite3.Error, ValueError) as exc:
        print(f"REPLAY_ERROR: {exc}")
        return 2
    if ns.json:
        print(json.dumps([x.as_dict() for x in results], ensure_ascii=False, indent=2))
    else:
        for result in results:
            status = "PASS" if result.passed else ("UNSAFE" if not result.mismatches else "FAIL")
            print(f"{status} {result.fixture} fixture={result.fixture_sha256[:12]} target={result.target_revision or '?'}")
            for mismatch in result.mismatches:
                print(f"  mismatch {mismatch.path}: expected={mismatch.expected!r} actual={mismatch.actual!r}")
            for gap in result.gaps:
                print(f"  gap: {gap}")
    failed = any(x.mismatches or (not x.passed and not ns.allow_unsafe) for x in results)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
