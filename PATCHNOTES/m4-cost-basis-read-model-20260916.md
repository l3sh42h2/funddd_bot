# M4: cost-basis read model

Date: 2026-09-16

- Adds `trade.cost_basis`: an exact, deal- and spot-leg-scoped weighted-average inventory projection from the existing append-only execution fact and cash journals.
- This is read-only. It does not change orders, execution state, balances, reports shown to the owner, or database schema.
- It emits no basis or realized PnL when a cash receipt is missing/mismatched, fees are incomplete, a third currency needs FX conversion, or an exit exceeds confirmed inventory.
- Regression tests cover exact partial exit arithmetic, restart rebuild, incomplete fees, scope isolation and oversell refusal.
- The projection is an operational `weighted_average_v1` basis, not a tax-lot statement. It is not yet wired as an authoritative PnL source; independent review and the remaining AC-15 replay comparison are required.
