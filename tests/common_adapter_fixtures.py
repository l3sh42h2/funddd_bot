"""Five independent adapter registrations; only the native transport is fake.

NativeAdapter still performs production prepare/claim/authorize/resolve. The
transport maintains external receipts, never writes the bot's position/ledger.
"""
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
import importlib.util
from pathlib import Path

from funding_bot.trade.adapters.contracts import (
    AdapterError, ErrorKind, Capabilities, LegSpec, Observation, Quote, Result,
    Status, NativeRef, RawAmount, QuoteAmount, ExecutionPage)
from funding_bot.trade.adapters.native import NativeAdapter, Bindings
from funding_bot.trade.adapters.context import AdapterContext
from funding_bot.trade.adapters.registry import AdapterRegistry


def _example(filename, name):
    path = Path(__file__).resolve().parents[1] / 'docs/migration/examples' / (filename + '.py')
    module_spec = importlib.util.spec_from_file_location('example_' + filename, path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return getattr(module, name)


CexSpot = _example('cex_spot', 'CexSpot')
CexFutures = _example('cex_futures', 'CexFutures')
EvmSpot = _example('evm_spot', 'EvmSpot')
SolSpot = _example('solana_spot', 'SolSpot')
DexFutures = _example('dex_futures', 'DexFutures')


ADAPTERS = {'fixture_cex_spot': CexSpot, 'fixture_cex_perp': CexFutures,
            'fixture_evm_spot': EvmSpot, 'fixture_sol_spot': SolSpot,
            'fixture_dex_perp': DexFutures}


def registry():
    result = AdapterRegistry()
    for key, cls in ADAPTERS.items():
        result.register(key, lambda spec, context, cls=cls: cls(spec, context.for_leg(spec)))
    return result


def spec(key, leg_id, direction, *, venue=None, quote='USDC', multiplier=D(1)):
    cls = ADAPTERS[key]
    spot = cls.market_kind == 'spot'
    family = cls.network_family
    dex = family is not None or cls is DexFutures
    return LegSpec(leg_id, 'inventory' if spot else 'hedge', direction, key,
        venue or key, 'account:' + leg_id, 'mint:CaseSensitive' if family else 'BASE',
        'underlying:verified', 'proof:fixture-issuer', multiplier, D('.001'), D('.01'),
        quote, quote, Capabilities('dex' if dex else 'cex', cls.market_kind, family,
            short=not spot, reduce_only=not spot, cancel=True),
        network='genesis:CaseSensitive' if family == 'solana' else 'eip155:56' if family else None,
        decimals=6 if spot else None, quote_decimals=6 if spot else None,
        margin_currency=quote if not spot else None, metadata_revision='fixture-1')


class Transport:
    """External state persists across core SQLite reconnects in crash tests."""
    def __init__(self):
        self.sent, self.receipts, self.positions = [], {}, {}
        self.allow = True
        self.fraction = D(1)
        self.fees = ()
        self.fail_after_send = False
        self.reject = False
        self.clock = 100.0
        self.price = D(2)

    def bindings(self, spec, journal):
        def authorize(s, action):
            if not self.allow:
                raise AdapterError(ErrorKind.CONFIG, 'readonly fixture')
        def quote(s, action, bounds):
            spot = s.capabilities.market_kind == 'spot'
            base = s.instrument if s.capabilities.venue_kind == 'dex' else s.asset_id
            spend = action.quantity if spot and action.side == 'SELL' else action.quantity * s.multiplier * self.price
            receive = action.quantity * self.price if spot and action.side == 'SELL' else action.quantity
            return Quote(action, self.clock + 60, spend, receive,
                base if spot and action.side == 'SELL' else s.quote_currency,
                s.quote_currency if spot and action.side == 'SELL' else base if spot else s.quote_currency)
        def submit(s, prepared):
            action = prepared.quote.action
            if prepared.attempt_id in self.receipts:
                raise AssertionError('external submit duplicated')
            self.sent.append((s.leg_id, prepared.attempt_id, action))
            qty = D(0) if self.reject else action.quantity * self.fraction
            fields = {}
            if qty > 0:
                if s.capabilities.market_kind == 'spot':
                    base = s.instrument if s.capabilities.venue_kind == 'dex' else s.asset_id
                    base_flow = RawAmount(base, int(qty * 10**6), 6)
                    quote_flow = RawAmount(s.quote_currency, int(qty * self.price * 10**6), 6)
                    fields = dict(spot_input_raw=quote_flow if action.side == 'BUY' else base_flow,
                                  spot_output_raw=base_flow if action.side == 'BUY' else quote_flow)
                else:
                    notional = QuoteAmount(qty * s.multiplier * self.price, s.quote_currency)
                    fields = dict(perp_quote=notional, trade_notional=notional)
            result = Result(Status.REJECTED if self.reject else Status.SETTLED if self.fraction == 1 else Status.PARTIAL,
                qty, 'fixture-final', False, ('external-receipt',), fees=self.fees,
                terminal=True, fees_complete=True, version=2, leg_id=s.leg_id,
                spec_hash=s.fingerprint, scope=s.scope, native_ref=NativeRef('order', prepared.attempt_id), **fields)
            self.receipts[prepared.attempt_id] = (result, action.side)
            self.positions[s.scope] = self.positions.get(s.scope, D(0)) + qty * (1 if action.side == 'BUY' else -1)
            if self.fail_after_send:
                raise TimeoutError('ACK lost after native execution')
            return result
        def resolve(s, ref):
            if ref not in self.receipts:
                return Result(Status.UNKNOWN, None, 'unresolved', True, ('no receipt',),
                    version=2, leg_id=s.leg_id, spec_hash=s.fingerprint,
                    scope=s.scope, native_ref=NativeRef('attempt', ref)), 'BUY'
            return self.receipts[ref]
        return Bindings(self, journal, authorize, quote, submit, resolve,
            lambda s: Observation(self.positions.get(s.scope, D(0)), self.clock, 'fixture', 'authoritative',
                                  available=D(1000), currency=s.quote_currency),
            lambda s, cursor: ExecutionPage(tuple(v[0] for v in self.receipts.values()), None, True),
            clock=lambda: self.clock)


def context(specs, transports, journals):
    ctx = AdapterContext()
    for s, transport, journal in zip(specs, transports, journals):
        ctx.add(s, transport.bindings(s, journal))
    return ctx
