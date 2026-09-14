"""Native PerpLeg binding, reusable by Aster/Gate/Hyperliquid and either spot network.

Core supplies durable journal and authorization. No knowledge of the opposing leg.
Trade operation / legacy journal mirroring is wired by M4 before any live activation.
"""
import json
import time
from decimal import Decimal as D
from .contracts import AdapterError, ErrorKind, Quote, Observation, ExecutionPage
from .native import Bindings


def bind(native, *, journal, authorize, attempt_lookup, on_signed, clock=time.time,
         hedge=False, links=None):
    # These are core-owned execution permissions and journal links, never quote input.
    if type(hedge) is not bool:
        raise AdapterError(ErrorKind.CONFIG, 'hedge permission must be boolean')
    native_links = dict(links) if links is not None else None
    if native_links is not None and set(native_links) != {'deal_id', 'intent_id', 'clip_id'}:
        raise AdapterError(ErrorKind.CONFIG, 'incomplete native journal links')
    def quote(spec, action, bounds):
        price = bounds.get('price_cap')
        if not isinstance(price, D) or not price.is_finite() or price <= 0:
            raise AdapterError(ErrorKind.INVALID, 'explicit Decimal price_cap required')
        instrument = native.instrument(spec.instrument)
        if instrument.m != spec.multiplier or instrument.symbol != spec.instrument or instrument.quote_asset != spec.quote_currency:
            raise AdapterError(ErrorKind.IDENTITY, "native contract identity or multiplier changed")
        filt = native.filters(spec.instrument)
        if filt.step != spec.step:
            raise AdapterError(ErrorKind.STALE, 'quantity step changed')
        quantize = getattr(native, 'quantize_px', None)
        valid = quantize(spec.instrument, price, action.side) == price if quantize else price % filt.tick == 0
        if not valid:
            raise AdapterError(ErrorKind.INVALID, 'price is off native tick')
        if action.quantity < filt.min_qty or action.quantity > filt.max_qty_limit:
            raise AdapterError(ErrorKind.INVALID, 'native quantity limits')
        value = price * action.quantity
        if value < filt.min_notional and not (action.reduce_only and spec.venue == 'hyperliquid'):
            raise AdapterError(ErrorKind.INVALID, 'native minimum notional')
        # IOC quantity/price bounds; fees are deliberately not declared known here.
        buy = action.side == 'BUY'
        return Quote(action, clock() + min(float(bounds.get('ttl_s', 5)), 5),
                     value if buy else action.quantity, action.quantity if buy else value,
                     spec.quote_currency if buy else spec.asset_id,
                     spec.asset_id if buy else spec.quote_currency,
                     json.dumps({'price_cap': str(price), 'symbol': spec.instrument}, sort_keys=True))

    def submit(spec, prepared):
        a = prepared.quote.action
        price = D(json.loads(prepared.quote.native)['price_cap'])
        # Existing native adapter performs its clock/mode, signing and unknown-result guards.
        kwargs = {'on_signed': lambda nonce: on_signed(prepared.attempt_id, nonce)}
        if hedge:
            kwargs['hedge'] = True
        if spec.venue == 'hyperliquid' and native_links is not None:
            kwargs['links'] = dict(native_links)
        return native.ioc(spec.instrument, a.side, a.quantity, price, prepared.attempt_id, a.reduce_only,
                          **kwargs)

    def resolve(spec, ref):
        record = attempt_lookup(ref)
        if record['leg_id'] != spec.leg_id or record['symbol'] != spec.instrument:
            raise AdapterError(ErrorKind.IDENTITY, 'stored native attempt differs from leg')
        return native.query(spec.instrument, ref), record['side']

    def observe(spec):
        qty = native.position(spec.instrument)
        margin = native.available_margin()
        return Observation(qty, clock(), spec.venue + ':account', 'unknown' if qty is None else 'authoritative',
                           margin, spec.margin_currency)

    def executions(spec, cursor):
        if spec.venue == 'hyperliquid':
            start = int(cursor) if cursor is not None else 0
            page = native.fills_since(spec.instrument, start)
            # Overlap the boundary millisecond; core deduplicates using scoped tid.
            rows = tuple({'dedup_key': (*spec.scope, r['time'], r['tid']), 'native': r} for r in page.rows)
            return ExecutionPage(rows, str(page.last_time) if page.last_time is not None else cursor, page.complete)
        rows = native.fills(spec.instrument, 0 if cursor is None else int(cursor))
        values = tuple({'dedup_key': (*spec.scope, r['trade_id']), 'native': r} for r in rows)
        # Both native adapters exhaust from_id pages or raise on their page limit.
        next_cursor = str(max(int(r['trade_id']) for r in rows) + 1) if rows else cursor
        return ExecutionPage(values, next_cursor, True)

    return Bindings(native, journal, authorize, quote, submit, resolve, observe, executions, clock=clock)
