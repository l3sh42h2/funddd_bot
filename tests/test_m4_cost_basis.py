from decimal import Decimal as D
import json

import pytest

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


_UNSET = '__unset_market_kind__'


def _raw_spot_event(con, s, *, native, side, cash, market_kind=_UNSET, deal_id='deal-1', operation_id='op'):
    """Insert a hand-built fact + matching cash receipt directly into exec_events,
    bypassing ExecutionFact.__post_init__ validation entirely.

    This is the only way to construct a fact whose ``market_kind`` is missing,
    null, empty or an unknown string -- exactly the shape of a damaged or
    pre-discriminator journal row that FINAL-03 is about. ``market_kind=_UNSET``
    (the default) omits the field from the fact altogether.
    """
    native_ref = f'order:{native}'
    scope = json.dumps(s.scope, ensure_ascii=False, separators=(',', ':'))
    identity = json.dumps((operation_id, s.leg_id, scope, native_ref), ensure_ascii=False, separators=(',', ':'))
    fact = dict(version=1, identity=identity, operation_id=operation_id, leg_id=s.leg_id,
                spec_hash=s.fingerprint, scope=scope, native_ref=native_ref, side=side,
                quality='terminal', fees_complete=True, base_currency=s.asset_id,
                settlement_currency=s.settlement_currency, fees=[], funding=[])
    if market_kind != _UNSET:
        fact['market_kind'] = market_kind
    store.event(con, 'leg_execution_fact_v1', deal_id=deal_id, **fact)
    store.event(con, 'leg_execution_cash_v1', deal_id=deal_id, version=1, identity=identity,
                operation_id=operation_id, leg_id=s.leg_id, spec_hash=s.fingerprint, scope=scope,
                native_ref=native_ref, complete=True, cash=cash, notional={}, rent_locked_delta={})


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


# --- FINAL-03: damaged market_kind must poison the leg, not be silently -----
# --- treated like a legitimate `perpetual` fact -----------------------------

@pytest.mark.parametrize('bad_kind', [None, '', 'unknown'])
def test_damaged_market_kind_after_valid_fact_poisons_leg(tmp_path, bad_kind):
    """A damaged discriminator on a second fact for an already-open leg must not
    be silently treated like a legitimate perpetual fact (same append-only-copy
    technique as test_malformed_second_fact_never_silently_understates_open_inventory)."""
    con = store.connect(tmp_path / 'trade.db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    _record(con, s, native='buy-1', side='BUY', qty='2', quote='4')
    raw = con.execute("SELECT json FROM exec_events WHERE kind='leg_execution_fact_v1'").fetchone()[0]
    damaged = json.loads(raw)
    damaged['market_kind'] = bad_kind
    store.event(con, 'leg_execution_fact_v1', deal_id='deal-1', **damaged)
    leg = cost_basis.rebuild(con, deal_id='deal-1')['legs'][0]
    assert leg['complete'] is False, leg
    assert leg['basis_quote'] is None and leg['average_cost_quote'] is None
    assert leg['reasons'] == ('malformed_market_kind',)


@pytest.mark.parametrize('bad_kind', [None, '', 'unknown'])
def test_damaged_market_kind_before_first_valid_fact_poisons_leg(tmp_path, bad_kind):
    """The damaged fact can precede the first valid fact for the same leg in an
    append-only journal; the pending failure must still attach once the leg is
    created -- mirrors the existing malformed_execution_event pending mechanism
    (`malformed_legs`), symmetrically, for the discriminator check."""
    con = store.connect(tmp_path / 'trade.db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    _raw_spot_event(con, s, native='corrupt-0', side='BUY',
                     cash={s.asset_id: '9', s.settlement_currency: '-90'}, market_kind=bad_kind)
    _record(con, s, native='buy-1', side='BUY', qty='2', quote='4')
    leg = cost_basis.rebuild(con, deal_id='deal-1')['legs'][0]
    assert leg['complete'] is False, leg
    assert leg['basis_quote'] is None and leg['average_cost_quote'] is None
    assert leg['reasons'] == ('malformed_market_kind',)
    assert leg['quantity'] == '2.000000'  # only the later, valid fact contributed


@pytest.mark.parametrize('bad_kind', [None, '', 'unknown'])
def test_damaged_market_kind_second_execution_no_longer_silently_dropped(tmp_path, bad_kind):
    """Reproduces the exact FINAL-03 scenario from the external review: two BUYs
    of one spot leg, 1 BASE each (10 and 30 quote); before the fix, a damaged
    market_kind on the second execution made the reader return quantity=1,
    basis_quote=10, average_cost_quote=10, complete=True, reasons=() -- a wrong
    but *trusted* number. It must now be untrusted instead."""
    con = store.connect(tmp_path / 'trade.db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    _raw_spot_event(con, s, native='buy-1', side='BUY',
                     cash={s.asset_id: '1', s.settlement_currency: '-10'}, market_kind='spot')
    _raw_spot_event(con, s, native='buy-2', side='BUY',
                     cash={s.asset_id: '1', s.settlement_currency: '-30'}, market_kind=bad_kind)
    leg = cost_basis.rebuild(con, deal_id='deal-1')['legs'][0]
    assert leg['complete'] is False, leg
    assert leg['basis_quote'] is None and leg['average_cost_quote'] is None
    assert leg['reasons'] == ('malformed_market_kind',)
    # The pre-fix bug reported exactly quantity=1 here too, but with
    # complete=True. The number is unchanged; only the (mis)trust in it is.
    assert leg['quantity'] == '1'


def test_missing_market_kind_field_is_normalized_to_spot_for_leg_accounting_compat(tmp_path):
    """Compatibility decision, documented in PATCHNOTES/final-review-03-cost-
    basis-market-kind-fix-20260916.md: a fact with NO market_kind key at all --
    the shape of a pre-discriminator journal row -- is normalized to "spot",
    exactly like leg_accounting._fact()'s `p.get("market_kind", "spot")`. This
    must actually contribute to the leg, not merely fail to crash. A present
    but null/empty/unknown value never gets this treatment (see the tests
    above)."""
    con = store.connect(tmp_path / 'trade.db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    _raw_spot_event(con, s, native='buy-1', side='BUY',
                     cash={s.asset_id: '1', s.settlement_currency: '-10'}, market_kind='spot')
    _raw_spot_event(con, s, native='buy-2', side='BUY',
                     cash={s.asset_id: '1', s.settlement_currency: '-30'})  # market_kind omitted
    leg = cost_basis.rebuild(con, deal_id='deal-1')['legs'][0]
    assert leg['complete'] is True, leg
    assert leg['quantity'] == '2'
    assert leg['basis_quote'] == '40'
    assert leg['average_cost_quote'] == '20'
    assert leg['reasons'] == ()


def test_legitimate_perpetual_fact_never_affects_spot_basis(tmp_path):
    """Control: a legitimate perpetual fact must still be safely ignored and
    leave the spot leg byte-for-byte unchanged."""
    con = store.connect(tmp_path / 'trade.db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    _record(con, s, native='buy-1', side='BUY', qty='2', quote='4')
    before = cost_basis.rebuild(con, deal_id='deal-1')['legs'][0]
    _raw_spot_event(con, s, native='perp-1', side='BUY',
                     cash={s.asset_id: '5', s.settlement_currency: '-50'}, market_kind='perpetual')
    after = cost_basis.rebuild(con, deal_id='deal-1')['legs'][0]
    assert after == before


def test_perpetual_fact_with_unreadable_leg_key_is_still_just_skipped(tmp_path):
    """A legitimate perpetual fact is skipped outright even with a garbage
    leg_id/scope -- it must not join the deal-wide malformed list, since it
    never claimed to be a spot execution in the first place. Only a *damaged*
    discriminator with an unreadable key does that (see the next test)."""
    con = store.connect(tmp_path / 'trade.db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    _record(con, s, native='buy-1', side='BUY', qty='2', quote='4')
    store.event(con, 'leg_execution_fact_v1', deal_id='deal-1', version=1,
                identity=json.dumps(('op', '', '', 'order:perp-x'), separators=(',', ':')),
                operation_id='op', leg_id='', scope='', native_ref='order:perp-x', side='BUY',
                market_kind='perpetual', fees_complete=True, base_currency='X', settlement_currency='Y')
    leg = cost_basis.rebuild(con, deal_id='deal-1')['legs'][0]
    assert leg['complete'] is True
    assert leg['quantity'] == '2.000000'


@pytest.mark.parametrize('bad_kind', [None, '', 'unknown'])
def test_damaged_market_kind_without_readable_key_poisons_whole_deal(tmp_path, bad_kind):
    """When leg_id/scope cannot be read at all AND market_kind is damaged (not
    the legitimate "perpetual" value), the fact must join the deal-wide
    `malformed` list -- the same treatment already given to any other keyless
    malformed fact -- instead of vanishing without a trace."""
    con = store.connect(tmp_path / 'trade.db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    _record(con, s, native='buy-1', side='BUY', qty='2', quote='4')
    store.event(con, 'leg_execution_fact_v1', deal_id='deal-1', version=1,
                identity=json.dumps(('op', '', '', 'order:mystery'), separators=(',', ':')),
                operation_id='op', leg_id='', scope='', native_ref='order:mystery', side='BUY',
                market_kind=bad_kind, fees_complete=True, base_currency='X', settlement_currency='Y')
    leg = cost_basis.rebuild(con, deal_id='deal-1')['legs'][0]
    assert leg['complete'] is False
    assert 'malformed_execution_event' in leg['reasons']  # deal-wide `malformed` marker


def test_third_currency_cash_movement_still_poisons_leg(tmp_path):
    """Named explicitly by the review among the positive controls that must
    keep passing; wasn't previously covered by this file (only by an
    unmerged scratchpad mutation script)."""
    con = store.connect(tmp_path / 'trade.db')
    s = spec('fixture_cex_spot', 'spot', 'long')
    _raw_spot_event(con, s, native='buy-1', side='BUY',
                     cash={s.asset_id: '1', s.settlement_currency: '-10', 'BNB': '0.1'}, market_kind='spot')
    leg = cost_basis.rebuild(con, deal_id='deal-1')['legs'][0]
    assert leg['complete'] is False
    assert leg['reasons'] == ('third_currency_cash',)
    assert leg['basis_quote'] is None
