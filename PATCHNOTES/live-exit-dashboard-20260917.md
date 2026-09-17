# Live exit and dashboard repair — 2026-09-17

Candidate commit: `0fca84c4ffe08423f172f3f6243967cf6f2c21a1`.

- A late confirmation of an expired exit plan now creates a fresh exit proposal. It never revives or submits the expired intent.
- The dashboard renders at most 100 coin groups at once and provides local pagination, preventing a large comparison set from exhausting a mobile browser's DOM.
- Final entry reports now show the known breakdown of an actual cost that materially exceeds its planned estimate. The new fields are explanatory only; they do not change accounting or execution.

Validation before packaging: 279 focused tests passed locally. The guarded critical profile is used for the Linux receipt.
