"""Process-local, market-only profile over the pinned installed funding_bot.

The immutable bot release and its trading configuration are never modified.
The public universe builder is replaced before Collector is constructed so FF
pairs never reach identity, history-window calculations or snapshot writes.
"""
from __future__ import annotations

import json
import sys

FUTURES = ("aster", "binance", "hyperliquid", "kucoin", "gate")
MODE = "spot_dex_futures_only"


def apply_profile():
    from funding_bot import config, universe

    missing = set(FUTURES) - set(config.PERP_VENUES)
    if missing:
        raise RuntimeError(f"Installed release lacks futures venues: {sorted(missing)}")
    config.PERP_VENUES = FUTURES
    config.PERP_EXCHANGES = FUTURES
    config.EXCHANGES = {k: v for k, v in config.EXCHANGES.items()
                        if k in FUTURES or k in config.SPOT_VENUES}
    # This is a deliberate process-local strategy override, not a source edit.
    universe.build_ff = lambda perps: []

    from funding_bot import collector
    base = collector.Collector

    class SpotFuturesCollector(base):
        def build_table(self):
            table = super().build_table()
            if table["ff_rows"]:
                raise RuntimeError("SF-only invariant violated: FF rows exist")
            table["scan_mode"] = MODE
            table["scan_futures"] = list(FUTURES)
            return table

    collector.Collector = SpotFuturesCollector
    return config, collector


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in (["collector"], ["serve"], ["check"]):
        raise SystemExit("Only collector, serve or check is allowed")
    config, collector = apply_profile()
    if args == ["check"]:
        from funding_bot.client import make_clients
        clients = make_clients()
        expected = set(FUTURES) | set(config.SPOT_VENUES) | {"okxdex"}
        if set(clients) != expected:
            raise RuntimeError(f"Unexpected/missing clients: {sorted(set(clients) ^ expected)}")
        if collector.universe.build_ff({"irrelevant": []}) != []:
            raise RuntimeError("FF builder is active")
        print(json.dumps({"mode": MODE, "futures": FUTURES,
                          "spots": config.SPOT_VENUES, "dex_chains": config.OKX_DEX_CHAINS,
                          "clients": sorted(clients)}, ensure_ascii=False))
        return 0
    from funding_bot.cli import main as cli_main
    return cli_main(args)


if __name__ == "__main__":
    sys.exit(main())
