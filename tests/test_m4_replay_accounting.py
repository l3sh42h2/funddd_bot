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
    assert result.actual["cost_basis"] == {
        "spot_acquired_raw": 1_200_000_000,
        "spot_quote_cost": "100",
        "spot_avg_entry_price": "0.08333333333333333333333333333",
        "perp_opened_contracts": "1.2",
        "perp_quote_credit": "120",
        "perp_avg_entry_price": "100",
        "complete": True,
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
    assert result.actual["cost_basis"]["spot_avg_entry_price"] == "2.5"
    assert result.actual["cost_basis"]["perp_avg_entry_price"] == "2550"
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
