from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tools.migration import replay_snapshot as rs


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "replay_fixtures"


def _snapshot(name: str) -> dict:
    source = json.loads((FIXTURES / name).read_text())
    cut = source["as_of_ms"]

    def within_cut(table: str, row: dict) -> bool:
        column = rs.MILLISECOND_TIME_COLUMNS.get(table)
        if column and row.get(column) is not None:
            return row[column] <= cut
        if table == "fee_events" and row.get("ts") is not None:
            return float(row["ts"]) * 1000 <= cut
        return True

    columns = dict(rs.COMMON_COLUMNS)
    if source["family"] == "solana":
        columns.update(rs.SOL_COLUMNS)
    tables = {}
    for table, allowed in columns.items():
        tables[table] = [{k: v for k, v in row.items() if k in allowed}
                         for row in source.get("tables", {}).get(table, []) if within_cut(table, row)]
    if source["family"] == "evm":
        seen = set()
        for batch in source["ingest_batches"]["perp_fills"]:
            for raw in batch["rows"]:
                row = {**raw, "venue": batch["venue"]}
                if not within_cut("perp_fills", row):
                    continue
                key = row["venue"], row["trade_id"]
                if key not in seen:
                    tables["perp_fills"].append({k: v for k, v in row.items() if k in columns["perp_fills"]})
                    seen.add(key)
        seen = set()
        for batch in source["ingest_batches"]["funding_income"]:
            for raw in batch["rows"]:
                row = {**raw, "venue": batch["venue"]}
                if not within_cut("funding_income", row):
                    continue
                key = row["venue"], row["tran_id"]
                if key not in seen:
                    tables["funding_income"].append({k: v for k, v in row.items() if k in columns["funding_income"]})
                    seen.add(key)
    else:
        seen = set()
        for batch in source["ingest_batches"]["hl_fills"]:
            for raw in batch["rows"]:
                row = {**raw, "account": raw["account"].lower()}
                if not within_cut("hl_fills", row):
                    continue
                key = row["network"], row["account"], row["coin"], row["time"], row["tid"]
                if key not in seen:
                    tables["hl_fills"].append({k: v for k, v in row.items() if k in columns["hl_fills"]})
                    seen.add(key)
        seen = set()
        for batch in source["ingest_batches"]["hl_funding"]:
            for raw in batch["rows"]:
                row = {**raw, "account": raw["account"].lower(), "hash": raw.get("hash") or ""}
                if not within_cut("hl_funding", row):
                    continue
                key = row["network"], row["account"], row["coin"], row["time"], row["hash"]
                if key not in seen:
                    tables["hl_funding"].append({k: v for k, v in row.items() if k in columns["hl_funding"]})
                    seen.add(key)
    return {"snapshot_version": 1, "family": source["family"], "as_of_ms": source["as_of_ms"],
            "deal_id": tables["deals"][0]["id"], "fee_rate": source["fee_rate"],
            "observation_ts": source["as_of_ms"] / 1000,
            "marks": {"spot_quote": "3", "perp_quote": "2600",
                      "native_quote": source["native_price_quote"]},
            "tables": tables, "approve_tx_hashes": []}


def _write(tmp_path: Path, doc: dict) -> Path:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(doc, sort_keys=True))
    return path


def test_real_snapshot_contract_rejects_secret_or_unwhitelisted_columns(tmp_path: Path) -> None:
    doc = _snapshot("m4_evm_known.json")
    doc["tables"]["dex_txs"][0]["raw_tx"] = "signed-secret"
    with pytest.raises(rs.SnapshotError, match="forbidden columns.*raw_tx"):
        rs.load_snapshot(_write(tmp_path, doc))


def test_solana_known_slice_is_exact_across_frozen_baseline_and_target(tmp_path: Path) -> None:
    path = _write(tmp_path, _snapshot("m4_solana_known.json"))

    result = rs.replay(path, target_root=ROOT, repo_root=ROOT)

    assert result["classification"] == "verified_known_exact"
    assert result["verified_equivalent"] is True
    assert result["mismatches"] == []
    assert result["strict_evidence"] == {"missing_monetary_evidence": [],
                                          "source_complete": {"fills": True, "funding": True},
                                          "accounting_complete": True}
    assert result["target"]["accounting"]["funding"] == "0.15"
    assert result["target"]["accounting"]["fees"] == "0.007"
    assert result["target"]["accounting"]["cash_basis_quote"] == "0.83935"


def test_solana_missing_fill_coverage_is_never_verified(tmp_path: Path) -> None:
    doc = _snapshot("m4_solana_known.json")
    doc["tables"]["hl_fills"] = []

    result = rs.replay(_write(tmp_path, doc), target_root=ROOT, repo_root=ROOT)

    assert result["verified_equivalent"] is False
    assert result["classification"] == "unsafe_shared_fallback"
    assert result["target"]["accounting"]["fees_estimated"] is True
    assert result["target"]["accounting"]["accounting_complete"] is False
    assert set(result["projection_gaps"]["target"]) >= {"fees_estimated", "accounting_complete"}
    assert "order:fb-DSOLK1-e01-c1-a1:fee_coverage" in result["strict_evidence"]["missing_monetary_evidence"]
    assert "order:fb-DSOLK1-x01-c1-a1:fee_coverage" in result["strict_evidence"]["missing_monetary_evidence"]


def test_solana_missing_receipt_fee_evidence_is_never_verified(tmp_path: Path) -> None:
    doc = _snapshot("m4_solana_known.json")
    doc["tables"]["fee_events"] = []

    result = rs.replay(_write(tmp_path, doc), target_root=ROOT, repo_root=ROOT)

    assert result["verified_equivalent"] is False
    assert result["classification"] == "unsafe_shared_fallback"
    assert result["target"]["accounting"]["gas_native_raw"] == 0
    assert result["target"]["accounting"]["cash_basis_quote"] == "0.843"
    assert "clip:11:network_fee_evidence" in result["strict_evidence"]["missing_monetary_evidence"]
    assert "clip:12:network_fee_evidence" in result["strict_evidence"]["missing_monetary_evidence"]


def test_solana_explicit_zero_receipt_fee_is_known_evidence(tmp_path: Path) -> None:
    doc = _snapshot("m4_solana_known.json")
    for event in doc["tables"]["fee_events"]:
        if event["kind"] == "network_total":
            event["amount_raw"] = "0"

    result = rs.replay(_write(tmp_path, doc), target_root=ROOT, repo_root=ROOT)

    assert result["verified_equivalent"] is True
    assert result["strict_evidence"]["missing_monetary_evidence"] == []
    assert result["target"]["accounting"]["gas_native_raw"] == 0


@pytest.mark.parametrize("substitute_kind", ["tip", "rent_nonrefundable"])
def test_solana_nonreceipt_native_cost_cannot_replace_receipt_fee(
        tmp_path: Path, substitute_kind: str) -> None:
    doc = _snapshot("m4_solana_known.json")
    for event in doc["tables"]["fee_events"]:
        if event["kind"] == "network_total":
            event["kind"] = substitute_kind

    result = rs.replay(_write(tmp_path, doc), target_root=ROOT, repo_root=ROOT)

    assert result["verified_equivalent"] is False
    assert result["classification"] == "unsafe_shared_fallback"
    assert "clip:11:network_fee_evidence" in result["strict_evidence"]["missing_monetary_evidence"]
    assert "clip:12:network_fee_evidence" in result["strict_evidence"]["missing_monetary_evidence"]


def test_snapshot_rejects_rows_later_than_accounting_cut(tmp_path: Path) -> None:
    doc = _snapshot("m4_solana_known.json")
    doc["tables"]["deals"][0]["state"] = "OPEN"
    doc["tables"]["hl_funding"].append({
        "network": "mainnet", "account": "0xabc123", "coin": "para:SYN",
        "time": doc["as_of_ms"] + 1, "hash": "after-cut", "usdc": "8",
        "szi": "0", "rate": "0", "ingested": doc["as_of_ms"] / 1000,
    })

    with pytest.raises(rs.SnapshotError, match=r"hl_funding.*time is outside 0\.\.as_of_ms"):
        rs.replay(_write(tmp_path, doc), target_root=ROOT, repo_root=ROOT)


def test_same_cut_excludes_later_funding_and_reports_evm_coverage_unknown(tmp_path: Path) -> None:
    path = _write(tmp_path, _snapshot("m4_evm_known.json"))

    result = rs.replay(path, target_root=ROOT, repo_root=ROOT)

    assert result["mismatches"] == []
    assert result["target"]["accounting"]["funding"] == "0.3"
    assert result["classification"] == "exact_observed_slice_incomplete_sources"
    assert result["verified_equivalent"] is False
    assert result["strict_evidence"]["source_complete"] == {"fills": None, "funding": None}


def test_missing_execution_quote_is_unknown_not_accepted_as_equivalence(tmp_path: Path) -> None:
    doc = _snapshot("m4_evm_known.json")
    doc["tables"]["perp_orders"][0]["cum_quote"] = None
    path = _write(tmp_path, doc)

    result = rs.replay(path, target_root=ROOT, repo_root=ROOT)

    assert "order:fb-DEVMK1-e01-c1-a1:cum_quote" in result["strict_evidence"]["missing_monetary_evidence"]
    assert result["verified_equivalent"] is False
    assert result["classification"] == "unsafe_legacy_projection_changed"
    paths = {row["path"] for row in result["safer_unknown_transitions"]}
    assert "accounting.perp_net" in paths
    assert "accounting.cash_basis_quote" in paths
    assert "same_cut_pnl_quote" in paths


def test_missing_fee_amount_is_flagged_even_if_both_revisions_share_numeric_fallback(tmp_path: Path) -> None:
    doc = _snapshot("m4_evm_known.json")
    doc["tables"]["perp_fills"][0]["commission_abs"] = None
    path = _write(tmp_path, doc)

    result = rs.replay(path, target_root=ROOT, repo_root=ROOT)

    assert result["mismatches"] == []
    assert result["classification"] == "unsafe_shared_fallback"
    assert "fill:gate:101:commission" in result["strict_evidence"]["missing_monetary_evidence"]


def test_missing_native_mark_is_exact_unknown_in_both_revisions(tmp_path: Path) -> None:
    doc = _snapshot("m4_evm_known.json")
    doc["marks"]["native_quote"] = None
    path = _write(tmp_path, doc)

    result = rs.replay(path, target_root=ROOT, repo_root=ROOT)

    assert result["classification"] == "exact_strict_unknown"
    assert result["target"]["accounting"]["gas_quote"] is None
    assert result["target"]["accounting"]["cash_basis_quote"] is None
    assert result["target"]["same_cut_pnl_quote"] is None


def test_known_value_difference_remains_actionable_mismatch() -> None:
    old = {"accounting": {"fees": "1", "cash_basis_quote": "2"}}
    new = copy.deepcopy(old)
    new["accounting"]["fees"] = "1.1"

    assert rs._flatten_diff(old, new) == [
        {"path": "accounting.fees", "baseline": "1", "target": "1.1"}
    ]
