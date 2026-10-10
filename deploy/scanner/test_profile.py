import types
import unittest
from unittest.mock import patch

import profile_runner


class ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config, cls.collector = profile_runner.apply_profile()

    def test_factory_creates_only_requested_sources(self):
        from funding_bot.client import make_clients
        clients = make_clients()
        self.assertEqual(set(clients), set(profile_runner.FUTURES) |
                         set(self.config.SPOT_VENUES) | {"okxdex"})
        self.assertTrue(clients["okxdex"].enabled())

    def test_rebuild_never_constructs_ff_and_preserves_sf(self):
        col = self.collector.Collector.__new__(self.collector.Collector)
        col.venues = ["binance"]
        col.spot_venues = ["binance_spot"]
        col.instruments = {"binance": {"x": {"symbol": "X"}},
                           "binance_spot": {"y": {"symbol": "Y"}}}
        col._with_shares = lambda venue, instrument: instrument
        col._log_class_splits = lambda perps: None
        col._resolve = lambda: None
        col.ident_summary = {}
        col.last = {}
        col.now = lambda: 10
        sf = [{"key": "expected"}]
        with patch.object(self.collector.universe, "build_sf", return_value=sf) as builder:
            col._rebuild()
        self.assertEqual(col.ff, [])
        self.assertEqual(col.sf, sf)
        builder.assert_called_once_with({"binance": [{"symbol": "X"}]},
                                        {"binance_spot": [{"symbol": "Y"}]})

    def test_trading_commands_are_rejected_before_cli(self):
        for command in ("core", "trader", "interface", "plan", "backfill"):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                profile_runner.main([command])

    def test_dex_networks_are_retained(self):
        self.assertEqual(self.config.OKX_DEX_CHAINS,
                         {"solana": "501", "bsc": "56", "robinhood": "4663"})
        self.assertEqual(len(self.config.SPOT_VENUES), 6)

    def test_profile_cannot_reuse_full_mode_table(self):
        self.assertEqual(str(self.config.TABLE_PATH),
                         '/var/lib/funding-bot/collector/public/sf-only/table.json')


if __name__ == "__main__":
    unittest.main()
