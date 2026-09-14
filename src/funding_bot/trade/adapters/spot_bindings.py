"""Bindings for existing EVM/Solana executors. No futures venue is required.

DAO callbacks point at core-owned legacy journals; no independent ledger is created.
Solana retains validator, router final checks and atomic receipt application.
"""
import json
import time
from decimal import Decimal as D
from .contracts import AdapterError, ErrorKind, Quote, Observation
from .native import Bindings


def _raw(value, decimals):
    if not isinstance(value, D) or not value.is_finite() or value <= 0:
        raise AdapterError(ErrorKind.INVALID, 'positive Decimal input amount required')
    raw = value * D(10) ** decimals
    if raw != raw.to_integral_value():
        raise AdapterError(ErrorKind.INVALID, 'input amount is off raw token units')
    return int(raw)


def evm(native, *, quote_token, quote_decimals, journal, authorize, resolve_row, read_executions,
        clip_ref, clock=time.time):
    def quote(spec, action, bounds):
        if native.wallet.lower() != spec.account.lower() or str(native.ci) != spec.network:
            raise AdapterError(ErrorKind.IDENTITY, 'EVM native scope mismatch')
        buy = action.side == 'BUY'
        token_in, token_out = (quote_token, spec.instrument) if buy else (spec.instrument, quote_token)
        din, dout = (quote_decimals, spec.decimals) if buy else (spec.decimals, quote_decimals)
        amount = bounds.get('spend') if buy else action.quantity
        raw = _raw(amount, din)
        build = native.build_swap(token_in, token_out, raw, preflight=False)
        minimum = D(build.min_receive) / D(10) ** dout
        required = action.quantity if buy else bounds.get('min_receive')
        if not isinstance(required, D) or minimum < required:
            raise AdapterError(ErrorKind.REJECTED, 'route does not meet minimum receive')
        payload = dict(token_in=token_in, token_out=token_out, amount=raw, minimum=build.min_receive)
        return Quote(action, clock() + 5, amount, minimum, token_in, token_out, json.dumps(payload, sort_keys=True))

    def submit(spec, prepared):
        p = json.loads(prepared.quote.native)
        # Allowance is a separate native journaled action; never hide approve inside a swap attempt.
        return native.swap(p['token_in'], p['token_out'], p['amount'], clip_ref(prepared.attempt_id),
                           approved_min_receive=p['minimum'])

    def resolve(spec, ref):
        row, side = resolve_row(spec, ref)
        if row['chain'] != native.chain or row['wallet'].lower() != spec.account.lower():
            raise AdapterError(ErrorKind.IDENTITY, 'EVM receipt scope mismatch')
        return native.resolve(row), side

    def observe(spec):
        balances = native.balances(spec.instrument)
        raw = balances.get('token')
        qty = None if raw is None else D(raw) / D(10) ** spec.decimals
        return Observation(qty, clock(), 'EVM:balanceOf', 'unknown' if qty is None else 'authoritative')

    return Bindings(native, journal, authorize, quote, submit, resolve, observe, read_executions, clock=clock)


def solana(native, router, *, token, quote_asset, journal, authorize, con, prices, hedge,
           resolve_ref, read_executions, apply, clip_ref, slippage_bps, min_validity_heights, clock=time.time):
    from ..spot_router import QuoteRequest
    from ..solana.accounts import ata
    cache = {}

    def quote(spec, action, bounds):
        if token.mint != spec.instrument or native.wallet != spec.account or native.genesis != spec.network:
            raise AdapterError(ErrorKind.IDENTITY, 'Solana native scope mismatch')
        buy = action.side == 'BUY'
        inp, out = (quote_asset, token) if buy else (token, quote_asset)
        amount = bounds.get('spend') if buy else action.quantity
        raw = _raw(amount, inp.decimals)
        side = 'entry' if buy else 'exit'
        req = QuoteRequest(side, inp, out, raw, native.wallet, slippage_bps, native.genesis,
                           router.clock() + 5, purpose=side,
                           input_account=ata(native.wallet, inp.mint, inp.program),
                           output_account=ata(native.wallet, out.mint, out.program),
                           account_rent=((inp.mint, native.account_rent(inp.mint, inp.program)),
                                         (out.mint, native.account_rent(out.mint, out.program))))
        decision = router.select(req, prices=prices(), block_height=native.chain.block_height, hedge=hedge,
                                 inflight_unknown=lambda: 'unresolved' if native.pending(con) else None)
        candidate = decision.winner
        if candidate is None:
            raise AdapterError(ErrorKind.REJECTED, 'no executable Solana route')
        minimum = D(candidate.effective_min_out) / D(10) ** out.decimals
        required = action.quantity if buy else bounds.get('min_receive')
        if not isinstance(required, D) or minimum < required:
            raise AdapterError(ErrorKind.REJECTED, 'Solana route below approved minimum')
        result = Quote(action, clock() + 5, amount, minimum, inp.mint, out.mint,
                       json.dumps({'message_hash': candidate.message_hash}, sort_keys=True))
        # Only unsigned validated candidates in memory. Restart needs a new quote/approval, never a resend.
        for key in list(cache):
            if cache[key][2] <= clock():
                del cache[key]
        cache[result.fingerprint] = (req, decision, result.expires_at)
        return result

    def submit(spec, prepared):
        saved = cache.pop(prepared.quote.fingerprint, None)
        if saved is None:
            raise AdapterError(ErrorKind.STALE, 'validated route not present; re-quote required')
        req, decision, _ = saved
        if router.presign_check(decision, block_height=native.chain.block_height()):
            raise AdapterError(ErrorKind.STALE, 'Solana route expired before signing')
        return native.swap(con, decision.winner, req, logical_action_id=prepared.quote.action.action_id,
                           clip_ref=clip_ref(prepared.attempt_id), meta={'adapter_attempt': prepared.attempt_id},
                           min_validity_heights=min_validity_heights, apply=apply)

    def resolve(spec, ref):
        attempt_id, side = resolve_ref(spec, ref)
        return native.resolve(con, attempt_id, apply=apply), side

    def observe(spec):
        raw = native.token_balance(token.mint, token.program)
        qty = None if raw is None else D(raw) / D(10) ** token.decimals
        return Observation(qty, clock(), 'Solana:tokenAccounts', 'unknown' if qty is None else 'authoritative')

    return Bindings(native, journal, authorize, quote, submit, resolve, observe, read_executions, clock=clock)
