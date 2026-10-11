# Stable numeric public dashboard address

- Owner: user. Implementer: Codex, no subagents. Status: deployed.
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

## Result

- Existing static GCP reservation `ireland-static` confirmed IN_USE at 34.65.234.12.
  No new IP reservation was necessary. Added narrowly targeted `funding-dashboard-http`
  rule (TCP 80 only) and `funding-dashboard-public` instance tag.
- External checks: `/` HTTP 200 in 3.45 s, `/data.json` HTTP 200 in 1.69 s;
  fresh snapshot age 5.3 s, 10169 SF rows, 1132 DEX rows, FF zero. `/cabinet` 404.
- Nginx configuration validated; nginx, SF web and SF collector enabled at boot.
  Temporary Cloudflare tunnel disabled/stopped, URL file now http://34.65.234.12/.
- Trading core/interface remained inactive. Installed bot release unchanged.
- Aggregate memory ceiling remains 1536 MiB, soft threshold 1280 MiB.
  Zero OOM kills; high events fell from continuous reclaim to small transient bursts.
- Public-IP installer and proxy config committed; future scanner starts use nginx
  when this site is installed instead of relaunching the temporary tunnel.
