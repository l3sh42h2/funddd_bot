# Public Spot + DEX / Futures scanner

- Owner: user. Implementer: Codex, no subagents. Reviewer: not assigned (public collection only).
- Status: deployed (dedicated scanner services; base bot release unchanged). Branch: codex/spot-dex-futures-only.
- Installed base: 24372f5379cb-8b71fdd60131; source revision from manifest
  24372f5379cbaa41c4c3c2b6a1fa141cae4f3551 is unavailable from GitHub.
  No attempt to replace that release from an older checkout is made.
- A dedicated process-local profile uses the existing immutable installed code.
  Five futures: Aster, Binance, Hyperliquid, KuCoin, Gate. All configured spot
  clients and OKX DEX chains remain. FF construction is skipped before identity,
  windows and snapshot calculations, rather than filtering the finished output.
- Separate web uses serve CLI; it does not start interface/core or Telegram.
- Collector reuses its market database/history; profile publishes into a new
  public/sf-only directory, so old table.json cannot be served at first boot.
- Aggregate systemd slice: one CPU, 1 GiB soft / 1.5 GiB hard memory, no swap.
- Original services and immutable release sources remain unchanged. A distinct
  sidecar installer pins release and hashes under the existing deployment lock.
- Stop the two sf services and tunnel to disable this profile. No schema changes,
  order sends, wallet reads or position migrations are involved.

## Verification and startup result

- Launcher commit installed: f70c756. Production release sources were verified
  against their existing manifest; original collector/core/interface remain inactive.
- Five real installed-module tests passed in 0.298 seconds. Syntax, shell syntax
  and Git diff checks passed. HTTP render forces SF mode and labels monitoring.
- Public HTTPS dashboard returned HTTP 200:
  https://dispatch-sort-sec-narrative.trycloudflare.com
- One 60-second startup observation: aggregate scanner CPU 4.2% of four-vCPU
  capacity, cgroup memory 470.1 MiB (~5.9% of 7950 MiB). This is a short sample,
  not a long-term load forecast; no available memory peak value was reported.
- Verified table: 9948 SF rows, zero FF rows; 915 DEX rows, zero stale DEX rows.
  Tick age 4.7 seconds, all 11 venue rate/book observations <1 second old at
  table generation; last successful DEX response 83.7 seconds old (180-second job).
  Collector notes empty. History backfill remains normal background work.
- Both dedicated services are enabled. CPU/RAM ceilings apply to their aggregate
  slice. Core, trader and Telegram/interface are not started by these units.
