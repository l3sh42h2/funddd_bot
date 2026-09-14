"""Use the generic adapter boundary with the existing perpetual order journal.

The returned native object is a compatibility projection for existing clip
accounting. It is released only after Result v2 proves a terminal execution;
an uncertain or malformed response follows the existing recovery path.
"""
from types import SimpleNamespace
from decimal import Decimal
from dataclasses import dataclass, replace
import hashlib
import json
import re

from .contracts import Action, AdapterError, ErrorKind, Status
from .futures_bindings import bind
from .mapping import map_perpetual
from .native_journal import PerpJournal, exclusive_transaction
from .registry import production_registry
from ..types import PerpFill, InstrumentSpec
from .. import store


class NativeNotSubmitted(AdapterError):
    def __init__(self):
        super().__init__(ErrorKind.REJECTED, 'native journal proves no submission')


def recover_not_submitted(con, *, deal, clip_id, native, account, client_id):
    """Resolve the claim/native-prepare crash gap under the sole execution owner.

    A missing signing nonce alone is never sufficient. Both the common prepared
    proof and the native adapter's durable no-submission proof are required.
    """
    if con.in_transaction:
        raise AdapterError(ErrorKind.CONFIG, 'recovery cannot join a caller transaction')
    _account_gate(con, deal, native, account)
    native = _connection_native(native, con, account)
    with exclusive_transaction(con):
        row = store.get_perp_order(con, client_id)
        if row is None or row['clip_id'] != clip_id or row['symbol'] != deal['symbol']:
            return False
        clip = store.get_clip(con, clip_id)
        intent = store.get_intent(con, clip['intent_id']) if clip else None
        if intent is None or intent['deal_id'] != deal['id']:
            return False
        if row['state'] not in ('SENT', 'UNKNOWN'):
            return False
        proof = con.execute("SELECT json FROM exec_events WHERE kind='adapter_perp_prepared' "
                            "AND clip_id=? AND json_extract(json,'$.attempt_id')=?",
                            (clip_id, client_id)).fetchone()
        if proof is None:
            return False
        payload = json.loads(proof[0])
        if payload.get('account') != account or payload.get('instrument') != deal['symbol']:
            return False
        prove = getattr(native, 'submission_absent', None)
        if prove is None or prove(deal['symbol'], client_id, account, proof_con=con) is not True:
            return False
        if row['state'] == 'SENT':
            store.perp_order_result(con, client_id, store.PerpOrderState.UNKNOWN)
        return store.perp_order_result(con, client_id, store.PerpOrderState.NOT_PLACED,
                                           err='common admission; native journal proves no submission')


def submit_ioc(con, *, deal, clip_id, native, account, fill_venue, client_id,
               side, quantity, price, reduce_only, clock, authorize, registry=None):
    persisted = store.get_deal(con, deal['id'])
    clip = store.get_clip(con, clip_id)
    intent = store.get_intent(con, clip['intent_id']) if clip else None
    if (persisted is None or intent is None or intent['deal_id'] != deal['id'] or
            any(persisted[k] != deal[k] for k in ('inst_json', 'symbol', 'perp_venue', 'sim'))):
        raise AdapterError(ErrorKind.IDENTITY, 'attempt, clip and frozen deal differ')
    letter = {'entry': 'e', 'exit': 'x', 'rehedge': 'h'}.get(intent['kind'])
    prefix = f"fb-{deal['id']}-{letter}{clip_id:02d}-"
    if letter is None or re.fullmatch(re.escape(prefix) + r'c[1-9][0-9]*-a[1-9][0-9]*', client_id) is None:
        raise AdapterError(ErrorKind.IDENTITY, 'client order ID differs from native clip')
    expected_venue = ('sim:' if deal['sim'] else '') + deal['perp_venue']
    if fill_venue != expected_venue or native.venue != deal['perp_venue']:
        raise AdapterError(ErrorKind.IDENTITY, 'native venue differs from frozen deal')
    _account_gate(con, deal, native, account)
    native = _connection_native(native, con, account)
    from .execution_scope import continuation_identity, check_continuation_action
    check_continuation_action(con, deal, intent, side, quantity, reduce_only)
    spec = map_perpetual(deal, account=account, filters=native.filters(deal['symbol']),
                         metadata_revision='frozen:' + deal['id'],
                         continuation_evidence=continuation_identity(con, deal, intent['kind']))
    attach = getattr(native, 'bind_execution_journal', None)
    barrier = attach(con, account) if callable(attach) else None
    journal = PerpJournal(con, clip_id=clip_id, fill_venue=fill_venue, spec=spec, send_barrier=barrier)
    bindings = bind(native, journal=journal, authorize=authorize,
                    attempt_lookup=journal.lookup, on_signed=journal.on_signed,
                    clock=clock, hedge=True,
                    links=dict(deal_id=journal.deal_id, intent_id=journal.intent_id, clip_id=clip_id))
    native_submit = bindings.submit
    captured = []

    def submit(leg, prepared):
        fill = native_submit(leg, prepared)
        if fill.client_id != prepared.attempt_id:
            raise AdapterError(ErrorKind.IDENTITY, 'native result belongs to another attempt')
        captured.append(fill)
        return fill

    bindings.submit = submit
    adapter = (registry or production_registry()).build(spec, SimpleNamespace(for_leg=lambda _: bindings))
    action = Action(client_id, spec.leg_id, side, quantity, reduce_only)
    prepared = adapter.prepare(client_id, adapter.quote(action, {'price_cap': price}))
    result = adapter.submit(prepared)
    if result.status == Status.UNKNOWN and recover_not_submitted(
            con, deal=deal, clip_id=clip_id, native=native, account=account, client_id=client_id):
        raise NativeNotSubmitted()
    return _compat(result, captured, client_id)


def _compat(result, captured, client_id):
    if (result.terminal and result.status in
            {Status.SETTLED, Status.PARTIAL, Status.CANCELLED, Status.REJECTED} and captured):
        fill = captured[0]
        qty = result.executed_quantity
        if (isinstance(qty, Decimal) and qty.is_finite() and qty >= 0 and fill.qty == qty and
                ((qty == 0 and fill.quote == 0 and fill.avg_px == 0) or
                 (qty > 0 and result.perp_quote is not None and result.avg_price is not None and
                  fill.quote == result.perp_quote.amount and fill.avg_px == result.avg_price.amount))):
            if result.status == Status.CANCELLED:
                return replace(fill, status='PARTIALLY_FILLED' if qty > 0 else 'EXPIRED')
            return fill
    return PerpFill(client_id, None, 'UNKNOWN', Decimal(0), Decimal(0), Decimal(0), 0)


@dataclass(frozen=True)
class RecoveryScope:
    """Immutable identity for reading an old attempt; no fabricated trading filters."""
    leg_id: str
    fingerprint: str
    scope: tuple
    quote_currency: str


def _account_gate(con, deal, native, account):
    # SOL retains its frozen account ID and native HL journal. EVM legacy
    # accounts require an independent durable execution binding.
    inst = InstrumentSpec.from_json(deal['inst_json'])
    if not deal['sim'] and inst.chain != 'solana':
        from .execution_scope import account_for
        if account_for(con, deal, native) != account:
            raise AdapterError(ErrorKind.IDENTITY, 'caller differs from proven execution account')


def _connection_native(native, con, account):
    factory = getattr(native, 'execution_view', None)
    return factory(con, account) if callable(factory) else native


def settle_ioc(con, *, deal, native, account, client_id, **recovery):
    """Resolve native evidence, including old attempts, through the same Result v2 gate.

    NOT_FOUND is accepted only from settle_unknown's full no-execution proof,
    never from a single query. Recovery creates no attempt and performs no send.
    """
    row = store.get_perp_order(con, client_id)
    clip = store.get_clip(con, row['clip_id']) if row else None
    intent = store.get_intent(con, clip['intent_id']) if clip else None
    if (intent is None or intent['deal_id'] != deal['id'] or row['symbol'] != deal['symbol'] or
            row['venue'] != ('sim:' if deal['sim'] else '') + deal['perp_venue'] or
            native.venue != deal['perp_venue']):
        raise AdapterError(ErrorKind.IDENTITY, 'recovery attempt differs from deal')
    persisted = store.get_deal(con, deal['id'])
    if persisted is None or persisted['inst_json'] != deal['inst_json']:
        raise AdapterError(ErrorKind.IDENTITY, 'recovery frozen instrument differs')
    _account_gate(con, deal, native, account)
    inst = InstrumentSpec.from_json(persisted['inst_json'])
    if (inst.perp_symbol != row['symbol'] or inst.perp_venue != native.venue or not inst.quote_asset or
            (inst.perp_account and inst.perp_account != account)):
        raise AdapterError(ErrorKind.IDENTITY, 'recovery native scope differs from frozen instrument')
    proof = con.execute("SELECT json FROM exec_events WHERE kind='adapter_perp_prepared' "
                        "AND clip_id=? AND json_extract(json,'$.attempt_id')=?",
                        (row['clip_id'], client_id)).fetchone()
    if proof:
        saved = json.loads(proof[0])
        if saved.get('account') != account or saved.get('instrument') != row['symbol']:
            raise AdapterError(ErrorKind.IDENTITY, 'recovery differs from prepared native scope')
        fingerprint = saved['spec_hash']
    else:
        # Legacy attempts predate LegSpec. Identify their frozen source without
        # rewriting it or pretending to know historical tick/step metadata.
        fingerprint = hashlib.sha256(json.dumps(
            ('legacy-recovery-v1', deal['id'], inst.inst_hash(), account)).encode()).hexdigest()
    scope = RecoveryScope(deal['id'] + ':perp', fingerprint,
                          (inst.perp_venue, inst.perp_network, account, inst.perp_dex, inst.perp_symbol),
                          inst.quote_asset)
    fill = native.settle_unknown(inst.perp_symbol, client_id, **recovery)
    _account_gate(con, deal, native, account)
    if fill.client_id != client_id:
        raise AdapterError(ErrorKind.IDENTITY, 'native recovery returned another attempt')
    if fill.status == 'NOT_FOUND':
        return fill
    from .outcomes import perpetual
    result = perpetual(fill, scope.scope, spec=scope, side=row['side'],
                       partial_terminal=getattr(native, 'ioc_partial_terminal', False))
    return _compat(result, [fill], client_id)
