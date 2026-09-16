# Cabinet: visible trading profile status

The authenticated cabinet now shows every supported trading profile as
`spot → futures`, with whether live trading is currently permitted. A disabled
or unavailable profile shows a short safe reason such as owner-disabled,
non-live mode, or not configured for live.

The status is produced by the core process and transported through the existing
private IPC projection. The web process does not open `owner.toml`; no keys,
wallet addresses, or environment-variable names are included.

Validated locally: cabinet, core IPC, and deployment health tests — 36 passed.
