# Guarded EVM resize and native Aster stop-loss — 2026-09-17

## Position resize

- `добор <deal|coin> <USDT>` creates a new entry operation only for the same open EVM deal and its frozen legs.
- Live use is disabled until the owner explicitly sets all three values in `[resize]`: `enabled`,
  `max_increase_usd_per_leg`, and `max_total_usd_per_leg`.
- The total ceiling is measured from the journal-owned token inventory at a fresh DEX pool price, not from the original
  `deals.leg_usd`; repeated increases cannot bypass it.
- `уменьшить` remains the existing partial exit path. It retains the existing token-unit, multiplier, and hedge checks.

## Stop-loss

- Entry syntax accepts `sl <token price>`, for example: `вход AIW3 okx·bsc aster 200 sl 0.03`.
- This candidate supports the live path only for EVM spot × Aster. Other perp venues refuse the SL explicitly; the bot
  never claims protection it cannot persist and query.
- A successful entry with SL is not reported open until the bot has durably recorded and Aster has acknowledged an exact
  `BUY TAKE_PROFIT_MARKET reduceOnly` conditional order. For a spot-long/perp-short hedge this is the correct order
  direction when price falls.
- The durable stop record raises the database reader gate to schema 6. A prior executable cannot start over an armed
  native order it cannot understand.
- The executor checks each armed conditional no more frequently than its configured interval (minimum 60 seconds). A
  fully proven fill creates one automatic spot-only exit for exactly journal-owned tokens. It does not send another
  perpetual buy.
- Unknown, missing, rejected, or partial conditional outcomes never trigger a blind token sale. The deal is paused for
  reconciliation instead.

## Required owner opt-in

```toml
[resize]
enabled = true
max_increase_usd_per_leg = 200
max_total_usd_per_leg = 1000

[stop_loss]
enabled = true
working_type = "MARK_PRICE" # or CONTRACT_PRICE
check_interval_s = 60
```

The sections are optional and absent settings preserve legacy frozen owner snapshots.
