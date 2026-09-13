# M2 staging contract (activation only in M5 through deploy/deploy.sh)

These units are templates for the isolated /opt/funding-bot/current release and /var/lib/funding-bot state.
They are deliberately NOT installed by the legacy deploy script. No second trader may be started beside production.
`prepare_layout.py` creates a fresh staging directory and splits env values without printing them. It refuses to
reuse a directory. It never copies a DB, changes a live file or starts a service. Run with explicit interface UID.

Required ownership for the M5 installer:
- accounts/groups funding-core, funding-interface, funding-collector;
- funding-ipc: core + interface; funding-market: collector + core + interface; funding-pace: collector + core;
- /var/lib/funding-bot root: root:root 0755; secrets root:root 0700, EnvironmentFiles root:root 0600
  (systemd reads them before dropping privileges; processes cannot read each other's env files);
- core directory: funding-core:funding-core 0700, trade.db and keypair files 0600; owner.toml/instruments.json private;
- interface directory: funding-interface:funding-interface 0700; state 0600;
- collector directory: funding-collector:funding-market 0710; funding_bot.db 0600; public directory
  funding-collector:funding-market 2750; table.json/collector_health.json 0640 or 0644;
- shared directory: root:funding-pace 2770; okxdex.pace root:funding-pace 0660 precreated once, never replaced/unlinked;
- /run/funding-bot: funding-core:funding-ipc 0750, core.sock funding-core:funding-ipc 0660.
  Set RuntimeDirectory group via explicit tmpfiles provisioning or core bootstrap group setting;
  core.service's RuntimeDirectory defaults to primary group, so bootstrap sets the socket parent group to funding-ipc.

The M5 job must validate these rights as each target UID, not merely inspect service file strings. It must also
copy the consistent trade.db and all required journals/config/registry/keypair paths during drained cutover, copy
collector state separately, preserve the one shared OKX quota and remap any keypair-file path in core.env.
It must never make trade.db public to get the cabinet working. UI reads only IPC DTO, not SQL.

First transition: old deployed trader without execution.lock must be stopped and its restart disabled before
any new core, under the server deploy lock. Updated legacy trader and core use the same stable lock path; when
changing runtime directories preserve the lock inode / override FUNDING_EXECUTION_LOCK for both during transition.
Plain rename of a release does not fence an old process. UI failure never stops core.

Before rollback: drain; resolve approved/in-flight/UNKNOWN; retain current DB; prove schema/offset compatibility.
Never revert state from M0 backup over later executions. Additive core_* tables alone do not prove a downgrade safe.
M3/M4 must remove remaining pure legacy message-format dependencies inside compatibility controllers; no Telegram
network client is imported or started by the headless core in M1/M2.
