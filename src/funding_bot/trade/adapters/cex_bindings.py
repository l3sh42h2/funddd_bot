"""Native CEX-spot order binding (first real user: Binance, binance_spot_trade.BinanceSpotTrade).

Symmetric to futures_bindings.py (which binds Aster/Gate/Hyperliquid's PerpLeg into the generic Adapter
contract) but for a plain CEX spot order book: no wallet, gas, chain or token identity — account/instrument
scope is the exchange's own account and symbol (ADAPTER_GUIDE.md; examples/cex_spot.py is the registration
template, this module is the first non-example implementation). Core supplies durable journal and
authorization; this module has no knowledge of the opposing leg and performs no network call beyond what
`native` already exposes.

Only LIMIT+IOC is quoted here, never a plain MARKET order: the common contract's Quote carries a bound
(max_spend/min_receive) that must be known and enforced BEFORE submit (mirrors futures_bindings.bind's
`price_cap`, and spot_bindings.evm's on-chain min_receive check) — a blind market order gives no such bound.
See binance_spot_trade.py's module docstring for why the native client still exposes a MARKET method that
this binding deliberately never calls.

Side-for-attempt is kept in an in-memory map, populated at submit() time: attempts.AttemptJournal (the
reusable, deal-agnostic durable barrier already used by test_generic_adapter_matrix.py) proves an attempt was
claimed exactly once, but does not itself carry `side`. This makes resolve() work within the same process
only; recovering a claimed-but-crashed attempt's side after a restart needs a persisted side column, which is
deferred together with the rest of the not-yet-built profile/coordinator wiring (see
PATCHNOTES/binance-perp-spot-adapters-20260918.md — open question). A crash before submit is safe regardless
(NativeAdapter.submit already turns any post-claim exception into an UNKNOWN v2 result).

Generic Adapter.cancel() is intentionally left unsupported here (Bindings.cancel stays None), matching every
other venue wired through this framework today (futures_bindings.bind and spot_bindings.evm/solana do not set
it either — IOC's own expiry is the cancellation mechanism for all of them). BinanceSpotTrade.cancel() still
exists and is tested directly as a native method; only the common-contract capability is not claimed.
"""
import json
import time
from decimal import Decimal as D
from .contracts import AdapterError, ErrorKind, Quote, Observation, ExecutionPage


def bind(native, *, journal, authorize, clock=time.time):
    sides: dict[str, str] = {}

    def quote(spec, action, bounds):
        price = bounds.get('price_cap')
        if not isinstance(price, D) or not price.is_finite() or price <= 0:
            raise AdapterError(ErrorKind.INVALID, 'explicit Decimal price_cap required')
        filt = native.filters(spec.instrument)
        if filt.step != spec.step or filt.tick != spec.tick:
            raise AdapterError(ErrorKind.STALE, 'quantity step or tick changed')
        if price % filt.tick != 0:
            raise AdapterError(ErrorKind.INVALID, 'price is off native tick')
        if action.quantity < filt.min_qty or (filt.max_qty > 0 and action.quantity > filt.max_qty):
            raise AdapterError(ErrorKind.INVALID, 'native quantity limits')
        value = price * action.quantity
        if value < filt.min_notional:
            raise AdapterError(ErrorKind.INVALID, 'native minimum notional')
        buy = action.side == 'BUY'
        return Quote(action, clock() + min(float(bounds.get('ttl_s', 5)), 5),
                     value if buy else action.quantity, action.quantity if buy else value,
                     spec.quote_currency if buy else spec.asset_id,
                     spec.asset_id if buy else spec.quote_currency,
                     json.dumps({'price_cap': str(price), 'symbol': spec.instrument}, sort_keys=True))

    def submit(spec, prepared):
        a = prepared.quote.action
        price = D(json.loads(prepared.quote.native)['price_cap'])
        sides[prepared.attempt_id] = a.side

        def on_signed(nonce):
            pass    # no deal-scoped native journal to write-ahead into for this leg (see module docstring)
        return native.order(spec.instrument, a.side, a.quantity, prepared.attempt_id, price=price,
                            on_signed=on_signed)

    def resolve(spec, ref):
        side = sides.get(ref)
        if side is None:
            raise AdapterError(ErrorKind.IDENTITY, 'attempt side unknown in this process; cannot resolve safely')
        return native.query(spec.instrument, ref), side

    def observe(spec):
        qty = native.balance(spec.asset_id)
        available = native.balance(spec.quote_currency)
        return Observation(qty, clock(), spec.venue + ':account', 'unknown' if qty is None else 'authoritative',
                           available, spec.quote_currency)

    def executions(spec, cursor):
        if cursor is not None and (type(cursor) is not str or not cursor.lstrip('-').isdigit()):
            raise AdapterError(ErrorKind.INVALID, 'canonical nonnegative trade cursor required')
        start = 0 if cursor is None else int(cursor)
        identity = getattr(native, 'history_account', None)
        if native.venue != spec.venue or not callable(identity) or identity() != spec.account:
            raise AdapterError(ErrorKind.IDENTITY, 'execution history account differs from frozen leg')
        reader = getattr(native, 'history_fills', None)
        if not callable(reader):
            raise AdapterError(ErrorKind.UNSUPPORTED, 'strict native execution history required')
        rows = reader(spec.instrument, start)
        if native.venue != spec.venue or identity() != spec.account:
            raise AdapterError(ErrorKind.IDENTITY, 'execution history account differs from frozen leg')
        if type(rows) not in (list, tuple):
            raise AdapterError(ErrorKind.INVALID, 'materialized execution history required')
        seen = {}
        for row in rows:
            if (type(row) is not dict or row.get('symbol') != spec.instrument or
                    type(row.get('trade_id')) is not int or row['trade_id'] < start):
                raise AdapterError(ErrorKind.IDENTITY, 'execution row outside requested instrument or cursor')
            tid = row['trade_id']
            if tid in seen and seen[tid] != row:
                raise AdapterError(ErrorKind.IDENTITY, 'conflicting native execution ID')
            seen[tid] = dict(row)
        values = tuple({'dedup_key': (*spec.scope, tid), 'native': seen[tid]} for tid in sorted(seen))
        next_cursor = str(max(seen) + 1) if seen else cursor
        return ExecutionPage(values, next_cursor, False)

    from .native import Bindings
    return Bindings(native, journal, authorize, quote, submit, resolve, observe, executions, clock=clock)
