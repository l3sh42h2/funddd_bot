# Critical: fixed approved DEX clip ceiling during replan

## Trigger

During the AIW3 entry, the initial approved plan had seven $350 clips. After
one completed clip, recovery replanning selected one $2,100 DEX swap.

## Change

The executor now passes the maximum size of the still-unexecuted approved
clip into the replan limits. A fresh quote may change the distribution below
that ceiling, but it cannot merge the remaining approved clips into a larger
swap. If owner configuration has an even smaller `clip_max_usd`, that smaller
limit wins.

## Validation

`tests/test_trade_engine.py::test_replan_never_merges_approved_remaining_clips`
asserts that a hypothetical $2,100 remaining amount is planned with a $350
ceiling. The focused local suite passed before Linux verification.
