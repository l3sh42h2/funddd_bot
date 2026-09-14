"""M4 normalized adapter results retain native execution accounting without network calls."""
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest

from funding_bot.trade.adapters import outcomes
from funding_bot.trade.adapters.contracts import (Action, AdapterError, Capabilities, ErrorKind,
                                                  LegSpec, NativeRef, Prepared, Quote, QuoteAmount,
                                                  RawAmount, Result, Status)
from funding_bot.trade.adapters.native import Bindings, FuturesAdapter
from funding_bot.trade import fees as native_fees, sol_exec
from funding_bot.trade.sol_exec import SwapOutcome
from funding_bot.trade.types import PerpFill, SwapResult


def perp_spec(venue='aster'):
    return LegSpec(
        leg_id=f'{venue}:hedge', role='hedge', direction='short', adapter_id=venue, venue=venue,
        account=f'account:{venue}', instrument='TOKEN_USDT', asset_id='asset:TOKEN',
        identity_evidence='exchange-metadata', multiplier=D(1), step=D('.001'), tick=D('.0001'),
        quote_currency='USDT', settlement_currency='USDC',
        capabilities=Capabilities('dex' if venue == 'hyperliquid' else 'cex', 'perpetual',
                                  short=True, reduce_only=True),
        metadata_revision='m4-test',
    )


def spot_spec(family='evm', **changes):
    values = dict(
        leg_id=f'{family}:spot', role='inventory', direction='long', adapter_id=f'{family}_spot',
        venue=f'{family}_dex', account=f'wallet:{family}', instrument='TOKEN/USDC',
        asset_id='asset:TOKEN', identity_evidence='chain-registry', multiplier=D(1), step=D('.000001'),
        tick=D('.000001'), quote_currency='USDC', settlement_currency='USDC',
        capabilities=Capabilities('dex', 'spot', family), network=f'{family}:network', decimals=18,
        quote_decimals=6, metadata_revision='m4-test',
    )
    values.update(changes)
    return LegSpec(**values)


def test_result_v1_positional_compatibility_and_v2_requires_identity_and_accounting():
    legacy = Result(Status.UNKNOWN, None, 'unresolved', True, ('legacy',))
    assert legacy.version == 1 and legacy.native_ref is None

    with pytest.raises(AdapterError, match='identity incomplete'):
        replace(legacy, version=2)
    with pytest.raises(AdapterError, match='execution accounting incomplete'):
        Result(Status.PARTIAL, D(1), 'exchange_observation', True, ('native',), version=2,
               leg_id='leg', spec_hash='hash', scope=('venue',), native_ref=NativeRef('order', '7'))


def test_raw_and_quote_values_are_typed_and_leg_fingerprint_covers_quote_decimals():
    amount = RawAmount('asset:TOKEN', 1234567, 6)
    assert amount.amount == D('1.234567')
    assert QuoteAmount(D('12.5'), 'USDT').currency == 'USDT'
    with pytest.raises(AdapterError):
        RawAmount('asset:TOKEN', True, 6)
    with pytest.raises(AdapterError):
        QuoteAmount(D('NaN'), 'USDT')

    current = spot_spec()
    assert current.fingerprint != replace(current, quote_decimals=8).fingerprint


@pytest.mark.parametrize('venue', ['aster', 'gate', 'hyperliquid'])
def test_perpetual_v2_preserves_fill_quote_price_and_order_reference(venue):
    spec = perp_spec(venue)
    fill = PerpFill('client-17', 17, 'FILLED', D('2'), D('3.25'), D('6.5'), 8)

    result = outcomes.perpetual(fill, spec.scope, spec=spec, side='SELL')

    assert result.version == 2 and result.status == Status.SETTLED and result.terminal
    assert (result.leg_id, result.spec_hash, result.scope) == (spec.leg_id, spec.fingerprint, spec.scope)
    assert result.native_ref == NativeRef('order', '17')
    assert result.executed_quantity == D('2')
    assert result.perp_quote == QuoteAmount(D('6.5'), 'USDT')
    assert result.trade_notional == QuoteAmount(D('6.5'), 'USDT')
    assert result.avg_price == QuoteAmount(D('3.25'), 'USDT')
    assert result.perp_quote.currency != spec.settlement_currency
    assert not result.fees_complete


def test_perpetual_incomplete_settlement_fails_closed_and_unknown_keeps_proven_amounts():
    spec = perp_spec()
    missing_quote = PerpFill('client', 3, 'FILLED', D(2), D(3), D(0), 1)
    result = outcomes.perpetual(missing_quote, spec.scope, spec=spec, side='SELL')
    assert result.status == Status.UNKNOWN and result.executed_quantity is None
    assert result.perp_quote is None and result.trade_notional is None and not result.terminal

    unresolved = replace(missing_quote, status='UNKNOWN', quote=D(6))
    result = outcomes.perpetual(unresolved, spec.scope, spec=spec, side='SELL')
    assert result.status == Status.UNKNOWN and result.provisional and not result.terminal
    assert result.executed_quantity == D(2)
    assert result.trade_notional == QuoteAmount(D(6), 'USDT')
    assert result.native_ref == NativeRef('order', '3')

    impossible_partial = replace(missing_quote, status='PARTIALLY_FILLED', qty=D(0))
    result = outcomes.perpetual(impossible_partial, spec.scope, spec=spec, side='SELL')
    assert result.status == Status.UNKNOWN and result.executed_quantity is None


def test_two_argument_perpetual_mapping_remains_version_one():
    spec = perp_spec()
    result = outcomes.perpetual(PerpFill('client', 7, 'FILLED', D(1), D(2), D(2), 1), spec.scope)
    assert result.version == 1 and result.status == Status.SETTLED and result.executed_quantity == D(1)


@pytest.mark.parametrize(
    ('side', 'amount_in', 'amount_out', 'input_asset', 'output_asset', 'quantity', 'price'),
    [
        ('BUY', 2_500_000, 10**18, 'USDC', 'asset:TOKEN', D(1), D('2.5')),
        ('SELL', 2 * 10**18, 5_000_000, 'asset:TOKEN', 'USDC', D(2), D('2.5')),
    ],
)
def test_evm_v2_preserves_native_raw_flows_and_derived_price(
        side, amount_in, amount_out, input_asset, output_asset, quantity, price):
    spec = spot_spec()
    native = SwapResult('0xtx', 'ok', amount_in, amount_out, 21_000, D('.01'), 42, 3)

    result = outcomes.evm_swap(native, spec, side)

    assert result.version == 2 and result.status == Status.SETTLED and result.terminal
    assert result.native_ref == NativeRef('transaction', '0xtx')
    assert result.executed_quantity == quantity
    assert result.spot_input_raw.raw == amount_in and result.spot_input_raw.asset_id == input_asset
    assert result.spot_output_raw.raw == amount_out and result.spot_output_raw.asset_id == output_asset
    assert result.spot_input_raw.decimals == (6 if input_asset == 'USDC' else 18)
    assert result.spot_output_raw.decimals == (6 if output_asset == 'USDC' else 18)
    assert result.avg_price == QuoteAmount(price, 'USDC')
    assert not result.fees_complete


def test_evm_settled_without_both_positive_amounts_becomes_unknown_not_zero():
    spec = spot_spec()
    result = outcomes.evm_swap(SwapResult('0xtx', 'ok', 2_000_000, 0, 21_000, None, 42, 3), spec, 'BUY')
    assert result.status == Status.UNKNOWN and result.executed_quantity is None
    assert result.spot_input_raw is None and result.spot_output_raw is None
    assert result.native_ref == NativeRef('transaction', '0xtx') and not result.terminal


def test_solana_v2_final_receipt_preserves_raw_flows_fee_completeness_and_price():
    spec = spot_spec('solana', decimals=6)
    fee, = native_fees.receipt_components(fee_lamports=5000, fee_payer='wallet:solana')
    native = SwapOutcome('ok', 'attempt-1', 'signature-1', 2_000_000, 1_000_000, 'finalized',
                         fees=(fee,), receipt='new', slot=123)

    result = outcomes.sol_swap(native, spec, 'BUY')

    assert result.status == Status.SETTLED and result.native_ref == NativeRef('transaction', 'signature-1')
    assert result.spot_input_raw == RawAmount('USDC', 2_000_000, 6)
    assert result.spot_output_raw == RawAmount('asset:TOKEN', 1_000_000, 6)
    assert result.executed_quantity == D(1) and result.avg_price == QuoteAmount(D(2), 'USDC')
    assert result.fees == (fee,) and result.fees_complete


def test_solana_fee_completeness_requires_exact_network_total_and_all_known_components():
    spec = spot_spec('solana', decimals=6)
    base = dict(state='ok', attempt_id='attempt-1', signature='signature-1', in_raw=2_000_000,
                out_raw=1_000_000, commitment='finalized', receipt='new')

    unknown_external = sol_exec._fees(
        {'in_mint': 'usdc', 'out_mint': 'token'}, wallet='wallet:solana', fee=5000, tip=0,
        deposit=0, refund=0, external=None, source='receipt',
    )
    assert any(f.kind == 'rent_nonrefundable' and f.amount_raw is None for f in unknown_external)
    assert not outcomes.sol_swap(SwapOutcome(fees=unknown_external, **base), spec, 'BUY').fees_complete
    assert not outcomes.sol_swap(SwapOutcome(fees=(), **base), spec, 'BUY').fees_complete

    estimated = native_fees.FeeComponent(
        kind='network_total', asset=native_fees.NATIVE_SOL, decimals=9, amount_raw=5000,
        payer='wallet:solana', included_in_input_output=False, estimated=True, source='estimate',
    )
    assert not outcomes.sol_swap(SwapOutcome(fees=(estimated,), **base), spec, 'BUY').fees_complete


def test_solana_provisional_receipt_stays_unknown_but_keeps_actual_raw_amounts():
    spec = spot_spec('solana', decimals=6)
    native = SwapOutcome('ok', 'attempt-1', 'signature-1', 2_000_000, 1_000_000, 'confirmed',
                         fees=('observed-fee',), receipt='new', slot=122)

    result = outcomes.sol_swap(native, spec, 'BUY')

    assert result.status == Status.UNKNOWN and result.provisional and not result.terminal
    assert result.executed_quantity == D(1)
    assert result.spot_input_raw.raw == 2_000_000 and result.spot_output_raw.raw == 1_000_000
    assert result.fees == ('observed-fee',) and not result.fees_complete


def test_solana_final_success_without_amounts_is_unknown_and_does_not_invent_zero():
    spec = spot_spec('solana', decimals=6)
    native = SwapOutcome('ok', 'attempt-1', 'signature-1', None, None, 'finalized', receipt='new')
    result = outcomes.sol_swap(native, spec, 'BUY')
    assert result.status == Status.UNKNOWN and result.executed_quantity is None
    assert result.spot_input_raw is None and result.spot_output_raw is None
    assert not result.fees_complete and not result.terminal


def test_legacy_spot_spec_without_quote_decimals_remains_version_one():
    spec = replace(spot_spec(), quote_decimals=None)
    result = outcomes.evm_swap(SwapResult('0xtx', 'ok', 2_000_000, 10**18, 1, None, 1, 1), spec, 'BUY')
    assert result.version == 1 and result.status == Status.SETTLED and result.executed_quantity == D(1)


def test_native_adapter_normalization_failure_returns_identity_stamped_unknown_attempt():
    spec = perp_spec()
    journal = NS(claim=lambda prepared: None)
    malformed = NS(status='FILLED', qty=D(1), quote=D(2), avg_px=D(2),
                   order_id=None, client_id='', sign_nonce=1, err_code=None)
    bindings = Bindings(
        native=None, journal=journal, authorize=lambda *_: None, quote=lambda *_: None,
        submit=lambda *_: malformed, resolve=lambda *_: None, observe=lambda *_: None,
        executions=lambda *_: None, clock=lambda: 100,
    )
    adapter = FuturesAdapter(spec, bindings)
    action = Action('action-1', spec.leg_id, 'SELL', D(1))
    quote = Quote(action, 200, D(2), D(1), 'USDT', 'asset:TOKEN')
    prepared = Prepared('attempt-1', quote, spec.fingerprint)

    result = adapter.submit(prepared)

    assert result.status == Status.UNKNOWN and result.version == 2 and result.error == ErrorKind.UNKNOWN
    assert result.native_ref == NativeRef('attempt', 'attempt-1')
    assert (result.leg_id, result.spec_hash, result.scope) == (spec.leg_id, spec.fingerprint, spec.scope)
    assert result.executed_quantity is None and not result.terminal
