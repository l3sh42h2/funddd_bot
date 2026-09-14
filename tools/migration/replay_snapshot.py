#!/usr/bin/env python3
"""Replay a redacted trade journal snapshot against two funding_bot revisions.

The driver never opens a production database and the workers have no network
access.  Each worker creates a disposable SQLite database from a strict column
whitelist, then calls the revision's read-only accounting projections.  Prices
and the accounting cut are inputs in the snapshot, so both revisions see the
same evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

BASELINE_REF = "95354c4"
SNAPSHOT_VERSION = 1

COMMON_COLUMNS = {
    "deals": {"id", "created", "state", "coin", "chain", "token", "token_dec", "perp_venue", "symbol",
              "leg_usd", "sim", "carry", "dust", "updated", "inst_json", "perp_scope"},
    "intents": {"id", "deal_id", "kind", "status", "created"},
    "clips": {"id", "intent_id", "seq", "state", "planned_in", "dex_in", "dex_out", "perp_qty",
              "perp_quote", "carry_in", "carry_out", "created", "updated"},
    "perp_orders": {"id", "clip_id", "client_id", "venue", "symbol", "side", "reduce_only", "state",
                    "order_id", "executed_qty", "avg_price", "cum_quote", "sent_ts", "resolved_ts"},
    "dex_txs": {"id", "clip_id", "kind", "chain", "tx_hash", "state", "status", "gas_used",
                "eff_gas_price", "amount_in", "amount_out", "sent_ts", "resolved_ts"},
    "perp_fills": {"venue", "trade_id", "order_id", "price", "qty", "quote_qty", "commission_abs",
                   "commission_asset", "maker", "realized_pnl", "ts"},
    "funding_income": {"venue", "tran_id", "symbol", "income", "ts"},
}
SOL_COLUMNS = {
    "hl_order_attempts": {"client_id", "cloid", "network", "account", "fullcoin", "state", "deal_id",
                          "intent_id", "clip_id"},
    "hl_fills": {"network", "account", "coin", "time", "tid", "oid", "cloid", "side", "px", "sz", "fee",
                 "fee_token", "builder_fee", "closed_pnl", "hash", "ingested"},
    "hl_funding": {"network", "account", "coin", "time", "hash", "usdc", "szi", "rate", "ingested"},
    "ingest_cursors": {"scope", "watermark_ms", "complete", "gap", "updated"},
    "fee_events": {"id", "ts", "origin_kind", "origin_ref", "idx", "deal_id", "operation_id", "clip_id",
                   "kind", "asset", "decimals", "amount_raw", "included", "estimated", "refundable",
                   "superseded", "source", "val_unit", "val_amount", "val_ts", "val_source"},
    "sol_tx_attempts": {"attempt_id", "clip_ref", "path", "state", "created"},
}
TOP_LEVEL = {"snapshot_version", "family", "as_of_ms", "deal_id", "fee_rate", "marks", "tables",
             "observation_ts", "approve_tx_hashes"}
MARK_COLUMNS = {"spot_quote", "perp_quote", "native_quote"}
FLOW_STATES = {"DEX_OK", "PERP_SENT", "BALANCED", "HEDGE_DEFICIT"}
FILLED_STATES = {"FILLED", "PARTIALLY_FILLED"}
OPEN_DEX_STATES = {"DEX_SENT", "DEX_UNKNOWN"}
OPEN_PERP_STATES = {"INTENT", "SENT", "UNKNOWN"}
STABLES = {"USD", "USDC", "USDT", "USD1"}
MILLISECOND_TIME_COLUMNS = {"perp_fills": "ts", "funding_income": "ts", "hl_fills": "time",
                            "hl_funding": "time"}


class SnapshotError(RuntimeError):
    pass


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return None
    try:
        out = Decimal(repr(value) if isinstance(value, float) else str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def _raw(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value)
    return int(text) if text.isdigit() else None


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value == 0:
            return "0"
        return format(value.normalize(), "f")
    if isinstance(value, Mapping):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_snapshot(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"cannot read snapshot: {exc}") from exc
    if not isinstance(doc, dict):
        raise SnapshotError("snapshot root must be an object")
    extra = set(doc) - TOP_LEVEL
    missing = TOP_LEVEL - set(doc)
    if extra or missing:
        raise SnapshotError(f"snapshot top-level fields: missing={sorted(missing)}, forbidden={sorted(extra)}")
    if doc["snapshot_version"] != SNAPSHOT_VERSION:
        raise SnapshotError(f"snapshot_version must be {SNAPSHOT_VERSION}")
    if doc["family"] not in {"evm", "solana"}:
        raise SnapshotError("family must be evm or solana")
    if not isinstance(doc["as_of_ms"], int) or doc["as_of_ms"] <= 0:
        raise SnapshotError("as_of_ms must be a positive integer")
    observed = _decimal(doc["observation_ts"])
    if doc["observation_ts"] is not None and (observed is None or observed < 0):
        raise SnapshotError("observation_ts must be a finite non-negative Unix timestamp or null")
    if not isinstance(doc["deal_id"], str) or not doc["deal_id"]:
        raise SnapshotError("deal_id must be a non-empty string")
    if doc["fee_rate"] is not None and (_decimal(doc["fee_rate"]) is None or _decimal(doc["fee_rate"]) < 0):
        raise SnapshotError("fee_rate must be a finite non-negative decimal string or null")
    if not isinstance(doc["marks"], dict) or set(doc["marks"]) != MARK_COLUMNS:
        raise SnapshotError(f"marks must contain exactly {sorted(MARK_COLUMNS)}")
    for name, value in doc["marks"].items():
        if value is not None and _decimal(value) is None:
            raise SnapshotError(f"marks.{name} must be a finite decimal string or null")
    columns = dict(COMMON_COLUMNS)
    if doc["family"] == "solana":
        columns.update(SOL_COLUMNS)
    if not isinstance(doc["tables"], dict) or set(doc["tables"]) != set(columns):
        raise SnapshotError(f"tables must contain exactly {sorted(columns)}")
    for table, rows in doc["tables"].items():
        if not isinstance(rows, list):
            raise SnapshotError(f"tables.{table} must be an array")
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not row:
                raise SnapshotError(f"tables.{table}[{index}] must be a non-empty object")
            forbidden = set(row) - columns[table]
            if forbidden:
                raise SnapshotError(f"tables.{table}[{index}] forbidden columns: {sorted(forbidden)}")
            time_column = MILLISECOND_TIME_COLUMNS.get(table)
            if time_column and row.get(time_column) is not None:
                timestamp = row[time_column]
                if isinstance(timestamp, bool) or not isinstance(timestamp, int):
                    raise SnapshotError(f"tables.{table}[{index}].{time_column} must be integer milliseconds")
                if timestamp < 0 or timestamp > doc["as_of_ms"]:
                    raise SnapshotError(f"tables.{table}[{index}].{time_column} is outside 0..as_of_ms")
            if table == "fee_events" and row.get("ts") is not None:
                timestamp = _decimal(row["ts"])
                if timestamp is None or timestamp < 0 or timestamp * 1000 > doc["as_of_ms"]:
                    raise SnapshotError(f"tables.fee_events[{index}].ts is outside 0..as_of_ms")
    approvals = doc["approve_tx_hashes"]
    if not isinstance(approvals, list):
        raise SnapshotError("approve_tx_hashes must be an array")
    for index, row in enumerate(approvals):
        if not isinstance(row, dict) or set(row) != {"intent_id", "tx_hash"}:
            raise SnapshotError(f"approve_tx_hashes[{index}] has invalid shape")
    deals = [row for row in doc["tables"]["deals"] if row.get("id") == doc["deal_id"]]
    if len(deals) != 1 or len(doc["tables"]["deals"]) != 1:
        raise SnapshotError("snapshot must contain exactly its one named deal")
    return doc


def _insert(con: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    known = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    bad = set(row) - known
    if bad:
        raise SnapshotError(f"revision schema lacks {table} columns {sorted(bad)}")
    cols = list(row)
    con.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
                [row[col] for col in cols])


def _materialize(doc: dict[str, Any], db_path: Path, store: Any) -> sqlite3.Connection:
    con = store.connect(db_path)
    order = ["deals", "intents", "clips", "dex_txs", "perp_orders", "perp_fills", "funding_income"]
    if doc["family"] == "solana":
        order += ["hl_order_attempts", "hl_fills", "hl_funding", "ingest_cursors", "fee_events",
                  "sol_tx_attempts"]
    try:
        con.execute("BEGIN IMMEDIATE")
        for table in order:
            for n, source in enumerate(doc["tables"][table]):
                row = dict(source)
                if table == "hl_order_attempts":
                    row.update(kind="snapshot", master="snapshot", signer="snapshot", nonce=n + 1,
                               action_json="{}", action_hash=f"snapshot:{n}")
                elif table == "sol_tx_attempts":
                    row.update(network="snapshot", wallet="snapshot", logical_action_id=f"snapshot:{n}",
                               provider="snapshot", payload_kind="snapshot", message_hash=f"snapshot:{n}",
                               recent_blockhash="snapshot", lvbh_exact=0, plan_json="{}",
                               updated=row.get("created", 0))
                _insert(con, table, row)
        for n, link in enumerate(doc["approve_tx_hashes"]):
            _insert(con, "exec_events", {"ts": doc["as_of_ms"] / 1000, "deal_id": doc["deal_id"],
                                          "intent_id": link["intent_id"], "kind": "approve",
                                          "json": json.dumps({"hashes": [link["tx_hash"]]}), "clip_id": None})
        con.commit()
        return con
    except Exception:
        con.rollback()
        con.close()
        raise


def _block_network() -> None:
    def denied(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("network disabled by replay_snapshot")
    socket.create_connection = denied  # type: ignore[assignment]
    socket.socket.connect = denied  # type: ignore[assignment]
    socket.socket.connect_ex = denied  # type: ignore[assignment]


def _worker(snapshot_path: Path, db_path: Path) -> dict[str, Any]:
    doc = load_snapshot(snapshot_path)
    _block_network()
    from funding_bot.trade import engine, marks, sol_ledger, store

    con = _materialize(doc, db_path, store)
    try:
        deal = store.get_deal(con, doc["deal_id"])
        if deal is None:
            raise SnapshotError("materialized deal was not found")
        book = engine.deal_book(con, doc["deal_id"])
        position = {"tokens_raw": book.tokens_raw, "short_contracts": book.short, "units_per_contract": book.m,
                    "instrument_verified": book.inst_ok, "instrument_reason": book.inst_why,
                    "position_reason": book.why}
        native = _decimal(doc["marks"]["native_quote"])
        spot_px = _decimal(doc["marks"]["spot_quote"])
        perp_px = _decimal(doc["marks"]["perp_quote"])
        if doc["family"] == "evm":
            ledger = marks.journal(con, deal, until_ms=doc["as_of_ms"])
            funding = Decimal(0) if deal["sim"] and ledger.funding is None else ledger.funding
            gas_quote = ledger.gas_usd(native)
            spot_flow, perp_flow = ledger.spot_flow, ledger.perp_flow
            base = None
            if None not in (spot_flow, perp_flow, ledger.fees, funding, gas_quote):
                base = spot_flow + perp_flow - ledger.fees + funding - gas_quote
            accounting = {"spot_debit": ledger.spot_in, "spot_credit": ledger.spot_out,
                          "spot_net": spot_flow, "perp_debit": ledger.perp_buy,
                          "perp_credit": ledger.perp_sell, "perp_net": perp_flow, "fees": ledger.fees,
                          "fees_estimated": ledger.fees_est, "funding": ledger.funding,
                          "gas_native": ledger.gas_native, "gas_quote": gas_quote,
                          "missing_flows": list(getattr(ledger, "missing_flows", ())),
                          "cash_basis_quote": base}
        else:
            rate = _decimal(doc["fee_rate"])
            ledger = sol_ledger.ledger(con, deal, fee_rate=rate)
            base = ledger.base(native)
            accounting = {"spot_debit": ledger.spot_in, "spot_credit": ledger.spot_out,
                          "spot_net": ledger.spot_flow, "perp_debit": ledger.perp_buy,
                          "perp_credit": ledger.perp_sell, "perp_net": ledger.perp_flow,
                          "fees": ledger.perp_fee, "fees_estimated": ledger.fees_est,
                          "funding": ledger.funding, "funding_rows": ledger.funding_n,
                          "gas_native_raw": ledger.network_lamports, "gas_quote": ledger.network_usdc(native),
                          "rent_locked_native_raw": ledger.rent_locked_lamports,
                          "spot_external_quote": ledger.spot_ext_usdc,
                          "other_unknown": list(ledger.other_unknown),
                          "missing_flows": list(getattr(ledger, "missing_flows", ())),
                          "fills_complete": ledger.fills_complete,
                          "funding_complete": ledger.funding_complete,
                          "accounting_complete": ledger.complete,
                          "foreign_fill_ids": list(ledger.foreign_fills), "routes": list(ledger.routes),
                          "cash_basis_quote": base}
        pnl = None
        if base is not None and book.tokens_raw is not None and book.short is not None:
            token_qty = Decimal(book.tokens_raw) / (Decimal(10) ** int(deal["token_dec"]))
            spot_value = Decimal(0) if token_qty == 0 else (None if spot_px is None else token_qty * spot_px)
            short_units = book.short * book.m
            perp_value = Decimal(0) if short_units == 0 else (None if perp_px is None else short_units * perp_px)
            if spot_value is not None and perp_value is not None:
                pnl = base + spot_value - perp_value
        return _json_value({"deal_state": deal["state"], "position": position, "accounting": accounting,
                            "same_cut_pnl_quote": pnl})
    finally:
        con.close()


def _strict_evidence(doc: dict[str, Any]) -> dict[str, Any]:
    missing: list[str] = []
    intents = {row.get("id"): row.get("kind") for row in doc["tables"]["intents"]}
    clip_ids = {row.get("id") for row in doc["tables"]["clips"]}
    token_raw: int | None = 0
    for row in doc["tables"]["clips"]:
        state, kind = row.get("state"), intents.get(row.get("intent_id"))
        if state in OPEN_DEX_STATES:
            missing.append(f"clip:{row.get('id')}:outcome")
            token_raw = None
        if state not in FLOW_STATES:
            continue
        exit_kinds = {"exit", "undo"} if doc["family"] == "evm" else {"exit"}
        field = "dex_in" if kind == "entry" else ("dex_out" if kind in exit_kinds else None)
        if field and _raw(row.get(field)) is None:
            missing.append(f"clip:{row.get('id')}:{field}")
        position_field = "dex_out" if kind == "entry" else ("dex_in" if kind in {"exit", "undo"} else None)
        position_amount = _raw(row.get(position_field)) if position_field else None
        if position_field and position_amount is None:
            missing.append(f"clip:{row.get('id')}:{position_field}")
            token_raw = None
        elif token_raw is not None and position_amount is not None:
            token_raw += position_amount if kind == "entry" else -position_amount
    deal_prefix = f"fb-{doc['deal_id']}-"
    orders = [row for row in doc["tables"]["perp_orders"] if str(row.get("client_id", "")).startswith(deal_prefix)]
    short: Decimal | None = Decimal(0)
    for row in orders:
        if row.get("state") in OPEN_PERP_STATES:
            missing.append(f"order:{row.get('client_id')}:outcome")
            short = None
        if row.get("state") in FILLED_STATES:
            q = _decimal(row.get("cum_quote"))
            qty = _decimal(row.get("executed_qty"))
            if q is None or q < 0:
                missing.append(f"order:{row.get('client_id')}:cum_quote")
            if qty is None or qty < 0:
                missing.append(f"order:{row.get('client_id')}:executed_qty")
                short = None
            elif short is not None:
                short += qty if row.get("side") == "SELL" else -qty
    if doc["family"] == "evm":
        fills = doc["tables"]["perp_fills"]
        tolerance = Decimal("0.01")
        known_orders = [x for x in orders if x.get("state") in FILLED_STATES and
                        _decimal(x.get("cum_quote")) is not None]
        for order in known_orders:
            rows = [x for x in fills if x.get("venue") == order.get("venue") and
                    x.get("order_id") == order.get("order_id")]
            covered = Decimal(0)
            for fill in rows:
                commission = _decimal(fill.get("commission_abs"))
                asset = str(fill.get("commission_asset") or "USDT").upper()
                if commission is None or commission < 0 or asset not in STABLES:
                    missing.append(f"fill:{fill.get('venue')}:{fill.get('trade_id')}:commission")
                quote = _decimal(fill.get("quote_qty"))
                if quote is None:
                    price, qty = _decimal(fill.get("price")), _decimal(fill.get("qty"))
                    quote = price * qty if price is not None and qty is not None else None
                if quote is None or quote < 0:
                    missing.append(f"fill:{fill.get('venue')}:{fill.get('trade_id')}:quote")
                else:
                    covered += quote
            total = _decimal(order.get("cum_quote"))
            if total is not None and abs(total - covered) > tolerance:
                missing.append(f"order:{order.get('client_id')}:fee_coverage")
        for row in doc["tables"]["funding_income"]:
            if row.get("venue") == doc["tables"]["deals"][0].get("perp_venue") and _decimal(row.get("income")) is None:
                missing.append(f"funding:{row.get('venue')}:{row.get('tran_id')}:income")
        completeness = {"fills": None, "funding": None}
    else:
        try:
            inst = json.loads(doc["tables"]["deals"][0].get("inst_json") or "{}")
        except ValueError:
            inst = {}
        parts = str(inst.get("perp_account") or "").split(":")
        scope = ((parts[1], parts[3].lower(), str(inst.get("perp_symbol"))) if len(parts) == 5 else None)
        scoped_fills = [row for row in doc["tables"]["hl_fills"]
                        if scope is not None and
                        (row.get("network"), str(row.get("account") or "").lower(), row.get("coin")) == scope]
        for row in scoped_fills:
            for field in ("px", "sz", "fee"):
                value = _decimal(row.get(field))
                if value is None or (field != "fee" and value < 0):
                    missing.append(f"hl_fill:{row.get('time')}:{row.get('tid')}:{field}")
            if str(row.get("fee_token") or "").upper() not in STABLES:
                missing.append(f"hl_fill:{row.get('time')}:{row.get('tid')}:fee_token")
        for row in doc["tables"]["hl_funding"]:
            if scope is None or (row.get("network"), str(row.get("account") or "").lower(), row.get("coin")) != scope:
                continue
            if _decimal(row.get("usdc")) is None:
                missing.append(f"hl_funding:{row.get('time')}:{row.get('hash')}:usdc")
        fee_events = [row for row in doc["tables"]["fee_events"] if row.get("deal_id") == doc["deal_id"]]
        for row in fee_events:
            if not row.get("included") and not row.get("superseded") and _raw(row.get("amount_raw")) is None:
                missing.append(f"fee_event:{row.get('id')}:amount_raw")
            if not row.get("included") and not row.get("superseded") and row.get("estimated"):
                missing.append(f"fee_event:{row.get('id')}:estimated")

        attempts = {}
        for row in doc["tables"]["hl_order_attempts"]:
            client_id = str(row.get("client_id") or "")
            same_deal = row.get("deal_id") == doc["deal_id"] or client_id.startswith(deal_prefix)
            same_scope = scope is not None and (
                row.get("network"), str(row.get("account") or "").lower(), row.get("fullcoin")) == scope
            if same_deal and same_scope and row.get("cloid"):
                attempts[client_id] = str(row["cloid"])
        fills_by_cloid: dict[str, list[dict[str, Any]]] = {}
        for row in scoped_fills:
            if row.get("cloid") is not None:
                fills_by_cloid.setdefault(str(row["cloid"]), []).append(row)
        for order in (row for row in orders if row.get("state") in FILLED_STATES):
            client_id = str(order.get("client_id") or "")
            fill_rows = fills_by_cloid.get(attempts.get(client_id, ""), [])
            covered = Decimal(0)
            valid_coverage = bool(fill_rows)
            for fill in fill_rows:
                price, qty = _decimal(fill.get("px")), _decimal(fill.get("sz"))
                if price is None or qty is None or price < 0 or qty < 0:
                    valid_coverage = False
                else:
                    covered += price * qty
            total = _decimal(order.get("cum_quote"))
            if not valid_coverage or total is None or abs(total - covered) > Decimal("0.000001"):
                missing.append(f"order:{client_id}:fee_coverage")

        if not doc["tables"]["deals"][0].get("sim"):
            for clip in doc["tables"]["clips"]:
                if clip.get("state") not in FLOW_STATES:
                    continue
                exact_fee = any(
                    row.get("clip_id") == clip.get("id") and not row.get("included") and
                    not row.get("superseded") and not row.get("estimated") and
                    row.get("asset") == "native:solana" and row.get("kind") == "network_total" and
                    _raw(row.get("amount_raw")) is not None
                    for row in fee_events
                )
                if not exact_fee:
                    missing.append(f"clip:{clip.get('id')}:network_fee_evidence")

        scopes = {}
        if scope is not None:
            prefix = f"hl:{parts[1]}:{parts[3].lower()}:{inst.get('perp_symbol')}:"
            cursor_rows = {row.get("scope"): row for row in doc["tables"]["ingest_cursors"]}
            deal = doc["tables"]["deals"][0]
            cut = min(doc["as_of_ms"], int(float(deal.get("updated") or doc["as_of_ms"] / 1000) * 1000)) \
                if deal.get("state") in {"CLOSED", "ABORTED"} else doc["as_of_ms"]
            for kind in ("fills", "funding"):
                row = cursor_rows.get(prefix + kind)
                scopes[kind] = bool(row and row.get("complete") and not row.get("gap") and
                                    row.get("watermark_ms") is not None and int(row["watermark_ms"]) >= cut)
        completeness = {"fills": scopes.get("fills"), "funding": scopes.get("funding")}
    # A receipt exists for every materialized EVM transaction. Missing receipt money is unknown even on reverts.
    if doc["family"] == "evm":
        approved = {x["tx_hash"] for x in doc["approve_tx_hashes"]}
        linked = [row for row in doc["tables"]["dex_txs"]
                  if row.get("clip_id") in clip_ids or row.get("tx_hash") in approved]
        for row in linked:
            if row.get("state") not in {"MINED_OK", "MINED_REVERTED"}:
                missing.append(f"tx:{row.get('id')}:outcome")
            used, price = _decimal(row.get("gas_used")), _decimal(row.get("eff_gas_price"))
            if used is None or price is None or used < 0 or price < 0:
                missing.append(f"tx:{row.get('id')}:gas")
        gas_paid = any((_decimal(row.get("gas_used")) or 0) * (_decimal(row.get("eff_gas_price")) or 0) > 0
                       for row in linked)
    else:
        gas_paid = any(not row.get("included") and not row.get("superseded") and
                       row.get("asset") == "native:solana" and (_raw(row.get("amount_raw")) or 0) > 0
                       for row in fee_events)
    if gas_paid and _decimal(doc["marks"]["native_quote"]) is None:
        missing.append("mark:native_quote")
    if token_raw not in (None, 0) and _decimal(doc["marks"]["spot_quote"]) is None:
        missing.append("mark:spot_quote")
    if short not in (None, 0) and _decimal(doc["marks"]["perp_quote"]) is None:
        missing.append("mark:perp_quote")
    accounting_complete = not missing and all(value is True for value in completeness.values())
    return {"missing_monetary_evidence": sorted(set(missing)), "source_complete": completeness,
            "accounting_complete": accounting_complete}


def _projection_gaps(projection: dict[str, Any], family: str) -> list[str]:
    """Projection-owned proof gates; equality alone cannot establish complete accounting."""
    if family != "solana":
        return []
    accounting = projection.get("accounting", {})
    gaps = []
    if accounting.get("fees_estimated") is not False:
        gaps.append("fees_estimated")
    for field in ("fills_complete", "funding_complete", "accounting_complete"):
        if accounting.get(field) is not True:
            gaps.append(field)
    if accounting.get("missing_flows"):
        gaps.append("missing_flows")
    if accounting.get("other_unknown"):
        gaps.append("other_unknown")
    if accounting.get("foreign_fill_ids"):
        gaps.append("foreign_fill_ids")
    return gaps


def _flatten_diff(old: Any, new: Any, path: str = "") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            child = f"{path}.{key}" if path else key
            if key not in old:
                out.append({"path": child, "baseline": "<absent>", "target": new[key]})
            elif key not in new:
                out.append({"path": child, "baseline": old[key], "target": "<absent>"})
            else:
                out.extend(_flatten_diff(old[key], new[key], child))
    elif old != new:
        out.append({"path": path, "baseline": old, "target": new})
    return out


def _revision(root: Path) -> str | None:
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain", "--", "src/funding_bot"], cwd=root,
                                check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
        if status:
            diff = subprocess.run(["git", "diff", "--binary", "HEAD", "--", "src/funding_bot"], cwd=root,
                                  check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
            revision += "+dirty:" + hashlib.sha256(status + b"\0" + diff).hexdigest()[:12]
        return revision
    except (OSError, subprocess.CalledProcessError):
        return None


def _missing_is_projected_unknown(projection: dict[str, Any], reasons: list[str]) -> bool:
    accounting = projection["accounting"]
    if any(reason == "mark:native_quote" and accounting.get("gas_quote") is not None for reason in reasons):
        return False
    if any(reason.startswith("mark:") for reason in reasons) and projection.get("same_cut_pnl_quote") is not None:
        return False
    journal_missing = any(not reason.startswith("mark:") for reason in reasons)
    if journal_missing and (accounting.get("cash_basis_quote") is not None or
                            projection.get("same_cut_pnl_quote") is not None):
        return False
    return True


def _run_revision(root: Path, snapshot: Path, work: Path) -> dict[str, Any]:
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1",
           "LC_ALL": "C", "LANG": "C"}
    command = [sys.executable, str(Path(__file__).resolve()), "_worker", str(snapshot), str(work / "trade.db")]
    run = subprocess.run(command, cwd=root, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if run.returncode:
        raise SnapshotError(f"projection failed for {root}: {run.stderr.strip()[-1200:]}")
    try:
        return json.loads(run.stdout)
    except ValueError as exc:
        raise SnapshotError(f"projection returned invalid JSON for {root}") from exc


def _export_ref(repo: Path, ref: str, destination: Path) -> None:
    try:
        archive = subprocess.run(["git", "archive", "--format=tar", ref, "src/funding_bot"], cwd=repo,
                                 check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tf:
            for member in tf.getmembers():
                target = (destination / member.name).resolve()
                if destination.resolve() not in target.parents and target != destination.resolve():
                    raise SnapshotError("unsafe path in git archive")
            tf.extractall(destination, filter="data")
    except (OSError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        raise SnapshotError(f"cannot export baseline {ref}: {exc}") from exc


def replay(snapshot: Path, *, target_root: Path, repo_root: Path | None = None,
           baseline_root: Path | None = None, baseline_ref: str = BASELINE_REF) -> dict[str, Any]:
    doc = load_snapshot(snapshot)
    evidence = _strict_evidence(doc)
    exported_baseline = baseline_root is None
    with tempfile.TemporaryDirectory(prefix="funding-snapshot-replay-") as raw:
        temp = Path(raw)
        if baseline_root is None:
            baseline_root = temp / "baseline"
            baseline_root.mkdir()
            _export_ref((repo_root or target_root).resolve(), baseline_ref, baseline_root)
        old_dir, new_dir = temp / "old", temp / "new"
        old_dir.mkdir()
        new_dir.mkdir()
        baseline = _run_revision(baseline_root.resolve(), snapshot.resolve(), old_dir)
        target = _run_revision(target_root.resolve(), snapshot.resolve(), new_dir)
    projection_gaps = {"baseline": _projection_gaps(baseline, doc["family"]),
                       "target": _projection_gaps(target, doc["family"])}
    differences = _flatten_diff(baseline, target)
    unsafe_paths = ("accounting.spot_", "accounting.perp_", "accounting.fees", "accounting.funding",
                    "accounting.gas_", "accounting.cash_basis_quote", "accounting.missing_flows", "same_cut_pnl_quote")
    unsafe_differences = [d for d in differences if evidence["missing_monetary_evidence"] and
                          d["path"].startswith(unsafe_paths)]
    safer = [d for d in unsafe_differences if d["baseline"] is not None and d["target"] is None]
    mismatches = [d for d in differences if d not in unsafe_differences]
    if mismatches:
        classification = "mismatch"
    elif unsafe_differences:
        classification = "unsafe_legacy_projection_changed"
    elif evidence["missing_monetary_evidence"]:
        classification = ("exact_strict_unknown" if
                          _missing_is_projected_unknown(baseline, evidence["missing_monetary_evidence"]) and
                          _missing_is_projected_unknown(target, evidence["missing_monetary_evidence"])
                          else "unsafe_shared_fallback")
    elif projection_gaps["baseline"] or projection_gaps["target"]:
        classification = "exact_incomplete_projection"
    elif not evidence["accounting_complete"]:
        classification = "exact_observed_slice_incomplete_sources"
    else:
        classification = "verified_known_exact"
    observed = _decimal(doc["observation_ts"])
    return {"snapshot_sha256": _digest(snapshot), "baseline_revision": baseline_ref if exported_baseline else
            (_revision(baseline_root) or str(baseline_root)), "target_revision": _revision(target_root),
            "as_of_ms": doc["as_of_ms"], "observation_ts": doc["observation_ts"],
            "observation_age_ms": None if observed is None else
            _json_value(Decimal(doc["as_of_ms"]) - observed * 1000),
            "deal_id": doc["deal_id"], "family": doc["family"],
            "classification": classification, "verified_equivalent": classification == "verified_known_exact",
            "strict_evidence": evidence, "baseline": baseline, "target": target,
            "projection_gaps": projection_gaps,
            "unsafe_projection_differences": unsafe_differences,
            "safer_unknown_transitions": safer, "mismatches": mismatches}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--target-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--baseline-ref", default=BASELINE_REF)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = replay(args.snapshot, target_root=args.target_root, repo_root=args.repo_root,
                        baseline_root=args.baseline_root, baseline_ref=args.baseline_ref)
    except (SnapshotError, sqlite3.Error, ValueError) as exc:
        print(f"REPLAY_ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{result['classification']} deal={result['deal_id']} cut={result['as_of_ms']} "
              f"baseline={result['baseline_revision']} target={result['target_revision'] or '?'}")
        for item in result["mismatches"]:
            print(f"  mismatch {item['path']}: {item['baseline']!r} -> {item['target']!r}")
        for item in result["safer_unknown_transitions"]:
            print(f"  strict-unknown {item['path']}: {item['baseline']!r} -> null")
        for reason in result["strict_evidence"]["missing_monetary_evidence"]:
            print(f"  missing: {reason}")
    return 0 if result["verified_equivalent"] else 1


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "_worker":
        try:
            print(json.dumps(_worker(Path(sys.argv[2]), Path(sys.argv[3])), ensure_ascii=False))
        except Exception as exc:  # worker boundary; parent emits the actionable stderr
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
            raise SystemExit(2)
    else:
        raise SystemExit(main())
