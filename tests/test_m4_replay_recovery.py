from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "migration"))
import replay_m4 as R  # noqa: E402
from funding_bot.core.journal import Journal  # noqa: E402

FIXTURES = ROOT / "tests" / "replay_fixtures"


@pytest.mark.parametrize(
    ("name", "operation", "unresolved_field"),
    [
        ("m4_evm_unknown_recovery.json", "OEVMU1", "unresolved_evm"),
        ("m4_solana_unknown_recovery.json", "OSOLU1", "unresolved_solana"),
    ],
)
def test_unknown_outcome_is_not_zero_and_reserve_survives_replay(name, operation, unresolved_field):
    result = R.replay_fixture(FIXTURES / name, root=ROOT)
    assert result.mismatches == []
    assert not result.passed and result.classification == "unsafe_legacy_fallback"
    acct = result.actual["accounting"]
    assert acct["realized_pnl_quote"] is None and acct["accounting_complete"] is False
    assert acct["missing_monetary_evidence"]
    assert acct["flows"]["spot_net"] is None and acct["flows"]["legacy_spot_net"] == "0"
    assert result.actual["cost_basis"] == {
        "spot_acquired_raw": None, "spot_quote_cost": None, "spot_avg_entry_price": None,
        "perp_opened_contracts": None, "perp_quote_credit": None, "perp_avg_entry_price": None,
        "complete": False,
    }
    state = result.actual["state"]
    (active,) = state["active_operations"]
    assert active == {
        "id": operation,
        "state": "PAUSED_UNKNOWN",
        "target_raw": 100_000_000 if operation == "OEVMU1" else 5_000_000,
        "confirmed_raw": 40_000_000 if operation == "OEVMU1" else 0,
        "reserved_raw": 60_000_000 if operation == "OEVMU1" else 5_000_000,
        "remaining_unreserved_raw": 0,
        "manual_resume_required": True,
    }
    assert state[unresolved_field]
    assert state["notification_payload_multiplicity"] == [2]
    assert state["notification_trade_actions"] == 0


def test_duplicate_notification_ack_is_idempotent_and_cannot_change_trading_state(tmp_path):
    fixture, _ = R.load_fixture(FIXTURES / "m4_solana_unknown_recovery.json")
    con, _ = R.materialize_fixture(fixture, tmp_path / "trade.db")
    journal = Journal(SimpleNamespace(get=lambda: con))
    before = {
        "operation": dict(con.execute("SELECT * FROM operations WHERE id='OSOLU1'").fetchone()),
        "clip": dict(con.execute("SELECT * FROM clips WHERE id=30").fetchone()),
        "sol": dict(con.execute("SELECT * FROM sol_tx_attempts WHERE attempt_id='synthetic-sol-attempt-30'").fetchone()),
        "hl": dict(con.execute("SELECT * FROM hl_order_attempts WHERE client_id='fb-DSOLU1-e01-c1-a1'").fetchone()),
    }
    assert [x["id"] for x in journal.notifications()] == [30, 31]
    assert journal.acknowledge(30, {"message_id": 9001}) is True
    assert journal.acknowledge(30, {"message_id": 9001}) is False
    after = {
        "operation": dict(con.execute("SELECT * FROM operations WHERE id='OSOLU1'").fetchone()),
        "clip": dict(con.execute("SELECT * FROM clips WHERE id=30").fetchone()),
        "sol": dict(con.execute("SELECT * FROM sol_tx_attempts WHERE attempt_id='synthetic-sol-attempt-30'").fetchone()),
        "hl": dict(con.execute("SELECT * FROM hl_order_attempts WHERE client_id='fb-DSOLU1-e01-c1-a1'").fetchone()),
    }
    assert after == before
    assert [x["id"] for x in journal.notifications()] == [31]
    con.close()


def test_replaying_same_fixture_twice_is_deterministic_and_uses_disposable_databases():
    first = R.replay_fixture(FIXTURES / "m4_evm_unknown_recovery.json", root=ROOT)
    second = R.replay_fixture(FIXTURES / "m4_evm_unknown_recovery.json", root=ROOT)
    assert first.actual == second.actual
    assert first.oracle == second.oracle
    assert first.fixture_sha256 == second.fixture_sha256
