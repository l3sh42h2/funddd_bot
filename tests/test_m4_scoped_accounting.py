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
    con.execute('UPDATE scoped_accounting_schema SET version=3,min_reader=3')
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


@pytest.fixture
def bound_con(con):
    # Use the real production schema, including order/clip/intent relationships.
    from funding_bot.trade import store
    con.executescript(store.SCHEMA)
    for did, created, state, updated in [('A', 1., 'CLOSED', 3.), ('B', 1., 'OPEN', 4.)]:
        con.execute('INSERT INTO deals(id,perp_venue,symbol,sim,created,state,updated) VALUES(?,?,?,?,?,?,?)',
                    (did, 'aster', '龙虾USDT', 0, created, state, updated))
        con.execute('INSERT INTO intents(id,deal_id,kind) VALUES(?,?,?)', ('i'+did, did, 'entry'))
    for cid, iid in [(1, 'iA'), (2, 'iB'), (3, 'iA')]:
        con.execute('INSERT INTO clips(id,intent_id,seq) VALUES(?,?,?)', (cid, iid, cid))
        con.execute('INSERT INTO perp_orders(clip_id,client_id,venue,symbol,order_id) VALUES(?,?,?,?,?)',
                    (cid, 'cid'+str(cid), 'aster', '龙虾USDT', 2))
    s.bind_deal(con, 'A', scope(), now=5)
    s.bind_deal(con, 'B', scope('OwnerB'), now=5)
    return con


def test_deal_account_binding_immutable_and_idempotent(bound_con):
    con = bound_con
    s.bind_deal(con, 'A', scope(), now=6)
    assert s.deal_scope(con, 'A') == scope()
    with pytest.raises(s.ScopedAccountingError, match='immutable'):
        s.bind_deal(con, 'A', scope('OwnerB'))
    with pytest.raises(s.ScopedAccountingError, match='venue/symbol'):
        s.bind_deal(con, 'A', scope(symbol='OTHER'))
    with pytest.raises(s.ScopedAccountingError, match='provenance'):
        s.bind_deal(con, 'A', s.ProvenScope(*scope().key, 'public_config', 'different'))
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        con.execute("DELETE FROM scoped_deal_accounts WHERE deal_id='A'")
    assert s.deal_scope(con, 'missing') is None


def test_bound_fills_never_mix_accounts_or_duplicate_join(bound_con):
    con = bound_con
    s.add_fills(con, scope(), [fill()])
    s.add_fills(con, scope('OwnerB'), [dict(fill(), commission_abs='4')])
    a, b = s.deal_fills(con, 'A'), s.deal_fills(con, 'B')
    assert len(a) == len(b) == 1  # Two A order rows must not double fees.
    assert a[0]['commission_abs'] == '0.01'
    assert b[0]['commission_abs'] == '4'
    assert s.deal_fills(con, 'A', 'iB') == []
    assert s.deal_fills(con, 'A', 'iA') == a
    assert s.deal_fills(con, 'missing') is None


def test_bound_empty_does_not_select_legacy_fills(bound_con):
    bound_con.execute("INSERT INTO perp_fills(venue,trade_id,order_id,commission_abs) VALUES('aster',1,2,'99')")
    assert s.deal_fills(bound_con, 'A') == []
    assert bound_con.execute('SELECT commission_abs FROM perp_fills').fetchone()[0] == '99'


def test_funding_uses_persisted_deal_window_and_account(bound_con):
    con = bound_con
    rows = [dict(tran_id=i, ts=ts, income='1') for i, ts in enumerate([999, 1000, 2000, 3000, 3001])]
    s.add_funding(con, scope(), rows)
    s.add_funding(con, scope('OwnerB'), [dict(r, income='9') for r in rows])
    # Caller cannot extend the closed deal's persisted window or substitute another scope.
    forged = dict(id='A', created=0, state='OPEN', updated=100, perp_venue='gate')
    assert [r['ts'] for r in s.deal_funding(con, forged, until_ms=9999)] == [1000, 2000, 3000]
    assert [r['income'] for r in s.deal_funding(con, forged)] == ['1'] * 3
    assert len(s.deal_funding(con, forged, until_ms=2000)) == 2
    assert len(s.deal_funding(con, dict(id='B'))) == 4
    with pytest.raises(s.ScopedAccountingError):
        s.deal_funding(con, forged, until_ms=999)
    with pytest.raises(s.ScopedAccountingError):
        s.deal_funding(con, forged, until_ms=True)


def test_binding_schema_upgrade_preserves_existing_sources(con):
    s.add_fills(con, scope(), [fill()])
    original = con.execute('SELECT * FROM scoped_perp_fills').fetchall()
    con.execute('DROP TABLE scoped_deal_accounts')
    con.execute('UPDATE scoped_accounting_schema SET version=1,min_reader=1')
    s.migrate(con)
    assert con.execute('SELECT version,min_reader FROM scoped_accounting_schema').fetchone() == (2, 2)
    assert con.execute('SELECT * FROM scoped_perp_fills').fetchall() == original
    assert s.deal_scope(con, 'missing') is None


@pytest.mark.parametrize('version', [2, 3])
def test_missing_binding_table_cannot_bypass_schema_gate(con, version):
    con.execute('DROP TABLE scoped_deal_accounts')
    con.execute('UPDATE scoped_accounting_schema SET version=?,min_reader=?', (version, version))
    with pytest.raises(s.ScopedAccountingError):
        s.deal_scope(con, 'A')
    with pytest.raises(s.ScopedAccountingError):
        s.deal_fills(con, 'A')


def test_scope_reader_refuses_mutated_deal_identity(bound_con):
    bound_con.execute("UPDATE deals SET symbol='OTHER' WHERE id='A'")
    with pytest.raises(s.ScopedAccountingError, match='identity changed'):
        s.deal_scope(bound_con, 'A')


def same_account_deal(con, *, created, state='OPEN', updated=4., order=False):
    con.execute("UPDATE deals SET state='CLOSED' WHERE id='B'")
    con.execute('INSERT INTO deals(id,perp_venue,symbol,sim,created,state,updated) VALUES(?,?,?,?,?,?,?)',
                ('C', 'aster', '龙虾USDT', 0, created, state, updated))
    s.bind_deal(con, 'C', scope())
    if order:
        con.execute("INSERT INTO intents(id,deal_id,kind) VALUES('iC','C','entry')")
        con.execute("INSERT INTO clips(id,intent_id,seq) VALUES(4,'iC',1)")
        con.execute("INSERT INTO perp_orders(clip_id,client_id,venue,symbol,order_id) VALUES(4,'cid4','aster','龙虾USDT',2)")


def test_same_account_order_cannot_be_charged_to_two_deals(bound_con):
    same_account_deal(bound_con, created=4., order=True)
    s.add_fills(bound_con, scope(), [fill()])
    for did in ('A', 'C'):
        with pytest.raises(s.ScopedAccountingError, match='multiple deals'):
            s.deal_fills(bound_con, did)


@pytest.mark.parametrize('created', [2., 3.])
def test_overlapping_funding_and_equal_boundary_refused(bound_con, created):
    same_account_deal(bound_con, created=created)
    s.add_funding(bound_con, scope(), [dict(tran_id=1, ts=3000, income='1')])
    for did in ('A', 'C'):
        with pytest.raises(s.ScopedAccountingError, match='overlapping'):
            s.deal_funding(bound_con, dict(id=did))


def test_disjoint_funding_windows_and_aborted_end(bound_con):
    same_account_deal(bound_con, created=4., state='ABORTED', updated=5.)
    s.add_funding(bound_con, scope(), [dict(tran_id=i, ts=i*1000, income='1') for i in (2,4,5,6)])
    assert [r['ts'] for r in s.deal_funding(bound_con, dict(id='A'))] == [2000]
    assert [r['ts'] for r in s.deal_funding(bound_con, dict(id='C'))] == [4000,5000]


@pytest.mark.parametrize('stream', ['fills', 'funding'])
def test_binding_guard_and_source_share_wal_snapshot(bound_con, tmp_path, stream):
    # Another connection introduces ambiguity and money in ONE commit exactly
    # after the reader's ownership check, before its source SELECT.
    path = tmp_path / 'sources.db'
    reader = sqlite3.connect(path, isolation_level=None)
    bound_con.backup(reader)
    reader.execute('PRAGMA journal_mode=WAL')
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("INSERT INTO deals(id,perp_venue,symbol,sim,created,state,updated) "
                   "VALUES('C','aster','龙虾USDT',0,2,'CLOSED',4)")
    writer.execute("INSERT INTO intents(id,deal_id,kind) VALUES('iC','C','entry')")
    writer.execute("INSERT INTO clips(id,intent_id,seq) VALUES(4,'iC',1)")
    writer.execute("INSERT INTO perp_orders(clip_id,client_id,venue,symbol,order_id) "
                   "VALUES(4,'cid4','aster','龙虾USDT',2)")
    called, errors = [], []
    def concurrent_commit(sql):
        trigger = 'SELECT f.*' if stream == 'fills' else 'SELECT ts,income,tran_id'
        if sql.startswith(trigger) and not called:
            called.append(True)
            try:
                writer.execute('BEGIN IMMEDIATE')
                s.bind_deal(writer, 'C', scope())
                if stream == 'fills':
                    s.add_fills(writer, scope(), [fill()])
                else:
                    s.add_funding(writer, scope(), [dict(tran_id=1, ts=2000, income='1')])
                writer.commit()
            except BaseException as exc:
                errors.append(exc)  # sqlite trace callbacks otherwise swallow exceptions.
    reader.set_trace_callback(concurrent_commit)
    read = lambda: s.deal_fills(reader, 'A') if stream == 'fills' else s.deal_funding(reader, dict(id='A'))
    try:
        assert read() == []
        assert called and not errors
        assert not reader.in_transaction
        with pytest.raises(s.ScopedAccountingError, match='multiple deals|overlapping'):
            read()
        assert not reader.in_transaction
    finally:
        writer.close()
        reader.close()


def test_scoped_reader_keeps_caller_transaction(bound_con):
    bound_con.execute('BEGIN')
    s.add_fills(bound_con, scope(), [fill()])
    assert len(s.deal_fills(bound_con, 'A')) == 1
    assert bound_con.in_transaction
    bound_con.rollback()
    assert s.deal_fills(bound_con, 'A') == []


@pytest.mark.parametrize('table', ['scoped_deal_accounts', 'scoped_perp_fills', 'scoped_funding_income'])
def test_migrate_cannot_mask_lost_financial_table(con, table):
    con.execute('DROP TABLE '+table)
    with pytest.raises(s.ScopedAccountingError, match='table is missing'):
        s.migrate(con)
    assert con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None


def test_migrate_cannot_reset_missing_schema_metadata(con):
    con.execute('DELETE FROM scoped_accounting_schema')
    with pytest.raises(s.ScopedAccountingError, match='metadata is missing'):
        s.migrate(con)


def test_migrate_refuses_lost_metadata_table(con):
    con.execute('DROP TABLE scoped_accounting_schema')
    with pytest.raises(s.ScopedAccountingError, match='without schema metadata'):
        s.migrate(con)
