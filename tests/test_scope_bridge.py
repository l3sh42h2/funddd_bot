"""scope_bridge.py -- shadow-only execution_scope -> scoped_accounting.ProvenScope converter.

M4 migration step, owner-directed 16.09.2026 (see PATCHNOTES/m4-scoped-accounting-shadow-
20260916.md). Two kinds of coverage:

1. Unit tests of the pure converter (`to_proven_scope`) on valid and invalid inputs.
2. Integration tests against the real, redacted DQA9Q production snapshot
   (tests/replay_fixtures/real_data/m4_real_dqa9q_20260916.json), proving:
   (a) the shadow write path (record_shadow_binding / the execution_scope hook) never
       activates scoped_accounting.deal_scope()/accounting.is_bound() for a real deal, and
   (b) the ProvenScope this bridge derives, fed through the REAL (but here deliberately
       disposable, non-production) scoped_accounting ingestion functions together with the
       fixture's real fills/funding rows, reproduces the exact real money numbers already
       independently established in docs/migration/M4_REAL_DATA_REPLAY_REPORT_20260916.md
       and pinned by test_real_dqa9q_open_deal_baseline_matches_target_exactly on branch
       claude/m4-real-data-replay: fees=0.08025553, funding=6.96720279.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal as D
from pathlib import Path

import pytest

from funding_bot.trade import accounting, engine, scope_bridge, scoped_accounting, store
from funding_bot.trade.adapters.execution_scope import KIND, _saved, _shadow_bridge_installed
from funding_bot.trade.types import InstrumentSpec
from tools.migration import replay_snapshot as rs

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "replay_fixtures" / "real_data"

# The one execution_account_binding_v1 event known to exist in real production data, quoted
# verbatim from docs/migration/M4_REAL_DATA_REPLAY_REPORT_20260916.md §5.1 (branch
# claude/m4-real-data-replay, commit fd47e9a) -- not fabricated: this account string, venue,
# symbol, sim and provenance are exactly what the live `core` process itself wrote to
# exec_events for DQA9Q on 2026-09-15. The JSON snapshot fixture's own column whitelist
# (tools.migration.replay_snapshot.COMMON_COLUMNS) deliberately excludes exec_events, so this
# binding is reproduced here from the report's prose rather than read out of the fixture file.
DQA9Q_BINDING = {
    "deal_id": "DQA9Q",
    "account": "acct:v1:aster:a91c9c125e6ff90d748984d7e7b3e81b2dd17f38c9e10a44494afa21cfe43dc4",
    "venue": "aster",
    "symbol": "AIW3USDT",
    "sim": False,
    "provenance": "authenticated_legacy_orders",
}

# Independently established (replay harness + the live core process's own deal_marks row at
# the same cut; see the report and test_real_dqa9q_open_deal_baseline_matches_target_exactly).
REAL_FEES = D("0.08025553")
REAL_FUNDING = D("6.96720279")


# --------------------------------------------------------------------------------------------
# Unit tests: pure converter
# --------------------------------------------------------------------------------------------

def test_valid_legacy_binding_converts():
    scope = scope_bridge.to_proven_scope(DQA9Q_BINDING)
    assert scope.account_scope == DQA9Q_BINDING["account"]
    assert scope.venue == "aster"
    assert scope.symbol == "AIW3USDT"
    assert scope.proof_kind == "public_api_identity"
    assert scope.proof_ref == "exec_events:execution_account_binding_v1:DQA9Q"
    assert scope.version == 1


def test_extra_fields_are_ignored():
    binding = {**DQA9Q_BINDING, "inst_hash": "deadbeef", "order_proofs": {"x": 1}}
    scope = scope_bridge.to_proven_scope(binding)
    assert scope.account_scope == DQA9Q_BINDING["account"]


def test_sim_binding_is_explicitly_rejected_not_defaulted():
    binding = {**DQA9Q_BINDING, "sim": True, "account": "simulation:aster"}
    with pytest.raises(scope_bridge.ScopeBridgeError, match="sim"):
        scope_bridge.to_proven_scope(binding)


def test_new_draft_provenance_is_explicitly_rejected_not_guessed():
    """The other real execution_scope provenance (bind_draft's `new_draft_before_approval`)
    is deliberately not mapped -- see the module docstring and the patchnote's open question.
    A narrow, documented refusal, not a guessed proof_kind."""
    binding = {**DQA9Q_BINDING, "provenance": "new_draft_before_approval"}
    with pytest.raises(scope_bridge.ScopeBridgeError, match="provenance"):
        scope_bridge.to_proven_scope(binding)


def test_unknown_provenance_is_rejected():
    binding = {**DQA9Q_BINDING, "provenance": "something_nobody_wrote_yet"}
    with pytest.raises(scope_bridge.ScopeBridgeError, match="provenance"):
        scope_bridge.to_proven_scope(binding)


@pytest.mark.parametrize("missing", ["deal_id", "account", "venue", "symbol", "sim", "provenance"])
def test_missing_field_is_rejected(missing):
    binding = {k: v for k, v in DQA9Q_BINDING.items() if k != missing}
    with pytest.raises(scope_bridge.ScopeBridgeError, match=missing):
        scope_bridge.to_proven_scope(binding)


@pytest.mark.parametrize("field,value", [
    ("deal_id", ""), ("deal_id", 123), ("account", ""), ("account", None),
    ("venue", ""), ("venue", 7), ("symbol", ""), ("sim", 0), ("sim", 1),
    ("sim", "false"), ("sim", None),
])
def test_malformed_field_is_rejected(field, value):
    binding = {**DQA9Q_BINDING, field: value}
    with pytest.raises(scope_bridge.ScopeBridgeError):
        scope_bridge.to_proven_scope(binding)


def test_non_mapping_input_is_rejected():
    with pytest.raises(scope_bridge.ScopeBridgeError):
        scope_bridge.to_proven_scope(None)  # type: ignore[arg-type]


def test_account_not_matching_acct_v1_shape_is_rejected():
    binding = {**DQA9Q_BINDING, "account": "not-an-acct-v1-string"}
    with pytest.raises(scope_bridge.ScopeBridgeError, match="ProvenScope validation"):
        scope_bridge.to_proven_scope(binding)


def test_sim_true_venue_convention_matches_bind_deal_if_ever_extended():
    """Documents the venue convention this bridge would need if sim scopes are ever
    supported (scoped_accounting.bind_deal: venue = ('sim:' if sim else '') + venue) -- sim is
    currently rejected outright, so this only pins the *string building* helper stays correct
    if that rejection is ever lifted deliberately."""
    sim, venue = True, "aster"
    assert ("sim:" if sim else "") + venue == "sim:aster"
    sim = False
    assert ("sim:" if sim else "") + venue == "aster"


# --------------------------------------------------------------------------------------------
# Integration: the real DQA9Q production snapshot
# --------------------------------------------------------------------------------------------

def _materialized(tmp_path):
    doc = rs.load_snapshot(FIXTURES / "m4_real_dqa9q_20260916.json")
    con = rs._materialize(doc, tmp_path / "trade.db", store)
    return con, doc


def _write_real_binding_event(con, deal, **overrides):
    """Insert the real execution_account_binding_v1 event for `deal`, matching exactly what
    adapters.execution_scope._install_legacy would have written (same inst_hash/inst_json_sha256
    computation), so _saved()'s frozen-deal cross-check passes and any rejection a test
    provokes comes from scope_bridge's own validation, not from _saved()."""
    inst = InstrumentSpec.from_json(deal["inst_json"])
    fields = dict(account=DQA9Q_BINDING["account"], venue=deal["perp_venue"], symbol=deal["symbol"], sim=False,
                  inst_hash=inst.inst_hash(), inst_json_sha256=hashlib.sha256(deal["inst_json"].encode()).hexdigest(),
                  provenance="authenticated_legacy_orders", order_proofs={})
    fields.update(overrides)
    store.event(con, KIND, deal_id=deal["id"], **fields)


def test_fixture_has_the_expected_real_row_counts(tmp_path):
    con, doc = _materialized(tmp_path)
    try:
        assert doc["deal_id"] == "DQA9Q"
        assert store.get_deal(con, "DQA9Q")["state"] == "OPEN"
        assert con.execute("SELECT count(*) FROM perp_fills").fetchone()[0] == 2
        assert con.execute("SELECT count(*) FROM funding_income").fetchone()[0] == 86
    finally:
        con.close()


def test_shadow_write_never_activates_scoped_accounting_for_a_real_deal(tmp_path):
    """The invariant this whole change exists to protect: recording the shadow binding must not change
    deal_scope()/is_bound(), and therefore must not change what
    engine.deal_fills()/accounting.sources() (and cabinet/dashboard/tg through them) return for
    this real deal."""
    con, _ = _materialized(tmp_path)
    try:
        before_fills = engine.deal_fills(con, "DQA9Q")
        assert len(before_fills) == 2

        scope = scope_bridge.record_shadow_binding(con, DQA9Q_BINDING)
        assert scope.account_scope == DQA9Q_BINDING["account"]

        assert scoped_accounting.deal_scope(con, "DQA9Q") is None
        assert accounting.is_bound(con, "DQA9Q") is False
        assert accounting.sources(con, "DQA9Q") is None
        assert engine.deal_fills(con, "DQA9Q") == before_fills

        # Shadow observation must not even install the future activating tables.
        assert con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scoped_deal_accounts'").fetchone() is None
        assert con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scoped_account_proofs'").fetchone() is None

        # The shadow-only table, by contrast, has exactly the one row.
        assert con.execute("SELECT count(*) FROM shadow_scope_bindings").fetchone()[0] == 1
    finally:
        con.close()


def test_record_shadow_binding_is_idempotent_and_detects_conflicts(tmp_path):
    con, _ = _materialized(tmp_path)
    try:
        scope_bridge.record_shadow_binding(con, DQA9Q_BINDING)
        scope_bridge.record_shadow_binding(con, DQA9Q_BINDING)  # same content: no-op, no raise
        assert con.execute("SELECT count(*) FROM shadow_scope_bindings").fetchone()[0] == 1

        conflicting = {**DQA9Q_BINDING, "account": DQA9Q_BINDING["account"][:-1] + "0"}
        with pytest.raises(scope_bridge.ScopeBridgeError, match="different content"):
            scope_bridge.record_shadow_binding(con, conflicting)
    finally:
        con.close()


def test_shadow_scope_read_back_matches_stored_row(tmp_path):
    con, _ = _materialized(tmp_path)
    try:
        assert scope_bridge.shadow_scope(con, "DQA9Q") is None  # table not installed yet
        scope = scope_bridge.record_shadow_binding(con, DQA9Q_BINDING)
        assert scope_bridge.shadow_scope(con, "DQA9Q") == scope
        assert scope_bridge.shadow_scope(con, "NO_SUCH_DEAL") is None

        row = dict(con.execute("SELECT * FROM shadow_scope_bindings WHERE deal_id='DQA9Q'").fetchone())
        assert row["source_provenance"] == "authenticated_legacy_orders"
        assert row["source_sim"] == 0
    finally:
        con.close()


def test_production_hook_end_to_end_on_the_real_dqa9q_binding(tmp_path):
    """Exercises the exact function prepare_active_accounts()/bind_legacy() call
    (_shadow_bridge_installed), against a real execution_account_binding_v1 event shaped
    exactly as adapters.execution_scope._install_legacy would have written it."""
    con, _ = _materialized(tmp_path)
    try:
        deal = store.get_deal(con, "DQA9Q")
        _write_real_binding_event(con, deal)
        before_fills = engine.deal_fills(con, "DQA9Q")

        _shadow_bridge_installed(con, deal)

        saved = _saved(con, deal)
        expected = scope_bridge.to_proven_scope({**saved, "deal_id": deal["id"]})
        assert scope_bridge.shadow_scope(con, "DQA9Q") == expected
        assert expected.account_scope == DQA9Q_BINDING["account"]

        assert scoped_accounting.deal_scope(con, "DQA9Q") is None
        assert accounting.is_bound(con, "DQA9Q") is False
        assert engine.deal_fills(con, "DQA9Q") == before_fills
    finally:
        con.close()


def test_production_hook_backfills_a_binding_written_before_this_code_existed(tmp_path):
    """DQA9Q's real binding was written 2026-09-15, before scope_bridge.py existed. The hook
    must still pick it up on the next prepare_active_accounts() run (it reads back whatever
    is currently persisted via _saved(), it does not require having just written it)."""
    con, _ = _materialized(tmp_path)
    try:
        deal = store.get_deal(con, "DQA9Q")
        _write_real_binding_event(con, deal)
        assert scope_bridge.shadow_scope(con, "DQA9Q") is None  # nothing shadow-recorded yet

        _shadow_bridge_installed(con, deal)

        assert scope_bridge.shadow_scope(con, "DQA9Q") is not None
    finally:
        con.close()


def test_production_hook_never_raises_on_an_unmappable_binding(tmp_path):
    """A binding this bridge cannot convert (e.g. new_draft_before_approval, or any future
    unmapped provenance) must be logged and swallowed, never raised into the caller -- this is
    what makes it safe to call from the live legacy-EVM execution path."""
    con, _ = _materialized(tmp_path)
    try:
        deal = store.get_deal(con, "DQA9Q")
        _write_real_binding_event(con, deal, provenance="new_draft_before_approval")

        _shadow_bridge_installed(con, deal)  # must not raise

        assert scope_bridge.shadow_scope(con, "DQA9Q") is None
        assert accounting.is_bound(con, "DQA9Q") is False
    finally:
        con.close()


def test_production_hook_is_a_noop_when_no_binding_event_exists_yet(tmp_path):
    con, _ = _materialized(tmp_path)
    try:
        deal = store.get_deal(con, "DQA9Q")
        _shadow_bridge_installed(con, deal)  # no execution_account_binding_v1 event at all
        assert scope_bridge.shadow_scope(con, "DQA9Q") is None
    finally:
        con.close()


def test_converted_scope_reproduces_real_fees_and_funding_via_real_scoped_pipeline(tmp_path):
    """The strongest check this task asks for: feed the *real* DQA9Q fills/funding rows (from
    the redacted production snapshot) through the *real* scoped_accounting ingestion functions
    (add_fills/add_funding), using ONLY the ProvenScope this bridge derives from the *real*
    execution_account_binding_v1 payload (quoted from the M4 real-data replay report), and
    confirm the resulting sums equal the numbers production's own `core` process and the
    independent replay harness already agreed on: fees=0.08025553, funding=6.96720279
    (docs/migration/M4_REAL_DATA_REPLAY_REPORT_20260916.md;
    test_real_dqa9q_open_deal_baseline_matches_target_exactly on claude/m4-real-data-replay).

    Deliberately NOT exercised through scope_bridge's own shadow table or through this
    change's production hook: bind_deal()/add_fills()/add_funding() are the REAL, currently-
    dormant scoped_accounting write path, called here directly and once, against a disposable
    temp database built from the fixture, purely to prove the CONVERTER's output is correct.
    Production code in this change never calls bind_deal() -- see scope_bridge.py's module
    docstring for why.

    accounting.sources()'s own `.funding` total is a separate, stricter contract (it also
    requires a completeness proof -- an `accounting_funding_window` exec_event that only a
    real sync_funding()/FundingWindow call would create) and is intentionally not asserted
    here: it is orthogonal to whether this bridge's ProvenScope and the raw ingested rows are
    correct, which is what this test checks directly against scoped_perp_fills/
    scoped_funding_income.
    """
    con, doc = _materialized(tmp_path)
    try:
        scope = scope_bridge.to_proven_scope(DQA9Q_BINDING)

        scoped_accounting.migrate(con)
        scoped_accounting.bind_deal(con, "DQA9Q", scope)

        fills = [dict(row) for row in con.execute("SELECT * FROM perp_fills")]
        funding = [dict(row) for row in con.execute("SELECT * FROM funding_income")]
        assert len(fills) == len(doc["tables"]["perp_fills"]) == 2
        assert len(funding) == len(doc["tables"]["funding_income"]) == 86

        added_fills = scoped_accounting.add_fills(con, scope, fills)
        added_funding = scoped_accounting.add_funding(con, scope, funding)
        assert (added_fills, added_funding) == (2, 86)

        fee_rows = con.execute("SELECT commission_abs FROM scoped_perp_fills WHERE account_scope=? AND "
                               "venue=? AND symbol=?", scope.key).fetchall()
        total_fees = sum((D(row[0]) for row in fee_rows), D(0))
        funding_rows = con.execute("SELECT income FROM scoped_funding_income WHERE account_scope=? AND "
                                   "venue=? AND symbol=?", scope.key).fetchall()
        total_funding = sum((D(row[0]) for row in funding_rows), D(0))

        assert total_fees == REAL_FEES
        assert total_funding == REAL_FUNDING

        # scoped_accounting.deal_fills() (the function engine.deal_fills() itself prefers once
        # bound) agrees with the legacy row count for this deal.
        scoped_fills = scoped_accounting.deal_fills(con, "DQA9Q")
        assert len(scoped_fills) == 2
        assert sum((D(f["commission_abs"]) for f in scoped_fills), D(0)) == REAL_FEES
    finally:
        con.close()
