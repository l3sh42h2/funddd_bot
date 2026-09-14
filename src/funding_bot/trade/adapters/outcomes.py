"""Native exchange states translated at the adapter boundary, never in the coordinator."""
from decimal import Decimal as D
from ..keys import redact
from .contracts import Result, Status, ErrorKind

_REDUCE = frozenset({'reduce_only', 'position_closed', 'reduce_out', 'REDUCE_ONLY', 'REDUCE_ONLY_FAIL',
                     'POSITION_EMPTY', 'REDUCE_EXCEEDED', 'INCREASE_POSITION'})


def rejection(fill):
    code, kind = fill.err_code, getattr(fill, 'err_kind', None)
    if code == -1111 or kind == 'tick_size':
        return 'precision'
    if code == -2022 or code in _REDUCE or kind == 'reduce_only':
        return 'reduce_only'
    return 'rejected'


def perpetual(fill, scope):
    statuses = {'FILLED': Status.SETTLED, 'PARTIALLY_FILLED': Status.PARTIAL,
                'EXPIRED': Status.CANCELLED, 'CANCELED': Status.CANCELLED, 'CANCELLED': Status.CANCELLED,
                'REJECTED': Status.REJECTED, 'NEW': Status.ACCEPTED}
    status = statuses.get(fill.status, Status.UNKNOWN)
    terminal = fill.status in {'FILLED', 'EXPIRED', 'CANCELED', 'CANCELLED', 'REJECTED'}
    # IOC partial acknowledgements alone do not prove terminal cancellation of the remainder.
    if getattr(fill, 'outcome', None) == 'PARTIAL_TERMINAL':
        terminal = True
    quantity = None if status == Status.UNKNOWN and fill.qty == 0 else fill.qty
    return Result(status, quantity, 'exchange_terminal' if terminal else 'exchange_observation', not terminal,
                  (redact(str((scope, fill.client_id, fill.order_id, fill.status))),), terminal=terminal,
                  error=ErrorKind.UNKNOWN if status == Status.UNKNOWN else
                  ErrorKind.REJECTED if status == Status.REJECTED else None)


def evm_swap(result, spec, side):
    status = {'ok': Status.SETTLED, 'reverted': Status.REJECTED}.get(result.status, Status.UNKNOWN)
    raw = result.amount_out if side == 'BUY' else result.amount_in
    qty = D(raw) / D(10) ** spec.decimals if status != Status.UNKNOWN else None
    return Result(status, qty, 'receipt' if status != Status.UNKNOWN else 'unresolved',
                  status == Status.UNKNOWN, (redact(str((spec.scope, result.tx_hash, result.block))), f'gas_wei:{result.gas_wei}'),
                  terminal=status != Status.UNKNOWN)


def sol_swap(result, spec, side):
    final = result.commitment == 'finalized'
    ok = result.state == 'ok' and final
    terminal = ok or (result.state == 'failed' and final) or result.state in {'expired', 'not_sent'}
    status = Status.SETTLED if ok else Status.REJECTED if terminal else Status.UNKNOWN
    raw = result.out_raw if side == 'BUY' else result.in_raw
    qty = None if raw is None else D(raw) / D(10) ** spec.decimals
    return Result(status, qty, result.commitment or ('not_sent' if terminal else 'unresolved'), not terminal,
                  (redact(str((spec.scope, result.attempt_id, result.signature, result.slot, result.state))),),
                  fees=result.fees, terminal=terminal)
