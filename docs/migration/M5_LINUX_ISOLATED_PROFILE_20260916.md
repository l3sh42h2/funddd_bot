# M5: isolated Linux profile — 2026-09-16

## Candidate and environment

- source commit: `a3d20e89f9846ea4d1a25a1bf26cbfb610ce92b2`;
- uploaded source archive SHA-256: `4bb33415a967d12661bfc7a7824238d9c119eb65a2f94562314682586473721c`;
- host: Ireland VPS, a freshly created directory under `/tmp`;
- Python: `/opt/funding-bot/current/.venv/bin/python`;
- command:

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests \
  /opt/funding-bot/current/.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_m5_deploy_server.py tests/test_m4_domain_notifications.py \
  tests/test_m4_final_report_boundary.py
```

## Result

`80 passed, 1 skipped` in `20.71s` (exit code 0).

The archive and temporary source directory were removed after the command.
The running bot, systemd services, runtime state, configuration, credentials and
production database were not read or changed by the test command.

## What this demonstrates

The Linux execution covered the existing server-job and notification regression
suite, including stale-base refusal before service mutation, durable transition
recovery around an interrupted pre-switch state, deployment/execution locks,
reader-gated code-only rollback that never restores the database, and UI-only
compatibility/refusal paths.

## What it does not demonstrate

This is not an immutable M5 artifact receipt, a live `deploy/deploy.sh install`,
or a systemd/DAC fault injection on a separate staging host. In particular it
does not prove that killing an SSH client during a real detached `systemd-run`
job preserves ownership, nor that real service users have the intended file
permissions. Those remain open M5 acceptance work and must be performed away
from the production runtime.
