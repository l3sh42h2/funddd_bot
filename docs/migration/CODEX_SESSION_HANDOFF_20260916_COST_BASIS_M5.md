# Codex handoff — cost basis and M5 Linux evidence

Branch: `codex/migration-m4-m5`  
Candidate head at handoff update: `d4485e5f695bb8fd90ff51e1e0cbe496a0600cf8`

## Changes for review

### `a3d20e89f9846ea4d1a25a1bf26cbfb610ce92b2` — cost-basis read model

Files:

- `src/funding_bot/trade/cost_basis.py`
- `tests/test_m4_cost_basis.py`
- `PATCHNOTES/m4-cost-basis-read-model-20260916.md`

It reads only `leg_execution_fact_v1` and `leg_execution_cash_v1` events of an
explicit `deal_id`. The result is split by `(leg_id, frozen scope)`, requires a
matching cash receipt and does not convert currencies. The stated policy is
`weighted_average_v1` for operational inventory accounting, not tax lots.

Review invariants:

1. no deal, account/scope, base asset or quote currency can mix with another;
2. a missing/mismatched cash receipt, incomplete fees, non-zero third-currency
   cash movement or oversell returns `complete=false` and no basis/PnL fields;
3. exact Decimal arithmetic is preserved across a reopen;
4. this module is not wired into execution, owner reports or an authoritative
   PnL decision yet;
5. decide whether weighted average is the accepted product policy before that
   wiring. It is explicit in the payload so a later policy cannot silently
   reinterpret prior values.

Executed locally:

```text
PYTHONPATH=src:tests python3 -m pytest -q \
  tests/test_m4_cost_basis.py tests/test_generic_leg_cash.py \
  tests/test_m4_leg_accounting.py -p no:cacheprovider
```

Result after integration and the ambiguous-cash regression: `60 passed in 2.78s`.

### `2d3a002e0d44e90b7355255ce4c8f8b23af0d1eb` — M5 Linux evidence

Adds `docs/migration/M5_LINUX_ISOLATED_PROFILE_20260916.md`: a reproducible
receipt for the isolated VPS test of source `a3d20e8`. The source archive hash,
command, result (`80 passed, 1 skipped in 20.71s`) and limits are recorded.
It performed no deployment and touched no bot service, production DB, state or
credentials.

## Known open work — do not mark accepted from this handoff

- AC-07 remains open. The later `m4-evm-execution-notice-dto-20260916` patch
  moves EVM execution/recovery notifications to a facts-only DTO, but
  `trade/engine.py` still creates HTML for desk/planning and some legacy
  `Refused.html` paths.
- AC-15 needs product approval of the basis policy and replay comparison with
  historical/legacy evidence before a report becomes authoritative.
- M5 still needs a separate staging host for a true detached-systemd/SSH-loss,
  DAC and install/rollback exercise, then a complete immutable artifact profile.
- Neither commit was deployed.
