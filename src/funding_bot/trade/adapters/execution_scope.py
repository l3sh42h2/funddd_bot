"""Execution identity without changing historical accounting attribution.

The event binds a frozen deal, not legacy fills/funding. Existing economic
activity requires independently established evidence; today's credentials are
never sufficient to assign an old deal to an account.
"""
import json
import hashlib
import logging
from decimal import Decimal as D

from .contracts import AdapterError, ErrorKind
from .native_journal import exclusive_transaction
from .. import store
from ..keys import redact
from ..types import InstrumentSpec

log = logging.getLogger(__name__)
KIND = 'execution_account_binding_v1'


class MissingExecutionBinding(AdapterError):
    """Only absent evidence may trigger legacy adoption; contradictions may not."""


def _saved(con, deal):
    rows = con.execute('SELECT json FROM exec_events WHERE deal_id=? AND kind=?',
                       (deal['id'], KIND)).fetchall()
    values = [json.loads(row[0]) for row in rows]
    if len(values) > 1:
        raise AdapterError(ErrorKind.IDENTITY, 'execution account binding is ambiguous')
    if values:
        value = values[0]
        inst = InstrumentSpec.from_json(deal['inst_json'])
        if (value.get('inst_hash') != inst.inst_hash() or
                value.get('inst_json_sha256') != hashlib.sha256(deal['inst_json'].encode()).hexdigest() or
                value.get('venue') != deal['perp_venue'] or
                value.get('symbol') != deal['symbol'] or value.get('sim') != bool(deal['sim'])):
            raise AdapterError(ErrorKind.IDENTITY, 'execution binding differs from frozen deal')
        return value
    return None


def native_account(native, *, sim):
    if sim:
        # Synthetic execution owns no exchange account or private credentials.
        return 'simulation:' + native.venue
    read = getattr(native, 'history_account', None)
    account = read() if callable(read) else None
    if not isinstance(account, str) or not account:
        raise AdapterError(ErrorKind.IDENTITY, 'native execution account is unproven')
    return account


def account_for(con, deal, native):
    persisted = store.get_deal(con, deal['id'])
    if persisted is None or any(persisted[k] != deal[k] for k in
                                ('inst_json', 'perp_venue', 'symbol', 'sim')):
        raise AdapterError(ErrorKind.IDENTITY, 'execution deal differs from persisted identity')
    if native.venue != deal['perp_venue']:
        raise AdapterError(ErrorKind.IDENTITY, 'execution venue differs from deal')
    current = native_account(native, sim=bool(deal['sim']))
    if deal['sim']:
        return current
    inst = InstrumentSpec.from_json(deal['inst_json'])
    saved = _saved(con, persisted)
    from ..scoped_accounting import deal_scope
    scoped = deal_scope(con, deal['id'])
    candidates = [x for x in (inst.perp_account, saved and saved['account'],
                              scoped and scoped.account_scope) if x]
    if scoped and (scoped.venue != deal['perp_venue'] or scoped.symbol != deal['symbol']):
        raise AdapterError(ErrorKind.IDENTITY, 'accounting proof differs from execution leg')
    if not candidates:
        raise MissingExecutionBinding(ErrorKind.IDENTITY, 'frozen execution account is absent; binding required')
    if any(x != current for x in candidates):
        raise AdapterError(ErrorKind.IDENTITY, 'frozen execution account is absent or differs; binding required')
    return current


def bind_draft(con, deal, native):
    """Bind a never-started new draft before approval; never adopt an old position."""
    account = native_account(native, sim=bool(deal['sim']))
    if native.venue != deal['perp_venue']:
        raise AdapterError(ErrorKind.IDENTITY, 'draft venue differs from native account')
    with exclusive_transaction(con):
        persisted = store.get_deal(con, deal['id'])
        if persisted is None or any(persisted[k] != deal[k] for k in
                                    ('inst_json', 'perp_venue', 'symbol', 'sim')):
            raise AdapterError(ErrorKind.IDENTITY, 'draft changed during account binding')
        old = _saved(con, persisted)
        if old:
            if old['account'] != account:
                raise AdapterError(ErrorKind.IDENTITY, 'draft account binding is immutable')
            return account
        if persisted['state'] != store.DealState.DRAFT or con.execute(
                'SELECT 1 FROM intents WHERE deal_id=? LIMIT 1', (deal['id'],)).fetchone():
            raise AdapterError(ErrorKind.IDENTITY, 'only a new draft can bind current account')
        inst = InstrumentSpec.from_json(persisted['inst_json'])
        if inst.perp_account and inst.perp_account != account:
            raise AdapterError(ErrorKind.IDENTITY, 'draft frozen account differs')
        store.require_reader(con, 4)
        store.event(con, KIND, deal_id=deal['id'], account=account,
                    venue=deal['perp_venue'], symbol=deal['symbol'], sim=bool(deal['sim']),
                    inst_hash=inst.inst_hash(),
                    inst_json_sha256=hashlib.sha256(deal['inst_json'].encode()).hexdigest(),
                    provenance='new_draft_before_approval')
    return account


def _orders(con, deal_id):
    return [dict(row) for row in con.execute(
        'SELECT p.* FROM perp_orders p JOIN clips c ON c.id=p.clip_id '
        'JOIN intents i ON i.id=c.intent_id WHERE i.deal_id=? ORDER BY p.id', (deal_id,))]


def continuation_identity(con, deal, kind):
    """Legacy continuation evidence, not a new cross-venue listing identity.

    Only disposal/hedging of an already proven position can reuse its frozen
    verified units. Fresh entry/resume-entry still requires asset identity.
    """
    inst = InstrumentSpec.from_json(deal['inst_json'])
    if inst.ident_ev or inst.identity_hash:
        return inst.identity_hash or inst.ident_ev
    proof = _saved(con, deal)
    if (kind not in ('exit', 'undo', 'rehedge', 'recovery') or not inst.verified or
            not inst.source or proof is None or proof.get('provenance') != 'authenticated_legacy_orders' or
            not proof.get('order_proofs')):
        return None
    return 'legacy-continuation:' + kind + ':' + proof['inst_json_sha256']


def check_continuation_action(con, deal, intent, side, quantity, reduce_only):
    inst = InstrumentSpec.from_json(deal['inst_json'])
    if inst.ident_ev or inst.identity_hash:
        return
    if not continuation_identity(con, deal, intent['kind']):
        raise AdapterError(ErrorKind.IDENTITY, 'legacy action lacks continuation proof')
    from ..engine import deal_book
    book = deal_book(con, deal['id'])
    if book.short is None or book.tokens_raw is None:
        raise AdapterError(ErrorKind.UNKNOWN, 'legacy exposure unresolved; no new hedge')
    if side == 'BUY':
        permitted = reduce_only and quantity <= book.short
    else:
        naked = D(book.tokens_raw).scaleb(-inst.token_dec) / inst.m - book.short
        permitted = intent['kind'] == 'rehedge' and not reduce_only and quantity <= max(D(0), naked)
    if not permitted:
        raise AdapterError(ErrorKind.IDENTITY, 'legacy continuation exceeds proven existing exposure')


def _legacy_candidate(con, deal, native):
    """Explicit migration probe; every sent legacy order must match its account.

    All HTTP reads precede the writer transaction. Revalidate the exact frozen
    bytes and order snapshot before atomically installing the binding/reader floor.
    """
    if con.in_transaction:
        raise AdapterError(ErrorKind.CONFIG, 'legacy binding cannot join caller transaction')
    if deal['sim']:
        raise AdapterError(ErrorKind.IDENTITY, 'legacy live proof cannot bind simulation')
    inst = InstrumentSpec.from_json(deal['inst_json'])
    if not inst.verified:
        raise AdapterError(ErrorKind.IDENTITY, 'legacy instrument is unverified')
    try:
        account_for(con, deal, native)
    except MissingExecutionBinding:
        pass
    account = native_account(native, sim=False)
    original = store.get_deal(con, deal['id'])
    if original != deal or native.venue != deal['perp_venue']:
        raise AdapterError(ErrorKind.IDENTITY, 'legacy frozen snapshot differs')
    rows = _orders(con, deal['id'])
    from .legacy_evidence import prove_order
    proofs = {}
    for row in rows:
        if row['state'] in ('INTENT', 'NOT_PLACED') and all(row[k] is None for k in
                ('sign_nonce', 'order_id', 'executed_qty', 'cum_quote')):
            continue
        proofs[row['client_id']] = prove_order(native, row, account)
    if not proofs or native_account(native, sim=False) != account:
        raise AdapterError(ErrorKind.IDENTITY, 'no sufficient retained legacy account evidence')
    inst = InstrumentSpec.from_json(deal['inst_json'])
    if inst.perp_account and inst.perp_account != account:
        raise AdapterError(ErrorKind.IDENTITY, 'legacy frozen account differs')
    return original, rows, account, proofs


def _install_legacy(con, candidate):
    deal, rows, account, proofs = candidate
    if store.get_deal(con, deal['id']) != deal or _orders(con, deal['id']) != rows:
        raise AdapterError(ErrorKind.IDENTITY, 'legacy snapshot changed while proving account')
    old = _saved(con, deal)
    if old:
        if old['account'] != account:
            raise AdapterError(ErrorKind.IDENTITY, 'conflicting concurrent account proof')
        return account
    inst = InstrumentSpec.from_json(deal['inst_json'])
    store.require_reader(con, 4)
    store.event(con, KIND, deal_id=deal['id'], account=account, venue=deal['perp_venue'],
                symbol=deal['symbol'], sim=False, inst_hash=inst.inst_hash(),
                inst_json_sha256=hashlib.sha256(deal['inst_json'].encode()).hexdigest(),
                provenance='authenticated_legacy_orders', order_proofs=proofs)
    return account


def _shadow_bridge_installed(con, deal):
    """Shadow-only M4 step (owner-directed, 16.09.2026): best-effort mirror of this deal's
    execution_account_binding_v1 event into scope_bridge's own audit-only tables.

    Must never affect the caller: every failure is caught and logged here, never raised.
    Not a source of truth for anything -- scope_bridge.py never writes to
    scoped_deal_accounts/scoped_account_proofs, so this cannot change
    scoped_accounting.deal_scope()/accounting.is_bound() for any deal, and therefore cannot
    change what engine.deal_fills()/accounting.sources()/marks.journal()
    (cabinet/dashboard/tg) return. See scope_bridge.py and
    PATCHNOTES/m4-scoped-accounting-shadow-20260916.md.

    Safe to call whether or not a binding already existed before this process started (it
    reads back whatever is currently persisted via _saved(), so it also opportunistically
    backfills the shadow table for bindings written before this code existed, e.g. DQA9Q's
    2026-09-15 binding).
    """
    try:
        saved = _saved(con, deal)
        if saved is None:
            return
        from .. import scope_bridge
        binding = {**saved, 'deal_id': deal['id']}
        scope_bridge.ensure_scoped_accounting_schema(con)
        scope_bridge.record_shadow_binding(con, binding)
    except Exception as exc:                                     # noqa — shadow-only, never blocks real execution
        log.warning('scope_bridge: shadow binding step failed for deal %s (non-fatal): %s',
                    deal['id'], redact(exc))


def bind_legacy(con, deal, native):
    if _saved(con, deal):
        account = account_for(con, deal, native)
        _shadow_bridge_installed(con, deal)
        return account
    candidate = _legacy_candidate(con, deal, native)
    with exclusive_transaction(con):
        account = _install_legacy(con, candidate)
    _shadow_bridge_installed(con, deal)
    return account


def prepare_active_accounts(con, legs_fn):
    """Prove the entire active set before any binding/reader-floor activation."""
    from ..runtime import is_sol_deal, legs_of
    from .. import owner
    from ..evm import _addr
    failures, candidates, active_evm, evm_deals = [], [], False, []
    snapshot = store.active_deals(con)
    for deal in snapshot:
        from ..generic_recovery import is_generic
        if deal['sim'] or is_sol_deal(deal) or is_generic(deal):
            continue
        active_evm = True
        evm_deals.append(deal)
        try:
            inst = InstrumentSpec.from_json(deal['inst_json'])
            if not inst.verified:
                raise AdapterError(ErrorKind.IDENTITY, 'legacy instrument is unverified')
            legs = legs_of(legs_fn, deal)
            if legs is None:
                raise AdapterError(ErrorKind.CONFIG, 'active execution venue unavailable')
            cfg = owner.OwnerCfg.from_frozen(deal['owner_json'])
            wallet = cfg.get(owner.EVM_WALLET_KEY.get(inst.chain, 'wallets.' + inst.chain))
            if (not wallet or _addr(wallet) != _addr(legs.spot.wallet) or
                    legs.spot.chain != inst.chain):
                raise AdapterError(ErrorKind.IDENTITY, 'active frozen wallet/network differs')
            try:
                account_for(con, deal, legs.perp)
            except MissingExecutionBinding:
                candidates.append(_legacy_candidate(con, deal, legs.perp))
        except Exception as error:
            failures.append(deal['id'] + ':' + type(error).__name__)
    if failures:
        raise AdapterError(ErrorKind.IDENTITY, 'active account migration unresolved: ' + ', '.join(failures))
    with exclusive_transaction(con):
        if store.active_deals(con) != snapshot:
            raise AdapterError(ErrorKind.IDENTITY, 'active set changed during migration proof')
        if active_evm:
            store.require_reader(con, 4)
        for candidate in candidates:
            _install_legacy(con, candidate)
    # Shadow-only M4 step, deliberately outside the transaction above: it must never hold that
    # transaction's exclusive lock open, and by this point `failures` is empty (or we would
    # already have raised), so every deal in evm_deals is either freshly bound or was already
    # bound. See _shadow_bridge_installed.
    for deal in evm_deals:
        _shadow_bridge_installed(con, deal)
