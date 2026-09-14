"""Common journal transitions. Native executors supply only proven outcomes.

The existing clip is the durable action identity and existing operations remain
the budget authority. No parallel operation or reservation journal is created.
All clip + budget mutations occur in the caller's receipt transaction or one
short IMMEDIATE transaction. No network calls belong in these transactions.
"""
from dataclasses import dataclass
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


class OperationController:
    def __init__(self, connection):
        self.con = connection

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
