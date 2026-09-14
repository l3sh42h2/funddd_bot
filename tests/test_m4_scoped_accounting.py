import json
import sqlite3
from decimal import Decimal, localcontext

import pytest
from funding_bot.trade import scoped_accounting as s


def scope(account='OwnerA', symbol='龙虾USDT'):
    return s.ProvenScope('acct:v1:'+account, 'aster', symbol, 'migration_manifest', 'fixture:1')


def fill(**kw):
    return dict(trade_id=1, order_id=2, price='2', qty='3', quote_qty='6',
                commission_abs='0.01', commission_asset='USDT', maker=False,
                realized_pnl='0', ts=1000, **kw)


@pytest.fixture
def con():
    c = sqlite3.connect(':memory:', isolation_level=None)
    c.execute('PRAGMA foreign_keys=ON')
    s.migrate(c)
    yield c
    c.close()


def test_account_symbol_namespaces_and_exact_replay(con):
    a, b, c = scope(), scope('OwnerB'), scope(symbol='OTHERUSDT')
    for sc in (a, b, c):
        assert s.add_fills(con, sc, [fill()]) == 1
        assert s.add_fills(con, sc, [fill()]) == 0
        assert s.add_funding(con, sc, [dict(tran_id=1, income='0.2', ts=1000)]) == 1
    assert con.execute('SELECT count(*) FROM scoped_perp_fills').fetchone()[0] == 3
    assert con.execute('SELECT count(*) FROM scoped_funding_income').fetchone()[0] == 3
    assert scope('OwnerA').key != scope('ownera').key
    with pytest.raises(s.ScopedAccountingError):
        s.add_fills(con, a, [dict(fill(), qty='4')])
    with pytest.raises(s.ScopedAccountingError):
        s.add_fills(con, a, [fill(symbol='OTHERUSDT')])


@pytest.mark.parametrize('outer', [False, True])
def test_page_and_cursor_rollback_even_when_caller_catches_exception(con, outer):
    if outer:
        con.execute('BEGIN')
    with pytest.raises(s.ScopedAccountingError):
        s.ingest_fills_page(con, scope(), [fill(), dict(fill(), trade_id=2, qty='NaN')],
                            cursor='3', watermark_ms=1000, complete=True)
    if outer:
        con.commit()
    assert con.execute('SELECT count(*) FROM scoped_perp_fills').fetchone()[0] == 0
    assert s.get_cursor(con, scope(), 'fills') is None


def test_cursor_does_not_hide_a_known_gap(con):
    a, b = scope(), scope('OwnerB')
    s.ingest_fills_page(con, a, [fill()], cursor='2', watermark_ms=1000, complete=False, gap='page_missing')
    s.ingest_fills_page(con, a, [], cursor='3', watermark_ms=1001, complete=True)
    row = s.get_cursor(con, a, 'fills')
    assert row['gap'] == 'page_missing' and row['complete'] == 0
    assert s.get_cursor(con, b, 'fills') is None
    s.ingest_fills_page(con, a, [], cursor='3', watermark_ms=1001, complete=True, clear_gap=True)
    assert s.get_cursor(con, a, 'fills')['complete'] == 1


@pytest.mark.parametrize('gap', [None, 'missing_page'])
def test_old_page_cannot_certify_newer_watermark_or_clear_its_gap(con, gap):
    a = scope()
    s.set_cursor(con, a, 'fills', cursor='2', watermark_ms=2000, complete=False, gap=gap)
    with pytest.raises(s.ScopedAccountingError):
        s.ingest_fills_page(con, a, [fill()], cursor='1', watermark_ms=1000,
                           complete=True, clear_gap=bool(gap))
    row = s.get_cursor(con, a, 'fills')
    assert row['watermark_ms'] == 2000 and row['complete'] == 0 and row['gap'] == gap
    assert con.execute('SELECT count(*) FROM scoped_perp_fills').fetchone()[0] == 0


def test_exact_decimal_normalization_does_not_round_to_context_precision(con):
    value = '123456789012345678901234567890.123456789'
    with localcontext() as ctx:
        ctx.prec = 6
        s.add_fills(con, scope(), [dict(fill(), price=Decimal(value), commission_abs=Decimal('-'+value))])
    assert con.execute('SELECT price FROM scoped_perp_fills').fetchone()[0] == value
    assert con.execute('SELECT commission_abs FROM scoped_perp_fills').fetchone()[0] == value
    assert s.add_fills(con, scope(), [dict(fill(), price=value+'0', commission_abs=value)]) == 0


def test_legacy_unknown_is_preserved_without_assigning_active_account(con):
    con.execute('CREATE TABLE funding_income(venue TEXT, tran_id INT, symbol TEXT, income TEXT, ts INT)')
    con.execute("INSERT INTO funding_income VALUES('aster',1,'龙虾USDT','0.12',1000)")
    report = s.import_legacy(con, {})
    assert report.unknown_recorded == 1 and report.funding_imported == 0
    saved = con.execute('SELECT payload_json,source_schema FROM scoped_legacy_unknown').fetchone()
    assert json.loads(saved[0])['income'] == '0.12'
    assert len(json.loads(saved[1])['columns']) == 5
    assert s.import_legacy(con, {}).unknown_recorded == 0
    mapping = {s.LegacyKey('funding', 'aster', 1): scope()}
    assert s.import_legacy(con, mapping).funding_imported == 1
    assert s.import_legacy(con, mapping).funding_imported == 0
    with pytest.raises(s.ScopedAccountingError):
        s.import_legacy(con, {s.LegacyKey('funding', 'aster', 1): scope('OwnerB')})
    assert con.execute('SELECT count(*) FROM scoped_funding_income').fetchone()[0] == 1
    assert con.execute('SELECT count(*) FROM scoped_legacy_unknown').fetchone()[0] == 1
    assert con.execute('SELECT income FROM funding_income').fetchone()[0] == '0.12'


def test_schema_version_gate_and_append_only(con):
    s.migrate(con)
    s.add_fills(con, scope(), [fill()])
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE scoped_perp_fills SET qty='4'")
    con.execute('UPDATE scoped_accounting_schema SET version=2,min_reader=2')
    with pytest.raises(s.SchemaTooNew):
        s.migrate(con)
    with pytest.raises(s.SchemaTooNew):
        s.add_fills(con, scope(), [fill()])


@pytest.mark.parametrize('field,value', [('account_scope','acct:v1:OwnerB'), ('venue','gate'), ('symbol','OTHER')])
@pytest.mark.parametrize('stream', ['fills', 'funding'])
def test_explicit_foreign_namespace_cannot_be_restamped(con, field, value, stream):
    row = fill() if stream == 'fills' else dict(tran_id=1, income='0.2', ts=1000)
    row[field] = value
    with pytest.raises(s.ScopedAccountingError):
        getattr(s, 'add_'+stream)(con, scope(), [row])


def test_negative_quote_refused_and_unknown_quote_retained(con):
    with pytest.raises(s.ScopedAccountingError):
        s.add_fills(con, scope(), [dict(fill(), quote_qty='-6')])
    s.add_fills(con, scope(), [dict(fill(), quote_qty=None)])
    assert con.execute('SELECT quote_qty FROM scoped_perp_fills').fetchone()[0] is None
