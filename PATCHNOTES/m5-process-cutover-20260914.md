# M5 staged process cutover: immutable build and configuration admission

Owner: Codex, assigned by project owner. Independent reviewer: Astra xhigh.
Base: 3bf6896. Branch: codex/migration-m4-m5. Date: 2026-09-14.
Status: deployed (staged process cutover), 2026-09-14 19:54:34 Moscow. Full M4 acceptance remains open.

This intermediate release switches collector/core/interface while retaining the
legacy EVM engine and its native recovery for existing positions. It does not
complete M4's generic EVM adapter adoption or historical account attribution.
Resize and SL remain deferred to a later patch.

First-install admission now fingerprints private configuration separately from
source, including owner-selected registries and arbitrary keypair env references.
Python caches no longer create false stale-source conflicts. The configuration
fingerprint is checked after fencing and handed into migration. Migration copies
captured bytes only, including the absence of optional files; a temporary config
file cannot evade admission by appearing then disappearing. Secrets remain private.

Before selection, each target service UID runs a network-isolated layout probe,
checking owner/registry/keyfile readability and cross-role DAC boundaries. No
trading entry point or signer is invoked. inspect-base disables Python bytecode
creation so root imports do not leave unremovable files in the upload directory.

Validation before build: deploy profile 49 passed, 1 platform skip. A fresh
financial whitelist export of OPEN DQA9Q was replayed against a976bec and this
candidate: no projection mismatches or unsafe differences; classification remains
exact_strict_unknown, not complete historical PnL proof. Snapshot SHA256:
9a5d47e906a0dc423cb88a665a660f34660fd44cf5e4652acb8a44bcb9a74748.
Offline startup with the recorded book and synthetic matching venue observations
preserved OPEN, quantities and frozen instrument, with zero sends. A separately
approved simulated full exit reached CLOSED with reduce-only futures; networking
was blocked. This is a compatibility check, not live liquidity/exit proof.

Full Linux results bind the final immutable artifact and appear in its receipt.
No live trade is part of deployment validation. Actual release selection, loaded
versions and deployment result must be established by the supervised installer.

Target-runtime build found Python 3.11.2 lacks tarfile's filter argument. Both
builder and installer now use the same regular-file extractor with exclusive
creation, symlink/traversal refusal and sanitized executable permissions; archive
ownership and setuid bits are never applied. Regression forbids extractall use.
Updated deploy profile: 50 passed, 1 skipped. The failed preliminary build did not
produce an accepted receipt and did not stop or switch the production bot.

The exact artifact intentionally has no Git database. Snapshot replay tests now
carry a frozen source-only baseline from 95354c4ca74b6efc208ae406ec411559c0726076,
with a pinned archive hash and tamper refusal. Other requested revisions still
use explicit Git export. Replay uses the Python-3.11.2-compatible extractor.
The baseline contains source files only, no runtime/configuration/private data.
Independent Astra review confirmed byte-for-byte Git archive provenance and all
15 snapshot tests passing in a copy without .git and with an empty PATH.
Build test output is written while tests run, and the full profile now records
its ten slowest tests and skip reasons. No tests are omitted for speed.
Combined local profile: 65 passed, 1 skipped. Exact-value scanning ran wholly on
VPS, including decompressed baseline sources: passed. Public DEX_EVM_ADDRESS and
ASTER_USER literals were excluded only when matching a strict EVM address format;
secret values and hashes were not exported by that scanner.

First complete Linux profile (c24071f): 2633 passed, 12 failed, 12 skipped,
1329.39 seconds. Ten failures were the now-fixed Git-dependent replay; one planner
wall-time smoke failure under 80% CPU quota passed unchanged at 100% CPU/Nice10.
The large-dashboard health failure exposed concurrent duplicate JSON rendering.
Serve now single-flights cache misses, rechecks snapshot mtime after waiting, and
returns the collector's atomically published data.json bytes without parsing and
re-encoding them under the GIL. HTML still renders normally; missing-file fallback
and gzip remain supported. No latency assertions were loosened.
Linux focused validation at 100% CPU/Nice10: both original failing tests passed,
13.54s total; 46,137,502-byte table, 3 clients, 9 downloads, 60 health requests,
health p95 145.68ms (required <200ms). Local serve/load profile: 5 passed, 1.25s.

Production result: `aa446f216047-a82af1f56cf0`, healthy; full Linux 2649 passed, root DAC 2 passed,
Mac JS 10 passed. Deployment took 122.475s. All three actual
processes and public status report the loaded release; current position preserved.
See docs/migration/MIGRATION_RESULT.md for scope and evidence. This post-deployment
documentation update does not change the installed code SHA above.
