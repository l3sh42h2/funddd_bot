"""Authenticated order identity proof for explicit EVM execution migration.

No order submission and no historical accounting activation. Missing retained
orders fail closed; a balance or a numeric order ID alone cannot bind a deal.
"""
from decimal import Decimal as D
import hashlib
import json
import re

from .contracts import AdapterError, ErrorKind


def _number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('missing numeric evidence')
    result = D(str(value))
    if not result.is_finite():
        raise ValueError('nonfinite evidence')
    return result


def _bool(value):
    if type(value) is bool:
        return value
    if value in ('true', 'false'):
        return value == 'true'
    raise ValueError('missing boolean evidence')


def _order_id(value):
    if type(value) is int and value > 0:
        return str(value)
    if type(value) is str and re.fullmatch(r'[1-9][0-9]*', value):
        return value
    raise ValueError('native order ID missing or invalid')


def prove_order(native, row, account):
    if native.history_account() != account:
        raise AdapterError(ErrorKind.IDENTITY, 'legacy evidence account changed')
    cid, symbol = row['client_id'], row['symbol']
    try:
        if native.venue == 'aster':
            body = native._signed_ok('GET', '/fapi/v3/order',
                                     {'symbol': symbol, 'origClientOrderId': cid}, 'migration order identity')
            identity = (body['clientOrderId'], body['symbol'], body['side'],
                        _number(body['origQty']), _number(body['price']), _bool(body['reduceOnly']))
            oid = body['orderId']
            qty, quote = _number(body['executedQty']), _number(body['cumQuote'])
        elif native.venue == 'gate':
            from urllib.parse import quote as escape
            ref = str(row['order_id']) if row['order_id'] is not None else 't-' + cid
            body = native._signed_ok('GET', f'/futures/{native.SETTLE}/orders/{escape(ref, safe="")}',
                                     what='migration order identity', critical=True)
            size = _number(body['size'])
            multiplier = native._m(symbol)
            identity = (body['text'].removeprefix('t-'), body['contract'],
                        'SELL' if size < 0 else 'BUY', abs(size),
                        _number(body['price']) * multiplier, _bool(body['is_reduce_only']))
            oid = body['id']
            qty = abs(size) - abs(_number(body['left']))
            quote = qty * _number(body['fill_price']) * multiplier
            if body['text'] != 't-' + cid:
                raise ValueError('Gate client prefix differs')
        else:
            raise ValueError('unsupported legacy evidence venue')
        expected = (cid, symbol, row['side'], D(row['qty']), D(row['price']), bool(row['reduce_only']))
        if identity != expected or qty < 0 or qty > expected[3] or quote < 0:
            raise ValueError('order identity/amount differs')
        oid = _order_id(oid)
        if row['order_id'] is not None and _order_id(row['order_id']) != oid:
            raise ValueError('native order ID differs')
        for name, amount in (('executed_qty', qty), ('cum_quote', quote)):
            if row[name] is not None and D(row[name]) != amount:
                raise ValueError('recorded execution differs')
        if row['venue'] != native.venue or native.history_account() != account:
            raise ValueError('account/venue changed during evidence read')
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise AdapterError(ErrorKind.IDENTITY, 'authenticated legacy order proof incomplete or conflicting') from None
    # Persist a digest of public order evidence, not request headers/signatures.
    values = dict(account=account, identity=identity, order_id=str(oid), qty=str(qty), quote=str(quote))
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()
