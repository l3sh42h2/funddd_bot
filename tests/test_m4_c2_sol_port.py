"""Targeted M4 C2 checks at the common Solana spot boundary."""
from dataclasses import replace

import pytest

from funding_bot.trade import store
from funding_bot.trade.engine import Refused
from funding_bot.trade.store import ClipState, DealState

import sol_c2_world as W


pytest.importorskip("solders")
pytest.importorskip("eth_account")


def test_common_submit_consumes_selected_route_once(tmp_path, monkeypatch):
    w = W.make_world(tmp_path)
    prop = w.desk.propose_profile_entry(W.entry_cmd(), chat=None)
    calls = []
    original = w.router.select

    def spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(w.router, "select", spy)
    W.approve_run(w, prop)

    # The execution route is selected once. submit_sol receives that decision
    # and the approved-selection binding never calls router.select again.
    assert len(calls) == 1
    assert len(w.sol.sends) == 1


@pytest.mark.parametrize("field", ["request_hash", "input_mint", "output_decimals"])
def test_tampered_approved_selection_refused_before_native_send(tmp_path, monkeypatch, field):
    w = W.make_world(tmp_path)
    prop = w.desk.propose_profile_entry(W.entry_cmd(), chat=None)
    from funding_bot.trade.adapters import spot_execution

    original = spot_execution.submit_sol

    def tampered(con, **kwargs):
        decision = kwargs["decision"]
        winner = decision.winner
        if field == "request_hash":
            decision = replace(decision, request_hash="tampered-request")
        else:
            winner = replace(winner, **{field: ("tampered-mint" if field == "input_mint" else 0)})
            decision = replace(decision, winner=winner)
        return original(con, **{**kwargs, "decision": decision})

    monkeypatch.setattr(spot_execution, "submit_sol", tampered)
    W.approve_run(w, prop)

    assert w.sol.sends == []
    deal = store.get_deal(w.con, prop.deal_id)
    assert deal["state"] == DealState.ABORTED
    clips = store.clips_of(w.con, prop.intent_id)
    assert clips and clips[0]["state"] == ClipState.DEX_REVERTED
    assert store.operation_of_intent(w.con, prop.intent_id)['reserved_raw'] == '0'
    assert not w.con.execute("SELECT 1 FROM exec_events WHERE kind='adapter_spot_claimed'").fetchone()
    assert w.con.execute('SELECT count(*) FROM sol_tx_attempts').fetchone()[0] == 0


def test_common_unknown_recovery_keeps_reserve_and_does_not_reselect(tmp_path):
    w = W.make_world(tmp_path)
    w.sol.blackhole = True
    w.sol.down = {"b"}
    prop = w.desk.propose_profile_entry(W.entry_cmd(), chat=None)
    W.approve_run(w, prop)

    op = store.operation_of_intent(w.con, prop.intent_id)
    clip = store.clips_of(w.con, prop.intent_id)[0]
    assert clip["state"] == ClipState.DEX_UNKNOWN
    assert op["reserved_raw"] == "150000000"
    sent = len(w.sol.sends)
    assert sent >= 1 and len({x for x in w.sol.sends}) == 1
    attempts_before = w.con.execute("SELECT count(*) FROM sol_tx_attempts").fetchone()[0]

    # Recovery may read the same native attempt, but cannot release reserve
    # while finality and the receipt remain unproven.
    assert w.engine.recover_sol(store.get_deal(w.con, prop.deal_id))
    op = store.operation_of_intent(w.con, prop.intent_id)
    assert op["reserved_raw"] == "150000000"
    assert len(w.sol.sends) >= sent and len({x for x in w.sol.sends}) == 1
    assert w.con.execute("SELECT count(*) FROM sol_tx_attempts").fetchone()[0] == attempts_before == 1


def test_presign_refusal_proves_common_terminal_without_signature(tmp_path, monkeypatch):
    w = W.make_world(tmp_path)
    prop = w.desk.propose_profile_entry(W.entry_cmd(), chat=None)
    import funding_bot.trade.sol_exec as sol_exec

    def refuse(*args, **kwargs):
        raise sol_exec.SignError("synthetic presign refusal")

    monkeypatch.setattr(sol_exec, "sign_checked", refuse)
    W.approve_run(w, prop)

    assert w.sol.sends == []
    assert w.con.execute("SELECT count(*) FROM sol_tx_attempts").fetchone()[0] == 1
    assert w.con.execute("SELECT state FROM sol_tx_attempts").fetchone()[0] == "ABANDONED_UNSIGNED"
    events = [__import__("json").loads(r[0]) for r in w.con.execute(
        "SELECT json FROM exec_events WHERE kind='adapter_spot_terminal'")]
    assert len(events) == 1 and events[0]["status"] == "REJECTED"
    assert events[0]["native_ref"]


def test_malformed_finalized_result_cannot_settle_common_reserve(tmp_path):
    w = W.make_world(tmp_path)
    w.sol.blackhole = True
    w.sol.down = {"b"}
    prop = w.desk.propose_profile_entry(W.entry_cmd(), chat=None)
    W.approve_run(w, prop)
    op = store.operation_of_intent(w.con, prop.intent_id)
    from funding_bot.trade.sol_exec import SwapOutcome
    from funding_bot.trade.sol_flow import apply_swap

    with pytest.raises(ValueError, match="proven terminal"):
        apply_swap(w.con, prop.deal_id, int(store.clips_of(w.con, prop.intent_id)[0]["id"]),
                   op["id"], int(op["reserved_raw"]),
                   SwapOutcome("ok", "malformed", "sig", None, None, "finalized"))
    assert store.operation_of_intent(w.con, prop.intent_id)["reserved_raw"] == "150000000"


def test_compose_clip_pair_calls_runtime_compose_once_with_both_real_bindings():
    from types import SimpleNamespace
    from funding_bot.trade.adapters.execution import compose_clip_pair

    spot = SimpleNamespace(leg_id="spot", describe=lambda: "spot")
    perp = SimpleNamespace(leg_id="perp", describe=lambda: "perp")
    seen = []

    class Registry:
        def compose(self, first, second, bindings):
            seen.append((first, second, bindings))
            return SimpleNamespace(first=first, second=second)

    sb, pb = object(), object()
    pair = compose_clip_pair(Registry(), spot_spec=spot, spot_bindings=sb,
                             perp_spec=perp, perp_bindings=pb)
    assert pair.first is spot and pair.second is perp
    assert len(seen) == 1 and seen[0][2] == {"spot": sb, "perp": pb}
