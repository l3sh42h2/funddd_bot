# Public Spot + DEX / Futures scanner

- Owner: user. Implementer: Codex, no subagents. Reviewer: not assigned (public collection only).
- Status: in_progress. Branch: codex/spot-dex-futures-only.
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
