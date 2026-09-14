# M4: mandatory durable signing barrier for Aster/Gate common execution

- Author/owner: Codex, migration assigned by owner.
- Reviewer: Astra xhigh, appointed by owner.
- Base: e9fb2c3; branch: codex/migration-m4-m5.
- Status: ready (code; not deployed). Updated: 2026-09-14.
- Components: adapters/signing_fence.py, native_journal.py, execution.py,
  AsterTrade/GateTrade and actual native signing tests.

The common journal claims an attempt before invoking the native adapter. A process
can stop between that claim and signing. Aster/Gate previously had no opt-in proof
that this attempt never reached HTTP; a NULL nonce alone was insufficient because
legacy callers could omit the callback.

Core's common submit port now explicitly binds these native instances to one
connection and a verified source account before opening a writer transaction. A
versioned send-barrier marker is stored in the existing prepared event. Opted-in IOC
requires a callback that commits an exact, fresh signature record before HTTP.
Missing/noop/failing/uncommitted callbacks, changed request fields and reused
signatures are refused. Legacy native instances remain unchanged until opt-in.

Local no-send recovery checks the same writer connection, account, venue, deal,
intent, clip and prepared marker, and requires no signature or execution evidence.
Recovery changes SENT/UNKNOWN to NOT_PLACED under the existing transaction. A
sender delayed behind that recovery fails its signing CAS. A committed signature,
including nonce/timestamp zero, never proves no send. Gate timestamps are not used
as unique attempt IDs. Reopening the persisted journal with a newly verified native
instance preserves recovery. Hyperliquid's separate native proof is unchanged.

The binding deliberately accepts the same connection only; another connection or
account is refused. No schema change, historical instrument rewrite, live order,
service restart or production deployment. This is a prerequisite for EVM engine
integration, not its activation: engine._child and generic startup integration,
legacy account attribution and compatibility remain outstanding.

Validation: 36 native fence tests passed in 0.46 s, including actual concurrent
recovery vs delayed signing and process-reopen reconstruction. Combined common
adapters/native history/EVM+Gate/SOL engine/final report profile: 411 passed in
9.20 s. Six initial fence test assertions counted metadata GET as submission;
corrected to count POST, preserving the no-order-send invariant. Independent
review closed by Astra xhigh. Full Linux artifact acceptance and production cutover not done.

Review closure: Astra reproduced a real two-signer race through nested store.tx
on one connection (two POSTs). Common admission/signature/recovery now acquire
strict BEGIN IMMEDIATE ownership; failed acquisition cannot roll back another
owner. Signature update uses a NULL-nonce CAS with rowcount check. The scheduling
regression now proves exactly one POST and size -2, not -4. Prior 411-test result
predates this race fix; final combined profile: 412 passed, 9.32 s. Independent
reviewer profile: 323 passed; P1/P2 closed, no new blocking findings. The binding's
same-connection requirement must be respected when wiring thread-local recovery.
Do not weaken it to accommodate another worker. Full elapsed development/review
time and model token counters are unavailable; timings above are pytest only.
