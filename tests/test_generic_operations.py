"""Generic two-leg root invariants beyond the adapter extension matrix."""
from dataclasses import replace
from decimal import Decimal as D
import json
import time

import pytest

from funding_bot.trade import reconcile, store
from funding_bot.trade.generic_operations import EventAttemptJournal, GenericOperationCoordinator
from funding_bot.trade.operation_plan import LegBound, OperationPlan
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
    with pytest.raises(store.StoreError, match="fingerprint"):
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
