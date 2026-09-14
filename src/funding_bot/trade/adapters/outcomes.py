"""Native exchange states translated at the adapter boundary, never in the coordinator."""
from decimal import Decimal as D

from ..keys import redact
from .contracts import (AdapterError, ErrorKind, NativeRef, QuoteAmount, RawAmount,
                        Result, Status)

_REDUCE = frozenset({'reduce_only', 'position_closed', 'reduce_out', 'REDUCE_ONLY', 'REDUCE_ONLY_FAIL',
                     'POSITION_EMPTY', 'REDUCE_EXCEEDED', 'INCREASE_POSITION'})
_PERP_TERMINAL = frozenset({'FILLED', 'EXPIRED', 'CANCELED', 'CANCELLED', 'REJECTED'})


def rejection(fill):
    code, kind = fill.err_code, getattr(fill, 'err_kind', None)
    if code == -1111 or kind == 'tick_size':
        return 'precision'
    if code == -2022 or code in _REDUCE or kind == 'reduce_only':
        return 'reduce_only'
    return 'rejected'


def _identity(spec, native_ref):
    return dict(version=2, leg_id=spec.leg_id, spec_hash=spec.fingerprint,
                scope=spec.scope, native_ref=native_ref)


def _decimal(value):
    return value if isinstance(value, D) and value.is_finite() and value >= 0 else None


def _raw(value):
    return value if type(value) is int and value >= 0 else None


def _perp_ref(fill):
    if getattr(fill, 'order_id', None) is not None:
        return NativeRef('order', str(fill.order_id))
    client_id = getattr(fill, 'client_id', None)
    if client_id is None or not str(client_id):
        raise AdapterError(ErrorKind.IDENTITY, 'perpetual native order reference missing')
    return NativeRef('client_order', str(client_id))


def _spot_ref(kind, value):
    if value is None or not str(value):
        raise AdapterError(ErrorKind.IDENTITY, 'spot native reference missing')
    return NativeRef(kind, str(value))


def _spot_amounts(spec, side, amount_in, amount_out):
    if side not in {'BUY', 'SELL'}:
        raise AdapterError(ErrorKind.IDENTITY, 'spot result side missing')
    if spec.decimals is None or spec.quote_decimals is None:
        raise AdapterError(ErrorKind.IDENTITY, 'spot asset decimals incomplete')
    raw_in, raw_out = _raw(amount_in), _raw(amount_out)
    if raw_in is None or raw_out is None:
        return None
    if side == 'BUY':
        incoming = RawAmount(spec.quote_currency, raw_in, spec.quote_decimals)
        outgoing = RawAmount(spec.asset_id, raw_out, spec.decimals)
        base, quote = outgoing.amount, incoming.amount
    else:
        incoming = RawAmount(spec.asset_id, raw_in, spec.decimals)
        outgoing = RawAmount(spec.quote_currency, raw_out, spec.quote_decimals)
        base, quote = incoming.amount, outgoing.amount
    avg = QuoteAmount(quote / base, spec.quote_currency) if base > 0 else None
    return incoming, outgoing, base, avg


def _legacy_perpetual(fill, scope):
    statuses = {'FILLED': Status.SETTLED, 'PARTIALLY_FILLED': Status.PARTIAL,
                'EXPIRED': Status.CANCELLED, 'CANCELED': Status.CANCELLED, 'CANCELLED': Status.CANCELLED,
                'REJECTED': Status.REJECTED, 'NEW': Status.ACCEPTED}
    status = statuses.get(fill.status, Status.UNKNOWN)
    terminal = fill.status in _PERP_TERMINAL
    if getattr(fill, 'outcome', None) == 'PARTIAL_TERMINAL':
        terminal = True
    quantity = None if status == Status.UNKNOWN and fill.qty == 0 else fill.qty
    return Result(status, quantity, 'exchange_terminal' if terminal else 'exchange_observation', not terminal,
                  (redact(str((scope, fill.client_id, fill.order_id, fill.status))),), terminal=terminal,
                  error=ErrorKind.UNKNOWN if status == Status.UNKNOWN else
                  ErrorKind.REJECTED if status == Status.REJECTED else None)


def perpetual(fill, scope, *, spec=None, side=None):
    """Map a native cumulative perpetual fill.

    The two-argument form remains the version-1 public API. NativeAdapter supplies
    ``spec`` and ``side`` and therefore receives the complete version-2 result.
    """
    if spec is None:
        return _legacy_perpetual(fill, scope)
    if scope != spec.scope or side not in {'BUY', 'SELL'}:
        raise AdapterError(ErrorKind.IDENTITY, 'perpetual result scope/side mismatch')

    statuses = {'FILLED': Status.SETTLED, 'PARTIALLY_FILLED': Status.PARTIAL,
                'EXPIRED': Status.CANCELLED, 'CANCELED': Status.CANCELLED, 'CANCELLED': Status.CANCELLED,
                'REJECTED': Status.REJECTED, 'NEW': Status.ACCEPTED}
    status = statuses.get(getattr(fill, 'status', None), Status.UNKNOWN)
    terminal = getattr(fill, 'status', None) in _PERP_TERMINAL
    if getattr(fill, 'outcome', None) == 'PARTIAL_TERMINAL':
        terminal = True
    quantity = _decimal(getattr(fill, 'qty', None))
    quote = _decimal(getattr(fill, 'quote', None))
    avg = _decimal(getattr(fill, 'avg_px', None))
    ref = _perp_ref(fill)
    evidence = (redact(str((scope, fill.client_id, fill.order_id, fill.status, side))),)

    # A native UNKNOWN zero is a placeholder, never proof of flat execution.
    if status == Status.UNKNOWN and quantity == 0:
        quantity = None
    has_execution = quantity is not None and quantity > 0
    complete_execution = has_execution and quote is not None and quote > 0 and avg is not None and avg > 0
    if has_execution and not complete_execution:
        return Result(Status.UNKNOWN, None, 'incomplete_exchange_execution', True, evidence,
                      error=ErrorKind.UNKNOWN, **_identity(spec, ref))
    if status in {Status.SETTLED, Status.PARTIAL} and not complete_execution:
        return Result(Status.UNKNOWN, None, 'incomplete_exchange_execution', True, evidence,
                      error=ErrorKind.UNKNOWN, **_identity(spec, ref))

    amounts = {}
    if complete_execution:
        cumulative = QuoteAmount(quote, spec.quote_currency)
        amounts = dict(perp_quote=cumulative, trade_notional=cumulative,
                       avg_price=QuoteAmount(avg, spec.quote_currency))
    return Result(status, quantity, 'exchange_terminal' if terminal else 'exchange_observation', not terminal,
                  evidence, terminal=terminal,
                  error=ErrorKind.UNKNOWN if status == Status.UNKNOWN else
                  ErrorKind.REJECTED if status == Status.REJECTED else None,
                  **_identity(spec, ref), **amounts)


def _legacy_evm(result, spec, side):
    status = {'ok': Status.SETTLED, 'reverted': Status.REJECTED}.get(result.status, Status.UNKNOWN)
    raw = result.amount_out if side == 'BUY' else result.amount_in
    qty = D(raw) / D(10) ** spec.decimals if status != Status.UNKNOWN else None
    return Result(status, qty, 'receipt' if status != Status.UNKNOWN else 'unresolved',
                  status == Status.UNKNOWN,
                  (redact(str((spec.scope, result.tx_hash, result.block))), f'gas_wei:{result.gas_wei}'),
                  terminal=status != Status.UNKNOWN)


def evm_swap(result, spec, side):
    # A legacy spec has no authoritative counter-asset precision, so it cannot
    # truthfully advertise the complete v2 raw accounting schema.
    if spec.quote_decimals is None:
        return _legacy_evm(result, spec, side)

    status = {'ok': Status.SETTLED, 'reverted': Status.REJECTED}.get(result.status, Status.UNKNOWN)
    ref = _spot_ref('transaction', result.tx_hash)
    evidence = (redact(str((spec.scope, result.tx_hash, result.block))), f'gas_wei:{result.gas_wei}')
    amounts = _spot_amounts(spec, side, result.amount_in, result.amount_out)
    usable = amounts is not None and amounts[0].raw > 0 and amounts[1].raw > 0
    if status == Status.SETTLED and not usable:
        return Result(Status.UNKNOWN, None, 'incomplete_receipt', True, evidence,
                      error=ErrorKind.UNKNOWN, **_identity(spec, ref))

    fields = {}
    qty = None
    if amounts is not None and (status != Status.UNKNOWN or usable):
        incoming, outgoing, qty, avg = amounts
        fields = dict(spot_input_raw=incoming, spot_output_raw=outgoing, avg_price=avg)
    return Result(status, qty, 'receipt' if status != Status.UNKNOWN else 'unresolved',
                  status == Status.UNKNOWN, evidence, terminal=status != Status.UNKNOWN,
                  error=ErrorKind.UNKNOWN if status == Status.UNKNOWN else
                  ErrorKind.REJECTED if status == Status.REJECTED else None,
                  **_identity(spec, ref), **fields)


def _legacy_sol(result, spec, side):
    final = result.commitment == 'finalized'
    ok = result.state == 'ok' and final
    terminal = ok or (result.state == 'failed' and final) or result.state in {'expired', 'not_sent'}
    status = Status.SETTLED if ok else Status.REJECTED if terminal else Status.UNKNOWN
    raw = result.out_raw if side == 'BUY' else result.in_raw
    qty = None if raw is None else D(raw) / D(10) ** spec.decimals
    return Result(status, qty, result.commitment or ('not_sent' if terminal else 'unresolved'), not terminal,
                  (redact(str((spec.scope, result.attempt_id, result.signature, result.slot, result.state))),),
                  fees=result.fees, terminal=terminal)


def sol_swap(result, spec, side):
    if spec.quote_decimals is None:
        return _legacy_sol(result, spec, side)

    final = result.commitment == 'finalized'
    ok = result.state == 'ok' and final
    terminal = ok or (result.state == 'failed' and final) or result.state in {'expired', 'not_sent'}
    status = Status.SETTLED if ok else Status.REJECTED if terminal else Status.UNKNOWN
    ref = (_spot_ref('transaction', result.signature) if result.signature else
           _spot_ref('attempt', result.attempt_id))
    evidence = (redact(str((spec.scope, result.attempt_id, result.signature, result.slot, result.state))),)
    amounts = _spot_amounts(spec, side, result.in_raw, result.out_raw)
    usable = amounts is not None and amounts[0].raw > 0 and amounts[1].raw > 0
    if status == Status.SETTLED and not usable:
        return Result(Status.UNKNOWN, None, 'incomplete_finalized_receipt', True, evidence,
                      fees=result.fees, error=ErrorKind.UNKNOWN, **_identity(spec, ref))

    fields = {}
    qty = None
    if amounts is not None and (status != Status.UNKNOWN or usable):
        incoming, outgoing, qty, avg = amounts
        fields = dict(spot_input_raw=incoming, spot_output_raw=outgoing, avg_price=avg)
    fees_complete = bool(final and result.receipt in {'new', 'same', 'finality'})
    return Result(status, qty, result.commitment or ('not_sent' if terminal else 'unresolved'), not terminal,
                  evidence, fees=result.fees, terminal=terminal,
                  error=ErrorKind.UNKNOWN if status == Status.UNKNOWN else
                  ErrorKind.REJECTED if status == Status.REJECTED else None,
                  fees_complete=fees_complete, **_identity(spec, ref), **fields)
