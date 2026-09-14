"""Common history must not invent account attribution or historical coverage."""
from types import SimpleNamespace as NS
from decimal import Decimal as D
import pytest
from funding_bot.trade.adapters.futures_bindings import bind
from funding_bot.trade.adapters.contracts import AdapterError
from test_migration_m3 import spec


def source(venue='gate', rows=None):
    leg = spec(venue)
    calls = []
    native = NS(venue=venue, history_account=lambda: leg.account)
    def read(symbol, start):
        calls.append((symbol, start))
        return [] if rows is None else rows
    native.history_fills = read
    native.fills = lambda *a: pytest.fail('permissive legacy history must not be used')
    b = bind(native, journal=None, authorize=None, attempt_lookup=None, on_signed=None)
    return leg, native, calls, b


@pytest.mark.parametrize('venue', ['aster', 'gate'])
def test_observed_pages_are_sorted_deduplicated_but_not_complete(venue):
    leg, native, calls, b = source(venue)
    row = dict(symbol=leg.instrument, trade_id=7, qty=D(2))
    native.history_fills = lambda *a: [dict(row, trade_id=9), row, dict(row)]
    page = b.executions(leg, '7')
    assert page.cursor == '10' and page.complete is False
    assert [r['dedup_key'] for r in page.executions] == [(*leg.scope, 7), (*leg.scope, 9)]
    row['qty'] = D(99)
    assert page.executions[0]['native']['qty'] == D(2)


@pytest.mark.parametrize('cursor', [None, '0', '7'])
def test_empty_page_preserves_cursor_without_proving_zero_history(cursor):
    leg, native, calls, b = source()
    page = b.executions(leg, cursor)
    assert page.executions == () and page.cursor == cursor and page.complete is False
    assert calls == [(leg.instrument, 0 if cursor is None else int(cursor))]


@pytest.mark.parametrize('cursor', [-1, True, 0, '-1', '01', '1.0', ' 1', '', '+1'])
def test_invalid_cursor_refused_before_read(cursor):
    leg, native, calls, b = source()
    with pytest.raises(AdapterError):
        b.executions(leg, cursor)
    assert calls == []


@pytest.mark.parametrize('bad', ['venue', 'account', 'missing_identity', 'missing_reader'])
def test_unproven_source_never_uses_legacy_history(bad):
    leg, native, calls, b = source()
    if bad == 'venue': native.venue = 'other'
    if bad == 'account': native.history_account = lambda: 'other'
    if bad == 'missing_identity': del native.history_account
    if bad == 'missing_reader': del native.history_fills
    with pytest.raises(AdapterError):
        b.executions(leg, None)
    assert calls == []


def test_account_change_during_fetch_invalidates_page():
    leg, native, calls, b = source()
    def read(*a):
        native.history_account = lambda: 'other'
        return []
    # Mutating the method must not bypass the post-fetch identity check.
    native.history_fills = read
    with pytest.raises(AdapterError):
        b.executions(leg, None)


@pytest.mark.parametrize('row', [None, {}, {'trade_id': True}, {'trade_id': '7'},
                               {'trade_id': 6}, {'trade_id': 7, 'symbol': 'OTHER'}])
def test_bad_row_does_not_advance_cursor(row):
    leg, native, calls, b = source()
    native.history_fills = lambda *a: [dict(symbol=leg.instrument, **row) if isinstance(row, dict)
                                      and 'symbol' not in row else row]
    with pytest.raises(AdapterError):
        b.executions(leg, '7')


def test_conflicting_duplicate_is_not_last_write_wins():
    leg, native, calls, b = source()
    native.history_fills = lambda *a: [dict(symbol=leg.instrument, trade_id=7, qty=D(q)) for q in (1,2)]
    with pytest.raises(AdapterError):
        b.executions(leg, None)
