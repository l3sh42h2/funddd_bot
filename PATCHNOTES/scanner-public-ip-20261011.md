# Stable numeric public dashboard address

- Owner: user. Implementer: Codex, no subagents. Status: in_progress.
- Branch: codex/spot-dex-futures-only. No trading/core modifications.
- Existing local web was unresponsive; aggregate memory was ~1.2 GiB while
  MemoryHigh was 1 GiB; memory.events had >3.7 million high events, no OOM kills.
- Increase aggregate soft ceiling to 1280 MiB, retaining hard 1536 MiB and one CPU.
- Nginx serves public market routes only on TCP 80, proxying local scanner web.
  Cabinet and all other routes are denied; methods other than GET/HEAD denied.
  Nginx joins the same bounded slice, restarts on failure and starts at boot.
- Promote existing external IP to a static GCP address if it is not reserved.
  Firewall rule targets only this VM via a dedicated tag and opens only TCP 80.
- Public market dashboard uses HTTP; no credentials or cabinet are served there.
- Stop the former temporary tunnel after verifying the numeric address externally.
