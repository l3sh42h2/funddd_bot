"""Generic two-leg root invariants beyond the adapter extension matrix."""
from dataclasses import replace
from decimal import Decimal as D, localcontext
import json
import time
from types import SimpleNamespace

import pytest

from funding_bot.trade import generic_operations, leg_accounting, operation_roots, quantity_units, reconcile, store
from funding_bot.trade.generic_operations import (
    EVENT_DISPATCH,
    EVENT_PREPARED,
    EVENT_RESULT,
    EVENT_SENT,
    EventAttemptJournal,
    GenericOperationCoordinator,
)
from funding_bot.trade.operation_plan import LegBound, OperationPlan
from funding_bot.trade.quantity_units import base_to_native_floor
from funding_bot.trade.adapters.contracts import AdapterError, NativeRef
from common_adapter_fixtures import Transport, context, spec, registry


def plan(a, b, *, operation_id):
    bounds = {}
    for leg in (a, b):
        bounds[leg.leg_id] = LegBound(
            "BUY" if leg.direction == "long" else "SELL", D(0), D(2) / leg.multiplier,
            leg.quote_currency, D(20), min_receive=D(0), min_receive_currency=leg.quote_currency,
        )
    return OperationPlan(operation_id, "entry", (a, b), a.leg_id, D(2), D(2),
                         time.time() + 600, "floor", bounds, {"owner": "fixture", "mode": "dry"})


def deal(con, p, did):
    return store.create_generic_deal(
        con, asset_id=p.legs[0].asset_id,
        position_spec={"generic_position_v1": True, "legs": p.to_dict()["legs"]},
        owner_json="{}", sim=True, deal_id=did,
    )


def approved_entry(tmp_path, *, operation_id, leading_leg_id=None, max_unhedged=None):
    """Build one approved real-port generic program for crash-boundary tests."""
    con = store.connect(tmp_path / "trade.db")
    a, b = spec("fixture_cex_spot", "lead", "long"), spec("fixture_cex_perp", "hedge", "short")
    p = plan(a, b, operation_id=operation_id)
    if leading_leg_id is not None:
        p = replace(p, leading_leg_id=leading_leg_id)
    if max_unhedged is not None:
        p = replace(p, max_unhedged_exposure=max_unhedged)
    did = deal(con, p, "D" + operation_id.upper()[:20])
    transports = (Transport(), Transport())
    for item in transports:
        item.clock = time.time()
    coordinator = GenericOperationCoordinator(con, registry(), None)
    iid, nonce = coordinator.propose(deal=store.get_deal(con, did), plan=p, profile_id="fixture")
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=iid, operation_id=p.operation_id,
                                    leg_id=leg.leg_id) for leg in p.legs]
    ctx = context(p.legs, transports, journals)
    coordinator.context = ctx
    assert coordinator.approve(iid, nonce)
    return con, did, p, iid, coordinator, transports, ctx


def reopen_generic(tmp_path, con, did, p, iid, transports):
    """A real DB reopen plus startup; no retained in-memory journal is trusted."""
    con.close()
    reopened = store.connect(tmp_path / "trade.db")
    reconcile.startup(reopened, lambda sim: None)
    journals = [EventAttemptJournal(reopened, deal_id=did, intent_id=iid, operation_id=p.operation_id,
                                    leg_id=leg.leg_id) for leg in p.legs]
    return reopened, GenericOperationCoordinator(reopened, registry(), context(p.legs, transports, journals))


def test_generic_proposal_raises_reader_floor_before_plan_persistence(tmp_path):
    con = store.connect(tmp_path / "trade.db")
    a, b = spec("fixture_cex_spot", "lead", "long"), spec("fixture_cex_perp", "hedge", "short")
    p = plan(a, b, operation_id="generic-floor")
    did = deal(con, p, "DGFLOOR")
    assert store.schema_info(con)["min_reader"] == 5
    coordinator = GenericOperationCoordinator(con, registry(), None)
    iid, _ = coordinator.propose(deal=store.get_deal(con, did), plan=p, profile_id="fixture")
    assert store.get_intent(con, iid)["plan_json"] == p.to_json()
    assert store.schema_info(con)["min_reader"] == 5


def test_generic_approval_rejects_changed_second_leg_fingerprint_atomically(tmp_path):
    con = store.connect(tmp_path / "trade.db")
    a, b = spec("fixture_cex_spot", "lead", "long"), spec("fixture_cex_perp", "hedge", "short")
    p = plan(a, b, operation_id="generic-fingerprint")
    did = deal(con, p, "DGFP")
    coordinator = GenericOperationCoordinator(con, registry(), None)
    iid, nonce = coordinator.propose(deal=store.get_deal(con, did), plan=p, profile_id="fixture")
    changed = p.to_dict()
    changed["legs"][1]["account"] = "different-account"
    con.execute("UPDATE intents SET plan_json=? WHERE id=?", (json.dumps(changed, sort_keys=True, separators=(",", ":")), iid))
    with pytest.raises(store.StoreError, match="fingerprint|frozen parent"):
        coordinator.approve(iid, nonce)
    assert store.get_intent(con, iid)["status"] == store.IntentStatus.PROPOSED
    assert store.get_operation(con, p.operation_id)["state"] == store.OpState.PROPOSED


def test_generic_active_leg_scope_blocks_second_parent_deal(tmp_path):
    con = store.connect(tmp_path / "trade.db")
    a, b = spec("fixture_cex_spot", "lead", "long"), spec("fixture_cex_perp", "hedge", "short")
    first = plan(a, b, operation_id="generic-first")
    first_id = deal(con, first, "DGFIRST")
    store.set_deal_state(con, first_id, store.DealState.ENTERING, expect=store.DealState.DRAFT)
    second = plan(a, b, operation_id="generic-second")
    second_id = deal(con, second, "DGSECOND")
    coordinator = GenericOperationCoordinator(con, registry(), None)
    with pytest.raises(store.StoreError, match="scope"):
        coordinator.propose(deal=store.get_deal(con, second_id), plan=second, profile_id="fixture")


def test_legacy_plan_json_without_min_receive_fields_remains_readable():
    a, b = spec("fixture_cex_spot", "lead", "long"), spec("fixture_cex_perp", "hedge", "short")
    p = plan(a, b, operation_id="generic-json")
    data = p.to_dict()
    for bound in data["bounds"].values():
        bound.pop("min_receive")
        bound.pop("min_receive_currency")
    restored = OperationPlan.from_json(json.dumps(data, sort_keys=True, separators=(",", ":")))
    assert all(bound.min_receive == 0 and bound.min_receive_currency is None for bound in restored.bounds.values())


def test_crash_after_reserve_before_prepare_reopens_without_submit(tmp_path, monkeypatch):
    con = store.connect(tmp_path / "trade.db")
    a, b = spec("fixture_cex_spot", "lead", "long"), spec("fixture_cex_perp", "hedge", "short")
    p = plan(a, b, operation_id="generic-crash-reserve")
    did = deal(con, p, "DGCRASH")
    coordinator = GenericOperationCoordinator(con, registry(), None)
    iid, nonce = coordinator.propose(deal=store.get_deal(con, did), plan=p, profile_id="fixture")
    transports = (Transport(), Transport())
    for item in transports:
        item.clock = time.time()
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=iid, operation_id=p.operation_id, leg_id=leg.leg_id)
                for leg in p.legs]
    ctx = context(p.legs, transports, journals)
    coordinator.context = ctx
    assert coordinator.approve(iid, nonce)
    ctx.for_leg(a).journal.prepare = lambda prepared: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        coordinator.execute(iid)
    assert store.get_operation(con, p.operation_id)["state"] == store.OpState.RUNNING
    assert int(store.get_operation(con, p.operation_id)["reserved_raw"]) > 0
    con.close()

    con = store.connect(tmp_path / "trade.db")
    reconcile.startup(con, lambda sim: None)
    assert store.get_operation(con, p.operation_id)["state"] == store.OpState.PAUSED_UNKNOWN
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=iid, operation_id=p.operation_id, leg_id=leg.leg_id)
                for leg in p.legs]
    recovered = GenericOperationCoordinator(con, registry(), context(p.legs, transports, journals)).resume(iid)
    assert recovered.state == store.OpState.STOPPED
    assert int(store.get_operation(con, p.operation_id)["reserved_raw"]) == 0
    assert [len(item.sent) for item in transports] == [0, 0]


def test_rehedge_after_resolved_leading_ack_loss_submits_only_missing_leg(tmp_path):
    con = store.connect(tmp_path / "trade.db")
    a, b = spec("fixture_cex_spot", "lead", "long"), spec("fixture_cex_perp", "hedge", "short")
    entry = plan(a, b, operation_id="generic-entry-loss")
    did = deal(con, entry, "DGREHEDGE")
    transports = (Transport(), Transport())
    for item in transports:
        item.clock = time.time()
    coordinator = GenericOperationCoordinator(con, registry(), None)
    iid, nonce = coordinator.propose(deal=store.get_deal(con, did), plan=entry, profile_id="fixture")
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=iid, operation_id=entry.operation_id, leg_id=leg.leg_id)
                for leg in entry.legs]
    coordinator.context = context(entry.legs, transports, journals)
    assert coordinator.approve(iid, nonce)
    transports[0].fail_after_send = True
    assert coordinator.execute(iid).state == store.OpState.PAUSED_UNKNOWN
    transports[0].fail_after_send = False
    assert coordinator.resume(iid).state == store.OpState.PAUSED_RISK
    assert [len(item.sent) for item in transports] == [1, 0]

    bounds = {a.leg_id: LegBound("BUY", D(0), D(2), a.quote_currency, D(20), min_receive=D(0),
                                 min_receive_currency=a.quote_currency),
              b.leg_id: LegBound("SELL", D(0), D(2), b.quote_currency, D(20), min_receive=D(0),
                                 min_receive_currency=b.quote_currency)}
    correction = OperationPlan("generic-rehedge", "rehedge", (a, b), b.leg_id, D(2), D(2),
                                time.time() + 600, "floor", bounds, {"owner": "fixture", "mode": "dry"})
    rid, rnonce = coordinator.propose(deal=store.get_deal(con, did), plan=correction, profile_id="fixture")
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=rid, operation_id=correction.operation_id, leg_id=leg.leg_id)
                for leg in correction.legs]
    coordinator.context = context(correction.legs, transports, journals)
    assert coordinator.approve(rid, rnonce)
    assert coordinator.execute(rid).state == store.OpState.OPEN
    assert [len(item.sent) for item in transports] == [1, 1]


def test_partial_pair_continues_same_root_with_fresh_plan_and_leg_identity(tmp_path):
    con = store.connect(tmp_path / "trade.db")
    a, b = spec("fixture_cex_spot", "lead", "long"), spec("fixture_cex_perp", "hedge", "short")
    first = plan(a, b, operation_id="generic-partial")
    did = deal(con, first, "DGPARTIAL")
    transports = (Transport(), Transport())
    for item in transports:
        item.clock = time.time()
        item.fraction = D(1)
    transports[0].fraction = D(".5")
    coordinator = GenericOperationCoordinator(con, registry(), None)
    iid, nonce = coordinator.propose(deal=store.get_deal(con, did), plan=first, profile_id="fixture")
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=iid, operation_id=first.operation_id, leg_id=leg.leg_id)
                for leg in first.legs]
    coordinator.context = context(first.legs, transports, journals)
    assert coordinator.approve(iid, nonce)
    assert coordinator.execute(iid).state == store.OpState.PARTIAL
    assert store.operation_remaining(store.get_operation(con, first.operation_id)) == 1_000

    bounds = {leg.leg_id: replace(bound, max_qty=D(1)) for leg, bound in
              ((a, first.bounds[a.leg_id]), (b, first.bounds[b.leg_id]))}
    continuation = OperationPlan(first.operation_id, "entry", (a, b), a.leg_id, D(1), D(1),
                                 time.time() + 600, "floor", bounds, {"owner": "fixture", "mode": "dry", "fresh": 1})
    for item in transports:
        item.fraction = D(1)
    cid, cnonce = coordinator.propose(deal=store.get_deal(con, did), plan=continuation, profile_id="fixture")
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=cid, operation_id=first.operation_id, leg_id=leg.leg_id)
                for leg in continuation.legs]
    coordinator.context = context(continuation.legs, transports, journals)
    assert coordinator.approve(cid, cnonce)
    assert coordinator.execute(cid).state == store.OpState.OPEN
    assert store.get_operation(con, first.operation_id)["confirmed_raw"] == "2000"


@pytest.mark.parametrize("boundary", ("prepared", "dispatched", "claimed"))
def test_crash_boundaries_reopen_without_duplicate_native_submission(tmp_path, monkeypatch, boundary):
    """Every write-ahead proof survives a BaseException and forbids a retry send."""
    con, did, p, iid, coordinator, transports, ctx = approved_entry(
        tmp_path, operation_id="generic-crash-" + boundary,
    )
    lead = p.legs[0]
    original_event = store.event
    if boundary == "prepared":
        def after_prepared(con_, kind, *args, **kwargs):
            if kind == EVENT_DISPATCH:
                raise KeyboardInterrupt("crash after prepare")
            return original_event(con_, kind, *args, **kwargs)
        monkeypatch.setattr(store, "event", after_prepared)
    elif boundary == "dispatched":
        monkeypatch.setattr(ctx.for_leg(lead).journal, "claim",
                            lambda prepared: (_ for _ in ()).throw(KeyboardInterrupt("crash before claim")))
    else:
        def after_claim(spec_, prepared):
            raise KeyboardInterrupt("crash after claim")
        monkeypatch.setattr(ctx.for_leg(lead), "submit", after_claim)

    with pytest.raises(KeyboardInterrupt):
        coordinator.execute(iid)
    monkeypatch.setattr(store, "event", original_event)
    kinds = [row["kind"] for row in store.events(con, did)]
    assert EVENT_PREPARED in kinds
    assert (EVENT_DISPATCH in kinds) is (boundary != "prepared")
    assert (EVENT_SENT in kinds) is (boundary == "claimed")
    assert [len(item.sent) for item in transports] == [0, 0]
    assert store.get_operation(con, p.operation_id)["state"] == store.OpState.RUNNING

    con, recovered = reopen_generic(tmp_path, con, did, p, iid, transports)
    root = store.get_operation(con, p.operation_id)
    assert root["state"] == store.OpState.PAUSED_UNKNOWN
    assert root["confirmed_raw"] == "0"
    if boundary == "prepared":
        assert recovered.resume(iid).state == store.OpState.STOPPED
        assert store.get_operation(con, p.operation_id)["reserved_raw"] == "0"
    else:
        assert recovered.resume(iid).state == store.OpState.PAUSED_UNKNOWN
        assert store.get_operation(con, p.operation_id)["reserved_raw"] == root["reserved_raw"]
    assert [len(item.sent) for item in transports] == [0, 0]


def test_unknown_crash_holds_scope_and_budget_across_reopen(tmp_path, monkeypatch):
    con, did, p, iid, coordinator, transports, ctx = approved_entry(tmp_path, operation_id="generic-hold")
    lead = p.legs[0]
    native_submit = ctx.for_leg(lead).submit

    def execute_then_crash(spec_, prepared):
        native_submit(spec_, prepared)
        raise KeyboardInterrupt("crash after external execution")

    monkeypatch.setattr(ctx.for_leg(lead), "submit", execute_then_crash)
    with pytest.raises(KeyboardInterrupt):
        coordinator.execute(iid)
    assert [len(item.sent) for item in transports] == [1, 0]

    con, recovered = reopen_generic(tmp_path, con, did, p, iid, transports)
    held = store.get_operation(con, p.operation_id)
    assert (held["state"], held["confirmed_raw"], held["reserved_raw"]) == (
        store.OpState.PAUSED_UNKNOWN, "0", "2000")
    # A different parent cannot take either frozen scope while this proof is unresolved.
    other = deal(con, replace(p, operation_id="generic-hold-other"), "DGHOLDOTHER")
    with pytest.raises(store.StoreError, match="scope"):
        recovered.propose(deal=store.get_deal(con, other),
                           plan=replace(p, operation_id="generic-hold-other"), profile_id="fixture")
    assert recovered.resume(iid).state == store.OpState.PAUSED_RISK
    settled = store.get_operation(con, p.operation_id)
    assert (settled["confirmed_raw"], settled["reserved_raw"]) == ("2000", "0")
    assert [len(item.sent) for item in transports] == [1, 0]


def test_crash_after_both_native_results_reopens_and_settles_idempotently(tmp_path, monkeypatch):
    con, did, p, iid, coordinator, transports, _ctx = approved_entry(tmp_path, operation_id="generic-both-results")
    monkeypatch.setattr(coordinator, "_apply_resolved",
                        lambda *args: (_ for _ in ()).throw(KeyboardInterrupt("crash before final settle")))
    with pytest.raises(KeyboardInterrupt):
        coordinator.execute(iid)
    assert [len(item.sent) for item in transports] == [1, 1]
    assert store.get_operation(con, p.operation_id)["state"] == store.OpState.RUNNING

    con, recovered = reopen_generic(tmp_path, con, did, p, iid, transports)
    assert store.get_operation(con, p.operation_id)["state"] == store.OpState.PAUSED_UNKNOWN
    assert recovered.resume(iid).state == store.OpState.OPEN
    settled = store.get_operation(con, p.operation_id)
    assert (settled["confirmed_raw"], settled["reserved_raw"]) == ("2000", "0")
    assert [len(item.sent) for item in transports] == [1, 1]


def test_partial_terminal_pair_reopens_with_only_proven_budget(tmp_path):
    con, did, p, iid, coordinator, transports, _ctx = approved_entry(tmp_path, operation_id="generic-partial-final")
    transports[0].fraction = D(".5")
    outcome = coordinator.execute(iid)
    root = store.get_operation(con, p.operation_id)
    assert outcome.state == store.OpState.PARTIAL
    assert (root["confirmed_raw"], root["reserved_raw"]) == ("1000", "0")
    assert [len(item.sent) for item in transports] == [1, 1]
    con, recovered = reopen_generic(tmp_path, con, did, p, iid, transports)
    root = store.get_operation(con, p.operation_id)
    assert (root["state"], root["confirmed_raw"], root["reserved_raw"]) == (
        store.OpState.PARTIAL, "1000", "0")
    # A continuation receives precisely the remaining root budget, not the original target.
    continuation = replace(
        p,
        bounds={leg.leg_id: replace(p.bounds[leg.leg_id], max_qty=D(1)) for leg in p.legs},
        target_exposure=D(1), max_unhedged_exposure=D(1), expires_at=time.time() + 600,
        authorization={"owner": "fixture", "mode": "dry", "continuation": 1},
    )
    transports[0].fraction = D(1)
    cid, nonce = recovered.propose(deal=store.get_deal(con, did), plan=continuation, profile_id="fixture")
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=cid, operation_id=p.operation_id,
                                    leg_id=leg.leg_id) for leg in p.legs]
    recovered.context = context(p.legs, transports, journals)
    assert recovered.approve(cid, nonce)
    assert recovered.execute(cid).state == store.OpState.OPEN
    assert [len(item.sent) for item in transports] == [2, 2]


def test_short_overhedge_rehedge_buys_reduce_only_without_crossing_zero(tmp_path):
    con, did, entry, iid, coordinator, transports, _ctx = approved_entry(
        tmp_path, operation_id="generic-short-overhedge", leading_leg_id="hedge",
    )
    # The first native short exists, but its acknowledgement is lost.
    transports[1].fail_after_send = True
    assert coordinator.execute(iid).state == store.OpState.PAUSED_UNKNOWN
    transports[1].fail_after_send = False
    assert coordinator.resume(iid).state == store.OpState.PAUSED_RISK
    assert [len(item.sent) for item in transports] == [0, 1]

    a, b = entry.legs
    bounds = {
        a.leg_id: LegBound("BUY", D(0), D(2), a.quote_currency, D(20), min_receive=D(0),
                            min_receive_currency=a.quote_currency),
        b.leg_id: LegBound("BUY", D(0), D(2), b.quote_currency, D(20), reduce_only=True,
                            min_receive=D(0), min_receive_currency=b.quote_currency),
    }
    correction = OperationPlan("generic-short-correct", "rehedge", (a, b), b.leg_id, D(2), D(2),
                                time.time() + 600, "floor", bounds, {"owner": "fixture", "mode": "dry"})
    rid, nonce = coordinator.propose(deal=store.get_deal(con, did), plan=correction, profile_id="fixture")
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=rid, operation_id=correction.operation_id,
                                    leg_id=leg.leg_id) for leg in correction.legs]
    coordinator.context = context(correction.legs, transports, journals)
    assert coordinator.approve(rid, nonce)
    assert coordinator.execute(rid).state == store.OpState.OPEN
    action = transports[1].sent[-1][2]
    assert (action.side, action.reduce_only, action.quantity) == ("BUY", True, D(2))


def test_single_leg_rehedge_ack_loss_recovers_from_parent_book_once(tmp_path):
    con, did, entry, iid, coordinator, transports, _ctx = approved_entry(
        tmp_path, operation_id="generic-rehedge-parent", leading_leg_id="hedge",
    )
    transports[1].fail_after_send = True
    assert coordinator.execute(iid).state == store.OpState.PAUSED_UNKNOWN
    transports[1].fail_after_send = False
    assert coordinator.resume(iid).state == store.OpState.PAUSED_RISK

    a, b = entry.legs
    bounds = {
        a.leg_id: LegBound("BUY", D(0), D(2), a.quote_currency, D(20), min_receive=D(0),
                           min_receive_currency=a.quote_currency),
        b.leg_id: LegBound("BUY", D(0), D(2), b.quote_currency, D(20), reduce_only=True,
                           min_receive=D(0), min_receive_currency=b.quote_currency),
    }
    correction = OperationPlan("generic-rehedge-recover", "rehedge", (a, b), b.leg_id, D(2), D(2),
                               time.time() + 600, "floor", bounds, {"owner": "fixture", "mode": "dry"})
    rid, nonce = coordinator.propose(deal=store.get_deal(con, did), plan=correction, profile_id="fixture")
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=rid, operation_id=correction.operation_id,
                                    leg_id=leg.leg_id) for leg in correction.legs]
    coordinator.context = context(correction.legs, transports, journals)
    assert coordinator.approve(rid, nonce)
    transports[1].fail_after_send = True
    assert coordinator.execute(rid).state == store.OpState.PAUSED_UNKNOWN
    transports[1].fail_after_send = False

    con, recovered = reopen_generic(tmp_path, con, did, correction, rid, transports)
    assert recovered.resume(rid).state == store.OpState.OPEN
    root = store.get_operation(con, correction.operation_id)
    projection = leg_accounting.rebuild(con, deal_id=did)
    correction_projection = leg_accounting.rebuild(con, operation_id=correction.operation_id)
    assert (root["confirmed_raw"], root["reserved_raw"]) == (root["target_raw"], "0")
    assert sum(D(row["qty"]) for row in projection["legs"]) == 0
    assert [(row["leg_id"], row["executions"]) for row in correction_projection["legs"]] == [(b.leg_id, 1)]
    assert [len(item.sent) for item in transports] == [0, 2]
    before = (root, projection)
    with pytest.raises(store.StoreError, match="paused unknown"):
        recovered.resume(rid)
    assert (store.get_operation(con, correction.operation_id), leg_accounting.rebuild(con, deal_id=did)) == before


def test_rehedge_multiplier_point_one_uses_native_contracts_without_crossing_owned_base(tmp_path):
    con = store.connect(tmp_path / "trade.db")
    a = spec("fixture_cex_spot", "lead", "long")
    b = spec("fixture_cex_perp", "hedge", "short", multiplier=D("0.1"))
    entry = replace(plan(a, b, operation_id="generic-m01-parent"), leading_leg_id=b.leg_id)
    did = deal(con, entry, "DGM01")
    transports = (Transport(), Transport())
    for item in transports:
        item.clock = time.time()
    coordinator = GenericOperationCoordinator(con, registry(), None)
    iid, nonce = coordinator.propose(deal=store.get_deal(con, did), plan=entry, profile_id="fixture")
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=iid, operation_id=entry.operation_id,
                                    leg_id=leg.leg_id) for leg in entry.legs]
    coordinator.context = context(entry.legs, transports, journals)
    assert coordinator.approve(iid, nonce)
    transports[1].fail_after_send = True
    assert coordinator.execute(iid).state == store.OpState.PAUSED_UNKNOWN
    transports[1].fail_after_send = False
    assert coordinator.resume(iid).state == store.OpState.PAUSED_RISK
    assert transports[1].sent[0][2].quantity == D(20)

    bounds = {
        a.leg_id: LegBound("BUY", D(0), D(2), a.quote_currency, D(20), min_receive=D(0),
                           min_receive_currency=a.quote_currency),
        b.leg_id: LegBound("BUY", D(0), D(20), b.quote_currency, D(20), reduce_only=True,
                           min_receive=D(0), min_receive_currency=b.quote_currency),
    }
    correction = OperationPlan("generic-m01-correction", "rehedge", (a, b), b.leg_id, D(2), D(2),
                               time.time() + 600, "floor", bounds, {"owner": "fixture", "mode": "dry"})
    rid, rnonce = coordinator.propose(deal=store.get_deal(con, did), plan=correction, profile_id="fixture")
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=rid, operation_id=correction.operation_id,
                                    leg_id=leg.leg_id) for leg in correction.legs]
    coordinator.context = context(correction.legs, transports, journals)
    assert coordinator.approve(rid, rnonce)
    assert coordinator.execute(rid).state == store.OpState.OPEN
    assert transports[1].sent[-1][2].quantity == D(20)
    assert sum(D(row["qty"]) for row in leg_accounting.rebuild(con, deal_id=did)["legs"]) == 0


def test_rehedge_native_floor_never_rounds_exposure_up_at_decimal_context_boundary():
    exposure_base = D("1.9999999999999999999999999999")
    assert base_to_native_floor(exposure_base, D("0.1"), D(1)) == D(19)


@pytest.mark.parametrize(("exposure_base", "multiplier"), (
    (D("1.9999999999999999999999999999"), D("0.1")),
    (D("199.99999999999999999999999999"), D(10)),
))
def test_rehedge_caller_preserves_exact_floor_for_small_and_large_multiplier(exposure_base, multiplier):
    coordinator = type("Sizing", (), {"_parent_delta": lambda *_: exposure_base})()
    leg = type("Leg", (), {"multiplier": multiplier, "step": D(1)})()
    with localcontext() as decimal_context:
        decimal_context.prec = 28
        assert GenericOperationCoordinator._hedge_quantity(coordinator, "D", None, leg, None) == D(19)


def test_parent_delta_sum_is_context_independent():
    exposure_base = D("1.9999999999999999999999999999")
    quantities = {"positive": exposure_base, "zero": D(0)}
    coordinator = type("Sizing", (), {"_leg_parent_qty": lambda self, _did, leg: quantities[leg]})()
    plan_with_legs = type("Plan", (), {"legs": ("positive", "zero")})()
    with localcontext() as decimal_context:
        decimal_context.prec = 28
        assert GenericOperationCoordinator._parent_delta(coordinator, "D", plan_with_legs) == exposure_base


@pytest.mark.parametrize(("actual_qty", "multiplier", "expected"), (
    (D("19.999999999999999999999999999"), D("0.1"), D("1.9999999999999999999999999999")),
    (D("19.999999999999999999999999999"), D(10), D("199.99999999999999999999999999")),
))
def test_parent_delta_rebuilds_real_execution_fact_without_context_rounding(
        tmp_path, actual_qty, multiplier, expected):
    con = store.connect(tmp_path / "trade.db")
    a = spec("fixture_cex_perp", "lead", "long", multiplier=multiplier)
    b = spec("fixture_cex_perp", "hedge", "short")
    entry = plan(a, b, operation_id="generic-exact-fact")
    did = deal(con, entry, "DGEXACTFACT")
    fact = leg_accounting.ExecutionFact(
        entry.operation_id, a.leg_id, a.fingerprint,
        json.dumps(a.scope, ensure_ascii=False, separators=(",", ":")),
        "order:exact-boundary", "BUY", actual_qty, multiplier, "final",
        market_kind="perpetual", base_currency=a.asset_id,
        settlement_currency=a.settlement_currency, fees_complete=True,
    )
    leg_accounting.record_fact(con, fact, deal_id=did)
    coordinator = GenericOperationCoordinator(con, registry(), None)

    with localcontext() as decimal_context:
        decimal_context.prec = 28
        assert coordinator._leg_parent_qty(did, a) == expected
        assert coordinator._parent_delta(did, entry) == expected


def test_rehedge_positive_owned_perp_multiplier_ten_sells_reduce_only_to_zero(tmp_path):
    con = store.connect(tmp_path / "trade.db")
    a = spec("fixture_cex_perp", "lead", "long", multiplier=D(10))
    b = spec("fixture_cex_perp", "hedge", "short")
    entry = plan(a, b, operation_id="generic-m10-parent")
    did = deal(con, entry, "DGM10")
    transports = (Transport(), Transport())
    for item in transports:
        item.clock = time.time()
    coordinator = GenericOperationCoordinator(con, registry(), None)
    iid, nonce = coordinator.propose(deal=store.get_deal(con, did), plan=entry, profile_id="fixture")
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=iid, operation_id=entry.operation_id,
                                    leg_id=leg.leg_id) for leg in entry.legs]
    coordinator.context = context(entry.legs, transports, journals)
    assert coordinator.approve(iid, nonce)
    transports[0].fail_after_send = True
    assert coordinator.execute(iid).state == store.OpState.PAUSED_UNKNOWN
    transports[0].fail_after_send = False
    assert coordinator.resume(iid).state == store.OpState.PAUSED_RISK

    bounds = {
        a.leg_id: LegBound("SELL", D(0), D("0.2"), a.quote_currency, D(20), reduce_only=True,
                           min_receive=D(0), min_receive_currency=a.quote_currency),
        b.leg_id: LegBound("SELL", D(0), D(2), b.quote_currency, D(20),
                           min_receive=D(0), min_receive_currency=b.quote_currency),
    }
    correction = OperationPlan("generic-m10-correction", "rehedge", (a, b), a.leg_id, D(2), D(2),
                               time.time() + 600, "floor", bounds, {"owner": "fixture", "mode": "dry"})
    rid, rnonce = coordinator.propose(deal=store.get_deal(con, did), plan=correction, profile_id="fixture")
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=rid, operation_id=correction.operation_id,
                                    leg_id=leg.leg_id) for leg in correction.legs]
    coordinator.context = context(correction.legs, transports, journals)
    assert coordinator.approve(rid, rnonce)
    assert coordinator.execute(rid).state == store.OpState.OPEN
    assert (transports[0].sent[-1][2].side, transports[0].sent[-1][2].reduce_only,
            transports[0].sent[-1][2].quantity) == ("SELL", True, D("0.2"))
    assert sum(D(row["qty"]) for row in leg_accounting.rebuild(con, deal_id=did)["legs"]) == 0


def test_rehedge_negative_owned_perp_multiplier_ten_buys_reduce_only_to_zero(tmp_path):
    con = store.connect(tmp_path / "trade.db")
    a = spec("fixture_cex_spot", "lead", "long")
    b = spec("fixture_cex_perp", "hedge", "short", multiplier=D(10))
    entry = replace(plan(a, b, operation_id="generic-negative-m10-parent"), leading_leg_id=b.leg_id)
    did = deal(con, entry, "DGNEGM10")
    transports = (Transport(), Transport())
    for item in transports:
        item.clock = time.time()
    coordinator = GenericOperationCoordinator(con, registry(), None)
    iid, nonce = coordinator.propose(deal=store.get_deal(con, did), plan=entry, profile_id="fixture")
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=iid, operation_id=entry.operation_id,
                                    leg_id=leg.leg_id) for leg in entry.legs]
    coordinator.context = context(entry.legs, transports, journals)
    assert coordinator.approve(iid, nonce)
    transports[1].fail_after_send = True
    assert coordinator.execute(iid).state == store.OpState.PAUSED_UNKNOWN
    transports[1].fail_after_send = False
    assert coordinator.resume(iid).state == store.OpState.PAUSED_RISK

    bounds = {
        a.leg_id: LegBound("BUY", D(0), D(2), a.quote_currency, D(20),
                           min_receive=D(0), min_receive_currency=a.quote_currency),
        b.leg_id: LegBound("BUY", D(0), D("0.2"), b.quote_currency, D(20), reduce_only=True,
                           min_receive=D(0), min_receive_currency=b.quote_currency),
    }
    correction = OperationPlan("generic-negative-m10-correction", "rehedge", (a, b), b.leg_id,
                               D(2), D(2), time.time() + 600, "floor", bounds,
                               {"owner": "fixture", "mode": "dry"})
    rid, rnonce = coordinator.propose(deal=store.get_deal(con, did), plan=correction, profile_id="fixture")
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=rid,
                                    operation_id=correction.operation_id, leg_id=leg.leg_id)
                for leg in correction.legs]
    coordinator.context = context(correction.legs, transports, journals)
    assert coordinator.approve(rid, rnonce)
    assert coordinator.execute(rid).state == store.OpState.OPEN
    assert (transports[1].sent[-1][2].side, transports[1].sent[-1][2].reduce_only,
            transports[1].sent[-1][2].quantity) == ("BUY", True, D("0.2"))
    assert sum(D(row["qty"]) for row in leg_accounting.rebuild(con, deal_id=did)["legs"]) == 0


def test_rehedge_cross_zero_refuses_before_dispatch(tmp_path, monkeypatch):
    con, did, entry, iid, coordinator, transports, _ctx = approved_entry(
        tmp_path, operation_id="generic-cross-parent", leading_leg_id="hedge",
    )
    transports[1].fail_after_send = True
    assert coordinator.execute(iid).state == store.OpState.PAUSED_UNKNOWN
    transports[1].fail_after_send = False
    assert coordinator.resume(iid).state == store.OpState.PAUSED_RISK

    a, b = entry.legs
    bounds = {
        a.leg_id: LegBound("BUY", D(0), D(2), a.quote_currency, D(20), min_receive=D(0),
                           min_receive_currency=a.quote_currency),
        b.leg_id: LegBound("BUY", D(0), D(3), b.quote_currency, D(20), reduce_only=True,
                           min_receive=D(0), min_receive_currency=b.quote_currency),
    }
    correction = OperationPlan("generic-cross-correction", "rehedge", (a, b), b.leg_id, D(3), D(3),
                               time.time() + 600, "floor", bounds, {"owner": "fixture", "mode": "dry"})
    rid, nonce = coordinator.propose(deal=store.get_deal(con, did), plan=correction, profile_id="fixture")
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=rid, operation_id=correction.operation_id,
                                    leg_id=leg.leg_id) for leg in correction.legs]
    coordinator.context = context(correction.legs, transports, journals)
    assert coordinator.approve(rid, nonce)
    monkeypatch.setattr(coordinator, "_parent_delta", lambda *_: D(-3))
    before = [len(item.sent) for item in transports]
    with pytest.raises(store.StoreError, match="cross"):
        coordinator.execute(rid)
    assert [len(item.sent) for item in transports] == before
    assert not [event for event in store.events(con, did)
                if event["kind"] == EVENT_DISPATCH and event["intent_id"] == rid]


@pytest.mark.parametrize("corruption", ("{", "", "[]", '{"generic_position_v1":true,"legs":[]}'))
def test_unknown_active_peer_with_unreadable_or_incomplete_identity_blocks_new_proposal(
        tmp_path, monkeypatch, corruption):
    con, did, original, iid, coordinator, transports, ctx = approved_entry(
        tmp_path, operation_id="generic-corrupt-peer-old",
    )
    monkeypatch.setattr(ctx.for_leg(original.legs[0]), "submit",
                        lambda *_: (_ for _ in ()).throw(KeyboardInterrupt("crash after durable claim")))
    with pytest.raises(KeyboardInterrupt):
        coordinator.execute(iid)
    con, _recovered = reopen_generic(tmp_path, con, did, original, iid, transports)
    root_before = store.get_operation(con, original.operation_id)
    assert root_before["state"] == store.OpState.PAUSED_UNKNOWN and int(root_before["reserved_raw"]) > 0
    con.execute("UPDATE deals SET inst_json=? WHERE id=?", (corruption, did))

    candidate = replace(original, operation_id="generic-corrupt-peer-new")
    candidate_did = deal(con, candidate, "DGCORRUPTNEW")
    candidate_coordinator = GenericOperationCoordinator(con, registry(), None)
    with pytest.raises(store.StoreError, match="active peer"):
        candidate_coordinator.propose(
            deal=store.get_deal(con, candidate_did), plan=candidate, profile_id="fixture",
        )
    assert store.get_operation(con, candidate.operation_id) is None
    assert not con.execute("SELECT 1 FROM intents WHERE deal_id=?", (candidate_did,)).fetchone()
    assert [len(item.sent) for item in transports] == [0, 0]
    assert not con.execute("SELECT 1 FROM exec_events WHERE deal_id=? AND kind=?",
                           (candidate_did, EVENT_DISPATCH)).fetchone()
    root_after = store.get_operation(con, original.operation_id)
    assert (root_after["state"], root_after["reserved_raw"]) == (
        root_before["state"], root_before["reserved_raw"])


def _pause_unknown_peer(con, deal_id, operation_id):
    store.set_deal_state(con, deal_id, store.DealState.ENTERING, expect=store.DealState.DRAFT)
    store.set_deal_state(con, deal_id, store.DealState.PAUSED, expect=store.DealState.ENTERING)
    root_id = store.create_operation(
        con, deal_id=deal_id, profile_id="fixture", inst_hash="peer:scope", mode="dry", side="entry",
        target_kind="generic_leading_quantity", target_asset="leg:hedge", target_decimals=0,
        target_raw=1, op_id=operation_id,
    )
    store.set_operation_state(con, root_id, store.OpState.APPROVED, expect=store.OpState.PROPOSED)
    store.set_operation_state(con, root_id, store.OpState.RUNNING, expect=store.OpState.APPROVED)
    store.operation_reserve(con, root_id, 1)
    store.set_operation_state(con, root_id, store.OpState.PAUSED_UNKNOWN, expect=store.OpState.RUNNING)
    return store.get_operation(con, root_id)


@pytest.mark.parametrize("peer_kind", ("generic", "legacy"))
@pytest.mark.parametrize("mandatory_index,mandatory_key", ((0, "venue"), (2, "account"), (4, "instrument")))
@pytest.mark.parametrize("invalid", (None, "", " "))
def test_unknown_peer_mandatory_scope_identifier_is_nonblank_before_proposal(
        tmp_path, peer_kind, mandatory_index, mandatory_key, invalid):
    con = store.connect(tmp_path / "trade.db")
    old_spot = spec("fixture_cex_spot", "lead", "long")
    old_perp = spec("fixture_cex_perp", "hedge", "short")
    old_plan = plan(old_spot, old_perp, operation_id="invalid-scope-old")
    if peer_kind == "generic":
        old_did = deal(con, old_plan, "DINVALIDSCOPEOLD")
        frozen = json.loads(store.get_deal(con, old_did)["inst_json"])
        frozen["legs"][1][mandatory_key] = invalid
    else:
        old_did = store.create_deal(
            con, coin="BASE", chain="bsc", token="0x" + "ab" * 20, token_dec=18,
            perp_venue="fixture", symbol="BASEUSDC", leg_usd=D(1), owner_json="{}",
            sim=True, deal_id="DINVALIDSCOPEOLD",
        )
        legacy_scope = list(old_perp.scope)
        legacy_scope[mandatory_index] = invalid
        frozen = {"perp_scope": legacy_scope}
    con.execute("UPDATE deals SET inst_json=? WHERE id=?", (
        json.dumps(frozen, ensure_ascii=False, separators=(",", ":")), old_did,
    ))
    old_root = _pause_unknown_peer(con, old_did, "OINVALIDSCOPEOLD")

    # The spot wallet differs.  The intended conflict is the old perpetual
    # scope whose one mandatory identifier was corrupted above.
    new_spot = replace(old_spot, account="account:new-spot-wallet")
    candidate = plan(new_spot, old_perp, operation_id="invalid-scope-new")
    candidate_did = deal(con, candidate, "DINVALIDSCOPENEW")
    coordinator = GenericOperationCoordinator(con, registry(), None)
    with pytest.raises(store.StoreError, match="active peer"):
        coordinator.propose(deal=store.get_deal(con, candidate_did), plan=candidate, profile_id="fixture")

    assert store.get_operation(con, candidate.operation_id) is None
    assert not con.execute("SELECT 1 FROM intents WHERE deal_id=?", (candidate_did,)).fetchone()
    assert not con.execute("SELECT 1 FROM exec_events WHERE deal_id=? AND kind=?",
                           (candidate_did, EVENT_DISPATCH)).fetchone()
    assert (store.get_operation(con, old_root["id"])["state"],
            store.get_operation(con, old_root["id"])["reserved_raw"]) == (
                store.OpState.PAUSED_UNKNOWN, old_root["reserved_raw"])


@pytest.mark.parametrize("spot_adapter", ("fixture_evm_spot", "fixture_sol_spot"))
@pytest.mark.parametrize("invalid_network", (None, "", " "))
def test_unknown_peer_required_dex_network_fails_closed_against_linked_plan(
        tmp_path, spot_adapter, invalid_network):
    """A damaged DEX network cannot make an UNKNOWN wallet scope available."""
    con = store.connect(tmp_path / "trade.db")
    old_spot = spec(spot_adapter, "lead", "long")
    old_perp = spec("fixture_cex_perp", "hedge", "short")
    old_plan = plan(old_spot, old_perp, operation_id=f"network-old-{spot_adapter}-{invalid_network!r}")
    old_did = deal(con, old_plan, f"DNETWORKOLD{spot_adapter[-3:].upper()}{len(str(invalid_network))}")
    old_coordinator = GenericOperationCoordinator(con, registry(), None)
    old_iid, _ = old_coordinator.propose(deal=store.get_deal(con, old_did), plan=old_plan, profile_id="fixture")
    old_root = store.get_operation(con, old_plan.operation_id)
    store.set_deal_state(con, old_did, store.DealState.ENTERING, expect=store.DealState.DRAFT)
    store.set_deal_state(con, old_did, store.DealState.PAUSED, expect=store.DealState.ENTERING)
    store.set_operation_state(con, old_root["id"], store.OpState.APPROVED, expect=store.OpState.PROPOSED)
    store.set_operation_state(con, old_root["id"], store.OpState.RUNNING, expect=store.OpState.APPROVED)
    store.operation_reserve(con, old_root["id"], 1)
    store.set_operation_state(con, old_root["id"], store.OpState.PAUSED_UNKNOWN, expect=store.OpState.RUNNING)
    frozen = json.loads(store.get_deal(con, old_did)["inst_json"])
    frozen["legs"][0]["network"] = invalid_network
    con.execute("UPDATE deals SET inst_json=? WHERE id=?", (json.dumps(frozen, separators=(",", ":")), old_did))

    # The perpetual account differs so only the damaged DEX wallet/network
    # scope determines whether this candidate is admitted.
    candidate = plan(old_spot, replace(old_perp, account="account:other-hedge"),
                     operation_id=f"network-new-{spot_adapter}-{invalid_network!r}")
    candidate_did = deal(con, candidate, f"DNETWORKNEW{spot_adapter[-3:].upper()}{len(str(invalid_network))}")
    with pytest.raises(store.StoreError, match="active peer"):
        GenericOperationCoordinator(con, registry(), None).propose(
            deal=store.get_deal(con, candidate_did), plan=candidate, profile_id="fixture")

    assert store.get_operation(con, candidate.operation_id) is None
    assert not con.execute("SELECT 1 FROM intents WHERE deal_id=?", (candidate_did,)).fetchone()
    held = store.get_operation(con, old_root["id"])
    assert (held["state"], held["reserved_raw"]) == (store.OpState.PAUSED_UNKNOWN, "1")


def test_linked_cex_peer_keeps_nullable_network_and_subaccount_valid(tmp_path):
    """CEX optional identity stays nullable after DEX peers became stricter."""
    con = store.connect(tmp_path / "trade.db")
    spot = spec("fixture_cex_spot", "lead", "long")
    perp = spec("fixture_cex_perp", "hedge", "short")
    old_plan = plan(spot, perp, operation_id="cex-nullable-old")
    old_did = deal(con, old_plan, "DCEXNULLABLEOLD")
    old_coordinator = GenericOperationCoordinator(con, registry(), None)
    old_coordinator.propose(deal=store.get_deal(con, old_did), plan=old_plan, profile_id="fixture")
    old_root = store.get_operation(con, old_plan.operation_id)
    store.set_deal_state(con, old_did, store.DealState.ENTERING, expect=store.DealState.DRAFT)
    store.set_operation_state(con, old_root["id"], store.OpState.APPROVED, expect=store.OpState.PROPOSED)

    assert spot.network is None and spot.subaccount is None
    candidate = plan(replace(spot, account="account:other-spot"),
                     replace(perp, account="account:other-hedge"), operation_id="cex-nullable-new")
    candidate_did = deal(con, candidate, "DCEXNULLABLENEW")
    iid, _ = GenericOperationCoordinator(con, registry(), None).propose(
        deal=store.get_deal(con, candidate_did), plan=candidate, profile_id="fixture")
    assert store.get_intent(con, iid) is not None


@pytest.mark.parametrize("peer_kind", ("generic", "legacy"))
def test_active_cex_peer_scope_accepts_nullable_optional_identifiers_without_normalizing(tmp_path, peer_kind):
    con = store.connect(tmp_path / "trade.db")
    spot = spec("fixture_cex_spot", "lead", "long")
    perp = spec("fixture_cex_perp", "hedge", "short")
    old_plan = plan(spot, perp, operation_id="nullable-scope-old")
    if peer_kind == "generic":
        old_did = deal(con, old_plan, "DNULLABLESCOPEOLD")
    else:
        old_did = store.create_deal(
            con, coin="BASE", chain="bsc", token="0x" + "cd" * 20, token_dec=18,
            perp_venue="fixture", symbol="BASEUSDC", leg_usd=D(1), owner_json="{}",
            sim=True, deal_id="DNULLABLESCOPEOLD",
        )
        con.execute("UPDATE deals SET inst_json=? WHERE id=?", (
            json.dumps({"perp_scope": list(perp.scope)}, separators=(",", ":")), old_did,
        ))
    _pause_unknown_peer(con, old_did, "ONULLABLESCOPEOLD")
    candidate = plan(replace(spot, account="account:new-spot-wallet"), perp,
                     operation_id="nullable-scope-new")
    candidate_did = deal(con, candidate, "DNULLABLESCOPENEW")

    assert perp.network is None and perp.subaccount is None
    with pytest.raises(store.StoreError, match="scope"):
        GenericOperationCoordinator(con, registry(), None).propose(
            deal=store.get_deal(con, candidate_did), plan=candidate, profile_id="fixture",
        )
    raw_scope = {"venue": " fixture_cex_perp ", "network": None, "account": "account:hedge",
                 "subaccount": None, "instrument": "BASE"}
    assert operation_roots._generic_scope(raw_scope)[0] == " fixture_cex_perp "


@pytest.mark.parametrize("phase", ("approval", "admission"))
def test_corrupt_peer_scope_fails_closed_if_it_appears_after_proposal(tmp_path, phase):
    con = store.connect(tmp_path / "trade.db")
    spot = spec("fixture_cex_spot", "lead", "long")
    perp = spec("fixture_cex_perp", "hedge", "short")
    candidate = plan(replace(spot, account="account:new-spot-wallet"), perp,
                     operation_id="phase-scope-new")
    candidate_did = deal(con, candidate, "DPHASESCOPENEW")
    coordinator = GenericOperationCoordinator(con, registry(), None)
    iid, nonce = coordinator.propose(
        deal=store.get_deal(con, candidate_did), plan=candidate, profile_id="fixture",
    )
    if phase == "admission":
        assert coordinator.approve(iid, nonce)
        transports = (Transport(), Transport())
        for transport in transports:
            transport.clock = time.time()
        journals = [EventAttemptJournal(con, deal_id=candidate_did, intent_id=iid,
                                        operation_id=candidate.operation_id, leg_id=leg.leg_id)
                    for leg in candidate.legs]
        coordinator.context = context(candidate.legs, transports, journals)

    old_plan = plan(spot, perp, operation_id="phase-scope-old")
    old_did = deal(con, old_plan, "DPHASESCOPEOLD")
    frozen = json.loads(store.get_deal(con, old_did)["inst_json"])
    frozen["legs"][1]["venue"] = None
    con.execute("UPDATE deals SET inst_json=? WHERE id=?", (
        json.dumps(frozen, ensure_ascii=False, separators=(",", ":")), old_did,
    ))
    old_root = _pause_unknown_peer(con, old_did, "OPHASESCOPEOLD")

    before_dispatch = con.execute("SELECT count(*) FROM exec_events WHERE kind=?", (EVENT_DISPATCH,)).fetchone()[0]
    with pytest.raises(store.StoreError, match="active peer"):
        if phase == "approval":
            coordinator.approve(iid, nonce)
        else:
            coordinator.execute(iid)
    candidate_intent = store.get_intent(con, iid)
    candidate_root = store.get_operation(con, candidate.operation_id)
    expected = store.IntentStatus.PROPOSED if phase == "approval" else store.IntentStatus.APPROVED
    assert candidate_intent["status"] == expected
    assert candidate_root["state"] == (store.OpState.PROPOSED if phase == "approval" else store.OpState.APPROVED)
    assert con.execute("SELECT count(*) FROM exec_events WHERE kind=?", (EVENT_DISPATCH,)).fetchone()[0] == before_dispatch
    assert (store.get_operation(con, old_root["id"])["state"],
            store.get_operation(con, old_root["id"])["reserved_raw"]) == (
                store.OpState.PAUSED_UNKNOWN, old_root["reserved_raw"])


def _exact_exit_fixture(tmp_path):
    quantity = D("1.0000000000000000000000000001")
    step = D("0.0000000000000000000000000001")
    spot = replace(spec("fixture_cex_spot", "lead", "long"), step=step)
    perp = replace(spec("fixture_cex_perp", "hedge", "short"), step=step)
    bounds = {
        spot.leg_id: LegBound("SELL", D(0), quantity, spot.quote_currency, D(20),
                              min_receive=D(0), min_receive_currency=spot.quote_currency),
        perp.leg_id: LegBound("BUY", D(0), quantity, perp.quote_currency, D(20), reduce_only=True,
                              min_receive=D(0), min_receive_currency=perp.quote_currency),
    }
    exit_plan = OperationPlan("exact-exit", "exit", (spot, perp), spot.leg_id, quantity, quantity,
                              time.time() + 600, "floor", bounds,
                              {"owner": "fixture", "mode": "dry"})
    con = store.connect(tmp_path / "trade.db")
    did = deal(con, exit_plan, "DGEXACTEXIT")
    for leg, side in ((spot, "BUY"), (perp, "SELL")):
        leg_accounting.record_fact(con, leg_accounting.ExecutionFact(
            "owned-entry", leg.leg_id, leg.fingerprint,
            json.dumps(leg.scope, ensure_ascii=False, separators=(",", ":")),
            "order:owned-" + leg.leg_id, side, quantity, leg.multiplier, "final",
            market_kind=leg.capabilities.market_kind, base_currency=leg.asset_id,
            settlement_currency=leg.settlement_currency, fees_complete=True,
        ), deal_id=did)
    return con, did, exit_plan, quantity, step


def test_exit_ownership_exact_long_and_short_survive_decimal_context(tmp_path):
    con, did, exit_plan, _quantity, _step = _exact_exit_fixture(tmp_path)
    coordinator = GenericOperationCoordinator(con, registry(), None)
    with localcontext() as decimal_context:
        decimal_context.prec = 28
        coordinator._require_exit_ownership(SimpleNamespace(did=did), exit_plan)


@pytest.mark.parametrize("excess_leg", ("lead", "hedge"))
def test_exit_ownership_refuses_one_minimal_unit_excess_and_spot_surplus_cannot_extend_it(
        tmp_path, excess_leg):
    con, did, exit_plan, _quantity, step = _exact_exit_fixture(tmp_path)
    excess = D("1.0000000000000000000000000002")
    bounds = dict(exit_plan.bounds)
    bounds[excess_leg] = replace(bounds[excess_leg], max_qty=excess)
    oversized = replace(exit_plan, operation_id="exact-exit-excess-" + excess_leg,
                        target_exposure=excess, max_unhedged_exposure=excess, bounds=bounds)
    assert excess - D("1.0000000000000000000000000001") == step
    with localcontext() as decimal_context:
        decimal_context.prec = 28
        with pytest.raises(store.StoreError, match="exceeds proven"):
            GenericOperationCoordinator(con, registry(), None)._require_exit_ownership(
                SimpleNamespace(did=did), oversized,
            )


def test_spot_wallet_surplus_does_not_authorize_exit_beyond_journal_owned_quantity(tmp_path):
    con, did, entry, iid, coordinator, transports, _ctx = approved_entry(
        tmp_path, operation_id="spot-surplus-entry",
    )
    assert coordinator.execute(iid).state == store.OpState.OPEN
    spot, perp = entry.legs
    transports[0].positions[spot.scope] = D(7)
    bounds = {
        spot.leg_id: LegBound("SELL", D(0), D(3), spot.quote_currency, D(20),
                              min_receive=D(0), min_receive_currency=spot.quote_currency),
        perp.leg_id: LegBound("BUY", D(0), D(2), perp.quote_currency, D(20), reduce_only=True,
                              min_receive=D(0), min_receive_currency=perp.quote_currency),
    }
    exit_plan = OperationPlan("spot-surplus-exit", "exit", entry.legs, spot.leg_id, D(3), D(3),
                              time.time() + 600, "floor", bounds,
                              {"owner": "fixture", "mode": "dry"})
    exit_iid, nonce = coordinator.propose(
        deal=store.get_deal(con, did), plan=exit_plan, profile_id="fixture",
    )
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=exit_iid,
                                    operation_id=exit_plan.operation_id, leg_id=leg.leg_id)
                for leg in exit_plan.legs]
    coordinator.context = context(exit_plan.legs, transports, journals)
    assert coordinator.approve(exit_iid, nonce)
    before = [len(transport.sent) for transport in transports]

    with pytest.raises(store.StoreError, match="exceeds proven"):
        coordinator.execute(exit_iid)
    assert [len(transport.sent) for transport in transports] == before
    assert store.get_intent(con, exit_iid)["status"] == store.IntentStatus.APPROVED
    assert store.get_operation(con, exit_plan.operation_id)["state"] == store.OpState.APPROVED
    assert not [event for event in store.events(con, did)
                if event["kind"] == EVENT_DISPATCH and event["intent_id"] == exit_iid]


@pytest.mark.parametrize(("quantity", "expected_raw"), (
    (D("1.0000000000000000000000000001"), 10000000000000000000000000001),
    (D("1.9999999999999999999999999999"), 19999999999999999999999999999),
))
def test_generic_target_and_settlement_raw_conversion_are_exact_at_precision_boundary(quantity, expected_raw):
    step = D("0.0000000000000000000000000001")
    a = replace(spec("fixture_cex_spot", "lead", "long"), step=step)
    b = replace(spec("fixture_cex_perp", "hedge", "short"), step=step)
    bounds = {
        a.leg_id: LegBound("BUY", D(0), quantity, a.quote_currency, D(20),
                           min_receive=D(0), min_receive_currency=a.quote_currency),
        b.leg_id: LegBound("SELL", D(0), quantity, b.quote_currency, D(20),
                           min_receive=D(0), min_receive_currency=b.quote_currency),
    }
    exact_plan = OperationPlan("exact-raw-target", "entry", (a, b), a.leg_id, D(2), D(2),
                               time.time() + 600, "floor", bounds,
                               {"owner": "fixture", "mode": "dry"})
    with localcontext() as decimal_context:
        decimal_context.prec = 28
        assert operation_roots._generic_target(exact_plan) == ("leg:lead", 28, expected_raw)
        assert generic_operations._raw(quantity, 28) == expected_raw
        assert quantity_units.native_to_raw(quantity, 28) == expected_raw


def test_native_to_raw_rejects_quantity_not_representable_at_frozen_scale():
    with pytest.raises(ValueError, match="represent"):
        quantity_units.native_to_raw(D("0.0000000000000000000000000001"), 27)
    with pytest.raises(store.StoreError, match="represented"):
        generic_operations._raw(D("0.0000000000000000000000000001"), 27)


def test_partial_pair_crash_reopen_preserves_exact_raw_target_and_settlement(tmp_path, monkeypatch):
    quantity = D("1.9999999999999999999999999999")
    expected_target = "19999999999999999999999999999"
    step = D("0.0000000000000000000000000001")
    a = replace(spec("fixture_cex_spot", "lead", "long"), step=step)
    b = replace(spec("fixture_cex_perp", "hedge", "short"), step=step)
    bounds = {
        a.leg_id: LegBound("BUY", D(0), quantity, a.quote_currency, D(20),
                           min_receive=D(0), min_receive_currency=a.quote_currency),
        b.leg_id: LegBound("SELL", D(0), quantity, b.quote_currency, D(20),
                           min_receive=D(0), min_receive_currency=b.quote_currency),
    }
    entry = OperationPlan("exact-raw-reopen", "entry", (a, b), a.leg_id, D(2), D(2),
                          time.time() + 600, "floor", bounds,
                          {"owner": "fixture", "mode": "dry"})
    con = store.connect(tmp_path / "trade.db")
    did = deal(con, entry, "DGEXACTRAWREOPEN")
    transports = (Transport(), Transport())
    for transport in transports:
        transport.clock = time.time()
    transports[0].fraction = D("0.5")
    coordinator = GenericOperationCoordinator(con, registry(), None)
    with localcontext() as decimal_context:
        decimal_context.prec = 28
        iid, nonce = coordinator.propose(deal=store.get_deal(con, did), plan=entry, profile_id="fixture")
    journals = [EventAttemptJournal(con, deal_id=did, intent_id=iid, operation_id=entry.operation_id,
                                    leg_id=leg.leg_id) for leg in entry.legs]
    coordinator.context = context(entry.legs, transports, journals)
    assert coordinator.approve(iid, nonce)
    monkeypatch.setattr(coordinator, "_apply_resolved",
                        lambda *_: (_ for _ in ()).throw(KeyboardInterrupt("crash before settlement")))
    with pytest.raises(KeyboardInterrupt), localcontext() as decimal_context:
        decimal_context.prec = 28
        coordinator.execute(iid)

    con, recovered = reopen_generic(tmp_path, con, did, entry, iid, transports)
    with localcontext() as decimal_context:
        decimal_context.prec = 28
        assert recovered.resume(iid).state == store.OpState.PARTIAL
    root = store.get_operation(con, entry.operation_id)
    assert (root["target_decimals"], root["target_raw"], root["confirmed_raw"], root["reserved_raw"]) == (
        28, expected_target, "10000000000000000000000000000", "0",
    )
    assert [len(transport.sent) for transport in transports] == [1, 1]


@pytest.mark.parametrize("fault", ("readonly_peer", "unhedged_cap"))
def test_pre_send_refusal_never_leaves_generic_root_running(tmp_path, fault):
    con, _did, p, iid, coordinator, transports, _ctx = approved_entry(
        tmp_path, operation_id="generic-refuse-" + fault,
        max_unhedged=D(".001") if fault == "unhedged_cap" else None,
    )
    if fault == "readonly_peer":
        transports[1].allow = False
    with pytest.raises(Exception):
        coordinator.execute(iid)
    assert store.get_operation(con, p.operation_id)["state"] != store.OpState.RUNNING
    assert store.get_intent(con, iid)["status"] != store.IntentStatus.RUNNING
    assert [len(item.sent) for item in transports] == [0, 0]


def test_generic_terminal_uses_shared_controller_end_event(tmp_path):
    con, did, p, iid, coordinator, _transports, _ctx = approved_entry(tmp_path, operation_id="generic-shared-end")
    assert coordinator.execute(iid).state == store.OpState.OPEN
    ends = [event for event in store.events(con, did) if event["kind"] == "operation_end"]
    assert len(ends) == 1
    payload = json.loads(ends[0]["json"])
    assert (payload["operation_id"], payload["deal_state"], payload["intent_state"]) == (
        p.operation_id, store.DealState.OPEN, store.IntentStatus.DONE)


def test_frozen_context_change_rolls_back_generic_final_root_transition(tmp_path, monkeypatch):
    con, did, p, iid, coordinator, transports, _ctx = approved_entry(tmp_path, operation_id="generic-frozen-final")
    apply = coordinator._apply_resolved

    def mutate_then_apply(*args):
        con.execute("UPDATE deals SET owner_json=? WHERE id=?", ('{"changed":true}', did))
        return apply(*args)

    monkeypatch.setattr(coordinator, "_apply_resolved", mutate_then_apply)
    with pytest.raises(store.StoreError, match="frozen context changed"):
        coordinator.execute(iid)
    root = store.get_operation(con, p.operation_id)
    # Both native receipts are durable, but the guarded transaction did not
    # consume their reserve or commit an OPEN terminal state on stale context.
    assert (root["state"], root["confirmed_raw"], root["reserved_raw"]) == (
        store.OpState.RUNNING, "0", "2000")
    assert [len(item.sent) for item in transports] == [1, 1]
    assert not [event for event in store.events(con, did) if event["kind"] == "operation_end"]


def test_recovery_frozen_context_change_rolls_back_settlement(tmp_path, monkeypatch):
    con, did, p, iid, coordinator, transports, _ctx = approved_entry(tmp_path, operation_id="generic-frozen-recover")
    transports[0].fail_after_send = True
    assert coordinator.execute(iid).state == store.OpState.PAUSED_UNKNOWN
    transports[0].fail_after_send = False
    con, recovered = reopen_generic(tmp_path, con, did, p, iid, transports)
    before = [event for event in store.events(con, did) if event["kind"] == "operation_end"]
    binding = recovered.context.for_leg(p.legs[0])
    resolve = binding.resolve

    def mutate_then_resolve(spec_, attempt_id):
        con.execute("UPDATE deals SET owner_json=? WHERE id=?", ('{"changed":true}', did))
        return resolve(spec_, attempt_id)

    monkeypatch.setattr(binding, "resolve", mutate_then_resolve)
    with pytest.raises(store.StoreError, match="frozen context changed"):
        recovered.resume(iid)
    root = store.get_operation(con, p.operation_id)
    assert (root["state"], root["confirmed_raw"], root["reserved_raw"]) == (
        store.OpState.PAUSED_UNKNOWN, "0", "2000")
    assert [event for event in store.events(con, did) if event["kind"] == "operation_end"] == before
    assert [len(item.sent) for item in transports] == [1, 0]


def test_result_event_keeps_dispatch_attempt_separate_from_native_order_id(tmp_path, monkeypatch):
    con, did, p, iid, coordinator, transports, ctx = approved_entry(tmp_path, operation_id="generic-native-ref")
    binding = ctx.for_leg(p.legs[0])
    submit = binding.submit

    def distinct_native_order(spec_, prepared):
        result = submit(spec_, prepared)
        distinct = replace(result, native_ref=NativeRef("venue_order", "venue-" + prepared.attempt_id))
        # The resolver receives the same receipt after an ACK loss/reopen.
        transports[0].receipts[prepared.attempt_id] = (distinct, prepared.quote.action.side)
        return distinct

    monkeypatch.setattr(binding, "submit", distinct_native_order)
    monkeypatch.setattr(coordinator, "_apply_resolved",
                        lambda *args: (_ for _ in ()).throw(KeyboardInterrupt("crash before terminal commit")))
    with pytest.raises(KeyboardInterrupt):
        coordinator.execute(iid)
    con, recovered = reopen_generic(tmp_path, con, did, p, iid, transports)
    assert recovered.resume(iid).state == store.OpState.OPEN
    dispatch = transports[0].sent[0][1]
    payloads = [json.loads(item["json"]) for item in store.events(con, did)
                if item["kind"] == EVENT_RESULT and json.loads(item["json"])["leg_id"] == p.legs[0].leg_id]
    assert payloads and all(payload["attempt_id"] == dispatch for payload in payloads)
    assert all((payload["native_ref_kind"], payload["native_ref_id"]) ==
               ("venue_order", "venue-" + dispatch) for payload in payloads)
    assert [row["executions"] for row in leg_accounting.rebuild(con, deal_id=did)["legs"]] == [1, 1]

@pytest.mark.parametrize("case", ("unknown", "stale", "foreign_perp"))
def test_new_generic_send_requires_fresh_native_book_before_any_submit(tmp_path, case):
    con, did, p, iid, coordinator, transports, ctx = approved_entry(tmp_path, operation_id="generic-native-book-" + case)
    if case == "unknown":
        from funding_bot.trade.adapters.contracts import Observation
        ctx.for_leg(p.legs[0]).observe = lambda _spec: Observation(None, time.time(), "fixture", "unknown")
    elif case == "stale":
        from funding_bot.trade.adapters.contracts import Observation
        ctx.for_leg(p.legs[1]).observe = lambda _spec: Observation(D(0), time.time() - 61, "fixture", "authoritative")
    else:
        transports[1].positions[p.legs[1].scope] = D(5)

    with pytest.raises(store.StoreError, match="native position"):
        coordinator.execute(iid)

    assert [len(item.sent) for item in transports] == [0, 0]
    assert store.get_operation(con, p.operation_id)["state"] == store.OpState.STOPPED
    assert store.get_intent(con, iid)["status"] == store.IntentStatus.FAILED


def test_stale_peer_quote_refuses_before_leading_native_send(tmp_path):
    con, did, p, iid, coordinator, transports, ctx = approved_entry(tmp_path, operation_id="generic-peer-stale-quote")
    binding = ctx.for_leg(p.legs[1])
    quote = binding.quote

    def stale_quote(spec_, action, bounds):
        return replace(quote(spec_, action, bounds), expires_at=time.time() - 1)

    binding.quote = stale_quote
    with pytest.raises(AdapterError, match="quote expired"):
        coordinator.execute(iid)

    assert [len(item.sent) for item in transports] == [0, 0]
    assert store.get_operation(con, p.operation_id)["state"] == store.OpState.STOPPED
    assert store.get_intent(con, iid)["status"] == store.IntentStatus.FAILED
