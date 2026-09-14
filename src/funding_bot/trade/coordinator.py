"""Venue-neutral business lifecycle over the durable operation controller.

This module deliberately knows nothing about a chain, a venue or a particular
pair.  Policies provide native quote/submit/apply and instrument math; this
module owns the order in which a proven leading-leg result is hedged and
settled.  The durable identity, reserve and UNKNOWN handling stay in
``OperationController``.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from .operations import OperationController


@dataclass(frozen=True)
class HedgeAction:
    """A bounded hedge derived from the current proven deal book."""

    side: str
    quantity: Any
    reduce_only: bool


@dataclass(frozen=True)
class HedgeProgram:
    """Policy hooks for a manual rehedge/resume correction.

    The coordinator creates exactly one zero-input clip only after policy has
    produced an action from the current durable book.  A no-op never creates a
    clip; an UNKNOWN submit remains the policy's native unresolved attempt and
    cannot fall through to ``verify`` or ``finish``.
    """

    intent_id: str
    prepare: Callable[[], HedgeAction | None]
    submit: Callable[[int, HedgeAction], Any]
    apply: Callable[[int, HedgeAction, Any], None]
    verify: Callable[[], None]
    finish: Callable[[HedgeAction | None], None]


@dataclass(frozen=True)
class TwoLegProgram:
    """Neutral lead/apply/hedge/apply sequence with native policies outside."""

    leading: Callable[[], Any]
    apply_leading: Callable[[Any], None]
    hedge: Callable[[], Any | None]
    apply_hedge: Callable[[Any], None]
    finish: Callable[[Any, Any | None], None]


class LifecycleCoordinator:
    """Shared execution sequencing; policies retain only native economics."""

    def __init__(self, connection):
        self.con = connection
        self.operations = OperationController(connection)

    def run_hedge(self, program: HedgeProgram) -> None:
        """Run the common one-clip rehedge lifecycle from a frozen approval."""
        action = program.prepare()
        if action is None:
            program.finish(None)
            return
        if (action.side not in {"BUY", "SELL"} or type(action.reduce_only) is not bool or
                not isinstance(action.quantity, Decimal) or not action.quantity.is_finite() or action.quantity <= 0):
            raise ValueError("hedge policy returned an invalid bounded action")
        clip_id = self._clip(program.intent_id)
        result = program.submit(clip_id, action)
        program.apply(clip_id, action, result)
        program.verify()
        program.finish(action)

    def run_two_leg(self, program: TwoLegProgram) -> None:
        lead = program.leading()
        program.apply_leading(lead)
        hedge = program.hedge()
        if hedge is not None:
            program.apply_hedge(hedge)
        program.finish(lead, hedge)

    def _clip(self, intent_id: str) -> int:
        # The durable clip row is the sole action identity for a manual hedge;
        # it carries no leading-leg input and so cannot consume spot reserve.
        from . import store
        return store.create_clip(self.con, intent_id, 1, 0)
