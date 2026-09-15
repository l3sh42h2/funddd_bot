"""Durable coordinator for a frozen, venue-neutral two-leg :class:`OperationPlan`.

It deliberately does not know a chain, token address, exchange name, or fake
balance.  Venue work stays behind the existing ``Adapter`` contract.  The
database protocol is always write-ahead: a shared root reserve and a sent event
are committed before ``submit``; a later invocation resolves that attempt and
never submits it again.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import math
import time
import json
from types import SimpleNamespace
from typing import Any

from . import store
from .adapters.contracts import Action, AdapterError, ErrorKind, NativeRef, Prepared, Result, Status
from .operation_plan import LegBound, OperationPlan
from .operation_roots import _generic_identity, generic_propose, validate_generic_parent
from .operations import EndDecision, OperationController
from .coordinator import HedgeAction, HedgeProgram, LifecycleCoordinator, TwoLegProgram
from .quantity_units import (base_to_native_floor, copy_abs, copy_negate, exact_leg_rebuild, exact_sum,
                             native_to_base, native_to_raw, reconcile_owned_inventory)


READER = 5
EVENT_PREPARED = "generic_leg_prepared_v1"
EVENT_SENT = "generic_leg_sent_v1"
EVENT_DISPATCH = "generic_leg_dispatch_v1"
EVENT_RESULT = "generic_leg_result_v1"


class _GenericHalt(Exception):
    """Local callback stop after a durable terminal/pause transition."""


def _plan(raw: str) -> OperationPlan:
    try:
        return OperationPlan.from_json(raw)
    except (TypeError, ValueError) as exc:
        raise store.StoreError(f"frozen generic plan is invalid: {exc}") from None


def _raw(quantity: Decimal, decimals: int) -> int:
    if not isinstance(quantity, Decimal) or not quantity.is_finite() or quantity < 0:
        raise store.StoreError("generic quantity must be a finite non-negative Decimal")
    try:
        return native_to_raw(quantity, decimals)
    except ValueError:
        raise store.StoreError("generic quantity cannot be represented by its frozen root scale")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _prepared_payload(prepared: Prepared, operation_id: str, leg_id: str) -> dict[str, Any]:
    action = prepared.quote.action
    return {"version": 1, "operation_id": operation_id, "leg_id": leg_id,
            "attempt_id": prepared.attempt_id, "spec_hash": prepared.spec_hash,
            "quote_hash": prepared.quote.fingerprint, "action_id": action.action_id,
            "side": action.side, "quantity": str(action.quantity),
            "reduce_only": action.reduce_only}


class EventAttemptJournal:
    """A NativeAdapter binding journal backed solely by ``exec_events``.

    It is intended for generic CEX and synthetic adapters whose execution does
    not already have a native attempt table.  It writes public fingerprints,
    never quote-native payloads or credentials.  ``claim`` is intentionally
    single-use: a repeated submit must resolve the same durable attempt.
    """

    def __init__(self, con, *, deal_id: str, intent_id: str, operation_id: str, leg_id: str):
        self.con = con
        self.deal_id, self.intent_id = deal_id, intent_id
        self.operation_id, self.leg_id = operation_id, leg_id

    def _existing(self, kind: str, attempt_id: str) -> dict | None:
        rows = self.con.execute(
            "SELECT json FROM exec_events WHERE kind=? AND intent_id=? ORDER BY rowid",
            (kind, self.intent_id),
        ).fetchall()
        for (raw,) in rows:
            data = json.loads(raw or "{}")
            if data.get("attempt_id") == attempt_id:
                return data
        return None

    def prepare(self, prepared: Prepared) -> None:
        payload = _prepared_payload(prepared, self.operation_id, self.leg_id)
        with store.tx(self.con):
            store.require_reader(self.con, READER)
            old = self._existing(EVENT_PREPARED, prepared.attempt_id)
            if old is not None:
                if _canonical(old) != _canonical(payload):
                    raise AdapterError(ErrorKind.IDENTITY, "prepared generic attempt differs from durable proof")
                return
            store.event(self.con, EVENT_PREPARED, deal_id=self.deal_id, intent_id=self.intent_id, **payload)

    def claim(self, prepared: Prepared) -> None:
        payload = _prepared_payload(prepared, self.operation_id, self.leg_id)
        with store.tx(self.con):
            if self._existing(EVENT_PREPARED, prepared.attempt_id) != payload:
                raise AdapterError(ErrorKind.IDENTITY, "generic attempt has no matching prepared proof")
            if self._existing(EVENT_SENT, prepared.attempt_id) is not None:
                raise AdapterError(ErrorKind.UNKNOWN, "generic attempt was already admitted; resolve it")
            store.event(self.con, EVENT_SENT, deal_id=self.deal_id, intent_id=self.intent_id, **payload)


@dataclass(frozen=True)
class GenericExecution:
    operation_id: str
    intent_id: str
    leading_result: Result | None
    hedge_result: Result | None
    state: str


class GenericOperationCoordinator:
    """Execute one approved immutable two-leg plan through production adapters."""

    def __init__(self, con, registry, context):
        self.con, self.registry, self.context = con, registry, context
        self.lifecycle = OperationController(con)
        self.shared = LifecycleCoordinator(con)

    def propose(self, *, deal, plan: OperationPlan, profile_id: str, chat: int | None = None,
                operation_id: str | None = None) -> tuple[str, str]:
        return generic_propose(self.con, deal=deal, plan=plan, profile_id=profile_id,
                               chat=chat, operation_id=operation_id)

    def approve(self, intent_id: str, nonce: str, *, now: float | None = None) -> bool:
        return store.approve_intent(self.con, intent_id, nonce, now=now)

    def execute(self, intent_id: str) -> GenericExecution:
        """Admit an approved plan then run leading result → hedge result once.

        A sent attempt is resolved before any quote or submit.  Therefore the
        method is safe to call after a process crash; it cannot create a second
        external attempt with the same durable identity.
        """
        run, plan, op = self._load(intent_id, required=store.IntentStatus.APPROVED)
        if plan.expires_at <= time.time():
            raise store.StoreError("generic frozen plan has expired")
        pair = self.registry.compose(plan.legs[0], plan.legs[1], self.context)
        self._require_exit_ownership(run, plan)
        def program():
            if plan.kind == "rehedge":
                return self._run_rehedge(run, plan, op, pair)
            return self._run(run, plan, op, pair)

        # Generic plans use exactly the same admission/error boundary as EVM
        # and SOL.  ``propagate`` preserves the direct API's failure signal;
        # Engine remains free to report it at its outer boundary.
        return self.lifecycle.run_operation(
            run, program,
            paused=lambda stop: self._fail_after_admission(run, stop.text),
            refused=lambda _text: None,
            activate=lambda: self._activate_deal_admitted(run, plan),
            propagate=True,
        )

    def resume(self, intent_id: str) -> GenericExecution:
        """Resolve a previously sent generic attempt; this method never submits.

        After a resolved partial root has no reserve, a caller must create and
        approve a new frozen continuation plan.  That deliberately prevents a
        recovery call from gaining new quote/spend authority.
        """
        run, plan, op = self._load(intent_id, required=None)
        if op["state"] != store.OpState.PAUSED_UNKNOWN or op["reserved_raw"] == "0":
            raise store.StoreError("generic resume requires a paused unknown reserved root")
        pair = self.registry.compose(plan.legs[0], plan.legs[1], self.context)
        sent = self._sent_attempts(run.iid)
        if not sent:
            return self._recover_unsent(run, op)
        by_leg = {leg.leg_id: adapter for leg, adapter in zip(plan.legs, (pair.first, pair.second))}
        results: dict[str, Result] = {}
        for leg_id, attempt_id in sent.items():
            adapter = by_leg.get(leg_id)
            if adapter is None:
                raise store.StoreError("generic sent attempt names unknown leg")
            results[leg_id] = adapter.resolve(attempt_id)
            self._validate_result(next(leg for leg in plan.legs if leg.leg_id == leg_id),
                                  self._dispatched_action(run, plan, leg_id, attempt_id), results[leg_id])
        if any(result.status == Status.UNKNOWN or not result.terminal for result in results.values()):
            self._pause_unknown(run, "generic result remains unresolved")
            return GenericExecution(op["id"], run.iid, results.get(plan.leading_leg_id),
                                    results.get(self._hedge_leg(plan).leg_id), store.OpState.PAUSED_UNKNOWN)
        return self._recover_resolved(run, plan, op, results)

    def _recover_unsent(self, run, op) -> GenericExecution:
        """A crash before dispatch has a durable local proof of no external send."""
        def proof(_it, _deal, current):
            if current is None or current["state"] != store.OpState.PAUSED_UNKNOWN or self._sent_attempts(run.iid):
                raise store.StoreError("generic unsent recovery proof changed")

        def settle(current):
            store.operation_settle(self.con, current["id"], released_raw=int(current["reserved_raw"]), executed_raw=0)

        self._transition(run, (store.OpState.RUNNING, store.OpState.STOPPED), None,
                         reason="generic recovery before dispatch", recovery=True, settle=settle, proof=proof)
        return GenericExecution(op["id"], run.iid, None, None, store.OpState.STOPPED)

    def _recover_resolved(self, run, plan: OperationPlan, op: dict, results: dict[str, Result]) -> GenericExecution:
        """Apply terminal recovery without reopening the already-ended intent."""
        leading, hedge = (next(leg for leg in plan.legs if leg.leg_id == plan.leading_leg_id),
                          self._hedge_leg(plan))
        lead, hedge_result = results.get(leading.leg_id), results.get(hedge.leg_id)
        if plan.kind == "rehedge" and set(results) != {leading.leg_id}:
            raise store.StoreError("generic rehedge recovery result set differs from its single planned action")
        for leg, result in ((leading, lead), (hedge, hedge_result)):
            if result is not None:
                self._record_result(run, op, leg, plan.bounds[leg.leg_id].side, result)
        if plan.kind == "rehedge":
            tolerance_base = native_to_base(leading.step, leading.multiplier)
            balanced = (lead is not None and self._proven_execution(lead)
                        and copy_abs(self._parent_delta(run.did, plan)) <= tolerance_base)
        else:
            balanced = (lead is not None and hedge_result is not None and self._proven_execution(lead)
                        and self._proven_execution(hedge_result)
                        and self._balanced_book(op["id"], leading, hedge))
        def proof(_it, _deal, current):
            if current is None or current["state"] != store.OpState.PAUSED_UNKNOWN:
                raise store.StoreError("generic recovery root changed")
            if not all(result is not None and result.terminal and not result.provisional and
                       result.status != Status.UNKNOWN for result in results.values()):
                raise store.StoreError("generic recovery lacks terminal result proof")

        def settle(current):
            executed_raw = 0 if lead is None or lead.executed_quantity is None else _raw(
                lead.executed_quantity, int(current["target_decimals"]))
            store.operation_settle(self.con, current["id"], released_raw=int(current["reserved_raw"]),
                                   executed_raw=executed_raw)

        state = store.OpState.PAUSED_RISK
        reason = "generic recovery is unbalanced"
        if balanced:
            if plan.kind == "exit" and not self._parent_is_flat(run.did, plan):
                reason = "generic recovered exit leaves parent position"
            else:
                remaining = int(op["target_raw"]) - int(op["confirmed_raw"]) - (
                    0 if lead is None or lead.executed_quantity is None else _raw(lead.executed_quantity, int(op["target_decimals"])))
                state = (store.OpState.PARTIAL if remaining else
                         (store.OpState.CLOSED if plan.kind == "exit" else store.OpState.OPEN))
                reason = "generic recovered terminal result"
        self._transition(run, (store.OpState.RUNNING, state), None, reason=reason, recovery=True,
                         settle=settle, proof=proof)
        return GenericExecution(op["id"], run.iid, lead, hedge_result, state)

    def _load(self, intent_id: str, *, required):
        intent = store.get_intent(self.con, intent_id)
        if intent is None:
            raise LookupError(f"generic intent {intent_id} is missing")
        if required is not None and intent["status"] != required:
            raise store.StoreError(f"generic intent {intent_id} is not {required}")
        spec = json.loads(intent["spec_json"])
        if spec.get("generic_operation_v1") is not True:
            raise store.StoreError("intent is not a generic operation")
        plan = _plan(intent["plan_json"])
        op = store.operation_of_intent(self.con, intent_id)
        deal = store.get_deal(self.con, intent["deal_id"])
        if op is None or deal is None or op["id"] != plan.operation_id:
            raise store.StoreError("generic intent lost its durable root or deal")
        # Recheck the immutable generic parent before every execute/resume;
        # proposal-time validation cannot protect a later parent rewrite.
        deal = validate_generic_parent(self.con, deal["id"], plan)
        if spec.get("plan_fingerprint") != plan.fingerprint or op["inst_hash"] != "generic:" + _generic_identity(plan):
            raise store.StoreError("generic intent frozen plan identity changed")
        run = SimpleNamespace(iid=intent_id, did=deal["id"], kind=intent["kind"], it=intent,
                              deal=deal, op_id=op["id"], legs=SimpleNamespace(sim=bool(deal["sim"])))
        return run, plan, op

    def _activate_deal_admitted(self, run, plan: OperationPlan) -> None:
        """Advance entry/exit only inside OperationController's admission txn."""
        deal = store.get_deal(self.con, run.did)
        if deal is None:
            raise store.StoreError("generic deal disappeared before admission")
        if plan.kind == "entry" and deal["state"] == store.DealState.DRAFT:
            store.set_deal_state(self.con, run.did, store.DealState.ENTERING,
                                 expect=store.DealState.DRAFT)
        elif plan.kind == "exit" and deal["state"] == store.DealState.OPEN:
            store.set_deal_state(self.con, run.did, store.DealState.EXITING,
                                 expect=store.DealState.OPEN)

    def _require_exit_ownership(self, run, plan: OperationPlan) -> None:
        """Exit only quantities proven for this parent deal, across all roots."""
        if plan.kind != "exit":
            return
        book = {(row["leg_id"], row["spec_hash"]): Decimal(row["qty"])
                for row in exact_leg_rebuild(self.con, deal_id=run.did)["legs"]}
        for leg in plan.legs:
            quantity = book.get((leg.leg_id, leg.fingerprint), Decimal(0))
            owned = quantity if leg.direction == "long" else copy_negate(quantity)
            requested = native_to_base(plan.bounds[leg.leg_id].max_qty, leg.multiplier)
            if owned < requested:
                raise store.StoreError("generic exit exceeds proven parent-deal position")

    def _run(self, run, plan: OperationPlan, op: dict, pair) -> GenericExecution:
        """Run the common lead/apply/hedge/apply lifecycle for a generic pair."""
        leading = next(leg for leg in plan.legs if leg.leg_id == plan.leading_leg_id)
        hedge = self._hedge_leg(plan)
        adapters = {leading.leg_id: pair.first if pair.first.describe().leg_id == leading.leg_id else pair.second,
                    hedge.leg_id: pair.first if pair.first.describe().leg_id == hedge.leg_id else pair.second}
        lead_action = self._action(run, leading, plan.bounds[leading.leg_id], sequence=1,
                                   quantity=plan.bounds[leading.leg_id].max_qty)
        if native_to_base(lead_action.quantity, leading.multiplier) > plan.max_unhedged_exposure:
            raise store.StoreError("generic leading clip exceeds frozen transient unhedged exposure cap")
        planned_hedge = self._action(run, hedge, plan.bounds[hedge.leg_id], sequence=2,
                                     quantity=plan.bounds[hedge.leg_id].max_qty)
        self._observe_native_book(run.did, plan, adapters)
        # Quote both maximum approved legs before a leading native request.  A
        # later quote derives the exact hedge after the proven leading fill.
        self._preflight_quotes(run.did, ((adapters[leading.leg_id], leading,
                                          plan.bounds[leading.leg_id], lead_action),
                                         (adapters[hedge.leg_id], hedge,
                                          plan.bounds[hedge.leg_id], planned_hedge)))
        self._authorize_before_first_send(((adapters[leading.leg_id], leading, lead_action),
                                           (adapters[hedge.leg_id], hedge, planned_hedge)))
        lead_result: Result | None = None
        hedge_result: Result | None = None
        hedge_action: Action | None = None

        def submit_leading():
            if self._sent_attempts(run.iid).get(leading.leg_id) != lead_action.action_id:
                self._observe_native_book(run.did, plan, adapters)
            return self._submit_or_resolve(run, op, adapters[leading.leg_id], leading,
                                           plan.bounds[leading.leg_id], lead_action, reserve=True)

        def apply_leading(native_result):
            nonlocal lead_result
            lead_result = native_result
            self._validate_result(leading, lead_action, lead_result)
            if self._unknown(lead_result):
                self._pause_unknown(run, "leading leg outcome is unknown")
                raise _GenericHalt()
            self._record_result(run, op, leading, lead_action.side, lead_result)
            if not self._proven_execution(lead_result):
                self._finish_no_execution(run, op, lead_result)
                raise _GenericHalt()

        def submit_hedge():
            nonlocal hedge_action
            assert lead_result is not None
            hedge_qty = self._hedge_quantity(run.did, plan, hedge, leading)
            hedge_action = self._action(run, hedge, plan.bounds[hedge.leg_id], sequence=2, quantity=hedge_qty)
            try:
                if self._sent_attempts(run.iid).get(hedge.leg_id) != hedge_action.action_id:
                    self._observe_native_book(run.did, plan, adapters)
                return self._submit_or_resolve(run, op, adapters[hedge.leg_id], hedge,
                                               plan.bounds[hedge.leg_id], hedge_action, reserve=False)
            except Exception:
                if self._sent_attempts(run.iid).get(hedge.leg_id) == hedge_action.action_id:
                    self._pause_unknown(run, "generic hedge failed after durable dispatch")
                else:
                    self._pause_risk(run, op, lead_result, None, "generic hedge preparation failed")
                raise _GenericHalt()

        def apply_hedge(native_result):
            nonlocal hedge_result
            assert hedge_action is not None
            hedge_result = native_result
            self._validate_result(hedge, hedge_action, hedge_result)
            if self._unknown(hedge_result):
                self._pause_unknown(run, "hedge leg outcome is unknown")
                raise _GenericHalt()
            self._record_result(run, op, hedge, hedge_action.side, hedge_result)

        def finish(_lead, _hedge):
            assert lead_result is not None
            self._apply_resolved(run, plan, op, {leading.leg_id: lead_result, hedge.leg_id: hedge_result})

        try:
            self.shared.run_two_leg(TwoLegProgram(submit_leading, apply_leading, submit_hedge,
                                                  apply_hedge, finish))
        except _GenericHalt:
            current = store.operation_of_intent(self.con, run.iid)
            return GenericExecution(op["id"], run.iid, lead_result, hedge_result, current["state"])
        current = store.operation_of_intent(self.con, run.iid)
        return GenericExecution(op["id"], run.iid, lead_result, hedge_result, current["state"])

    def _run_rehedge(self, run, plan: OperationPlan, op: dict, pair) -> GenericExecution:
        """One bounded corrective leg from the proven parent book, through shared lifecycle."""
        leg = next(item for item in plan.legs if item.leg_id == plan.leading_leg_id)
        adapters = {item.leg_id: pair.first if pair.first.describe().leg_id == item.leg_id else pair.second
                    for item in plan.legs}
        adapter = adapters[leg.leg_id]
        bound = plan.bounds[leg.leg_id]
        result: Result | None = None
        action: Action | None = None

        def prepare():
            nonlocal action
            delta = self._parent_delta(run.did, plan)
            tolerance = native_to_base(leg.step, leg.multiplier)
            if copy_abs(delta) <= tolerance:
                self._stop_without_reserve(run, op, "generic rehedge is already within approved residual")
                raise _GenericHalt()
            side = "SELL" if delta > 0 else "BUY"
            if bound.side != side:
                raise store.StoreError("generic rehedge leading leg cannot reduce proven parent delta")
            qty = base_to_native_floor(copy_abs(delta), leg.multiplier, leg.step)
            if qty <= 0 or qty < bound.min_qty or qty > bound.max_qty:
                raise store.StoreError("generic rehedge correction lies outside frozen approved bound")
            existing = self._leg_parent_qty(run.did, leg)
            reduces_existing = (side == "BUY" and existing < 0) or (side == "SELL" and existing > 0)
            if reduces_existing and native_to_base(qty, leg.multiplier) > copy_abs(existing):
                raise store.StoreError("generic rehedge would cross the proven leg position through zero")
            increases_direction = (side == "BUY" and existing >= 0) or (side == "SELL" and existing <= 0)
            if increases_direction and ((side == "BUY" and leg.direction != "long") or
                                        (side == "SELL" and leg.direction != "short")):
                raise store.StoreError("generic rehedge would increase an unsupported leg direction")
            if bound.reduce_only != reduces_existing:
                raise store.StoreError("generic rehedge reduce-only authorization differs from proven position")
            action = self._action(run, leg, bound, sequence=1, quantity=qty)
            self._observe_native_book(run.did, plan, adapters)
            self._authorize_before_first_send(((adapter, leg, action),))
            return HedgeAction(action.side, action.quantity, action.reduce_only)

        def submit(_clip_id, _hedge_action):
            assert action is not None
            if self._sent_attempts(run.iid).get(leg.leg_id) != action.action_id:
                self._observe_native_book(run.did, plan, adapters)
            return self._submit_or_resolve(run, op, adapter, leg, bound, action, reserve=True)

        def apply(_clip_id, _hedge_action, native_result):
            nonlocal result
            assert action is not None
            result = native_result
            self._validate_result(leg, action, result)
            if self._unknown(result):
                self._pause_unknown(run, "generic rehedge outcome is unknown")
                raise _GenericHalt()
            self._record_result(run, op, leg, action.side, result)
            tolerance = native_to_base(leg.step, leg.multiplier)
            if not self._proven_execution(result) or copy_abs(self._parent_delta(run.did, plan)) > tolerance:
                self._pause_risk(run, op, result, None, "generic rehedge remains outside approved exposure")
                raise _GenericHalt()

        def verify():
            return None

        def finish(_action):
            current = store.get_operation(self.con, op["id"])
            if result is None or current is None:
                raise store.StoreError("generic rehedge finished without a result/root")
            raw = 0 if result.executed_quantity is None else _raw(result.executed_quantity, int(current["target_decimals"]))
            remaining = int(current["target_raw"]) - int(current["confirmed_raw"]) - raw
            if remaining < 0:
                raise store.StoreError("generic rehedge exceeds frozen root target")
            state = store.OpState.OPEN if remaining == 0 else store.OpState.PARTIAL

            def proof(_it, _deal, _root):
                if not self._proven_execution(result):
                    raise store.StoreError("generic rehedge final transition lacks terminal proof")

            def settle(root):
                store.operation_settle(self.con, root["id"], released_raw=int(root["reserved_raw"]), executed_raw=raw)

            self._transition(run, (state,),
                             store.IntentStatus.DONE if state == store.OpState.OPEN else store.IntentStatus.PARTIAL,
                             reason="generic rehedge applied", settle=settle, proof=proof)

        try:
            self.shared.run_hedge(HedgeProgram(run.iid, prepare, submit, apply, verify, finish))
        except _GenericHalt:
            return GenericExecution(op["id"], run.iid, result, None, store.operation_of_intent(self.con, run.iid)["state"])
        final = store.operation_of_intent(self.con, run.iid)
        return GenericExecution(op["id"], run.iid, result, None, final["state"])

    @staticmethod
    def _hedge_leg(plan: OperationPlan):
        return next(leg for leg in plan.legs if leg.leg_id != plan.leading_leg_id)

    def _action(self, run, leg, bound: LegBound, *, sequence: int, quantity: Decimal) -> Action:
        if quantity < bound.min_qty or quantity > bound.max_qty:
            raise store.StoreError("generic action quantity lies outside its frozen leg bounds")
        action_id = hashlib.sha256(f"{run.op_id}:{run.iid}:{leg.leg_id}:{sequence}".encode()).hexdigest()[:40]
        return Action(action_id, leg.leg_id, bound.side, quantity, bound.reduce_only)

    def _hedge_quantity(self, deal_id: str, plan: OperationPlan, hedge, leading) -> Decimal:
        # The accounting projection includes a proven spot base fee.  Deriving
        # from the Result quantity alone would leave an unhedged inventory.
        exposure = copy_abs(self._parent_delta(deal_id, plan))
        if exposure == 0:
            raise store.StoreError("leading generic result did not create a hedgeable parent delta")
        quantity = base_to_native_floor(exposure, hedge.multiplier, hedge.step)
        if quantity <= 0:
            raise store.StoreError("proven leading exposure is below hedge precision; automatic under-hedge refused")
        return quantity

    def _observe_native_book(self, deal_id: str, plan: OperationPlan, adapters: dict[str, Any]) -> dict[str, Any]:
        """Require fresh authoritative venue inventory before every new send.

        Perpetual scopes cannot contain a personal, unjournaled position, so
        their native quantity is exact.  A spot wallet can contain independent
        assets; it must at least cover the parent-deal quantity, while all
        generic sell sizing remains bounded by the journal-owned quantity.
        """
        now = time.time()
        observations: dict[str, Any] = {}
        for leg in plan.legs:
            adapter = adapters.get(leg.leg_id)
            if adapter is None:
                raise store.StoreError("generic adapter map misses frozen leg")
            try:
                observation = adapter.observe()
            except Exception as exc:
                raise store.StoreError("generic native position observation is unavailable") from exc
            quantity = getattr(observation, "quantity", None)
            as_of = getattr(observation, "as_of", None)
            quality = getattr(observation, "quality", None)
            if (not isinstance(quantity, Decimal) or not quantity.is_finite() or
                    isinstance(as_of, bool) or not isinstance(as_of, (int, float)) or not math.isfinite(as_of) or
                    abs(now - as_of) > 60 or quality not in {"authoritative", "confirmed", "finalized"}):
                raise store.StoreError("generic native position observation is not fresh authoritative proof")
            expected = self._leg_parent_qty(deal_id, leg)
            if not expected.is_finite():
                raise store.StoreError("generic parent inventory is not finite")
            coverage = reconcile_owned_inventory(
                market_kind=leg.capabilities.market_kind, observed_qty_native=quantity,
                owned_exposure_base=expected, multiplier=leg.multiplier,
            )
            if not coverage.matched:
                if leg.capabilities.market_kind == "perpetual":
                    raise store.StoreError("generic perpetual native position differs from parent journal")
                raise store.StoreError("generic spot balance does not cover parent journal inventory")
            observations[leg.leg_id] = observation
        return observations

    def _preflight_quotes(self, deal_id: str, items) -> None:
        """Validate both maximum frozen actions before the leading send.

        ``Observation.available`` has no unit/initial-margin semantics for
        perpetuals, so this method never treats notional as margin.  Native
        authorization remains the authoritative margin policy.  Spot cash is
        compared only where its observation states the exact spend currency.
        """
        for adapter, leg, bound, action in items:
            quote = adapter.quote(action, self._bounds(bound))
            self._validate_quote(quote, action, bound, leg, self._leg_parent_qty(deal_id, leg))
            observation = adapter.observe()
            available, currency = observation.available, observation.currency
            if leg.capabilities.market_kind == "spot" and available is not None and currency == quote.spend_currency:
                if not isinstance(available, Decimal) or not available.is_finite() or available < quote.max_spend:
                    raise store.StoreError("generic spot available balance does not cover frozen quote spend")

    @staticmethod
    def _authorize_before_first_send(items) -> None:
        """Check both frozen legs before the leading native request is admitted."""
        for adapter, leg, action in items:
            authorize = getattr(getattr(adapter, "bindings", None), "authorize", None)
            if not callable(authorize):
                raise store.StoreError("generic adapter lacks pre-submit authorization capability")
            authorize(leg, action)

    def _leg_parent_qty(self, deal_id: str, leg) -> Decimal:
        values = {(row["leg_id"], row["spec_hash"]): Decimal(row["qty"])
                  for row in exact_leg_rebuild(self.con, deal_id=deal_id)["legs"]}
        return values.get((leg.leg_id, leg.fingerprint), Decimal(0))

    def _parent_delta(self, deal_id: str, plan: OperationPlan) -> Decimal:
        return exact_sum(self._leg_parent_qty(deal_id, leg) for leg in plan.legs)

    def _submit_or_resolve(self, run, op, adapter, leg, bound, action, *, reserve: bool) -> Result:
        sent = self._sent_attempts(run.iid)
        attempt_id = action.action_id
        if sent.get(leg.leg_id) == attempt_id:
            return adapter.resolve(attempt_id)
        quote = adapter.quote(action, self._bounds(bound))
        self._validate_quote(quote, action, bound, leg, self._leg_parent_qty(run.did, leg))
        self._first_dispatch_deadline(run, reserve)
        with store.tx(self.con):
            current = store.get_operation(self.con, op["id"])
            if current is None or current["state"] != store.OpState.RUNNING:
                raise store.StoreError("generic root is no longer running")
            if reserve:
                store.operation_reserve(self.con, op["id"], store.operation_remaining(current))
        try:
            prepared = adapter.prepare(attempt_id, quote)
            if not isinstance(prepared, Prepared) or prepared.attempt_id != attempt_id:
                raise store.StoreError("adapter returned a different generic prepared attempt")
            self._first_dispatch_deadline(run, reserve)
        except Exception:
            if reserve:
                self._release_unsent(run, op, "generic prepare failed")
            raise
        with store.tx(self.con):
            # Written before submit even for adapters with their own native
            # journal.  A crash in the tiny window is conservatively resolved.
            store.event(self.con, EVENT_DISPATCH, deal_id=run.did, intent_id=run.iid,
                        operation_id=op["id"], leg_id=leg.leg_id, attempt_id=attempt_id,
                        spec_hash=leg.fingerprint, quote_hash=quote.fingerprint,
                        quantity=str(action.quantity), side=action.side, reduce_only=action.reduce_only)
        return adapter.submit(prepared)

    @staticmethod
    def _first_dispatch_deadline(run, first: bool) -> None:
        # A fresh venue quote cannot extend the owner's plan authorization.
        # Once the leading action was committed, the bounded hedge is still
        # needed to reduce the exposure even if the entry deadline has passed.
        if first and _plan(run.it['plan_json']).expires_at <= time.time():
            raise store.StoreError('generic first dispatch approval expired during preflight')

    @staticmethod
    def _bounds(bound: LegBound) -> dict[str, Any]:
        return {"min_qty": bound.min_qty, "max_qty": bound.max_qty, "max_spend": bound.max_spend,
                "quote_currency": bound.quote_currency, "reduce_only": bound.reduce_only}

    @staticmethod
    def _base_currency(leg) -> str:
        """The exact asset a spot action acquires or spends on this frozen leg."""
        return leg.instrument if leg.capabilities.venue_kind == "dex" else leg.asset_id

    @classmethod
    def _validate_quote(cls, quote, action: Action, bound: LegBound, leg, owned_quantity: Decimal) -> None:
        if quote.action != action:
            raise store.StoreError("generic quote action differs from frozen action")
        if quote.expires_at <= __import__("time").time():
            raise store.StoreError("generic quote has expired")
        spot = leg.capabilities.market_kind == "spot"
        base_currency = cls._base_currency(leg) if spot else None
        if action.side == "BUY":
            if quote.spend_currency != bound.quote_currency:
                raise store.StoreError("generic buy quote spend currency differs from frozen quote currency")
            if quote.max_spend > bound.max_spend:
                raise store.StoreError("generic quote exceeds approved quote budget")
            if spot and quote.receive_currency != base_currency:
                raise store.StoreError("generic buy quote receive asset differs from frozen spot asset")
            return
        if action.side != "SELL":
            raise store.StoreError("generic quote side is invalid")
        if spot:
            if quote.spend_currency != base_currency:
                raise store.StoreError("generic sell quote spend asset differs from frozen spot asset")
            if quote.max_spend > action.quantity or quote.max_spend > bound.max_qty:
                raise store.StoreError("generic sell quote exceeds approved base quantity")
            if native_to_base(quote.max_spend, leg.multiplier) > owned_quantity:
                raise store.StoreError("generic sell quote exceeds proven parent spot inventory")
        elif quote.spend_currency == bound.quote_currency and quote.max_spend > bound.max_spend:
            raise store.StoreError("generic quote exceeds approved quote budget")
        elif quote.spend_currency != bound.quote_currency:
            raise store.StoreError("generic buy quote spend currency differs from frozen quote currency")
        if bound.min_receive_currency is None:
            raise store.StoreError("generic sell lacks explicit approved minimum proceeds currency")
        if quote.receive_currency != bound.min_receive_currency or quote.min_receive < bound.min_receive:
            raise store.StoreError("generic sell quote minimum proceeds exceed approval")

    def _sent_attempts(self, intent_id: str) -> dict[str, str]:
        rows = self.con.execute("SELECT json FROM exec_events WHERE kind=? AND intent_id=? ORDER BY rowid",
                                (EVENT_DISPATCH, intent_id)).fetchall()
        found: dict[str, str] = {}
        for (raw,) in rows:
            event = json.loads(raw or "{}")
            leg_id, attempt_id = event.get("leg_id"), event.get("attempt_id")
            if not isinstance(leg_id, str) or not isinstance(attempt_id, str):
                raise store.StoreError("generic sent evidence is malformed")
            previous = found.setdefault(leg_id, attempt_id)
            if previous != attempt_id:
                raise store.StoreError("generic leg has conflicting sent attempts")
        return found

    def _dispatched_action(self, run, plan: OperationPlan, leg_id: str, attempt_id: str) -> Action:
        row = self.con.execute("SELECT json FROM exec_events WHERE kind=? AND intent_id=? ORDER BY rowid DESC",
                               (EVENT_DISPATCH, run.iid)).fetchall()
        for (raw,) in row:
            event = json.loads(raw or "{}")
            if event.get("leg_id") != leg_id or event.get("attempt_id") != attempt_id:
                continue
            try:
                action = Action(attempt_id, leg_id, event["side"], Decimal(event["quantity"]),
                                bool(event["reduce_only"]))
            except (KeyError, ValueError, TypeError):
                raise store.StoreError("generic dispatch evidence has invalid action") from None
            leg = next(item for item in plan.legs if item.leg_id == leg_id)
            action.validate(leg)
            return action
        raise store.StoreError("generic dispatch evidence has no action")

    @staticmethod
    def _unknown(result: Result) -> bool:
        return result.status == Status.UNKNOWN or not result.terminal or result.provisional

    @staticmethod
    def _proven_execution(result: Result) -> bool:
        return (result.terminal and not result.provisional and result.executed_quantity is not None and
                result.executed_quantity > 0 and result.status in {Status.SETTLED, Status.PARTIAL, Status.CANCELLED})

    @staticmethod
    def _validate_result(leg, action: Action, result: Result) -> None:
        if result.status == Status.UNKNOWN:
            return
        if result.leg_id != leg.leg_id or result.spec_hash != leg.fingerprint or tuple(result.scope or ()) != tuple(leg.scope):
            raise store.StoreError("generic result identity differs from frozen leg")
        quantity = result.executed_quantity
        if quantity is not None and quantity > action.quantity:
            raise store.StoreError("generic result exceeds submitted frozen quantity")

    def _record_result(self, run, op, leg, side: str, result: Result) -> None:
        if not result.terminal or result.provisional or result.status == Status.UNKNOWN:
            raise store.StoreError("unknown result cannot be applied")
        attempt_id = self._sent_attempts(run.iid).get(leg.leg_id)
        if attempt_id is None:
            raise store.StoreError("generic result has no dispatched attempt identity")
        if result.native_ref is None:
            raise store.StoreError("generic result has no native receipt identity")
        from .leg_accounting import record_result
        with store.tx(self.con):
            record_result(self.con, result, leg, operation_id=op["id"], side=side,
                          deal_id=run.did, intent_id=run.iid)
            store.event(self.con, EVENT_RESULT, deal_id=run.did, intent_id=run.iid,
                        operation_id=op["id"], leg_id=leg.leg_id, attempt_id=attempt_id,
                        native_ref_kind=result.native_ref.kind, native_ref_id=result.native_ref.id,
                        status=str(result.status), executed_quantity=None if result.executed_quantity is None
                        else str(result.executed_quantity), terminal=True)

    def _apply_resolved(self, run, plan: OperationPlan, op: dict, results: dict[str, Result]) -> GenericExecution:
        leading = next(leg for leg in plan.legs if leg.leg_id == plan.leading_leg_id)
        hedge = self._hedge_leg(plan)
        lead = results.get(leading.leg_id)
        hedge_result = results.get(hedge.leg_id)
        if lead is None or self._unknown(lead) or (hedge_result is not None and self._unknown(hedge_result)):
            self._pause_unknown(run, "generic resolve remains unknown")
            return GenericExecution(op["id"], run.iid, lead, hedge_result, store.OpState.PAUSED_UNKNOWN)
        # A crash may happen after native finality and before the fact write.
        # record_result deduplicates by the result native reference, so both the
        # normal path and recovery can invoke it safely.
        for leg, result in ((leading, lead), (hedge, hedge_result)):
            if result is not None:
                self._record_result(run, op, leg, plan.bounds[leg.leg_id].side, result)
        if hedge_result is None or not self._proven_execution(lead) or not self._proven_execution(hedge_result):
            return self._pause_risk(run, op, lead, hedge_result, "generic pair is not proven balanced")
        if not self._balanced_book(op["id"], leading, hedge):
            return self._pause_risk(run, op, lead, hedge_result, "generic proven exposures differ")
        root_scale = int(op["target_decimals"])
        executed_raw = _raw(lead.executed_quantity, root_scale)
        current = store.get_operation(self.con, op["id"])
        if current is None or current["reserved_raw"] == "0":
            raise store.StoreError("generic pair has no reserve to settle")
        remaining = int(current["target_raw"]) - int(current["confirmed_raw"]) - executed_raw
        if remaining < 0:
            raise store.StoreError("generic pair exceeds frozen root target")
        if remaining == 0 and plan.kind == "exit" and not self._parent_is_flat(run.did, plan):
            return self._pause_risk(run, op, lead, hedge_result, "generic exit leaves proven parent position")
        state = (store.OpState.PARTIAL if remaining else
                 (store.OpState.CLOSED if plan.kind == "exit" else store.OpState.OPEN))
        intent_state = store.IntentStatus.PARTIAL if state == store.OpState.PARTIAL else store.IntentStatus.DONE

        def proof(_it, _deal, _root):
            if not self._proven_execution(lead) or not self._proven_execution(hedge_result):
                raise store.StoreError("generic final transition lacks terminal two-leg proof")

        def settle(root):
            store.operation_settle(self.con, root["id"], released_raw=int(root["reserved_raw"]),
                                   executed_raw=executed_raw)

        self._transition(run, (state,), intent_state,
                         reason="generic partial pair" if state == store.OpState.PARTIAL else None,
                         settle=settle, proof=proof)
        return GenericExecution(op["id"], run.iid, lead, hedge_result, state)

    def _finish_no_execution(self, run, op, result):
        def settle(root):
            store.operation_settle(self.con, root["id"], released_raw=int(root["reserved_raw"]), executed_raw=0)
        self._transition(run, (store.OpState.STOPPED,), store.IntentStatus.FAILED,
                         reason="generic leading leg did not execute", settle=settle)
        return GenericExecution(op["id"], run.iid, result, None, store.OpState.STOPPED)

    def _release_unsent(self, run, op, reason: str) -> None:
        """Prepare failed before a dispatch event, so releasing this reserve is proven safe."""
        current = store.get_operation(self.con, op["id"])
        if current is None or current["state"] != store.OpState.RUNNING:
            return

        def proof(_it, _deal, _root):
            if self._sent_attempts(run.iid):
                raise store.StoreError("generic unsent release has dispatch evidence")

        def settle(root):
            if root["reserved_raw"] != "0":
                store.operation_settle(self.con, root["id"], released_raw=int(root["reserved_raw"]), executed_raw=0)

        self._transition(run, (store.OpState.STOPPED,), store.IntentStatus.FAILED,
                         reason=reason, settle=settle, proof=proof)

    def _fail_after_admission(self, run, reason: str) -> None:
        """A synchronous refusal cannot leave an admitted root running.

        Once a durable dispatch or reserve exists, the same error is uncertain
        rather than a safe rollback and recovery must resolve it first.
        """
        op = store.operation_of_intent(self.con, run.iid)
        if op is None or op["state"] != store.OpState.RUNNING:
            return
        if op["reserved_raw"] != "0" or self._sent_attempts(run.iid):
            self._pause_unknown(run, reason)
            return
        self._release_unsent(run, op, reason)

    def _stop_without_reserve(self, run, op, reason: str) -> None:
        def proof(_it, _deal, current):
            if current is None or current["state"] != store.OpState.RUNNING or current["reserved_raw"] != "0":
                raise store.StoreError("generic no-action root is not safely stoppable")
        self._transition(run, (store.OpState.STOPPED,), store.IntentStatus.FAILED, reason=reason, proof=proof)

    def _balanced_book(self, operation_id: str, leading, hedge) -> bool:
        values = {(row["leg_id"], row["spec_hash"]): Decimal(row["qty"])
                  for row in exact_leg_rebuild(self.con, operation_id=operation_id)["legs"]}
        lead = values.get((leading.leg_id, leading.fingerprint))
        paired = values.get((hedge.leg_id, hedge.fingerprint))
        return lead is not None and paired is not None and exact_sum((lead, paired)) == 0

    def _parent_is_flat(self, deal_id: str, plan: OperationPlan) -> bool:
        values = {(row["leg_id"], row["spec_hash"]): Decimal(row["qty"])
                  for row in exact_leg_rebuild(self.con, deal_id=deal_id)["legs"]}
        return all(values.get((leg.leg_id, leg.fingerprint), Decimal(0)) == 0 for leg in plan.legs)

    def _pause_unknown(self, run, reason: str) -> None:
        intent = store.get_intent(self.con, run.iid)
        if intent is None:
            raise store.StoreError("generic unknown intent disappeared")
        recovering = intent["status"] != store.IntentStatus.RUNNING
        self._transition(run, (store.OpState.PAUSED_UNKNOWN,),
                         None if recovering else store.IntentStatus.PARTIAL,
                         reason=reason, recovery=recovering)

    def _pause_risk(self, run, op, lead, hedge, reason: str) -> GenericExecution:
        def settle(root):
            if root["reserved_raw"] != "0":
                # Known terminal results may release the admission reserve; the
                # persisted facts retain the unhedged exposure for manual action.
                executed_raw = 0 if lead is None or lead.executed_quantity is None else _raw(
                    lead.executed_quantity, int(root["target_decimals"]))
                store.operation_settle(self.con, root["id"], released_raw=int(root["reserved_raw"]),
                                       executed_raw=executed_raw)
        self._transition(run, (store.OpState.PAUSED_RISK,), store.IntentStatus.PARTIAL,
                         reason=reason, settle=settle)
        return GenericExecution(op["id"], run.iid, lead, hedge, store.OpState.PAUSED_RISK)

    def _transition(self, run, root_states, intent_state, *, reason=None, fields=None,
                    recovery: bool = False, settle=None, proof=None) -> None:
        """Use the shared frozen-context terminal transaction for every generic path."""
        final_root = root_states[-1]
        if final_root == store.OpState.OPEN:
            deal_state = store.DealState.OPEN
        elif final_root == store.OpState.CLOSED:
            deal_state = store.DealState.CLOSED
        else:
            deal_state = store.DealState.PAUSED
        states = ((store.IntentStatus.RUNNING,) if not recovery else
                  (store.IntentStatus.RUNNING, store.IntentStatus.PARTIAL, store.IntentStatus.INTERRUPTED))
        self.lifecycle.commit_transition(
            run, EndDecision(deal_state, intent_state, tuple(root_states), fields=fields, reason=reason,
                             error=reason),
            intent_statuses=states, settle=settle, proof=proof,
        )
