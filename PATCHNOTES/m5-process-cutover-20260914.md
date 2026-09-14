# M5 staged process cutover: immutable build and configuration admission

Owner: Codex, assigned by project owner. Independent reviewer: Astra xhigh.
Base: 3bf6896. Branch: codex/migration-m4-m5. Date: 2026-09-14.
Status: candidate; production acceptance recorded separately after deployment.

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
