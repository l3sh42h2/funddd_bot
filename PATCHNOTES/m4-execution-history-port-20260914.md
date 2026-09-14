# M4: scoped native history through the common futures adapter

- Author/owner: Codex, migration assigned by project owner.
- Reviewer: Astra xhigh, appointed by owner.
- Base: e9fb2c3; branch: codex/migration-m4-m5.
- Status: ready (code; not deployed). Updated: 2026-09-14.
- Scope: trade/adapters/futures_bindings.py, execution history tests. No production deployment.

The Aster/Gate common adapter previously called permissive legacy `fills` and returned
`ExecutionPage.complete=True` for any returned list. This did not establish the source
account or complete historical coverage, including exchange retention limits.

The common reader now requires the strict native history API, checks the frozen
account and venue before and after the read, rejects noncanonical cursors, foreign
instruments, invalid IDs, cursor regression and conflicting duplicates. It returns
observed rows with scoped deduplication keys and `complete=False`; empty history
preserves the cursor and does not prove zero executions. Hyperliquid's paged reader
is unchanged. No database or historical instrument records are rewritten.

Validation: focused new history, M3 adapter and native history profile: 66 passed,
0.24 s. An initial regression test caught replacement of the identity method during
a read; both source checks now obtain the current identity method. Independent
review closed by Astra xhigh. Strict native IDs are also validated before int() conversion;
bool/fractional IDs cannot be silently attributed to another order. Thirty-six
actual native invalid trade/order ID regressions cover both venues. This closes one history adapter gap, not M4/M5 acceptance.

Remaining execution switch blockers established by code review: legacy EVM
instruments can lack identity evidence/account; Aster/Gate need an explicit durable
sign-before-send fence before using common claim admission. Do not bypass these by
inventing historical account attribution or weakening recovery checks.
