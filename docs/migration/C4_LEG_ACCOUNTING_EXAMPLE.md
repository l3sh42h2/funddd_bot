# C4 leg accounting boundary — implementation in progress

The format helper accepts existing Result v2 and frozen LegSpec, records terminal
execution facts in exec_events, and reconstructs a private two-leg DTO. It is not
yet connected to the generic core operation path; it does not prove C4 acceptance.

Only a terminal, non-provisional result with exact leg/spec/scope/native reference
may produce a fact. Terminal partial/cancelled executions retain their proven
quantity. Nonterminal cumulative updates remain in native journals until terminal
resolution; they are not summed as additional executions. Repeating identical
terminal evidence is idempotent. Conflicting evidence for the same scoped native
reference is rejected rather than overwriting history. Later fee corrections will
need a separate versioned correction event; they are not silently accepted here.

Result.fees_complete survives serialization/reopen/DTO. Unknown fees never become
zero. Embedded token fees are preserved for reporting and are not deducted twice.
An additional proven base fee debits inventory for both BUY and SELL, only on spot;
perpetual contract quantity is unaffected. FeeComponent metadata is retained.
Refundable/superseded/sponsor items are excluded from this expense projection;
a complete cash/rent view remains outside this helper and is still required.

Currencies remain separate. Exact DEX mint/address maps to the frozen asset_id;
no ticker alias or USDT/USDC/USD parity is inferred. Persisting these facts alone
uses reader 4, while future incompatible generic operation plans still require
an atomic newer reader floor before activation.
