from decimal import Decimal as D
import json

from funding_bot.trade import cost_basis, leg_accounting, store
from funding_bot.trade.adapters.contracts import NativeRef, RawAmount, Result, Status
from common_adapter_fixtures import spec


def _result(s, *, native, side, qty, quote, complete=True):
    base = RawAmount(s.asset_id, int(D(qty) * 10**6), 6)
    money = RawAmount(s.quote_currency, int(D(quote) * 10**6), 6)
    return Result(Status.SETTLED, D(qty), 'final', False, ('receipt',), terminal=True,
        fees_complete=complete, version=2, leg_id=s.leg_id, spec_hash=s.fingerprint, scope=s.scope,
        native_ref=NativeRef('order', native),
        spot_input_raw=money if side == 'BUY' else base,
        spot_output_raw=base if side == 'BUY' else money)


def _record(con, s, *, native, side, qty, quote, complete=True):
    return leg_accounting.record_result(con, _result(s, native=native, side=side, qty=qty, quote=quote,
                                        complete=complete), s, operation_id='op', side=side, deal_id='deal-1')


def test_weighted_average_basis_is_exact_for_partial_exit_and_reopen(tmp_path):
    con = store.connect(tmp_path / 'trade.db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    _record(con, s, native='buy-1', side='BUY', qty='2', quote='4')
    _record(con, s, native='buy-2', side='BUY', qty='2', quote='8')
    _record(con, s, native='sell-1', side='SELL', qty='3', quote='12')
    con.close()
    con = store.connect(tmp_path / 'trade.db')
    leg = cost_basis.rebuild(con, deal_id='deal-1')['legs'][0]
    assert cost_basis.POLICY == 'weighted_average_v1'
    assert leg == {
        'leg_id': 'spot', 'scope': json.dumps(s.scope, ensure_ascii=False, separators=(',', ':')), 'spec_hash': s.fingerprint,
        'base_currency': s.asset_id, 'quote_currency': 'USDC', 'quantity': '1.000000',
        'basis_quote': '3.000000', 'average_cost_quote': '3', 'proceeds_quote': '12.000000',
        'released_basis_quote': '9.000000', 'realized_pnl_quote': '3.000000', 'complete': True, 'reasons': (),
    }


def test_missing_cash_receipt_or_unknown_fee_never_emits_pnl(tmp_path):
    con = store.connect(tmp_path / 'trade.db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    _record(con, s, native='buy-1', side='BUY', qty='2', quote='4', complete=False)
    leg = cost_basis.rebuild(con, deal_id='deal-1')['legs'][0]
    assert not leg['complete'] and leg['basis_quote'] is None and leg['realized_pnl_quote'] is None
    assert leg['reasons'] == ('fees_incomplete',)


def test_sell_cannot_use_other_deal_or_unconfirmed_inventory(tmp_path):
    con = store.connect(tmp_path / 'trade.db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    _record(con, s, native='buy-1', side='BUY', qty='2', quote='4')
    _record(con, s, native='sell-1', side='SELL', qty='3', quote='9')
    one = cost_basis.rebuild(con, deal_id='deal-1')['legs'][0]
    assert not one['complete'] and one['realized_pnl_quote'] is None
    assert one['reasons'] == ('sell_exceeds_confirmed_inventory',)
    assert cost_basis.rebuild(con, deal_id='another')['legs'] == ()


def test_generic_report_exposes_confirmed_basis_without_claiming_total_pnl(tmp_path):
    from funding_bot.core.leg_report import build
    from funding_bot.interface.leg_presenter import render
    con = store.connect(tmp_path / 'trade.db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    _record(con, s, native='buy-1', side='BUY', qty='2', quote='4')
    _record(con, s, native='sell-1', side='SELL', qty='1', quote='3')
    report = build(con, deal_id='deal-1')
    basis = report['legs'][0]['cost_basis']
    assert basis['complete'] and basis['realized_pnl_quote'] == '1.000000'
    assert report['pnl'] is None and 'Реализованный спот-результат: 1.000000 USDC.' in render(report)


def test_duplicate_cash_receipt_is_not_silently_selected(tmp_path):
    import json
    con = store.connect(tmp_path / 'trade.db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    _record(con, s, native='buy-1', side='BUY', qty='2', quote='4')
    raw = con.execute("SELECT json FROM exec_events WHERE kind='leg_execution_cash_v1'").fetchone()[0]
    store.event(con, 'leg_execution_cash_v1', deal_id='deal-1', **json.loads(raw))
    leg = cost_basis.rebuild(con, deal_id='deal-1')['legs'][0]
    assert not leg['complete'] and leg['reasons'] == ('cash_receipt_ambiguous',)


def test_malformed_second_fact_never_silently_understates_open_inventory(tmp_path):
    """A corrupt append-only fact for a known spot leg must poison its report."""
    con = store.connect(tmp_path / 'trade.db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    _record(con, s, native='buy-1', side='BUY', qty='2', quote='4')
    raw = con.execute("SELECT json FROM exec_events WHERE kind='leg_execution_fact_v1'").fetchone()[0]
    damaged = json.loads(raw)
    damaged['side'] = 'BROKEN'
    store.event(con, 'leg_execution_fact_v1', deal_id='deal-1', **damaged)
    leg = cost_basis.rebuild(con, deal_id='deal-1')['legs'][0]
    assert not leg['complete']
    assert leg['basis_quote'] is None and leg['average_cost_quote'] is None
    assert leg['reasons'] == ('malformed_execution_event',)
