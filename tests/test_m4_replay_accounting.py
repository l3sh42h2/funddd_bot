from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "migration"))
import replay_m4 as R  # noqa: E402

FIXTURES = ROOT / "tests" / "replay_fixtures"


def replay(name: str) -> R.ReplayResult:
    return R.replay_fixture(FIXTURES / name, root=ROOT)


def test_evm_known_facts_match_frozen_oracle_exactly():
    result = replay("m4_evm_known.json")
    assert result.passed and result.classification == "verified_known_exact"
    assert result.mismatches == []
    assert result.oracle_base_sha == "95354c4"
    assert result.actual["identity"]["inst_hash"] == "07f16e59a0e3e37b"
    assert result.actual["identity"]["inst_json_sha256"] == \
        "3e7005292af94a6af2f34e312d23244e3cb27fff49e422d121c794ee81114368"
    assert result.actual["positions"] == {
        "spot_raw": 0, "perp_contracts": "0", "spot_base_units": "0", "perp_base_units": "0",
        "delta_base_units": "0", "known": True, "unknown_reasons": [],
    }
    assert result.actual["accounting"]["fees"] == {
        "perp_quote": "0.09", "estimated": False, "network_native": "0.000051", "network_quote": "0.102",
    }
    assert result.actual["accounting"]["funding"] == {"quote": "0.3", "events": 2, "complete": True}
    assert result.actual["accounting"]["realized_pnl_quote"] == "22.108"
    assert result.ingest_counts == {"perp_fills": [2, 1, 1], "funding_income": [2, 1, 1]}
    assert any("not account-scoped" in gap for gap in result.gaps)


def test_solana_known_facts_match_and_namespaces_do_not_collide():
    result = replay("m4_solana_known.json")
    assert result.passed and result.mismatches == []
    assert result.actual["identity"]["inst_hash"] == "cbad1e45a2347250"
    assert result.actual["identity"]["inst_json_sha256"] == \
        "24a34bf7acc8052d5050730ea7b98e637adcb8c3973c3b638dbf29040cb64f8d"
    assert result.actual["accounting"]["fees"] == {
        "perp_quote": "0.007", "estimated": False, "network_lamports": 11_000,
        "network_quote": "0.00165", "rent_locked_lamports": 0, "spot_external_quote": "0.002",
        "other_unknown": [],
    }
    assert result.actual["accounting"]["funding"] == {"quote": "0.15", "events": 2, "complete": True}
    assert result.actual["accounting"]["realized_pnl_quote"] == "0.83935"
    # Page overlap adds zero rows for the duplicate while the same tid/hash in another account remains distinct.
    assert result.ingest_counts == {"hl_fills": [1, 1, 1], "hl_funding": [2, 1, 1]}


@pytest.mark.parametrize("name", ["m4_evm_known.json", "m4_solana_known.json"])
def test_fixture_duplicate_collision_is_an_error_not_silent_overwrite(name):
    fixture, _ = R.load_fixture(FIXTURES / name)
    fixture = copy.deepcopy(fixture)
    if fixture["family"] == "evm":
        fixture["ingest_batches"]["funding_income"][1]["rows"][0]["income"] = "123"
        match = "funding_income: conflicting duplicate key"
    else:
        fixture["ingest_batches"]["hl_funding"][1]["rows"][0]["usdc"] = "123"
        match = "hl_funding: conflicting duplicate key"
    with pytest.raises(R.ReplayError, match=match):
        R.oracle_projection(fixture)


def test_field_level_mismatch_is_actionable():
    fixture, _ = R.load_fixture(FIXTURES / "m4_evm_known.json")
    expected = R.oracle_projection(fixture)
    actual = copy.deepcopy(expected)
    actual["accounting"]["realized_pnl_quote"] = "22.109"
    mismatches = R._compare(expected, actual, fixture["name"])
    assert [(x.path, x.expected, x.actual) for x in mismatches] == [
        ("accounting.realized_pnl_quote", "22.108", "22.109")
    ]


def test_frozen_evm_oracle_charges_reverted_receipt_gas(tmp_path):
    fixture, _ = R.load_fixture(FIXTURES / "m4_evm_known.json")
    fixture = copy.deepcopy(fixture)
    fixture["tables"]["dex_txs"].append({
        "id": 3, "clip_id": 1, "kind": "swap", "chain": "robinhood",
        "wallet": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "nonce": 9,
        "tx_hash": "0x" + "33" * 32, "state": "MINED_REVERTED", "status": 0,
        "gas_used": 10_000, "eff_gas_price": "1000000000",
    })
    oracle = R.oracle_projection(fixture)
    con, _ = R.materialize_fixture(fixture, tmp_path / "trade.db")
    try:
        actual = R.current_projection(con, fixture)
    finally:
        con.close()
    assert oracle["accounting"]["fees"]["network_native"] == "0.000061"
    assert actual["accounting"]["fees"]["network_native"] == "0.000061"


def test_frozen_evm_fee_coverage_uses_one_cent_tolerance(tmp_path):
    fixture, _ = R.load_fixture(FIXTURES / "m4_evm_known.json")
    fixture = copy.deepcopy(fixture)
    # Order quote 120; actual fill coverage 119.995. Frozen marks.FEE_TOL=.01 treats the .005 gap as rounding.
    for batch in fixture["ingest_batches"]["perp_fills"]:
        for row in batch["rows"]:
            if batch["venue"] == "gate" and row.get("order_id") == 11 and row.get("trade_id") == 101:
                row["quote_qty"] = "69.995"
    accounting = R.oracle_projection(fixture)["accounting"]
    con, _ = R.materialize_fixture(fixture, tmp_path / "trade.db")
    try:
        actual = R.current_projection(con, fixture)["accounting"]
    finally:
        con.close()
    assert accounting["fees"]["estimated"] is False
    assert accounting["fees"]["perp_quote"] == "0.09"
    assert actual["fees"]["estimated"] is False
    assert actual["fees"]["perp_quote"] == "0.09"


def test_open_evm_funding_window_ends_at_as_of_not_deal_updated(tmp_path):
    fixture, _ = R.load_fixture(FIXTURES / "m4_evm_unknown_recovery.json")
    fixture = copy.deepcopy(fixture)
    fixture["tables"]["deals"][0]["updated"] = 1789361000.0
    fixture["ingest_batches"] = {"funding_income": [{"venue": "gate", "rows": [{
        "tran_id": 900, "symbol": "1000SYN_USDT", "income": "0.25", "ts": 1789362000000,
    }]}]}
    accounting = R.oracle_projection(fixture)["accounting"]
    con, _ = R.materialize_fixture(fixture, tmp_path / "trade.db")
    try:
        actual = R.current_projection(con, fixture)["accounting"]
    finally:
        con.close()
    assert accounting["funding"] == {"quote": "0.25", "events": 1, "complete": True}
    assert actual["funding"] == accounting["funding"]


def test_cli_does_not_green_unsafe_legacy_fallback():
    unsafe = FIXTURES / "m4_evm_unknown_recovery.json"
    assert R.main([str(unsafe)]) == 1
    assert R.main(["--allow-unsafe", str(unsafe)]) == 0


def test_all_replay_inputs_are_explicitly_synthetic_and_secret_free():
    for path in FIXTURES.glob("m4_*.json"):
        fixture, _ = R.load_fixture(path)
        raw = path.read_text().lower()
        assert fixture["synthetic"] is True
        assert "private_key" not in raw and "api_secret" not in raw and "signed_payload" not in raw
