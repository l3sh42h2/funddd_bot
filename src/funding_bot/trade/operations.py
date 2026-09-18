"""Common journal transitions. Native executors supply only proven outcomes.

The existing clip is the durable action identity and existing operations remain
the budget authority. No parallel operation or reservation journal is created.
All clip + budget mutations occur in the caller's receipt transaction or one
short IMMEDIATE transaction. No network calls belong in these transactions.
"""
from dataclasses import dataclass
import json
from typing import Callable, Any
from . import store
from .store import ClipState as C


@dataclass(frozen=True)
class SpotSettlement:
    executed: bool
    input_raw: int = 0
    output_raw: int = 0

    def __post_init__(self):
        if type(self.executed) is not bool:
            raise ValueError('settlement requires an explicit proven execution verdict')
        for value in (self.input_raw, self.output_raw):
            if type(value) is not int or value < 0:
                raise ValueError('settlement quantities must be nonnegative raw integers')
        if not self.executed and (self.input_raw or self.output_raw):
            raise ValueError('non-execution cannot carry token flows')


@dataclass
class ClipLifecycle:
    """Ports for the common ordered lifecycle, never an entire venue flow.

    Venue preparation/settlement stays behind individual ports. In particular,
    hedge follows the proven spot effect even if owner pause arrived meanwhile.
    Guard applies before a NEW clip; the hedge port owns completion of its pair.
    """
    intent_id: str
    amounts: list[int]
    progress: Callable[[int, int], None]
    guard: Callable[[], None]
    select_amount: Callable[[int, bool], int]
    prepare: Callable[[int, bool], Any]
    spot: Callable[[int, int, Any], None]
    hedge: Callable[[int, bool, Any], None]
    settle: Callable[[int, Any], None]
    next_amounts: Callable[[int, int, list[int], Any], list[int]]
    finish: Callable[[], None]


@dataclass(frozen=True)
class EndDecision:
    deal_state: str
    # Recovery may settle a root while preserving an already-interrupted
    # intent.  Normal callers always supply a terminal IntentStatus.
    intent_state: str | None
    root_states: tuple[str, ...] = ()
    fields: dict | None = None
    reason: str | None = None
    error: str | None = None


class OperationController:
    def __init__(self, connection):
        self.con = connection

    def admit(self, run, *, activate: Callable[[], None] | None = None):
        """One CAS owns admission; both intent and root become running or neither."""
        from .adapters.native_journal import exclusive_transaction
        from .adapters.obligations import require_resolved
        from .operation_roots import start_linked
        with exclusive_transaction(self.con):
            it = store.get_intent(self.con, run.iid)
            deal = store.get_deal(self.con, run.did)
            if it is None or it['status'] != store.IntentStatus.APPROVED:
                return False
            if deal is None or it['deal_id'] != run.did or any(
                    it[k] != run.it[k] for k in ('spec_json', 'plan_json', 'kind')) or (
                    any(deal[k] != run.deal[k] for k in ('inst_json', 'owner_json', 'chain', 'token',
                                                    'token_dec', 'symbol', 'perp_venue', 'sim'))):
                raise store.StoreError('admission frozen context changed')
            native = store.get_native_stop(self.con, run.did)
            # The only exception is the dedicated risk-reducing stop unwind.
            # A normal exit still reaches its own cancellation gate before any
            # spot action, so merely seeing a triggered stop never authorizes
            # a new trade.
            allow_triggered = run.kind == 'exit' and native is not None and native['state'] == 'TRIGGERED'
            allow_native = run.kind == 'exit' and bool(run.spec.get('all')) and not run.spec.get('perp_only')
            require_resolved(self.con, deal, allow_triggered_native_stop=allow_triggered,
                             allow_native_stop=allow_native)
            # Generic entry/exit activation is part of admission, so a
            # rejected pre-send check cannot dirty a DRAFT deal.
            if activate is not None:
                activate()
            op = store.operation_of_intent(self.con, run.iid)
            if (op['id'] if op else None) != run.op_id:
                raise store.StoreError('admission root changed')
            if op is not None and op['bounds_hash'] != store._json_hash(json.loads(it['spec_json']).get('approval')):
                raise store.StoreError('admission approved bounds changed')
            if not store.set_intent_status(self.con, run.iid, store.IntentStatus.RUNNING,
                                           expect=store.IntentStatus.APPROVED):
                raise store.StoreError('admission CAS failed')
            if op is not None:
                start_linked(self.con, it)
            store.event(self.con, 'start', deal_id=run.did, intent_id=run.iid,
                        intent_kind=run.kind, sim=run.legs.sim)
        return True

    def run_operation(self, run, execute, *, paused, refused, activate: Callable[[], None] | None = None,
                      propagate: bool = False):
        """The sole outer execution lifecycle. Ports run outside transactions.

        Report failures after terminal commit cannot transition money state back
        to paused. Engine remains the sole queue/thread and execution-lock owner.
        """
        from .engine import Pause
        from .keys import redact
        try:
            admitted = self.admit(run, activate=activate)
        except Exception as exc:
            refused('операция не начата: ' + redact(exc))
            if propagate:
                raise
            return None
        if not admitted:
            return None
        try:
            return execute()
        except Exception as exc:
            current = store.get_intent(self.con, run.iid)
            if current['status'] != store.IntentStatus.RUNNING:
                # Propagate to Engine's reporting boundary without reclassifying
                # an already committed operation or repeating native execution.
                raise
            stop = exc if isinstance(exc, Pause) else Pause(
                'error', f'сбой исполнителя: {type(exc).__name__}: {redact(exc)}')
            paused(stop)
            if propagate:
                raise
            return None

    def commit_end(self, run, decision: EndDecision, *, now=None):
        """Commit root/intent/deal together; callers enrich reports afterwards.

        No transition error is swallowed. Incompatible identity or a stale
        intent rolls the complete transition back, including the root.
        """
        return self.commit_transition(run, decision, now=now,
                                      intent_statuses=(store.IntentStatus.RUNNING,))

    def commit_transition(self, run, decision: EndDecision, *, intent_statuses: tuple[str, ...],
                          settle: Callable[[dict], None] | None = None,
                          proof: Callable[[dict, dict, dict | None], None] | None = None,
                          now=None):
        """Guard one terminal or recovery transition in the common lifecycle.

        ``commit_end`` remains the legacy RUNNING-intent wrapper.  Recovery can
        retain an interrupted/partial intent by passing ``intent_state=None``;
        its root settlement and all state mutations still share this one frozen
        context transaction.  ``proof`` is a pure database/result check, never
        a network call.  It is required by callers which settle a previously
        UNKNOWN generic attempt; a zero reserve alone is not proof of outcome.
        """
        if not intent_statuses or any(not isinstance(state, str) for state in intent_statuses):
            raise ValueError('transition needs explicit expected intent states')
        if decision.intent_state is not None and not isinstance(decision.intent_state, str):
            raise ValueError('transition intent state must be a string or None')
        from .adapters.obligations import require_resolved
        from .adapters.native_journal import exclusive_transaction
        with exclusive_transaction(self.con):
            it = store.get_intent(self.con, run.iid)
            deal = store.get_deal(self.con, run.did)
            if it is None or deal is None or it['deal_id'] != run.did:
                raise store.StoreError('operation context no longer exists')
            if any(it[k] != run.it[k] for k in ('spec_json', 'plan_json', 'kind')) or (
                    any(deal[k] != run.deal[k] for k in ('inst_json', 'owner_json', 'chain', 'token',
                                                    'token_dec', 'symbol', 'perp_venue', 'sim'))):
                raise store.StoreError('operation frozen context changed')
            if it['status'] not in intent_statuses:
                if intent_statuses == (store.IntentStatus.RUNNING,):
                    raise store.StoreError('operation is no longer running')
                raise store.StoreError('operation intent state changed')
            op = store.operation_of_intent(self.con, run.iid)
            if (op['id'] if op else None) != run.op_id:
                raise store.StoreError('operation root changed')
            if proof is not None:
                proof(it, deal, op)
            if settle is not None:
                if op is None:
                    raise store.StoreError('root settlement without a root')
                settle(op)
            if decision.intent_state == store.IntentStatus.DONE or decision.deal_state in (
                    store.DealState.OPEN, store.DealState.CLOSED, store.DealState.ABORTED) or any(
                        state in (store.OpState.OPEN, store.OpState.CLOSED, store.OpState.ABANDONED)
                        for state in decision.root_states):
                # Generic finality first settles only after its explicit
                # terminal-result proof.  The unresolved gate then observes
                # the post-settlement state; a failed gate rolls it back.
                require_resolved(self.con, deal)
            for target in decision.root_states:
                if op is None:
                    raise store.StoreError('root transition without a root')
                store.set_operation_state(self.con, op['id'], target, reason=decision.reason, now=now)
            # An unresolved prerequisite before clip creation still owns the
            # DRAFT. Preserve an active deal instead of abandoning its identity.
            if deal['state'] == store.DealState.DRAFT and decision.deal_state == store.DealState.PAUSED:
                store.set_deal_state(self.con, run.did, store.DealState.ENTERING, expect=deal['state'], now=now)
            store.set_deal_state(self.con, run.did, decision.deal_state,
                                 reason=decision.reason, now=now, **(decision.fields or {}))
            resulting_intent = it['status']
            if decision.intent_state is not None:
                if not store.set_intent_status(self.con, run.iid, decision.intent_state,
                                               expect=it['status'], err=decision.error):
                    raise store.StoreError('operation terminal CAS failed')
                resulting_intent = decision.intent_state
            store.event(self.con, 'operation_end', deal_id=run.did, intent_id=run.iid,
                        operation_id=run.op_id, deal_state=str(decision.deal_state),
                        intent_state=str(resulting_intent), reason=decision.reason)

    def pause(self, run, stop, *, progressed, empty, require_unprogressed_empty=False, now=None):
        from .adapters.obligations import unresolved
        deal = store.get_deal(self.con, run.did)
        op = store.operation_of_intent(self.con, run.iid)
        unknown = bool(unresolved(self.con, deal)) or stop.reason in (
            'dex_unknown', 'perp_unknown', 'position_unknown', 'book_unknown')
        if unknown:
            target = store.DealState.HALTED_MISMATCH if stop.reason in (
                'position_mismatch', 'book_unknown') and deal['state'] != store.DealState.DRAFT else store.DealState.PAUSED
        elif deal['state'] == store.DealState.DRAFT:
            target = store.DealState.ABORTED
        elif stop.reason in ('position_mismatch', 'book_unknown'):
            target = store.DealState.HALTED_MISMATCH
        elif run.kind == 'entry' and empty and deal['state'] == store.DealState.ENTERING and (
                not require_unprogressed_empty or not progressed):
            target = store.DealState.ABORTED
        else:
            target = store.DealState.PAUSED
        states = ()
        if op is not None and op['state'] not in store.OP_DONE:
            prefix = (store.OpState.APPROVED,) if op['state'] == store.OpState.PROPOSED else ()
            if unknown:
                states = (*prefix, store.OpState.PAUSED_UNKNOWN)
            elif target == store.DealState.ABORTED or (require_unprogressed_empty and not progressed and not int(op['confirmed_raw'])):
                states = (*prefix, store.OpState.STOPPED, store.OpState.ABANDONED)
            else:
                states = (*prefix, store.OpState.STOPPED if stop.reason in ('stop', 'terminate', 'drain')
                          else store.OpState.PAUSED_RISK)
        self.commit_end(run, EndDecision(target, store.IntentStatus.PARTIAL if progressed else store.IntentStatus.FAILED,
                                        states, reason=stop.reason, error=stop.text), now=now)
        return target

    def run_clips(self, program: ClipLifecycle):
        """One execution order for EVM/SOL and adapter-backed clip programs.

        No SQLite transaction may surround a network phase. Remaining schedule
        is bounded by the approved input quantity, not recomputed from prices.
        Native full-exit selection is an explicit snapshot policy in select_amount.
        """
        queue = list(program.amounts)
        seq = done = 0
        while queue:
            if self.con.in_transaction:
                raise store.StoreError('execution cannot run inside a database transaction')
            planned, queue = queue[0], queue[1:]
            if type(planned) is not int or planned <= 0:
                raise store.StoreError('invalid clip input quantity')
            seq += 1
            last = not queue
            program.progress(seq, seq + len(queue))
            program.guard()
            amount = program.select_amount(planned, last)
            if type(amount) is not int or amount < 0:
                raise store.StoreError('invalid selected clip quantity')
            if amount == 0:
                break
            ticket = program.prepare(amount, last)
            clip_id = store.create_clip(self.con, program.intent_id, seq, amount)
            program.spot(clip_id, amount, ticket)
            program.hedge(clip_id, last, ticket)
            program.settle(clip_id, ticket)
            done += amount
            revised = program.next_amounts(clip_id, done, list(queue), ticket)
            if any(type(v) is not int or v <= 0 for v in revised) or sum(revised) > sum(queue):
                raise store.StoreError('replanned clips exceed the remaining input budget')
            queue = list(revised)
        program.finish()

    def _clip(self, clip_id, operation_id):
        clip = store.get_clip(self.con, clip_id)
        if clip is None:
            raise store.StoreError(f'clip {clip_id} missing')
        linked = store.operation_of_intent(self.con, clip['intent_id'])
        if (linked['id'] if linked else None) != operation_id:
            raise store.StoreError('clip does not belong to the supplied operation')
        return clip

    def begin_spot(self, clip_id, *, operation_id=None, reserve_raw=None, retry_reverted=False):
        with store.tx(self.con):
            self._clip(clip_id, operation_id)
            allowed = (C.PLANNED, C.DEX_REVERTED) if retry_reverted else C.PLANNED
            if not store.set_clip_state(self.con, clip_id, C.DEX_SENT, expect=allowed):
                raise store.StoreError('action already started; resolve instead of submitting')
            if operation_id is not None:
                store.operation_reserve(self.con, operation_id, reserve_raw)

    def settle_spot(self, clip_id, outcome: SpotSettlement, *, operation_id=None, reserve_raw=None):
        """Return true for the first application. UNKNOWN must never call this.

        Duplicate evidence verifies previous quantities and never releases the
        reservation twice. A contradictory final outcome refuses the whole tx.
        """
        with store.tx(self.con):
            clip = self._clip(clip_id, operation_id)
            state = clip['state']
            fresh = state in (C.DEX_SENT, C.DEX_UNKNOWN)
            if not fresh:
                if outcome.executed:
                    if state not in (C.DEX_OK, C.PERP_SENT, C.BALANCED, C.HEDGE_DEFICIT) or (
                        store.units(clip['dex_in']), store.units(clip['dex_out'])
                    ) != (outcome.input_raw, outcome.output_raw):
                        raise store.StoreError('settled clip conflicts with execution evidence')
                elif state not in (C.DEX_REVERTED, C.PLANNED):
                    raise store.StoreError('settled clip conflicts with non-execution evidence')
                return False
            fields = dict(dex_in=outcome.input_raw, dex_out=outcome.output_raw) if outcome.executed else {}
            store.set_clip_state(self.con, clip_id, C.DEX_OK if outcome.executed else C.DEX_REVERTED, **fields)
            if operation_id is not None:
                store.operation_settle(self.con, operation_id, released_raw=reserve_raw,
                                       executed_raw=outcome.input_raw if outcome.executed else 0)
            return True
