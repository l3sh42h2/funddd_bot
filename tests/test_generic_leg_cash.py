from dataclasses import replace
from decimal import Decimal as D
import pytest

from funding_bot.trade import store, leg_accounting, leg_cash
from funding_bot.trade.adapters.contracts import Result, Status, NativeRef, RawAmount, QuoteAmount
from funding_bot.trade.fees import FeeComponent
from funding_bot.trade.quantity_units import native_to_base
from common_adapter_fixtures import spec


def receipt(s, *, side='BUY', fees=(), complete=True):
    if s.capabilities.market_kind == 'spot':
        base = s.instrument if s.capabilities.venue_kind == 'dex' else s.asset_id
        token, quote = RawAmount(base, 2000000, 6), RawAmount(s.quote_currency, 4000000, 6)
        fields = dict(spot_input_raw=quote if side == 'BUY' else token,
                      spot_output_raw=token if side == 'BUY' else quote)
    else:
        fields = dict(perp_quote=QuoteAmount(D(4), s.quote_currency), trade_notional=QuoteAmount(D(4), s.quote_currency))
    return Result(Status.SETTLED, D(2), 'final', False, ('receipt',), fees=fees,
        terminal=True, fees_complete=complete, version=2, leg_id=s.leg_id, spec_hash=s.fingerprint,
        scope=s.scope, native_ref=NativeRef('order', 'same'), **fields)


def test_spot_cash_and_external_base_fee_rebuild_atomically(tmp_path):
    con = store.connect(tmp_path / 'db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    fee = FeeComponent('platform', s.asset_id, 6, 100000, s.account, False, False)
    r = receipt(s, fees=(fee,))
    assert leg_accounting.record_result(con, r, s, operation_id='op', side='BUY')
    assert not leg_accounting.record_result(con, r, s, operation_id='op', side='BUY')
    con.close()
    con = store.connect(tmp_path / 'db')
    assert D(leg_accounting.rebuild(con)['legs'][0]['qty']) == D('1.9')
    cash = leg_cash.rebuild(con)['legs'][0]
    assert {k: D(v) for k, v in cash['cash'].items()} == {'USDC': D(-4), s.asset_id: D('1.9')}
    assert con.execute('SELECT min_reader FROM schema_version').fetchone()[0] == 5


@pytest.mark.parametrize('adapter', ('fixture_cex_spot', 'fixture_sol_spot'))
@pytest.mark.parametrize('multiplier', (D('.1'), D(1), D(10)))
@pytest.mark.parametrize('side, native_owned', (('BUY', D('1.9')), ('SELL', D('-2.1'))))
def test_external_native_spot_fee_scales_only_inventory_exposure(tmp_path, adapter, multiplier, side, native_owned):
    """Cash fee stays native; the inventory debit is converted exactly to base."""
    con = store.connect(tmp_path / 'db')
    s = spec(adapter, 'spot', 'long', multiplier=multiplier)
    native_asset = s.instrument if s.capabilities.venue_kind == 'dex' else s.asset_id
    fee = FeeComponent('platform', native_asset, 6, 100000, s.account, False, False)
    leg_accounting.record_result(con, receipt(s, side=side, fees=(fee,)), s, operation_id='op', side=side)
    leg = leg_accounting.rebuild(con)['legs'][0]
    assert D(leg['qty']) == native_to_base(native_owned, multiplier)
    assert D(leg['fees'][s.asset_id]) == D('.1')


def test_cash_identity_failure_rolls_back_quantity_and_reader_floor(tmp_path):
    con = store.connect(tmp_path / 'db')
    before = con.execute('SELECT min_reader FROM schema_version').fetchone()[0]
    s = spec('fixture_cex_spot', 'spot', 'long')
    r = replace(receipt(s), spot_input_raw=RawAmount('USDT', 4000000, 6))
    with pytest.raises(ValueError, match='cash assets'):
        leg_accounting.record_result(con, r, s, operation_id='op', side='BUY')
    assert con.execute('SELECT COUNT(*) FROM exec_events').fetchone()[0] == 0
    assert con.execute('SELECT min_reader FROM schema_version').fetchone()[0] == before


def test_raw_token_flow_must_support_the_reported_execution_quantity(tmp_path):
    con = store.connect(tmp_path / 'db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    r = replace(receipt(s), executed_quantity=D(1))
    with pytest.raises(ValueError, match='native token flow'):
        leg_accounting.record_result(con, r, s, operation_id='op', side='BUY')
    assert con.execute('SELECT COUNT(*) FROM exec_events').fetchone()[0] == 0


def test_rent_embedded_and_sponsor_do_not_become_double_fee(tmp_path):
    con = store.connect(tmp_path / 'db')
    s = spec('fixture_sol_spot', 'spot', 'long')
    fees = (FeeComponent('platform', s.instrument, 6, 100000, s.account, True, False),
            FeeComponent('rent_deposit', 'native:solana', 9, 2000000, s.account, False, False, refundable=True),
            FeeComponent('rent_refund', 'native:solana', 9, 1000000, s.account, False, False, refundable=True),
            FeeComponent('network_total', 'native:solana', 9, 5000, 'sponsor', False, False))
    leg_accounting.record_result(con, receipt(s, fees=fees), s, operation_id='op', side='BUY')
    cash = leg_cash.rebuild(con)['legs'][0]
    assert D(cash['cash'][s.asset_id]) == 2
    assert D(cash['cash']['native:solana']) == D('-.001')
    assert D(cash['rent_locked_delta']['native:solana']) == D('.001')
    assert 'native:solana' not in leg_accounting.rebuild(con)['legs'][0]['fees']


def test_perp_notional_is_not_cash_funding_scoped_dedup(tmp_path):
    con = store.connect(tmp_path / 'db')
    a = spec('fixture_cex_perp', 'long', 'long', quote='USDT')
    b = spec('fixture_dex_perp', 'short', 'short', quote='USDC')
    for s, side in ((a, 'BUY'), (b, 'SELL')):
        leg_accounting.record_result(con, receipt(s), s, operation_id='op', side=side)
        assert leg_cash.record_funding(con, s, operation_id='op', native_id='same-funding',
            amount=D('-0.2') if side == 'BUY' else D('.3'), currency=s.settlement_currency, evidence='native-page')
    assert not leg_cash.record_funding(con, b, operation_id='op', native_id='same-funding',
        amount=D('.3'), currency='USDC', evidence='native-page')
    with pytest.raises(ValueError, match='conflicting'):
        leg_cash.record_funding(con, b, operation_id='another-op', native_id='same-funding',
            amount=D('.3'), currency='USDC', evidence='native-page')
    legs = {x['leg_id']: x for x in leg_cash.rebuild(con)['legs']}
    assert legs['long']['cash'] == {'USDT': '-0.2'}
    assert legs['short']['cash'] == {'USDC': '0.3'}
    assert legs['long']['notional'] == {'USDT': '4'}


def test_unknown_fee_preserves_quantity_and_marks_cash_incomplete(tmp_path):
    con = store.connect(tmp_path / 'db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    fee = FeeComponent('platform', s.asset_id, 6, None, s.account, False, False)
    leg_accounting.record_result(con, receipt(s, fees=(fee,), complete=False), s, operation_id='op', side='BUY')
    assert leg_cash.rebuild(con)['legs'][0]['complete'] is False
    assert D(leg_accounting.rebuild(con)['legs'][0]['qty']) == 2


def test_native_execution_cannot_be_credited_to_two_operations(tmp_path):
    con = store.connect(tmp_path / 'db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    r = receipt(s)
    leg_accounting.record_result(con, r, s, operation_id='first', side='BUY')
    with pytest.raises(ValueError, match='another operation'):
        leg_accounting.record_result(con, r, s, operation_id='second', side='BUY')
    assert len(leg_accounting.rebuild(con)['legs']) == 1
    assert leg_accounting.rebuild(con)['legs'][0]['executions'] == 1


def test_generic_report_two_legs_has_no_fabricated_pnl_or_network_fields(tmp_path):
    from funding_bot.core.leg_report import build
    from funding_bot.interface.leg_presenter import render
    con = store.connect(tmp_path / 'db')
    for key, leg_id, direction, side, quote in (
        ('fixture_cex_perp', 'long', 'long', 'BUY', 'USDT'),
        ('fixture_dex_perp', 'short', 'short', 'SELL', 'USDC')):
        s = spec(key, leg_id, direction, quote=quote)
        leg_accounting.record_result(con, receipt(s), s, operation_id='op', side=side)
        leg_cash.record_funding(con, s, operation_id='op', native_id='funding1',
            amount=D('.1'), currency=quote, evidence='scoped-history')
    report = build(con, operation_id='op')
    assert len(report['legs']) == 2 and report['pnl'] is None
    assert {tuple(x['funding']) for x in report['legs']} == {('USDT',), ('USDC',)}
    assert all('wallet' not in x and 'gas' not in x for x in report['legs'])
    text = render(report)
    assert 'USDT' in text and 'USDC' in text and 'нет подтверждённой оценки' in text


def test_generic_positions_notification_fences_old_interface_and_preserves_legacy_wire(tmp_path):
    from funding_bot.core.journal import Journal, Outbox
    from funding_bot.trade.engine import Conns
    from funding_bot.ipc.reports import PositionView
    from funding_bot.interface.presenter import present
    out = Outbox(Journal(Conns(tmp_path / 'db')))
    old = PositionView('D1', 'BASE', 'OPEN')
    out.positions_report(1, [old], at=100, matched=True, mismatch=None, sim=True)
    event = out.journal.notifications()[0]
    assert event['dto_version'] == 2
    assert 'generic_legs' not in event['snapshots'][0]
    report = {'version': 2, 'legs': (), 'pnl': None}
    out.positions_report(1, [replace(old, generic_legs=report)], at=100, matched=None, mismatch=None, sim=True)
    event = out.journal.notifications()[-1]
    assert event['dto_version'] == 4
    assert 'PnL' in present(event)['text']
    assert out.journal.conns.get().execute("SELECT value FROM core_meta WHERE key='notification_dto_version'").fetchone()[0] == '4'


def test_previous_reader_refuses_generic_financial_records_before_any_write(tmp_path, monkeypatch):
    con = store.connect(tmp_path / 'db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    leg_accounting.record_result(con, receipt(s), s, operation_id='op', side='BUY')
    before = tuple(con.execute('SELECT version,min_reader FROM schema_version').fetchone())
    con.close()
    monkeypatch.setattr(store, 'SCHEMA_VERSION', 4)
    with pytest.raises(store.SchemaTooNew):
        store.connect(tmp_path / 'db')
    import sqlite3
    with sqlite3.connect(tmp_path / 'db') as raw:
        assert tuple(raw.execute('SELECT version,min_reader FROM schema_version').fetchone()) == before
