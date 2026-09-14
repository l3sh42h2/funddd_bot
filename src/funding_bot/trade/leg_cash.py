"""Currency-separated execution flows and later funding, from public evidence.

Trade notional is not a perpetual cash payment. Margin balances are not inferred
from notional. Refundable rent is cash locked/released, never a trading fee.
All writes use the existing execution journal and reader floor in one transaction.
"""
import hashlib
import json
from decimal import Decimal as D

from . import store
from .adapters.contracts import from_raw

CASH_KIND = 'leg_execution_cash_v1'
FUNDING_KIND = 'leg_funding_fact_v1'
READER = 5


def _write(con, kind, identity, body, *, deal_id=None, intent_id=None, clip_id=None, now=None):
    payload = dict(version=1, identity=identity, **body)
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    with store.tx(con):
        store.require_reader(con, READER, now=now)
        for row in con.execute('SELECT json FROM exec_events WHERE kind=?', (kind,)):
            old = json.loads(row[0])
            if old['identity'] != identity:
                continue
            if old['digest'] != digest:
                raise ValueError('conflicting scoped cash evidence')
            return False
        store.event(con, kind, deal_id=deal_id, intent_id=intent_id, clip_id=clip_id,
                    now=now, digest=digest, **payload)
    return True


def record_execution_cash(con, result, spec, *, operation_id, side, **links):
    from .leg_accounting import fact_from_result
    fact = fact_from_result(result, spec, operation_id=operation_id, side=side)
    flows, notionals, rent = {}, {}, {}
    def add(bucket, currency, amount):
        bucket[currency] = bucket.get(currency, D(0)) + amount
    if result.spot_input_raw is not None:
        if spec.capabilities.market_kind != 'spot':
            raise ValueError('spot flows on perpetual leg')
        inp, out = result.spot_input_raw, result.spot_output_raw
        base = spec.instrument if spec.capabilities.venue_kind == 'dex' else spec.asset_id
        expected = (spec.quote_currency, base) if side == 'BUY' else (base, spec.quote_currency)
        if (inp.asset_id, out.asset_id) != expected:
            raise ValueError('cash assets differ from frozen leg and side')
        base_flow = out if side == 'BUY' else inp
        if from_raw(base_flow.raw, base_flow.decimals) != result.executed_quantity:
            raise ValueError('proven spot quantity differs from native token flow')
        for amount, sign in ((inp, -1), (out, 1)):
            currency = spec.asset_id if amount.asset_id == base else amount.asset_id
            add(flows, currency, sign * from_raw(amount.raw, amount.decimals))
    elif result.trade_notional is not None:
        if (spec.capabilities.market_kind != 'perpetual' or result.trade_notional.currency != spec.quote_currency
                or result.perp_quote.currency != spec.quote_currency):
            raise ValueError('notional differs from frozen perpetual currency')
        add(notionals, result.trade_notional.currency, result.trade_notional.amount)
    complete = result.fees_complete
    for fee in fact.fees:
        c = fee.component or {}
        if c.get('superseded') or (c.get('payer') is not None and c['payer'] != spec.account):
            continue
        if not fee.known or fee.amount is None:
            complete = False
            continue
        if c.get('included'):
            continue
        sign = 1 if c.get('kind') == 'rent_refund' else -1
        add(flows, fee.currency, sign * fee.amount)
        if c.get('refundable'):
            add(rent, fee.currency, -sign * fee.amount)
    return _write(con, CASH_KIND, fact.durable_identity,
        dict(operation_id=operation_id, leg_id=spec.leg_id, spec_hash=spec.fingerprint,
             scope=fact.scope, native_ref=fact.native_ref,
             cash={k: str(v) for k, v in flows.items()},
             notional={k: str(v) for k, v in notionals.items()},
             rent_locked_delta={k: str(v) for k, v in rent.items()}, complete=complete), **links)


def record_funding(con, spec, *, operation_id, native_id, amount, currency, evidence, **links):
    """A signed amount: positive is received, negative paid; native ID dedups."""
    if spec.capabilities.market_kind != 'perpetual':
        raise ValueError('funding requires perpetual leg')
    if not isinstance(amount, D) or not amount.is_finite():
        raise ValueError('funding requires exact finite Decimal')
    if currency not in {spec.settlement_currency, spec.margin_currency}:
        raise ValueError('funding currency differs from frozen leg')
    for value in (operation_id, native_id, currency, evidence):
        if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
            raise ValueError('public funding identity/evidence required')
    scope = json.dumps(spec.scope, ensure_ascii=False, separators=(',', ':'))
    # Funding is native-account scoped; assigning the same receipt to another
    # operation is a conflict, not a second credit.
    identity = json.dumps((scope, native_id), ensure_ascii=False, separators=(',', ':'))
    return _write(con, FUNDING_KIND, identity,
        dict(operation_id=operation_id, leg_id=spec.leg_id, spec_hash=spec.fingerprint,
             scope=scope, native_ref=native_id, amount=str(amount), currency=currency,
             evidence=evidence), **links)


def rebuild(con, *, operation_id=None, deal_id=None):
    legs = {}
    query = 'SELECT kind,json FROM exec_events WHERE kind IN (?,?)'
    args = (CASH_KIND, FUNDING_KIND)
    if deal_id is not None:
        query += ' AND deal_id=?'
        args += (deal_id,)
    for kind, raw in con.execute(query + ' ORDER BY rowid', args):
        p = json.loads(raw)
        if operation_id is not None and p['operation_id'] != operation_id:
            continue
        key = (p['leg_id'], p['scope'])
        leg = legs.setdefault(key, dict(leg_id=p['leg_id'], scope=p['scope'],
            spec_hash=p['spec_hash'], cash={}, notional={}, rent_locked_delta={}, funding={}, complete=True))
        if leg['spec_hash'] != p['spec_hash']:
            raise ValueError('cash scope changed frozen specification')
        if kind == FUNDING_KIND:
            sources = {'funding': {p['currency']: p['amount']}, 'cash': {p['currency']: p['amount']}}
        else:
            sources = {field: p[field] for field in ('cash', 'notional', 'rent_locked_delta')}
            leg['complete'] = leg['complete'] and p['complete']
        for field, values in sources.items():
            for currency, value in values.items():
                leg[field][currency] = leg[field].get(currency, D(0)) + D(value)
    return dict(version=1, operation_id=operation_id, legs=tuple(
        {**leg, **{field: {k: str(v) for k, v in leg[field].items()}
                  for field in ('cash', 'notional', 'rent_locked_delta', 'funding')}}
        for leg in legs.values()))
