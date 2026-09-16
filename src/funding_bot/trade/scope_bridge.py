"""Shadow-only bridge: execution_scope account bindings -> scoped_accounting.ProvenScope.

This module is the missing conversion step named in
``docs/migration/M4_REAL_DATA_REPLAY_REPORT_20260916.md`` §5.1/§9: execution
identity (``adapters/execution_scope.py``, event kind
``execution_account_binding_v1``) already proves, for a handful of live legacy
EVM deals, which exchange account executed them. ``scoped_accounting.py``
separately defines the target shape (``ProvenScope``) that the M4/M5 account-
scoped accounting engine needs. Nothing in the codebase converted one into the
other, so ``scoped_accounting`` has never been fed real data.

Hard boundary (owner-directed, 16.09.2026): this module is NOT authoritative
and must never become a source of truth for anything a human or the execution
path reads.

- It never calls ``scoped_accounting.bind_deal()`` and never writes to
  ``scoped_deal_accounts`` or ``scoped_account_proofs``. Those are exactly the
  tables ``scoped_accounting.deal_scope()`` (and, through it,
  ``accounting.is_bound()``/``accounting.sources()``,
  ``engine.deal_fills()`` and ``marks.journal()`` -- which is what
  cabinet.py/dashboard.py/tg/* ultimately read) consult to decide whether a
  deal has switched off the legacy accounting path. Writing there from an
  unreviewed, automatic converter would silently flip real cabinet/dashboard/
  Telegram numbers for a live deal -- exactly what this module must not do.
- Everything this module writes goes to its own table, ``shadow_scope_bindings``
  (see ``_SHADOW_DDL`` below), created by this module and read only by this
  module's own helpers and its tests. Grep the tree: no other module names
  that table.
- ``ensure_scoped_accounting_schema()`` below does call the *real*
  ``scoped_accounting.migrate()``. That is deliberate and safe: it only
  installs the (already-written, already-tested, currently never-invoked)
  scoped_accounting DDL with ``CREATE TABLE/INDEX/TRIGGER IF NOT EXISTS``, and
  ``scoped_accounting.deal_scope()`` returns ``None`` for every deal as long
  as ``scoped_deal_accounts`` has no row for it -- which stays true forever
  unless something calls ``bind_deal()``, which this module never does. See
  that function's docstring for the full argument.

Callers (currently: ``adapters/execution_scope.py``'s legacy-binding paths)
must treat every function here as best-effort: wrap calls in
``try/except Exception`` and log, never let a failure here affect a real
trade. This module does not enforce that itself (a pure library function
should not swallow its own errors), so the call site is responsible -- see
``execution_scope._shadow_bridge_installed``.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Mapping

from . import scoped_accounting
from .scoped_accounting import ProvenScope

REQUIRED_FIELDS = ("deal_id", "account", "venue", "symbol", "sim", "provenance")

# The only execution_scope provenance this bridge currently knows how to map to a
# scoped_accounting proof_kind. `authenticated_legacy_orders` (adapters/execution_scope.py
# `_install_legacy`) means: the account string came from the venue adapter's own
# `history_account()` (an exchange account-identity API/derivation, not a secret), AND that
# identity was independently corroborated by matching this deal's own historical orders
# against it (`legacy_evidence.prove_order`). That is exactly what scoped_accounting's
# `public_api_identity` proof_kind describes ("public account identity", `scoped_accounting.py`
# module docstring) -- not `frozen_leg` (that is InstrumentSpec.perp_account, a config-time
# freeze, a different field entirely), not `public_config` (no static config is involved), and
# not `migration_manifest` (that names a deliberate, reviewed, human-curated mapping document;
# labeling an automatic conversion that way would misrepresent its provenance).
#
# `new_draft_before_approval` (adapters/execution_scope.py `bind_draft`) is deliberately NOT
# mapped: it is the same native-identity call but WITHOUT the historical-order corroboration
# (there are no orders yet -- the deal is a brand-new draft), and it is not "legacy" in the
# migration sense this bridge exists for. Whether it deserves the same proof_kind is a judgment
# call left open here on purpose -- see the patchnote's open question. Converting it raises
# ScopeBridgeError below rather than guessing.
_SUPPORTED_PROVENANCE = "authenticated_legacy_orders"
_PROOF_KIND = "public_api_identity"


class ScopeBridgeError(RuntimeError):
    """A binding could not be converted. Raised instead of guessing a ProvenScope field."""


def to_proven_scope(binding: Mapping[str, Any]) -> ProvenScope:
    """Pure conversion: an execution_account_binding_v1 payload -> a ProvenScope.

    `binding` must contain the fields `adapters/execution_scope.py` writes into that event's
    JSON (`account`, `venue`, `symbol`, `sim`, `provenance`), plus `deal_id` (a separate
    `exec_events` column, not part of the JSON blob -- callers must merge it in; see
    `execution_scope._shadow_bridge_installed`). Extra keys (`inst_hash`,
    `inst_json_sha256`, `order_proofs`, ...) are ignored here.

    Raises ScopeBridgeError -- never fabricates a default -- when:
    - a required field is missing or the wrong type;
    - `sim` is True: a sim account is a synthetic `'simulation:' + venue` string, not a
      scoped_accounting `acct:v1:...` account_scope, so there is nothing valid to build;
    - `provenance` is not the one kind this bridge currently maps (see module docstring);
    - the derived fields fail ProvenScope's own canonical-shape validation.
    """
    if not isinstance(binding, Mapping):
        raise ScopeBridgeError("execution_scope binding must be a mapping")
    missing = [field for field in REQUIRED_FIELDS if field not in binding]
    if missing:
        raise ScopeBridgeError(f"execution_scope binding is missing field(s): {', '.join(missing)}")
    deal_id, account, venue, symbol, sim, provenance = (binding[field] for field in REQUIRED_FIELDS)
    if not isinstance(deal_id, str) or not deal_id:
        raise ScopeBridgeError("execution_scope binding.deal_id must be a non-empty string")
    if type(sim) is not bool:
        raise ScopeBridgeError("execution_scope binding.sim must be a boolean")
    if sim:
        raise ScopeBridgeError(
            "shadow conversion is only defined for live (non-sim) accounts; a sim binding's "
            "account is a synthetic 'simulation:<venue>' identity, not a scoped_accounting "
            "acct:v1 account_scope -- there is no valid ProvenScope to build here"
        )
    if provenance != _SUPPORTED_PROVENANCE:
        raise ScopeBridgeError(
            f"execution_scope provenance {provenance!r} is not mapped to a scoped_accounting "
            f"proof_kind (only {_SUPPORTED_PROVENANCE!r} is supported today); choosing a "
            "proof_kind for another provenance is an architecture decision for the owner, not "
            "a guess this bridge should make -- see M4_REAL_DATA_REPLAY_REPORT_20260916.md §9 "
            "and this change's patchnote"
        )
    if not isinstance(account, str) or not account:
        raise ScopeBridgeError("execution_scope binding.account must be a non-empty string")
    if not isinstance(venue, str) or not venue:
        raise ScopeBridgeError("execution_scope binding.venue must be a non-empty string")
    if not isinstance(symbol, str) or not symbol:
        raise ScopeBridgeError("execution_scope binding.symbol must be a non-empty string")
    # scoped_accounting.bind_deal()'s own convention is venue = ('sim:' if sim else '') + venue
    # (see scoped_accounting.py:398). sim is rejected above, so this is always just `venue`;
    # spelled out so a future extension to sim scopes keeps the same convention rather than
    # reinventing it.
    scoped_venue = ("sim:" if sim else "") + venue
    # Points at the one (DB-enforced unique, see store.py's execution_account_binding_once
    # index) execution_account_binding_v1 row for this deal, without duplicating its content --
    # exactly the audit-metadata role scoped_accounting.ProvenScope.proof_ref documents.
    proof_ref = f"exec_events:execution_account_binding_v1:{deal_id}"
    try:
        return ProvenScope(account_scope=account, venue=scoped_venue, symbol=symbol,
                            proof_kind=_PROOF_KIND, proof_ref=proof_ref, version=1)
    except scoped_accounting.ScopedAccountingError as exc:
        raise ScopeBridgeError(f"converted scope failed ProvenScope validation: {exc}") from exc


def ensure_scoped_accounting_schema(con, *, now: float | None = None) -> None:
    """Install the real (still-dormant) M4 scoped_accounting schema, if it is missing.

    Safe and additive: delegates to ``scoped_accounting.migrate()``, which only issues
    ``CREATE TABLE/INDEX/TRIGGER IF NOT EXISTS`` and is documented not to touch
    ``store.SCHEMA_VERSION``. This does not bind any deal and does not change
    ``scoped_accounting.deal_scope()``/``accounting.is_bound()`` for any deal: both still
    require a row in ``scoped_deal_accounts``, and nothing in this module (or anywhere else
    right now) writes one. Per the M4 real-data replay report, these tables do not exist on
    production today -- ``scoped_accounting.migrate()`` has never been called outside tests --
    so this closes that specific gap without activating anything.
    """
    scoped_accounting.migrate(con, now=now)


_SHADOW_DDL = (
    """CREATE TABLE IF NOT EXISTS shadow_scope_bindings(
         deal_id TEXT PRIMARY KEY, account_scope TEXT NOT NULL, venue TEXT NOT NULL, symbol TEXT NOT NULL,
         proof_kind TEXT NOT NULL, proof_ref TEXT NOT NULL, version INTEGER NOT NULL,
         source_provenance TEXT NOT NULL, source_sim INTEGER NOT NULL,
         inst_hash TEXT, inst_json_sha256 TEXT, created REAL NOT NULL)""",
    """CREATE TRIGGER IF NOT EXISTS shadow_scope_bindings_no_update
         BEFORE UPDATE ON shadow_scope_bindings BEGIN SELECT RAISE(ABORT, 'append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS shadow_scope_bindings_no_delete
         BEFORE DELETE ON shadow_scope_bindings BEGIN SELECT RAISE(ABORT, 'append-only'); END""",
)


def _ensure_shadow_schema(con) -> None:
    for statement in _SHADOW_DDL:
        con.execute(statement)


@contextmanager
def _tx(con):
    """Same nested-savepoint shape as scoped_accounting._tx: join the caller's transaction via
    a SAVEPOINT when there is one, else open a short-lived transaction of our own."""
    if con.in_transaction:
        con.execute("SAVEPOINT scope_bridge_shadow")
        try:
            yield con
        except BaseException:
            con.execute("ROLLBACK TO scope_bridge_shadow")
            con.execute("RELEASE scope_bridge_shadow")
            raise
        con.execute("RELEASE scope_bridge_shadow")
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def record_shadow_binding(con, binding: Mapping[str, Any], *, now: float | None = None) -> ProvenScope:
    """Shadow-only: convert `binding` and store it in this module's OWN append-only table.

    Never touches scoped_accounting's scoped_deal_accounts/scoped_account_proofs -- see the
    module docstring for why. Nothing outside this module and its tests reads
    shadow_scope_bindings, so this can never change what engine.deal_fills(),
    accounting.sources(), marks.journal() (and therefore cabinet/dashboard/tg) return for any
    deal.

    Idempotent: recording the same deal_id again with identical derived content is a no-op.
    A conflicting re-record (same deal_id, different account/venue/symbol/proof) raises
    ScopeBridgeError rather than silently overwriting -- the same append-only discipline
    scoped_accounting itself uses for scoped_deal_accounts/scoped_account_proofs.
    """
    scope = to_proven_scope(binding)
    deal_id = binding["deal_id"]
    stamp = time.time() if now is None else now
    with _tx(con):
        _ensure_shadow_schema(con)
        _record(con, deal_id, scope, binding, stamp)
    return scope


def _record(con, deal_id: str, scope: ProvenScope, binding: Mapping[str, Any], stamp: float) -> None:
    payload = (scope.account_scope, scope.venue, scope.symbol, scope.proof_kind, scope.proof_ref, scope.version)
    row = con.execute("SELECT account_scope,venue,symbol,proof_kind,proof_ref,version "
                      "FROM shadow_scope_bindings WHERE deal_id=?", (deal_id,)).fetchone()
    if row is not None:
        if tuple(row) != payload:
            raise ScopeBridgeError(f"shadow scope binding for deal {deal_id!r} already recorded with "
                                   "different content")
        return
    con.execute("INSERT INTO shadow_scope_bindings(deal_id,account_scope,venue,symbol,proof_kind,proof_ref,"
                "version,source_provenance,source_sim,inst_hash,inst_json_sha256,created) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (deal_id, *payload, binding.get("provenance"), int(bool(binding.get("sim"))),
                 binding.get("inst_hash"), binding.get("inst_json_sha256"), stamp))


def shadow_scope(con, deal_id: str) -> ProvenScope | None:
    """Read back this module's own shadow record for `deal_id`, or None.

    Audit/test helper only. Deliberately not used by any production read path -- production
    code that wants a deal's proven scope must keep using
    scoped_accounting.deal_scope()/accounting.is_bound(), which this table has no effect on.
    """
    exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                         "name='shadow_scope_bindings'").fetchone()
    if exists is None:
        return None
    row = con.execute("SELECT account_scope,venue,symbol,proof_kind,proof_ref,version "
                      "FROM shadow_scope_bindings WHERE deal_id=?", (deal_id,)).fetchone()
    return None if row is None else ProvenScope(*tuple(row))
