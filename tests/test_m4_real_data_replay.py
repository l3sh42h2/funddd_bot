"""Real-data AC-15 replay regression (docs/migration/M4_REAL_DATA_REPLAY_REPORT_20260916.md).

Fixtures here are NOT synthetic. They are a strict-column-whitelist redaction of the two
deal rows that exist in the live production ``trade.db`` as of 2026-09-16 (a disposable
sqlite ``.backup()`` copy of the production file was made, transferred locally, replayed,
then deleted from the VPS -- the production database itself was never written to and this
suite never opens it). No wallet address, private key, raw signed transaction, or owner
config is present: the columns come from ``tools.migration.replay_snapshot.COMMON_COLUMNS``,
the same allowlist ``load_snapshot()`` enforces for every snapshot, synthetic or real.

- ``m4_real_dqa9q_20260916.json``: deal ``DQA9Q`` (state OPEN, sim=0), the funding_bot's one
  live entry, at the cut of its 990th (most recent observed) ``deal_marks`` row. This is the
  only completed EVM entry (dex swap + perp fill) that exists anywhere in production.
- ``m4_real_d9w8m_aborted_20260916.json``: deal ``D9W8M`` (state ABORTED, sim=0), a real
  live-mode entry attempt that failed at the Aster margin-mode setup step before any clip,
  perp order or dex tx was created -- a genuine zero-flow real deal.

``marks.native_quote`` in the DQA9Q fixture is not an independently sourced market price: it
is *derived* from two numbers production itself already recorded for this exact deal -- the
mined tx receipts' ``gas_used * eff_gas_price`` (approve + swap, in BNB) and the cash gas cost
USD production itself wrote to the chosen ``deal_marks`` row -- solved for the one unknown.
See the report for the derivation and for why this is not a fabricated parameter.

These tests pin two independent facts:

1. baseline (95354c4, pre-M4, no accounting.py/scoped_accounting.py module at all) and target
   (this checkout) compute byte-identical accounting for both real deals -- AC-15's quantities/
   cash-flows/fees/funding/PnL equivalence, demonstrated on the only real data that exists.
2. Both revisions' output matches, to full Decimal precision, the *independently computed*
   numbers the live `core` process itself wrote into `deal_marks` / `funding_income` at that
   same cut -- so the equivalence above is not a self-consistent-but-wrong coincidence.

A change to marks.journal(), engine.deal_book() or their shared legacy fallback that alters
any of these real figures, or that lets the EVM branch of `_strict_evidence` accidentally
start claiming proven fills/funding completeness, should fail this test loudly rather than
silently.
"""
from __future__ import annotations

from pathlib import Path

from tools.migration import replay_snapshot as rs

ROOT = Path(__file__).resolve().parents[1]
# A dedicated subdirectory, not the top-level tests/replay_fixtures/*.json glob: these are
# real (not synthetic) replay_snapshot.py-schema fixtures, and
# tests/test_m4_replay_accounting.py::test_all_replay_inputs_are_explicitly_synthetic_and_secret_free
# asserts every top-level ``m4_*.json`` there is a synthetic replay_m4.py fixture with
# ``fixture["synthetic"] is True``. Keeping real data out of that glob is deliberate, not a
# workaround -- it lets that guardrail keep doing its job.
FIXTURES = ROOT / "tests" / "replay_fixtures" / "real_data"


def test_real_dqa9q_open_deal_baseline_matches_target_exactly() -> None:
    path = FIXTURES / "m4_real_dqa9q_20260916.json"

    result = rs.replay(path, target_root=ROOT, repo_root=ROOT)

    assert result["deal_id"] == "DQA9Q"
    assert result["family"] == "evm"
    assert result["baseline_revision"] == "95354c4"
    assert result["mismatches"] == []
    assert result["unsafe_projection_differences"] == []
    # Legacy EVM fills/funding completeness is never provable from a static snapshot alone
    # (no ingestion-cursor evidence exists for the plain perp_fills/funding_income tables,
    # unlike the Solana/HL scoped cursor path) -- this is the documented ceiling for real
    # EVM data, matching the synthetic m4_evm_known.json fixture's own snapshot-replay
    # classification. It must not silently become "verified_known_exact" nor a mismatch.
    assert result["classification"] == "exact_observed_slice_incomplete_sources"
    assert result["verified_equivalent"] is False
    assert result["strict_evidence"]["missing_monetary_evidence"] == []
    assert result["strict_evidence"]["source_complete"] == {"fills": None, "funding": None}

    for side in ("baseline", "target"):
        acc = result[side]["accounting"]
        assert acc["fees"] == "0.08025553"
        assert acc["fees_estimated"] is False
        assert acc["funding"] == "6.96720279"
        assert acc["spot_net"] == "-200"
        assert acc["perp_net"] == "200.63886"
        assert acc["cash_basis_quote"] == "7.51225284424971829191527475"
        assert result[side]["same_cut_pnl_quote"] == "6.510014232597412055756261"
        assert result[side]["position"]["tokens_raw"] == 4902151005294831441679
        assert result[side]["position"]["short_contracts"] == "4902"

    # Cross-check against the number the live `core` process itself wrote to deal_marks at
    # this exact cut (2026-09-16T08:02:55.816525Z), independent of this harness entirely:
    # pnl_now = 6.5100142325974120557562610, funding = 6.96720279, fees = 0.08025553.
    # The match (to full precision) is the report's primary evidence that this replay
    # methodology reproduces real production behaviour rather than a vacuous no-op.


def test_real_d9w8m_aborted_deal_has_zero_flows_on_both_revisions() -> None:
    path = FIXTURES / "m4_real_d9w8m_aborted_20260916.json"

    result = rs.replay(path, target_root=ROOT, repo_root=ROOT)

    assert result["deal_id"] == "D9W8M"
    assert result["mismatches"] == []
    assert result["unsafe_projection_differences"] == []
    assert result["classification"] == "exact_observed_slice_incomplete_sources"
    assert result["verified_equivalent"] is False

    for side in ("baseline", "target"):
        assert result[side]["deal_state"] == "ABORTED"
        acc = result[side]["accounting"]
        assert acc["cash_basis_quote"] == "0"
        assert result[side]["same_cut_pnl_quote"] == "0"
        assert result[side]["position"]["tokens_raw"] == 0
