"""Account-aware accounting boundary used by execution, reconciliation and readers.

Unbound deals retain the legacy reader during explicit migration. Bound deals
never use a legacy fill, cursor, funding sum or cached money result. A native
history list proves observed rows, not completeness of an arbitrary time window.
"""
from dataclasses import dataclass
from decimal import Decimal as D
import hashlib
import json
from types import MappingProxyType
from collections.abc import Mapping

from . import scoped_accounting as scoped, store

ZERO = D(0)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def _decimal(value):
    try:
        n = D(str(value))
        return n if n.is_finite() else None
    except Exception:
        return None


def _rows(con, sql, args=()):
    cur = con.execute(sql, args)
    names = [x[0] for x in cur.description]
    return [dict(zip(names, tuple(row))) for row in cur]


def is_bound(con, deal_id):
    return scoped.deal_scope(con, deal_id) is not None


def _native_matches(native, scope):
    identity = getattr(native, 'history_account', None)
    if native.venue != scope.venue or not callable(identity) or identity() != scope.account_scope:
        raise scoped.ScopedAccountingError('history source account is not proven for the bound deal')


def _funding_currency(con, deal_id, rows):
    deal = store.get_deal(con, deal_id)
    try:
        quote = json.loads(deal['inst_json'])['quote_asset']
    except (KeyError, TypeError, ValueError):
        quote = None
    if not quote or any(row.get('asset') != quote for row in rows):
        raise scoped.ScopedAccountingError('funding currency is not proven for the frozen instrument')


def sync_fills(con, deal, legs):
    scope = scoped.deal_scope(con, deal['id'])
    if scope is None:
        last = store.last_trade_id(con, legs.fill_venue)
        store.add_perp_fills(con, legs.fill_venue, legs.perp.fills(deal['symbol'], None if last is None else last + 1))
        return
    _native_matches(legs.perp, scope)
    cursor = scoped.get_cursor(con, scope, 'fills')
    start = int(cursor['cursor']) if cursor and cursor['cursor'] is not None else 0
    rows = legs.perp.history_fills(scope.symbol, start)
    _native_matches(legs.perp, scope)
    next_id = max([start] + [int(r['trade_id']) + 1 for r in rows])
    # from_id=0 may still mean a retention-limited window. Completeness of
    # commissions is established below by exact per-order quantity/quote cover.
    scoped.ingest_fills_page(con, scope, rows, cursor=str(next_id), watermark_ms=None, complete=False)


def sync_funding(con, deal, native, *, legacy_start=None):
    scope = scoped.deal_scope(con, deal['id'])
    if scope is None:
        start = int(float(deal['created']) * 1000) if legacy_start is None else legacy_start
        store.add_funding_income(con, native.venue, native.funding_income(deal['symbol'], start))
        return
    _native_matches(native, scope)
    # Until an adapter supplies a proven range, retain observations but don't
    # claim that an empty response represents a complete zero-income history.
    saved = store.get_deal(con, deal['id'])
    start = int(float(saved['created']) * 1000)
    window_fn = getattr(native, 'history_funding_window', None)
    if callable(window_fn):
        window = window_fn(scope.symbol, start)
        if not isinstance(window, FundingWindow):
            raise scoped.ScopedAccountingError('native funding window has unsupported type')
        _native_matches(native, scope)
        save_funding_window(con, deal['id'], scope, window)
    else:
        rows = native.history_funding(scope.symbol, start)
        _native_matches(native, scope)
        _funding_currency(con, deal['id'], rows)
        scoped.ingest_funding_page(con, scope, rows, cursor=None, watermark_ms=None, complete=False)


@dataclass(frozen=True)
class FundingWindow:
    """Adapter evidence for a fully traversed, retention-supported time range.

    This is a contract with the native adapter, not a flag inferred from list
    length. The adapter must refuse lost pages, conflicting IDs, unknown account
    identity and a requested start outside documented retrievable history.
    """
    rows: tuple
    start_ms: int
    through_ms: int
    complete: bool
    proof_ref: str

    def __post_init__(self):
        if type(self.rows) is not tuple:
            raise scoped.ScopedAccountingError('funding rows must be a materialized tuple')
        keys = {'tran_id','income','ts','asset','symbol','venue','account_scope'}
        copied = []
        for row in self.rows:
            if not isinstance(row, Mapping):
                raise scoped.ScopedAccountingError('funding row must be an object')
            values = {key: row[key] for key in keys if key in row}
            if any(value is not None and type(value) not in (str,int,bool,D) for value in values.values()):
                raise scoped.ScopedAccountingError('funding row fields must be immutable exact scalars')
            copied.append(MappingProxyType(values))
        object.__setattr__(self, 'rows', tuple(copied))


def _funding_digest(con, scope, start, end):
    return _hash(_rows(con, 'SELECT tran_id,income,ts FROM scoped_funding_income WHERE account_scope=? AND venue=? AND symbol=? AND ts>=? AND ts<=? ORDER BY ts,tran_id',
                       (*scope.key, start, end)))


def save_funding_window(con, deal_id, scope, window):
    """Commit validated observations and their range proof as one atomic event."""
    if type(window) is not FundingWindow:
        raise scoped.ScopedAccountingError('funding window must use the immutable history contract')
    start = scoped._timestamp_ms(window.start_ms, 'window.start')
    end = scoped._timestamp_ms(window.through_ms, 'window.end')
    if end < start or type(window.complete) is not bool:
        raise scoped.ScopedAccountingError('invalid funding coverage window')
    if not isinstance(window.proof_ref, str) or scoped._PUBLIC_REF.fullmatch(window.proof_ref) is None:
        raise scoped.ScopedAccountingError('funding proof reference must be public')
    for row in window.rows:
        if not start <= scoped._timestamp_ms(row.get('ts')) <= end:
            raise scoped.ScopedAccountingError('funding observation outside proven window')
    with scoped._tx(con):
        if scoped.deal_scope(con, deal_id) != scope:
            raise scoped.ScopedAccountingError('funding window differs from bound deal')
        _funding_currency(con, deal_id, window.rows)
        scoped.add_funding(con, scope, window.rows)
        if window.complete:
            expected = {}
            for row in window.rows:
                values = scoped._funding_values(scope, row, 0)
                expected[values[3]] = dict(tran_id=values[3], income=values[4], ts=values[5])
            evidence_hash = _hash(sorted(expected.values(), key=lambda r: (r['ts'], r['tran_id'])))
            if evidence_hash != _funding_digest(con, scope, start, end):
                raise scoped.ScopedAccountingError('complete funding evidence omits persisted observations')
            store.event(con, 'accounting_funding_window', deal_id=deal_id, account_scope=scope.account_scope,
                        venue=scope.venue, symbol=scope.symbol, start_ms=start, through_ms=end,
                        proof_ref=window.proof_ref, source_hash=evidence_hash)


def _funding_total(con, deal, scope, rows, until_ms):
    end = until_ms
    if deal['state'] in ('CLOSED', 'ABORTED'):
        closed = int(float(deal['updated']) * 1000)
        end = closed if end is None else min(end, closed)
    if end is None:
        return None, None  # Active accounting requires an explicit valuation cut.
    start = int(float(deal['created']) * 1000)
    for event in _rows(con, "SELECT json FROM exec_events WHERE deal_id=? AND kind='accounting_funding_window' ORDER BY rowid DESC", (deal['id'],)):
        try:
            proof = json.loads(event['json'])
            a, b = proof['start_ms'], proof['through_ms']
            if ((proof['account_scope'], proof['venue'], proof['symbol']) != scope.key or
                    type(a) is not int or type(b) is not int or a > start or b < end):
                continue
            if proof['source_hash'] != _funding_digest(con, scope, a, b):
                continue
            values = [_decimal(r['income']) for r in rows]
            return (None if None in values else sum(values, ZERO)), b
        except (KeyError, TypeError, ValueError):
            continue
    return None, None


@dataclass(frozen=True)
class Sources:
    account: str
    fills: tuple
    funding_rows: tuple
    fees: D | None
    funding: D | None
    missing: tuple[str, ...]
    revision: str
    funding_through_ms: int | None = None


@scoped._consistent_read
def sources(con, deal_id, *, until_ms=None):
    scope = scoped.deal_scope(con, deal_id)
    if scope is None:
        return None
    deal = _rows(con, 'SELECT * FROM deals WHERE id=?', (deal_id,))[0]
    fills = scoped.deal_fills(con, deal_id)
    funding = scoped.deal_funding(con, deal, until_ms=until_ms)
    orders = _rows(con, '''SELECT o.* FROM perp_orders o JOIN clips c ON c.id=o.clip_id
                         JOIN intents i ON i.id=c.intent_id WHERE i.deal_id=? ORDER BY o.id''', (deal_id,))
    try:
        quote = json.loads(deal['inst_json'])['quote_asset']
    except (ValueError, TypeError, KeyError):
        quote = None
    missing, fees = [], ZERO
    grouped = {}
    for f in fills:
        grouped.setdefault(str(f['order_id']), []).append(f)
    counted = set()
    ids = [str(o['order_id']) for o in orders if o['order_id'] is not None]
    for o in orders:
        key = str(o['order_id'])
        rows = grouped.get(key, [])
        qty, value = _decimal(o['executed_qty']), _decimal(o['cum_quote'])
        if o['venue'] != scope.venue or o['symbol'] != scope.symbol or ids.count(key) > 1:
            missing.append('fees:order_identity:' + str(o['id']))
            continue
        if o['state'] in ('NOT_PLACED', 'REJECTED') and qty in (None, ZERO) and value in (None, ZERO) and not rows:
            continue
        if o['state'] not in ('FILLED', 'PARTIALLY_FILLED', 'EXPIRED', 'CANCELED', 'CANCELLED') or qty is None or qty < 0:
            missing.append('fees:order:' + str(o['id']))
            continue
        if qty == 0 and value == ZERO:
            if rows:
                missing.append('fees:zero_order_has_fills:' + str(o['id']))
            continue
        if o['state'] not in ('FILLED', 'PARTIALLY_FILLED'):
            missing.append('execution:positive_cancel:' + str(o['id']))
            continue
        quantities = [_decimal(f['qty']) for f in rows]
        values = [_decimal(f['quote_qty']) for f in rows]
        commissions = [_decimal(f['commission_abs']) for f in rows]
        if (not quote or not rows or value is None or value <= 0 or None in quantities + values + commissions or
                sum(quantities, ZERO) != qty or sum(values, ZERO) != value or
                any(f['commission_asset'] != quote for f in rows)):
            missing.append('fees:order:' + str(o['id']))
            continue
        if key not in counted:
            fees += sum(commissions, ZERO)
            counted.add(key)
    known_fees = None if missing else fees
    from .engine import intent_txs
    for (iid,) in con.execute('SELECT id FROM intents WHERE deal_id=?', (deal_id,)):
        if not gas_complete(intent_txs(con, iid)):
            missing.append('gas:receipt_unproven:' + str(iid))
    fund, through = _funding_total(con, deal, scope, funding, until_ms)
    if fund is None:
        missing.append('funding:coverage_unproven')
    revision = _hash((scope.key, deal, orders, fills, funding, fund, through, missing))
    return Sources(scope.account_scope, tuple(fills), tuple(funding), known_fees,
                   fund, tuple(missing), revision, through)


def gas_complete(txs):
    """Every submitted EVM transaction needs explicit nonnegative receipt costs."""
    from .report import SWAP_KINDS
    txs = tuple(txs)
    def mined(row):
        used, price = _decimal(row.get('gas_used')), _decimal(row.get('eff_gas_price'))
        return (row.get('state') in ('MINED_OK', 'MINED_REVERTED') and
                used is not None and price is not None and used >= 0 and price >= 0)
    for row in txs:
        if row.get('kind') not in (*SWAP_KINDS, 'approve'):
            continue
        if row.get('state') == 'REPLACED':
            group = tuple(row.get(k) for k in ('chain', 'wallet', 'nonce'))
            if (None not in group and all(group[:2]) and row.get('gas_used') is None and
                    row.get('eff_gas_price') is None and sum(
                    bool(sibling.get('tx_hash')) and sibling.get('tx_hash') != row.get('tx_hash') and
                    tuple(sibling.get(k) for k in ('chain', 'wallet', 'nonce')) == group and mined(sibling)
                    for sibling in txs) == 1):
                continue
            return False
        if not mined(row):
            return False
    return True


def legacy_event_allowed(con, deal_id):
    """Old final.cost_usd has no source revision and cannot survive binding."""
    return not is_bound(con, deal_id)


@scoped._consistent_read
def cost_revision(con, deal_id, intent_id):
    """Proven execution inputs for one final cost; funding is a separate stream."""
    scope = scoped.deal_scope(con, deal_id)
    if scope is None:
        return None
    intent = store.get_intent(con, intent_id)
    if intent is None or intent['deal_id'] != deal_id:
        raise scoped.ScopedAccountingError('cost intent belongs to another deal')
    from .engine import intent_txs
    return _hash((scope.key, store.get_deal(con, deal_id)['inst_json'], intent['plan_json'],
                  _rows(con, 'SELECT * FROM clips WHERE intent_id=? ORDER BY id', (intent_id,)),
                  _rows(con, '''SELECT o.* FROM perp_orders o JOIN clips c ON c.id=o.clip_id
                               WHERE c.intent_id=? ORDER BY o.id''', (intent_id,)),
                  scoped.deal_fills(con, deal_id, intent_id), intent_txs(con, intent_id)))


@scoped._consistent_read
def event_cost(con, deal_id, intent_id, payload):
    cost = _decimal(payload.get('cost_usd'))
    if not is_bound(con, deal_id):
        return cost
    expected = cost_revision(con, deal_id, intent_id)
    return cost if payload.get('accounting_cost_revision') == expected else None


@scoped._consistent_read
def execution_summary(con, deal_id, intent_id):
    """Confirmed order amounts are independent of arrival of accounting fills."""
    scope = scoped.deal_scope(con, deal_id)
    if scope is None:
        return None
    intent = store.get_intent(con, intent_id)
    if intent is None or intent['deal_id'] != deal_id:
        raise scoped.ScopedAccountingError('execution summary belongs to another deal')
    rows = _rows(con, '''SELECT o.* FROM perp_orders o JOIN clips c ON c.id=o.clip_id
                        WHERE c.intent_id=? ORDER BY o.id''', (intent_id,))
    fills = scoped.deal_fills(con, deal_id, intent_id)
    qty, quote, seen = ZERO, ZERO, set()
    for row in rows:
        q, v = _decimal(row['executed_qty']), _decimal(row['cum_quote'])
        matched = [f for f in fills if str(f['order_id']) == str(row['order_id'])]
        if row['venue'] != scope.venue or row['symbol'] != scope.symbol:
            qty = quote = None
            break
        if row['state'] in ('REJECTED', 'NOT_PLACED') and q in (None, ZERO) and v in (None, ZERO) and not matched:
            continue
        if (row['state'] not in ('FILLED','PARTIALLY_FILLED','EXPIRED','CANCELLED','CANCELED') or
                q is None or q < 0 or v is None or v < 0 or (q == 0) != (v == 0) or
                row['order_id'] in seen or sum((_decimal(f['qty']) for f in matched),ZERO) > q):
            qty = quote = None
            break
        seen.add(row['order_id'])
        qty += q
        quote += v
    source = sources(con, deal_id)
    fees = sum((_decimal(f['commission_abs']) for f in fills),ZERO) if source.fees is not None else None
    return dict(qty=qty, quote=quote, vwap=quote/qty if qty else None, commission_usd=fees,
                commission_other={}, maker_quote=None, taker_quote=None, maker_share=None,
                realized_pnl=None, fills=len(fills))
