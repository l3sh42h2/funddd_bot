# SL / resize reviewer fixes — 2026-09-18

Base: `af59c19`. This candidate addresses F01–F06 from the external review.

- Deployment compatibility declares schema/reader 6, matching the durable native-stop reader floor.
- Native SL and its linked perpetual order now resolve atomically; an armed, unknown, or triggered stop is visible to execution gates.
- Resize and partial exit are refused while a native stop is active. A full exit first cancels the conditional and requires a terminal response before allowance, swap, or manual hedge.
- Stop unwind rereads the exact scoped perpetual position and owned spot balance immediately before the spot sale.
- Entry requote keeps the frozen SL token price and refuses a changed `working_type` rather than silently changing trigger semantics.
- Native-stop polling continues during deployment drain in read-only mode. Recovery records the observed result but never starts a swap during drain.

No production deployment is included. The candidate requires independent review and a frozen-SHA Linux build before a switch.
