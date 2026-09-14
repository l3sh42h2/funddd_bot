"""Versioned account-scoped ingestion storage for perpetual fills and funding.

This is accounting source storage, not an execution-attempt journal.  It never
submits, resolves, or authorizes an order.  A :class:`ProvenScope` must come
from public account identity or a frozen/configured mapping established by the
caller.  ``proof_ref`` is audit metadata; an arbitrary supplied string is not
proof of authorization, and this module deliberately has no credential or
secret-to-scope derivation API.

Legacy rows are imported only with an explicit per-row attribution.  Rows
without one are retained byte-for-value as JSON with their source schema in an
append-only unknown table.  They are never assigned to the currently active
account.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 2
MIN_READER = 2
STREAMS = frozenset({"fills", "funding"})
PROOF_KINDS = frozenset({"frozen_leg", "public_config", "public_api_identity", "migration_manifest"})
_ACCOUNT_SCOPE = re.compile(r"acct:v1:[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}\Z")
_VENUE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}\Z")
_SYMBOL = re.compile(r"[^\s\x00-\x1f\x7f]{1,128}\Z")
_PUBLIC_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}\Z")


class ScopedAccountingError(RuntimeError):
    pass


class SchemaTooNew(ScopedAccountingError):
    pass


@dataclass(frozen=True)
class ProvenScope:
    """Explicit public accounting namespace plus its auditable provenance.

    Construction validates canonical shape only.  The integration boundary is
    responsible for proving that ``account_scope`` belongs to the frozen leg;
    it must use public identity/configuration and must not derive it from an API
    secret.  ``proof_ref`` should identify that evidence without containing it.
    """

    account_scope: str
    venue: str
    symbol: str
    proof_kind: str
    proof_ref: str
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ScopedAccountingError("unsupported proven-scope version")
        checks = ((_ACCOUNT_SCOPE, self.account_scope, "account_scope"),
                  (_VENUE, self.venue, "venue"), (_SYMBOL, self.symbol, "symbol"),
                  (_PUBLIC_REF, self.proof_ref, "proof_ref"))
        for pattern, value, name in checks:
            if not isinstance(value, str) or pattern.fullmatch(value) is None:
                raise ScopedAccountingError(f"{name} is not a canonical public identifier")
        if self.proof_kind not in PROOF_KINDS:
            raise ScopedAccountingError("proof_kind is not an accepted public provenance type")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.account_scope, self.venue, self.symbol


@dataclass(frozen=True)
class LegacyKey:
    stream: str
    venue: str
    native_id: str | int

    def __post_init__(self) -> None:
        if self.stream not in STREAMS:
            raise ScopedAccountingError("legacy stream must be fills or funding")
        if not isinstance(self.venue, str) or _VENUE.fullmatch(self.venue) is None:
            raise ScopedAccountingError("legacy venue is not canonical")
        object.__setattr__(self, "native_id", _native_id(self.native_id, "native_id"))


@dataclass(frozen=True)
class ImportReport:
    fills_imported: int = 0
    funding_imported: int = 0
    unknown_recorded: int = 0


_DDL = (
    """CREATE TABLE IF NOT EXISTS scoped_deal_accounts(
         deal_id TEXT PRIMARY KEY, account_scope TEXT NOT NULL, venue TEXT NOT NULL,
         symbol TEXT NOT NULL, created REAL NOT NULL,
         FOREIGN KEY(account_scope,venue,symbol)
           REFERENCES scoped_account_proofs(account_scope,venue,symbol))""",
    """CREATE TRIGGER IF NOT EXISTS scoped_deal_accounts_no_update
         BEFORE UPDATE ON scoped_deal_accounts BEGIN SELECT RAISE(ABORT, 'append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS scoped_deal_accounts_no_delete
         BEFORE DELETE ON scoped_deal_accounts BEGIN SELECT RAISE(ABORT, 'append-only'); END""",
    """CREATE TABLE IF NOT EXISTS scoped_accounting_schema(
         id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, min_reader INTEGER NOT NULL,
         updated REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS scoped_account_proofs(
         account_scope TEXT NOT NULL, venue TEXT NOT NULL, symbol TEXT NOT NULL,
         proof_kind TEXT NOT NULL, proof_ref TEXT NOT NULL, version INTEGER NOT NULL, created REAL NOT NULL,
         PRIMARY KEY(account_scope, venue, symbol))""",
    """CREATE TABLE IF NOT EXISTS scoped_perp_fills(
         account_scope TEXT NOT NULL, venue TEXT NOT NULL, symbol TEXT NOT NULL, trade_id TEXT NOT NULL,
         order_id TEXT, price TEXT NOT NULL, qty TEXT NOT NULL, quote_qty TEXT, commission_abs TEXT,
         commission_asset TEXT, maker INTEGER, realized_pnl TEXT, ts INTEGER NOT NULL, ingested REAL NOT NULL,
         PRIMARY KEY(account_scope, venue, symbol, trade_id),
         FOREIGN KEY(account_scope, venue, symbol)
           REFERENCES scoped_account_proofs(account_scope, venue, symbol))""",
    """CREATE TABLE IF NOT EXISTS scoped_funding_income(
         account_scope TEXT NOT NULL, venue TEXT NOT NULL, symbol TEXT NOT NULL, tran_id TEXT NOT NULL,
         income TEXT, ts INTEGER NOT NULL, ingested REAL NOT NULL,
         PRIMARY KEY(account_scope, venue, symbol, tran_id),
         FOREIGN KEY(account_scope, venue, symbol)
           REFERENCES scoped_account_proofs(account_scope, venue, symbol))""",
    """CREATE TABLE IF NOT EXISTS scoped_accounting_cursors(
         account_scope TEXT NOT NULL, venue TEXT NOT NULL, symbol TEXT NOT NULL,
         stream TEXT NOT NULL CHECK(stream IN ('fills','funding')), cursor TEXT, watermark_ms INTEGER,
         complete INTEGER NOT NULL CHECK(complete IN (0,1)), gap TEXT, updated REAL NOT NULL,
         PRIMARY KEY(account_scope, venue, symbol, stream),
         FOREIGN KEY(account_scope, venue, symbol)
           REFERENCES scoped_account_proofs(account_scope, venue, symbol))""",
    """CREATE TABLE IF NOT EXISTS scoped_legacy_unknown(
         stream TEXT NOT NULL, venue TEXT NOT NULL, native_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
         source_schema TEXT NOT NULL, payload_json TEXT NOT NULL, reason TEXT NOT NULL, observed REAL NOT NULL,
         PRIMARY KEY(stream, venue, native_id, payload_hash))""",
    """CREATE TABLE IF NOT EXISTS scoped_legacy_attributions(
         stream TEXT NOT NULL, venue TEXT NOT NULL, native_id TEXT NOT NULL,
         payload_hash TEXT NOT NULL, account_scope TEXT NOT NULL, symbol TEXT NOT NULL,
         PRIMARY KEY(stream,venue,native_id))""",
    """CREATE TRIGGER IF NOT EXISTS scoped_legacy_attributions_no_update
         BEFORE UPDATE ON scoped_legacy_attributions BEGIN SELECT RAISE(ABORT, 'append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS scoped_legacy_attributions_no_delete
         BEFORE DELETE ON scoped_legacy_attributions BEGIN SELECT RAISE(ABORT, 'append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS scoped_account_proofs_no_update
         BEFORE UPDATE ON scoped_account_proofs BEGIN SELECT RAISE(ABORT, 'append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS scoped_account_proofs_no_delete
         BEFORE DELETE ON scoped_account_proofs BEGIN SELECT RAISE(ABORT, 'append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS scoped_perp_fills_no_update
         BEFORE UPDATE ON scoped_perp_fills BEGIN SELECT RAISE(ABORT, 'append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS scoped_perp_fills_no_delete
         BEFORE DELETE ON scoped_perp_fills BEGIN SELECT RAISE(ABORT, 'append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS scoped_funding_income_no_update
         BEFORE UPDATE ON scoped_funding_income BEGIN SELECT RAISE(ABORT, 'append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS scoped_funding_income_no_delete
         BEFORE DELETE ON scoped_funding_income BEGIN SELECT RAISE(ABORT, 'append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS scoped_legacy_unknown_no_update
         BEFORE UPDATE ON scoped_legacy_unknown BEGIN SELECT RAISE(ABORT, 'append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS scoped_legacy_unknown_no_delete
         BEFORE DELETE ON scoped_legacy_unknown BEGIN SELECT RAISE(ABORT, 'append-only'); END""",
)


@contextmanager
def _tx(con: sqlite3.Connection):
    if con.in_transaction:
        con.execute('SAVEPOINT scoped_accounting_page')
        try:
            yield con
        except BaseException:
            con.execute('ROLLBACK TO scoped_accounting_page')
            con.execute('RELEASE scoped_accounting_page')
            raise
        con.execute('RELEASE scoped_accounting_page')
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def migrate(con: sqlite3.Connection, *, now: float | None = None) -> None:
    """Install this additive schema atomically without changing store.SCHEMA_VERSION."""
    stamp = time.time() if now is None else _finite_time(now, "now")
    with _tx(con):
        expected_tables = [m[1] for statement in _DDL
                           if (m := re.match(r'CREATE TABLE IF NOT EXISTS (\w+)\(', statement))]
        if _table_exists(con, "scoped_accounting_schema"):
            row = con.execute("SELECT version, min_reader FROM scoped_accounting_schema WHERE id=1").fetchone()
            if row is None:
                raise ScopedAccountingError('installed accounting schema metadata is missing')
            if int(row[0]) > SCHEMA_VERSION or int(row[1]) > SCHEMA_VERSION:
                raise SchemaTooNew(f"scoped accounting schema {row[0]} requires reader {row[1]}")
            # IF NOT EXISTS must not turn a lost financial table into an empty,
            # apparently valid source. Only the real v1 upgrade may add bindings.
            for name in expected_tables:
                if int(row[0]) == 1 and name == 'scoped_deal_accounts':
                    continue
                if not _table_exists(con, name):
                    raise ScopedAccountingError('installed accounting table is missing: ' + name)
        elif any(_table_exists(con, name) for name in expected_tables):
            raise ScopedAccountingError('accounting tables exist without schema metadata')
        for statement in _DDL:
            con.execute(statement)
        row = con.execute("SELECT version, min_reader FROM scoped_accounting_schema WHERE id=1").fetchone()
        if row is None:
            con.execute("INSERT INTO scoped_accounting_schema VALUES(1,?,?,?)",
                        (SCHEMA_VERSION, MIN_READER, stamp))
        elif int(row[0]) == 1 and int(row[1]) <= 1:
            con.execute('UPDATE scoped_accounting_schema SET version=?,min_reader=?,updated=? WHERE id=1',
                        (SCHEMA_VERSION, MIN_READER, stamp))
        elif int(row[0]) != SCHEMA_VERSION or int(row[1]) > SCHEMA_VERSION:
            raise SchemaTooNew(f"unsupported scoped accounting schema {tuple(row)}")


def _require_schema(con: sqlite3.Connection) -> None:
    if not _table_exists(con, "scoped_accounting_schema"):
        raise ScopedAccountingError("scoped accounting schema is not installed")
    row = con.execute("SELECT version, min_reader FROM scoped_accounting_schema WHERE id=1").fetchone()
    if row is None or int(row[0]) != SCHEMA_VERSION or int(row[1]) > SCHEMA_VERSION:
        raise SchemaTooNew("scoped accounting schema is absent or unsupported")


def _finite_time(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
        raise ScopedAccountingError(f"{name} must be a finite non-negative timestamp")
    return float(value)


def _timestamp_ms(value: Any, name: str = "ts") -> int:
    if isinstance(value, bool):
        raise ScopedAccountingError(f"{name} must be non-negative integer milliseconds")
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ScopedAccountingError(f"{name} must be non-negative integer milliseconds") from None
    if out < 0 or str(value).strip() != str(out):
        raise ScopedAccountingError(f"{name} must be non-negative integer milliseconds")
    return out


def _amount(value: Any, name: str, *, optional: bool = True, positive: bool = False,
            absolute: bool = False, nonnegative: bool = False) -> str | None:
    if value is None:
        if optional:
            return None
        raise ScopedAccountingError(f"{name} is required")
    if isinstance(value, (bool, float)):
        raise ScopedAccountingError(f"{name} must be an exact Decimal/int/string")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ScopedAccountingError(f"{name} must be a finite decimal") from None
    if not number.is_finite() or (positive and number <= 0) or (nonnegative and number < 0):
        raise ScopedAccountingError(f"{name} must be {'positive' if positive else 'finite'}")
    if absolute:
        number = number.copy_abs()
    if number == 0:
        return "0"
    text = format(number, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _native_id(value: Any, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ScopedAccountingError(f"{name} must be a non-empty public exchange identifier")
    out = str(value)
    if not out or len(out) > 160 or any(ch.isspace() or ord(ch) < 32 for ch in out):
        raise ScopedAccountingError(f"{name} must be a non-empty public exchange identifier")
    return out


def _public_text(value: Any, name: str, *, optional: bool = True) -> str | None:
    if value is None:
        if optional:
            return None
        raise ScopedAccountingError(f"{name} is required")
    if not isinstance(value, str) or not value or len(value) > 160 or any(ord(ch) < 32 for ch in value):
        raise ScopedAccountingError(f"{name} must be a non-empty public string")
    return value


def _ensure_scope(con: sqlite3.Connection, scope: ProvenScope, stamp: float) -> None:
    row = con.execute(
        "SELECT proof_kind, proof_ref, version FROM scoped_account_proofs "
        "WHERE account_scope=? AND venue=? AND symbol=?", scope.key).fetchone()
    proof = (scope.proof_kind, scope.proof_ref, scope.version)
    if row is not None:
        if tuple(row) != proof:
            raise ScopedAccountingError(f"scope {scope.key!r} was reused with different provenance")
        return
    con.execute("INSERT INTO scoped_account_proofs VALUES(?,?,?,?,?,?,?)",
                (*scope.key, *proof, stamp))


def _validate_row_scope(scope: ProvenScope, row: Mapping[str, Any]) -> None:
    for name in ('account_scope', 'venue', 'symbol'):
        if name in row and row[name] != getattr(scope, name):
            raise ScopedAccountingError(f'row {name} differs from proven scope')


def _fill_values(scope: ProvenScope, row: Mapping[str, Any], stamp: float) -> tuple[Any, ...]:
    _validate_row_scope(scope, row)
    maker = row.get('maker')
    if maker is not None and (type(maker) not in (int, bool) or maker not in (0, 1)):
        raise ScopedAccountingError('maker must be a boolean or 0/1')
    return (*scope.key, _native_id(row.get("trade_id"), "trade_id"),
            None if row.get("order_id") is None else _native_id(row.get("order_id"), "order_id"),
            _amount(row.get("price"), "price", optional=False, positive=True),
            _amount(row.get("qty"), "qty", optional=False, positive=True),
            _amount(row.get("quote_qty"), "quote_qty", nonnegative=True),
            _amount(row.get("commission_abs"), "commission_abs", absolute=True),
            _public_text(row.get("commission_asset"), "commission_asset"),
            None if maker is None else int(maker),
            _amount(row.get("realized_pnl"), "realized_pnl"), _timestamp_ms(row.get("ts")), stamp)


def _funding_values(scope: ProvenScope, row: Mapping[str, Any], stamp: float) -> tuple[Any, ...]:
    _validate_row_scope(scope, row)
    return (*scope.key, _native_id(row.get("tran_id"), "tran_id"),
            _amount(row.get("income"), "income"), _timestamp_ms(row.get("ts")), stamp)


def _insert_exact(con: sqlite3.Connection, table: str, key_columns: tuple[str, ...],
                  value_columns: tuple[str, ...], values: tuple[Any, ...]) -> bool:
    columns = key_columns + value_columns + ("ingested",)
    key = values[:len(key_columns)]
    old = con.execute(f"SELECT {','.join(value_columns)} FROM {table} WHERE " +
                      " AND ".join(f"{name}=?" for name in key_columns), key).fetchone()
    expected = values[len(key_columns):-1]
    if old is not None:
        if tuple(old) != expected:
            raise ScopedAccountingError(f"{table}: conflicting duplicate key {key!r}")
        return False
    con.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", values)
    return True


_FILL_KEYS = ("account_scope", "venue", "symbol", "trade_id")
_FILL_VALUES = ("order_id", "price", "qty", "quote_qty", "commission_abs", "commission_asset", "maker",
                "realized_pnl", "ts")
_FUNDING_KEYS = ("account_scope", "venue", "symbol", "tran_id")
_FUNDING_VALUES = ("income", "ts")


def _add_fills(con: sqlite3.Connection, scope: ProvenScope, rows: Iterable[Mapping[str, Any]], stamp: float) -> int:
    _ensure_scope(con, scope, stamp)
    return sum(_insert_exact(con, "scoped_perp_fills", _FILL_KEYS, _FILL_VALUES,
                             _fill_values(scope, row, stamp)) for row in rows)


def add_fills(con: sqlite3.Connection, scope: ProvenScope, rows: Iterable[Mapping[str, Any]],
              *, now: float | None = None) -> int:
    _require_schema(con)
    stamp = time.time() if now is None else _finite_time(now, "now")
    with _tx(con):
        return _add_fills(con, scope, rows, stamp)


def _add_funding(con: sqlite3.Connection, scope: ProvenScope, rows: Iterable[Mapping[str, Any]], stamp: float) -> int:
    _ensure_scope(con, scope, stamp)
    return sum(_insert_exact(con, "scoped_funding_income", _FUNDING_KEYS, _FUNDING_VALUES,
                             _funding_values(scope, row, stamp)) for row in rows)


def add_funding(con: sqlite3.Connection, scope: ProvenScope, rows: Iterable[Mapping[str, Any]],
                *, now: float | None = None) -> int:
    _require_schema(con)
    stamp = time.time() if now is None else _finite_time(now, "now")
    with _tx(con):
        return _add_funding(con, scope, rows, stamp)


def bind_deal(con, deal_id: str, scope: ProvenScope, *, now=None):
    """Bind an explicitly proven account; never infer it from matching native IDs.

    The caller must establish the frozen account's provenance before invoking
    this function, exactly as for ingestion. No legacy financial rows are moved.
    """
    _require_schema(con)
    stamp = time.time() if now is None else _finite_time(now, 'now')
    with _tx(con):
        deal = con.execute('SELECT perp_venue,symbol,sim FROM deals WHERE id=?', (deal_id,)).fetchone()
        if deal is None or scope.venue != ('sim:' if deal[2] else '') + deal[0] or scope.symbol != deal[1]:
            raise ScopedAccountingError('proven account differs from deal venue/symbol')
        old = con.execute('SELECT account_scope,venue,symbol FROM scoped_deal_accounts WHERE deal_id=?',
                          (deal_id,)).fetchone()
        if old is not None and tuple(old) != scope.key:
            raise ScopedAccountingError('deal account attribution is immutable')
        _ensure_scope(con, scope, stamp)
        con.execute('INSERT OR IGNORE INTO scoped_deal_accounts VALUES(?,?,?,?,?)',
                    (deal_id, *scope.key, stamp))


def _consistent_read(fn):
    """Validate attribution and select its rows from one SQLite read snapshot."""
    @wraps(fn)
    def read(con, *args, **kwargs):
        own = not con.in_transaction
        if own:
            con.execute('BEGIN')  # Deferred read transaction; does not lock out the writer in WAL.
        try:
            result = fn(con, *args, **kwargs)
        except BaseException:
            if own:
                con.rollback()
            raise
        if own:
            con.commit()
        return result
    return read


@_consistent_read
def deal_scope(con, deal_id: str) -> ProvenScope | None:
    """None means legacy unbound, never 'use the current account'."""
    if not _table_exists(con, 'scoped_accounting_schema') and not _table_exists(con, 'scoped_deal_accounts'):
        return None
    _require_schema(con)
    if not _table_exists(con, 'scoped_deal_accounts'):
        raise ScopedAccountingError('installed binding table is missing')
    row = con.execute('SELECT p.account_scope,p.venue,p.symbol,p.proof_kind,p.proof_ref,p.version '
                      'FROM scoped_deal_accounts d JOIN scoped_account_proofs p USING(account_scope,venue,symbol) '
                      'WHERE d.deal_id=?', (deal_id,)).fetchone()
    if row is None:
        if con.execute('SELECT 1 FROM scoped_deal_accounts WHERE deal_id=?', (deal_id,)).fetchone():
            raise ScopedAccountingError('bound deal has no account proof')
        return None
    scope = ProvenScope(*tuple(row))
    deal = con.execute('SELECT perp_venue,symbol,sim FROM deals WHERE id=?', (deal_id,)).fetchone()
    if deal is None or scope.venue != ('sim:' if deal[2] else '') + deal[0] or scope.symbol != deal[1]:
        raise ScopedAccountingError('bound deal identity changed')
    return scope


@_consistent_read
def deal_fills(con, deal_id: str, intent_id: str | None = None) -> list[dict] | None:
    """None selects legacy reader; [] is a scoped empty result, with no fallback."""
    scope = deal_scope(con, deal_id)
    if scope is None:
        return None
    # Native IDs are not a per-deal namespace. If two deals on one account
    # claim an order, refuse attribution rather than charge its fills twice.
    ambiguous = con.execute('''SELECT 1 FROM perp_orders a
        JOIN clips ca ON ca.id=a.clip_id JOIN intents ia ON ia.id=ca.intent_id
        JOIN perp_orders b ON b.venue=a.venue AND b.symbol=a.symbol AND b.order_id=a.order_id
        JOIN clips cb ON cb.id=b.clip_id JOIN intents ib ON ib.id=cb.intent_id
        JOIN scoped_deal_accounts db ON db.deal_id=ib.deal_id
        WHERE ia.deal_id=? AND ib.deal_id<>ia.deal_id
          AND db.account_scope=? AND db.venue=? AND db.symbol=?
          AND a.venue=db.venue AND a.symbol=db.symbol LIMIT 1''', (deal_id, *scope.key)).fetchone()
    if ambiguous:
        raise ScopedAccountingError('native order is attributed to multiple deals on this account')
    query = ('SELECT f.* FROM scoped_perp_fills f WHERE f.account_scope=? AND f.venue=? AND f.symbol=? '
             'AND EXISTS(SELECT 1 FROM perp_orders o JOIN clips c ON c.id=o.clip_id '
             'JOIN intents i ON i.id=c.intent_id WHERE i.deal_id=? AND o.venue=f.venue '
             'AND o.symbol=f.symbol AND CAST(o.order_id AS TEXT)=f.order_id')
    args = [*scope.key, deal_id]
    if intent_id is not None:
        query += ' AND i.id=?'
        args.append(intent_id)
    cursor = con.execute(query + ') ORDER BY f.ts,f.trade_id', args)
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, tuple(row))) for row in cursor]


@_consistent_read
def deal_funding(con, deal, *, until_ms=None) -> list[dict] | None:
    scope = deal_scope(con, deal['id'])
    if scope is None:
        return None
    saved = con.execute('SELECT created,state,updated FROM deals WHERE id=?', (deal['id'],)).fetchone()
    if saved is None:
        raise ScopedAccountingError('bound deal is missing')
    start = int(_finite_time(saved[0], 'deal.created') * 1000)
    if until_ms is not None:
        until_ms = _timestamp_ms(until_ms, 'until_ms')
    if saved[1] in ('CLOSED', 'ABORTED'):
        closed = int(_finite_time(saved[2], 'deal.updated') * 1000)
        until_ms = closed if until_ms is None else min(until_ms, closed)
    if until_ms is not None and until_ms < start:
        raise ScopedAccountingError('funding window ends before deal creation')
    # An account-level payment cannot be allocated by ticker/time when two
    # deal windows overlap (including an equal close/open boundary).
    others = con.execute('''SELECT d.created,d.state,d.updated FROM deals d
        JOIN scoped_deal_accounts b ON b.deal_id=d.id
        WHERE b.account_scope=? AND b.venue=? AND b.symbol=? AND d.id<>?''',
        (*scope.key, deal['id']))
    for other in others:
        other_start = int(_finite_time(other[0], 'other.created') * 1000)
        other_end = int(_finite_time(other[2], 'other.updated') * 1000) if other[1] in ('CLOSED', 'ABORTED') else None
        if other_end is not None and other_end < other_start:
            raise ScopedAccountingError('other deal funding window is invalid')
        if (until_ms is None or other_start <= until_ms) and (other_end is None or other_end >= start):
            raise ScopedAccountingError('account funding has overlapping deal windows; allocation proof required')
    query = 'SELECT ts,income,tran_id FROM scoped_funding_income WHERE account_scope=? AND venue=? AND symbol=? AND ts>=?'
    args = [*scope.key, start]
    if until_ms is not None:
        query += ' AND ts<=?'
        args.append(int(until_ms))
    cursor = con.execute(query + ' ORDER BY ts,tran_id', args)
    return [dict(zip(('ts', 'income', 'tran_id'), tuple(row))) for row in cursor]


def get_cursor(con: sqlite3.Connection, scope: ProvenScope, stream: str) -> dict[str, Any] | None:
    _require_schema(con)
    if stream not in STREAMS:
        raise ScopedAccountingError("cursor stream must be fills or funding")
    row = con.execute("SELECT * FROM scoped_accounting_cursors WHERE account_scope=? AND venue=? AND symbol=? "
                      "AND stream=?", (*scope.key, stream)).fetchone()
    if row is None:
        return None
    names = [column[0] for column in con.execute("SELECT * FROM scoped_accounting_cursors LIMIT 0").description]
    return dict(zip(names, tuple(row)))


def _set_cursor(con: sqlite3.Connection, scope: ProvenScope, stream: str, *, cursor: str | int | None,
                watermark_ms: int | None, complete: bool, gap: str | None, clear_gap: bool, stamp: float) -> None:
    if stream not in STREAMS or type(complete) is not bool or type(clear_gap) is not bool:
        raise ScopedAccountingError("invalid cursor stream or completeness flag")
    if gap is not None:
        gap = _public_text(gap, "gap", optional=False)
        if complete:
            raise ScopedAccountingError("a cursor with a gap cannot be complete")
    if clear_gap and (not complete or gap is not None):
        raise ScopedAccountingError("clearing a gap requires an explicitly complete page")
    cursor_value = None if cursor is None else _native_id(cursor, "cursor")
    watermark = None if watermark_ms is None else _timestamp_ms(watermark_ms, "watermark_ms")
    _ensure_scope(con, scope, stamp)
    old = con.execute("SELECT cursor, watermark_ms, complete, gap FROM scoped_accounting_cursors WHERE "
                      "account_scope=? AND venue=? AND symbol=? AND stream=?", (*scope.key, stream)).fetchone()
    if old is not None:
        if complete and old[1] is not None and (watermark is None or watermark < int(old[1])):
            raise ScopedAccountingError('stale watermark cannot prove complete coverage or clear a gap')
        if old[1] is not None and (watermark is None or watermark < int(old[1])):
            watermark = int(old[1])
        if cursor_value is None:
            cursor_value = old[0]
        if old[3] is not None and gap is None and not clear_gap:
            gap = old[3]
            complete = False
    con.execute("INSERT INTO scoped_accounting_cursors VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account_scope,venue,symbol,stream) DO UPDATE SET "
                "cursor=excluded.cursor, watermark_ms=excluded.watermark_ms, complete=excluded.complete, "
                "gap=excluded.gap, updated=excluded.updated",
                (*scope.key, stream, cursor_value, watermark, 1 if complete else 0, gap, stamp))


def set_cursor(con: sqlite3.Connection, scope: ProvenScope, stream: str, *, cursor: str | int | None,
               watermark_ms: int | None, complete: bool, gap: str | None = None, clear_gap: bool = False,
               now: float | None = None) -> None:
    _require_schema(con)
    stamp = time.time() if now is None else _finite_time(now, "now")
    with _tx(con):
        _set_cursor(con, scope, stream, cursor=cursor, watermark_ms=watermark_ms, complete=complete,
                    gap=gap, clear_gap=clear_gap, stamp=stamp)


def ingest_fills_page(con: sqlite3.Connection, scope: ProvenScope, rows: Iterable[Mapping[str, Any]], *,
                      cursor: str | int | None, watermark_ms: int | None, complete: bool,
                      gap: str | None = None, clear_gap: bool = False, now: float | None = None) -> int:
    _require_schema(con)
    stamp = time.time() if now is None else _finite_time(now, "now")
    with _tx(con):
        count = _add_fills(con, scope, rows, stamp)
        _set_cursor(con, scope, "fills", cursor=cursor, watermark_ms=watermark_ms, complete=complete,
                    gap=gap, clear_gap=clear_gap, stamp=stamp)
        return count


def ingest_funding_page(con: sqlite3.Connection, scope: ProvenScope, rows: Iterable[Mapping[str, Any]], *,
                        cursor: str | int | None, watermark_ms: int | None, complete: bool,
                        gap: str | None = None, clear_gap: bool = False, now: float | None = None) -> int:
    _require_schema(con)
    stamp = time.time() if now is None else _finite_time(now, "now")
    with _tx(con):
        count = _add_funding(con, scope, rows, stamp)
        _set_cursor(con, scope, "funding", cursor=cursor, watermark_ms=watermark_ms, complete=complete,
                    gap=gap, clear_gap=clear_gap, stamp=stamp)
        return count


def _legacy_schema(con: sqlite3.Connection, table: str) -> tuple[list[str], str]:
    fields = con.execute(f"PRAGMA table_info({table})").fetchall()
    columns = [str(row[1]) for row in fields]
    schema = [{"name": str(row[1]), "type": str(row[2]), "notnull": int(row[3]), "pk": int(row[5])}
              for row in fields]
    return columns, json.dumps({"table": table, "columns": schema}, sort_keys=True, separators=(",", ":"))


def _record_unknown(con: sqlite3.Connection, key: LegacyKey, row: Mapping[str, Any], source_schema: str,
                    stamp: float) -> bool:
    payload = json.dumps(dict(row), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256((source_schema + "\0" + payload).encode()).hexdigest()
    before = con.total_changes
    con.execute("INSERT OR IGNORE INTO scoped_legacy_unknown VALUES(?,?,?,?,?,?,?,?)",
                (key.stream, key.venue, key.native_id, digest, source_schema, payload,
                 "account_scope_unproven", stamp))
    return con.total_changes != before


def import_legacy(con: sqlite3.Connection, attributions: Mapping[LegacyKey, ProvenScope], *,
                  now: float | None = None) -> ImportReport:
    """Import legacy rows only when the caller proves each row's immutable scope.

    ``attributions`` must be built from historical public/frozen evidence.  A
    current runtime account is not accepted as an implicit default.  Missing
    entries are copied to ``scoped_legacy_unknown`` with every original column
    and its SQLite source schema.
    """
    _require_schema(con)
    stamp = time.time() if now is None else _finite_time(now, "now")
    imported_fills = imported_funding = unknown = 0
    sources = (("fills", "perp_fills", "trade_id"), ("funding", "funding_income", "tran_id"))
    with _tx(con):
        for stream, table, id_column in sources:
            if not _table_exists(con, table):
                continue
            columns, source_schema = _legacy_schema(con, table)
            for raw in con.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall():
                row = dict(zip(columns, tuple(raw)))
                key = LegacyKey(stream, str(row["venue"]), row[id_column])
                scope = attributions.get(key)
                if scope is None:
                    unknown += int(_record_unknown(con, key, row, source_schema, stamp))
                    continue
                if scope.venue != key.venue:
                    raise ScopedAccountingError("legacy attribution venue differs from source row")
                if stream == "funding" and row.get("symbol") != scope.symbol:
                    raise ScopedAccountingError("legacy attribution symbol differs from source row")
                payload = json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
                digest = hashlib.sha256((source_schema+'\0'+payload).encode()).hexdigest()
                attribution = (digest, scope.account_scope, scope.symbol)
                prior = con.execute('SELECT payload_hash,account_scope,symbol FROM scoped_legacy_attributions '
                                    'WHERE stream=? AND venue=? AND native_id=?',
                                    (stream, key.venue, key.native_id)).fetchone()
                if prior is not None and tuple(prior) != attribution:
                    raise ScopedAccountingError('legacy row already attributed to different scope or content')
                if stream == "fills":
                    imported_fills += _add_fills(con, scope, (row,), stamp)
                else:
                    imported_funding += _add_funding(con, scope, (row,), stamp)
                con.execute('INSERT OR IGNORE INTO scoped_legacy_attributions VALUES(?,?,?,?,?,?)',
                            (stream, key.venue, key.native_id, *attribution))
    return ImportReport(imported_fills, imported_funding, unknown)
