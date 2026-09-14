from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest

from funding_bot.trade.adapters import hl_preflight
from funding_bot.trade.adapters.perp_preflight import PreflightRefused, PreflightRequest, entry
from funding_bot.trade.types import Book, Filters, PerpInstrument


class CexPerp:
    venue = "gate"

    def __init__(self, *, available=D("100"), position=D("-2")):
        self.available, self.position_value = available, position
        self.calls = []

    def instrument(self, symbol):
        return PerpInstrument(symbol, "ASSET", "ASSET", D(1), "USDT", "PERPETUAL")

    def filters(self, symbol):
        return Filters(D(".01"), D("1"), D("1"), D("1000"), D("1000"), D(".01"), frozenset({"IOC"}))

    def book(self, symbol, limit):
        self.calls.append("book")
        return Book(((D("9"), D("10")),), ((D("10"), D("10")),), 1)

    def available_margin(self):
        self.calls.append("margin")
        return self.available

    def position(self, symbol):
        self.calls.append("position")
        return self.position_value

    def setup(self, symbol, leverage, margin_type):
        self.calls.append(("setup", leverage, margin_type))


def request(**kw):
    values = dict(symbol="ASSET_USDT", quote_currency="USDT", multiplier=D(1), leverage=10,
                  capacity=D(2), fee_rate=lambda: D(".1"), reserve=D(1), expected_position=D("-2"))
    values.update(kw)
    return PreflightRequest(**values)


def test_cex_preflight_never_requires_hl_methods_and_sets_up_after_evidence():
    perp = CexPerp()
    result = entry(perp, request())
    assert result.step == D(1)
    assert result.position == D("-2")
    assert perp.calls == ["book", "margin", "position", ("setup", 10, "ISOLATED")]


def test_cex_missing_margin_refuses_before_setup_or_send():
    perp = CexPerp(available=D("1"))
    with pytest.raises(PreflightRefused, match="маржи"):
        entry(perp, request())
    assert "setup" not in perp.calls
    assert "position" not in perp.calls


def test_hl_entry_delegates_existing_validator(monkeypatch):
    seen = {}
    fake = NS(venue="hyperliquid", identity=lambda: NS(sz_decimals=2),
              position=lambda symbol: D("-3"),
              margin=lambda: NS(available=D("10")))
    monkeypatch.setattr(hl_preflight, "entry", lambda *a, **kw: seen.update(args=a, kwargs=kw))
    result = entry(fake, request(expected_position=D("-3")), inst=NS(perp_symbol="ASSET"), venue="hyperliquid")
    assert seen["args"][0] is fake and seen["args"][1].perp_symbol == "ASSET"
    assert seen["kwargs"]["expected_short"] == D("3")
    assert result.identity is None
