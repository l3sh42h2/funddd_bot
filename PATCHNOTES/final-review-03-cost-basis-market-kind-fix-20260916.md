# FINAL-03: cost-basis reader no longer treats a damaged `market_kind` as a safe perpetual fact

Date: 2026-09-16
Author: Claude
Source: independent external review (ChatGPT, GPT-6 Astra Pro) of candidate `8eca9d0`,
`not approved` verdict, finding **FINAL-03** (P2). Full review:
`docs/migration/CLAUDE_COST_BASIS_REVIEW_20260916.md` covers a related but distinct P1
found earlier in the same file (`malformed_execution_event`); this patch is about the
`market_kind` discriminator specifically, reported separately by the external reviewer.

## What was broken

`src/funding_bot/trade/cost_basis.py`, top of the per-fact loop in `rebuild()`:

```python
for _, fact in facts:
    if fact.get("market_kind") != "spot":
        continue
```

This ran **before** `key = (leg_id, scope)` was computed and before any of the existing
malformed-fact handling. A fact whose `market_kind` was missing, `null`, `""`, or any
unrecognised string was silently treated exactly like a legitimate `perpetual` fact and
skipped with a bare `continue` -- no `_fail()`, no entry in `reasons`.

Reproduced exactly as the review described: two BUYs of one spot leg, 1 BASE each (10 and
30 quote). Damaging only the second fact's `market_kind` (any of the four ways above) made
`rebuild()` return `quantity=1, basis_quote=10, average_cost_quote=10, complete=True,
reasons=()` -- a wrong number reported as trustworthy, instead of an incomplete leg. If the
damaged fact was the second one for an *already open* leg, the leg's quantity/basis stayed
at whatever the first fact alone produced, silently missing the second execution's
contribution -- the same class of fail-open bug this module's docstring explicitly promises
not to have ("makes the affected leg incomplete instead of fabricating PnL").

## The fix

`key`/`leg_id`/`scope` readability (`has_key`) is now determined before the discriminator
is classified, and the discriminator itself is classified into exactly three buckets instead
of the old binary `== "spot"` check:

1. **`"perpetual"`** (explicit) -- unchanged behavior: safely skipped, doesn't touch `key`,
   `legs`, or `malformed` at all, since it never claimed to be a spot execution.
2. **`"spot"`** (explicit, or missing field normalized to `"spot"` -- see compatibility
   decision below) -- unchanged behavior: proceeds through the existing identity/side/
   cash-matching pipeline exactly as before this patch.
3. **Anything else** (`None`, `""`, an unrecognised string) -- new: a damaged fact, not a
   legitimate non-spot one. If `leg_id`/`scope` are readable, it poisons that specific leg
   through the *same* `malformed_legs` pending-failure mechanism already used for
   `malformed_execution_event` (`_fail(legs[key], "malformed_market_kind")` if the leg
   already exists, or recorded in `malformed_legs[key]` and applied retroactively the moment
   a later valid fact creates the leg -- an append-only journal can have the damaged fact
   arrive before or after the first valid one for the same leg, and both orders are covered
   by tests). If `leg_id`/`scope` are *not* readable either, it joins the existing deal-wide
   `malformed` list exactly like any other keyless malformed fact, poisoning every leg's
   `complete` flag for that deal.

New failure reason: **`malformed_market_kind`**. This is a new name rather than reusing
`malformed_execution_event`, because the two are not the same condition: the latter means
"this is a spot execution fact, but some other field on it is corrupt"; the former means "we
cannot even establish whether this fact belongs to spot cost basis at all." Keeping them
distinct preserves a more precise diagnostic for anyone triaging a poisoned leg later.

## Compatibility decision: a fact with no `market_kind` field at all

The review flagged that `leg_accounting._fact()` (`src/funding_bot/trade/leg_accounting.py:247`)
already defaults a **missing** `market_kind` key to `"spot"` (`p.get("market_kind", "spot")`),
and asked for an explicit choice between two options rather than a silent one:

- **(a) Sync with `leg_accounting`**: a *truly absent* `market_kind` key (not present in the
  JSON at all) is normalized to `"spot"`, the same way `leg_accounting._fact()` already does.
  A present-but-`null`/`""`/unknown value still gets the malformed treatment above -- only
  the absent-key case gets this pass.
- **(b) No guessing**: `cost_basis` never infers a missing field; any absence is treated as
  malformed too, even though this disagrees with `leg_accounting`.

**Chosen: (a).** Reasons:

- No contraindication was found in the code for treating missing-vs-explicit-spot as
  equivalent. `cost_basis.rebuild()` never reads `market_kind` for anything other than this
  one gate -- there is no other branch anywhere in the function whose behavior would differ
  between "explicitly spot" and "missing, normalized to spot."
- `leg_accounting.rebuild()` (the quantity reader that already runs against the same journal)
  goes through `ExecutionFact.__post_init__`, which *raises* `ValueError` on a `market_kind`
  that is present but `None`/`""`/unknown -- an uncaught exception that aborts the whole
  read, not a soft per-leg failure. A `cost_basis` reader that raised on the same input would
  match that reader's strictness, but `cost_basis` is deliberately a fail-*soft* read model
  (its own docstring: incomplete rather than fabricated, never an exception for a damaged
  row) and every other malformed-fact branch in this file already follows that fail-soft
  pattern. Raising here would be inconsistent with the rest of the module and would turn one
  damaged row into a hard failure of the entire deal's report instead of an isolated,
  diagnosable `complete=False`. This was a deliberate design read, not an oversight: the two
  readers already disagree in strictness for present-but-invalid values (`leg_accounting`
  crashes, `cost_basis` now reports `malformed_market_kind`); this patch does not attempt to
  reconcile that, since changing `leg_accounting`'s or the fact-writer's behavior is out of
  scope for a `cost_basis.py`-only fix.
- Choosing (a) only for the genuinely-absent-key case keeps the two readers agreeing on
  *quantity-relevant* classification for old rows written before this discriminator existed,
  which is the scenario the review's own text anticipates ("отсутствующее поле" as a
  distinct, narrower case than `null`/`""`/garbage).

This is documented here as the explicit, reviewable decision the review asked for, not a
silent default. It is a read-model interpretation, not a change to any stored data, and it
can be revisited by the owner later without a schema change if evidence turns up that old
rows without this field should instead be excluded.

## What was not changed

- No changes outside `src/funding_bot/trade/cost_basis.py` and `tests/test_m4_cost_basis.py`.
  `leg_accounting.py`, `leg_cash.py`, `interface/runtime.py`, `tg/sender.py` untouched.
- No new monetary thresholds, defaults, or historical facts/prices invented.
- The legitimate `perpetual` fast-path behavior is unchanged (test:
  `test_legitimate_perpetual_fact_never_affects_spot_basis`).
- A pre-existing, unrelated imprecision was noticed but left alone (out of scope for this
  fix): the deal-wide `malformed` list's `reasons` marker is hardcoded to the literal string
  `"malformed_execution_event"` regardless of which keyless-malformed condition actually
  triggered it (JSON parse failure, unreadable identity, or now also a damaged `market_kind`
  with an unreadable key). This patch does not rename that pre-existing marker.

## Tests

Added to `tests/test_m4_cost_basis.py` (kept in the normal suite, not only in the evidence
archive):

- `test_damaged_market_kind_after_valid_fact_poisons_leg` (parametrized: `None`, `""`,
  `"unknown"`) -- damaged second fact for an already-open leg.
- `test_damaged_market_kind_before_first_valid_fact_poisons_leg` (same 3 variants) --
  damaged fact arrives *before* the first valid fact for the same leg; pending-failure
  mechanism must still catch it.
- `test_damaged_market_kind_second_execution_no_longer_silently_dropped` (same 3 variants)
  -- the exact numeric reproduction from the review (1 BASE / 10 quote, then 1 BASE / 30
  quote with a damaged second `market_kind`).
- `test_missing_market_kind_field_is_normalized_to_spot_for_leg_accounting_compat` --
  variant (a): an entirely absent field is accepted and actually contributes to quantity/
  basis, not just "doesn't crash."
- `test_legitimate_perpetual_fact_never_affects_spot_basis` -- control: unchanged behavior.
- `test_perpetual_fact_with_unreadable_leg_key_is_still_just_skipped` -- control: a
  legitimate `perpetual` fact with garbage `leg_id`/`scope` is still just skipped, not
  added to the deal-wide `malformed` list.
- `test_damaged_market_kind_without_readable_key_poisons_whole_deal` (3 variants) -- a
  damaged discriminator *and* an unreadable key together join the existing deal-wide
  `malformed` list.
- `test_third_currency_cash_movement_still_poisons_leg` -- named explicitly by the review
  among the positive controls that must keep passing; this specific scenario
  (`third_currency_cash`) had no dedicated test in this file before this patch.

All pre-existing tests in the file are unmodified and still pass, confirming no regression
in `BUY/BUY/SELL` weighted-average arithmetic, restart/reopen durability, oversell refusal,
duplicate-cash-receipt detection, `fees_incomplete`, and the pre-existing
`malformed_execution_event` pending-failure mechanism.

### Mutation check

Before finalizing, the fix was temporarily reverted (`git stash` on `cost_basis.py` alone)
and the new test file was run against the original, unfixed code: all 13 new
fix-dependent test cases failed as expected (the three parametrized `market_kind`-poisoning
tests at 3 variants each, plus the missing-field-compat test, plus the
without-readable-key test at 3 variants), while the 9 fix-independent cases (the perpetual
controls, the new third-currency test, and the 6 pre-existing tests) passed on both the
buggy and fixed code. The fix was then restored (`git stash pop`) before committing.

## Results

```
PYTHONPATH=src:tests python3 -m pytest -q tests/test_m4_cost_basis.py -p no:cacheprovider
22 passed in 0.17s

PYTHONPATH=src:tests python3 -m pytest -q \
  tests/test_m4_cost_basis.py tests/test_m4_leg_accounting.py tests/test_generic_leg_cash.py \
  -p no:cacheprovider
54 passed in 0.42s

# Wide run: the three files above plus every other test file in the suite that
# references cost_basis/market_kind/leg_accounting/leg_cash/generic_leg_cash
# (found via `grep -rl` over tests/):
PYTHONPATH=src:tests python3 -m pytest -q \
  tests/test_m4_cost_basis.py tests/test_m4_leg_accounting.py tests/test_generic_leg_cash.py \
  tests/test_generic_adapter_matrix.py tests/test_generic_operations.py \
  tests/test_generic_recovery_owned_inventory.py tests/test_migration_m3.py \
  -p no:cacheprovider
223 passed in 4.42s
```

## Acceptance

- [x] `market_kind` is validated with `key` already available, not before it.
- [x] Legitimate `perpetual` facts are still safely skipped (unchanged behavior, tested).
- [x] Missing/`null`/`""`/unknown `market_kind` no longer silently mimics `perpetual`.
- [x] Damaged fact after the first valid fact for a leg: poisons that leg (tested).
- [x] Damaged fact before the first valid fact for a leg: poisons that leg once created,
      via the existing pending-failure mechanism (tested).
- [x] Damaged fact with an unreadable `leg_id`/`scope`: joins the deal-wide `malformed`
      list like other keyless malformed facts (tested).
- [x] Compatibility choice for a fully-absent field is explicit, documented, and justified,
      not guessed (this document; variant (a) chosen).
- [x] No historical facts, prices, or thresholds invented.
- [x] Regression tests for `tests/test_m4_cost_basis.py`'s existing positive scenarios
      (BUY/BUY/SELL, third currency, incomplete fee) all still pass.
- [x] Only `src/funding_bot/trade/cost_basis.py` and its test file were touched.
