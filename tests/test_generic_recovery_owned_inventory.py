"""Recovery reconciles journal-owned inventory without claiming wallet surplus."""
from dataclasses import replace
from decimal import Decimal as D
import time

from funding_bot.trade import generic_recovery, leg_accounting, store
from funding_bot.trade.generic_operations import EventAttemptJournal, GenericOperationCoordinator
from funding_bot.trade.operation_plan import LegBound, OperationPlan
from common_adapter_fixtures import Transport, context, registry, spec


def _plan(a, b, *, operation_id, kind="entry"):
    bounds = {}
    for leg in (a, b):
        side = "BUY" if leg.direction == "long" else "SELL"
        if kind == "exit":
            side = "SELL" if side == "BUY" else "BUY"
        bounds[leg.leg_id] = LegBound(
            side, D(0), D(2) / leg.multiplier, leg.quote_currency, D(20),
            reduce_only=kind == "exit" and leg.capabilities.market_kind == "perpetual",
            min_receive=D(0), min_receive_currency=leg.quote_currency,
        )
    return OperationPlan(operation_id, kind, (a, b), a.leg_id, D(2), D(2), time.time() + 600,
                         "floor", bounds, {"owner": "fixture", "mode": "dry"})


def _context_factory(plan, transports):
    def factory(con, intent, deal, _frozen):
        journals = [EventAttemptJournal(con, deal_id=deal["id"], intent_id=intent["id"],
                                        operation_id=plan.operation_id, leg_id=leg.leg_id)
                    for leg in plan.legs]
        return context(plan.legs, transports, journals)
    return factory


def _open(tmp_path):
    con = store.connect(tmp_path / "trade.db")
    a = spec("fixture_cex_spot", "lead", "long")
    b = spec("fixture_cex_perp", "hedge", "short")
    entry = _plan(a, b, operation_id="generic-owned-entry")
    did = store.create_generic_deal(
        con, asset_id=a.asset_id,
        position_spec={"generic_position_v1": True, "legs": entry.to_dict()["legs"]},
        owner_json="{}", sim=True, deal_id="DGOWNED",
    )
    transports = (Transport(), Transport())
    for transport in transports:
        transport.clock = time.time()
    coordinator = GenericOperationCoordinator(con, registry(), None)
    iid, nonce = coordinator.propose(deal=store.get_deal(con, did), plan=entry, profile_id="fixture")
    coordinator.context = _context_factory(entry, transports)(con, store.get_intent(con, iid),
                                                              store.get_deal(con, did), {})
    assert coordinator.approve(iid, nonce)
    assert coordinator.execute(iid).state == store.OpState.OPEN
    return con, did, entry, coordinator, transports


def test_spot_wallet_surplus_reconciles_but_stays_personal_and_outside_exit(tmp_path):
    con, did, entry, coordinator, transports = _open(tmp_path)
    spot, perp = entry.legs
    transports[0].positions[spot.scope] = D(7)
    check = generic_recovery.check(
        con, store.get_deal(con, did), registry=registry(),
        context_factory=_context_factory(entry, transports), now=transports[0].clock,
    )
    assert check.matched is True
    assert check.personal_surplus_base == ((spot.leg_id, D(5)),)
    assert "личный спотовый избыток: lead=5 базовых ед." in check.detail
    assert {row["leg_id"]: D(row["qty"]) for row in leg_accounting.rebuild(con, deal_id=did)["legs"]} == {
        spot.leg_id: D(2), perp.leg_id: D(-2),
    }

    exit_plan = _plan(spot, perp, operation_id="generic-owned-exit", kind="exit")
    iid, nonce = coordinator.propose(deal=store.get_deal(con, did), plan=exit_plan, profile_id="fixture")
    coordinator.context = _context_factory(exit_plan, transports)(con, store.get_intent(con, iid),
                                                                  store.get_deal(con, did), {})
    assert coordinator.approve(iid, nonce)
    assert coordinator.execute(iid).state == store.OpState.CLOSED
    assert transports[0].sent[-1][2].quantity == D(2)
    assert transports[0].positions[spot.scope] == D(5)
    assert all(D(row["qty"]) == 0 for row in leg_accounting.rebuild(con, deal_id=did)["legs"])


def test_spot_wallet_deficit_mismatches_while_perp_remains_exact(tmp_path):
    con, did, entry, _coordinator, transports = _open(tmp_path)
    spot, perp = entry.legs
    transports[0].positions[spot.scope] = D(1)
    check = generic_recovery.check(
        con, store.get_deal(con, did), registry=registry(),
        context_factory=_context_factory(entry, transports), now=transports[0].clock,
    )
    assert check.matched is False and check.personal_surplus_base == ()

    transports[0].positions[spot.scope] = D(7)
    transports[1].positions[perp.scope] = D(-3)
    check = generic_recovery.check(
        con, store.get_deal(con, did), registry=registry(),
        context_factory=_context_factory(entry, transports), now=transports[0].clock,
    )
    assert check.matched is False


def test_recovery_uses_total_spot_quantity_not_reduced_available_balance(tmp_path):
    con, did, entry, _coordinator, transports = _open(tmp_path)
    spot = entry.legs[0]
    transports[0].positions[spot.scope] = D(7)

    def factory(connection, intent, deal, frozen):
        ctx = _context_factory(entry, transports)(connection, intent, deal, frozen)
        binding = ctx.for_leg(spot)
        observe = binding.observe
        binding.observe = lambda frozen_spot: replace(
            observe(frozen_spot), available=D(1), currency=spot.asset_id,
        )
        return ctx

    check = generic_recovery.check(
        con, store.get_deal(con, did), registry=registry(), context_factory=factory,
        now=transports[0].clock,
    )
    assert check.matched is True
    assert check.personal_surplus_base == ((spot.leg_id, D(5)),)
