"""Production EVM spot port backed by existing clip and native tx journals."""
import json
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

from .contracts import Action, AdapterError, Capabilities, ErrorKind, LegSpec, Status
from .native_journal import exclusive_transaction
from .registry import production_registry
from .spot_bindings import evm
from .. import owner, store, tconfig
from ..types import InstrumentSpec


def evm_spec(deal, native, stable, stable_dec, *, continuation_evidence=None):
    from ..evm import _addr
    inst = InstrumentSpec.from_json(deal['inst_json'])
    if not inst.verified or not (inst.ident_ev or inst.identity_hash or continuation_evidence):
        raise AdapterError(ErrorKind.IDENTITY, 'spot instrument identity unproven')
    if native.chain != inst.chain or inst.token != deal['token'] or inst.token_dec != deal['token_dec']:
        raise AdapterError(ErrorKind.IDENTITY, 'spot frozen instrument differs')
    if not deal['sim']:
        cfg = owner.OwnerCfg.from_frozen(deal['owner_json'])
        wallet = cfg.get(owner.EVM_WALLET_KEY.get(inst.chain, 'wallets.' + inst.chain))
        if not wallet or _addr(wallet) != _addr(native.wallet):
            raise AdapterError(ErrorKind.IDENTITY, 'spot wallet differs from frozen owner configuration')
    return LegSpec(deal['id'] + ':spot', 'inventory', 'long', 'okx_evm', 'okx',
                   _addr(native.wallet), inst.token, f'{inst.chain}:{inst.token}',
                   inst.identity_hash or inst.ident_ev or continuation_evidence, inst.fs, D(1).scaleb(-inst.token_dec), D(1),
                   stable, stable, Capabilities('dex', 'spot', 'evm', amount=True),
                   network=str(tconfig.chain_index(inst.chain)), decimals=inst.token_dec,
                   quote_decimals=stable_dec, metadata_revision='frozen:' + deal['id'], legacy_hash=inst.inst_hash())


class SpotJournal:
    def __init__(self, con, deal, clip_id, spec):
        self.con, self.deal, self.clip_id, self.spec = con, deal, clip_id, spec

    def _event(self, kind):
        rows = self.con.execute('SELECT json FROM exec_events WHERE kind=? AND clip_id=? ORDER BY rowid',
                                (kind, self.clip_id)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def prepare(self, prepared):
        with exclusive_transaction(self.con):
            clip = store.get_clip(self.con, self.clip_id)
            intent = store.get_intent(self.con, clip['intent_id']) if clip else None
            if (intent is None or intent['deal_id'] != self.deal['id'] or
                    clip['state'] != store.ClipState.DEX_SENT or prepared.spec_hash != self.spec.fingerprint):
                raise AdapterError(ErrorKind.IDENTITY, 'spot attempt differs from reserved clip')
            old = self._event('adapter_spot_prepared')
            completed = self._event('adapter_spot_terminal')
            if old and (not completed or completed[-1]['attempt_id'] != old[-1]['attempt_id'] or
                        completed[-1]['status'] != Status.REJECTED.value):
                raise AdapterError(ErrorKind.UNKNOWN, 'prior spot attempt must resolve before retry')
            store.require_reader(self.con, 4)
            store.event(self.con, 'adapter_spot_prepared', deal_id=self.deal['id'],
                        intent_id=intent['id'], clip_id=self.clip_id, attempt_id=prepared.attempt_id,
                        spec_hash=prepared.spec_hash, quote_hash=prepared.quote.fingerprint,
                        account=self.spec.account, network=self.spec.network,
                        request=json.loads(prepared.quote.native))

    def claim(self, prepared):
        with exclusive_transaction(self.con):
            rows = self._event('adapter_spot_prepared')
            claimed = self._event('adapter_spot_claimed')
            if (not rows or rows[-1]['attempt_id'] != prepared.attempt_id or
                    rows[-1]['quote_hash'] != prepared.quote.fingerprint or
                    any(r['attempt_id'] == prepared.attempt_id for r in claimed)):
                raise AdapterError(ErrorKind.UNKNOWN, 'spot attempt already claimed or differs')
            store.event(self.con, 'adapter_spot_claimed', deal_id=self.deal['id'], clip_id=self.clip_id,
                        attempt_id=prepared.attempt_id)


def submit_evm(con, *, deal, clip_id, native, stable, stable_dec, token_in, token_out,
               amount_raw, clock, authorize, registry=None, pair=None, compose_context=None):
    from .execution_scope import continuation_identity
    clip = store.get_clip(con, clip_id)
    intent = store.get_intent(con, clip['intent_id']) if clip else None
    if intent is None or intent['deal_id'] != deal['id']:
        raise AdapterError(ErrorKind.IDENTITY, 'spot clip differs from deal')
    spec = evm_spec(deal, native, stable, stable_dec,
                    continuation_evidence=continuation_identity(con, deal, intent['kind']))
    side = 'BUY' if token_in.lower() == stable.lower() else 'SELL'
    expected = (stable, spec.instrument) if side == 'BUY' else (spec.instrument, stable)
    if (token_in.lower(), token_out.lower()) != tuple(t.lower() for t in expected):
        raise AdapterError(ErrorKind.IDENTITY, 'spot input/output differs from approved leg')
    journal = SpotJournal(con, deal, clip_id, spec)
    attempt = len(journal._event('adapter_spot_prepared')) + 1
    aid = f'spot:{deal["id"]}:{clip_id}:{attempt}'
    from .contracts import from_raw
    quantity = from_raw(1 if side == 'BUY' else amount_raw, spec.decimals)
    bindings = evm(native, quote_token=stable, quote_decimals=stable_dec, journal=journal,
                   authorize=authorize, resolve_row=None, read_executions=None,
                   clip_ref=lambda _: str(clip_id), clock=clock)
    captured = []
    send = bindings.submit
    def submit(leg, prepared):
        result = send(leg, prepared)
        captured.append(result)
        return result
    bindings.submit = submit
    bindings._captured_by_attempt = {aid: captured}
    if compose_context is not None and pair is None:
        from .execution import compose_clip_pair
        pair = compose_clip_pair(compose_context['registry'], spot_spec=spec, spot_bindings=bindings,
                                 perp_spec=compose_context['perp_spec'], perp_bindings=compose_context['perp_bindings'])
        compose_context['pair'] = pair
    if pair is not None:
        if pair.first.describe() != spec:
            raise AdapterError(ErrorKind.IDENTITY, 'composed spot leg differs from frozen spec')
        adapter = pair.first
    else:
        adapter = (registry or production_registry()).build(spec, SimpleNamespace(for_leg=lambda _: bindings))
    action = Action(aid, spec.leg_id, side, quantity)
    bounds = {'spend': from_raw(amount_raw, stable_dec)} if side == 'BUY' else {'min_receive': D(0)}
    prepared = adapter.prepare(aid, adapter.quote(action, bounds))
    result = adapter.submit(prepared)
    if result.terminal and result.status in (Status.SETTLED, Status.REJECTED) and captured:
        with exclusive_transaction(con):
            store.event(con, 'adapter_spot_terminal', deal_id=deal['id'], clip_id=clip_id,
                        attempt_id=aid, status=result.status.value, native_ref=result.native_ref.id)
        return captured[0]
    if captured:
        return replace(captured[0], status='unknown')
    from ..evm import SentUnknown
    raise SentUnknown(-1, [], 'common spot attempt unresolved; recover native journal')


def ensure_evm_allowance(native, token, amount_raw):
    """Native wallet journals approval separately, before a dependent swap."""
    return native.ensure_allowance(token, amount_raw, '')


def sol_spec(deal, native, *, wallet=None):
    """Frozen SOL identity view; no registry refresh during execution/recovery."""
    inst = InstrumentSpec.from_json(deal['inst_json'])
    cfg = owner.OwnerCfg.from_frozen(deal['owner_json'])
    expected_wallet = cfg.get('wallets.sol_hl.solana_address') if inst.profile_id == owner.SOL_HL else \
        cfg.get('wallets.solana.solana_address')
    actual_wallet = wallet or native.wallet
    if (not inst.verified or not inst.identity_hash or inst.chain != 'solana'
            or inst.token != deal['token'] or inst.token_dec != deal['token_dec']
            or not expected_wallet or expected_wallet != actual_wallet):
        raise AdapterError(ErrorKind.IDENTITY, 'Solana frozen instrument or wallet differs')
    return LegSpec(deal['id'] + ':spot', 'inventory', 'long', 'sol_best', 'sol_best',
                   actual_wallet, inst.token, f'{inst.chain}:{inst.token}', inst.identity_hash,
                   inst.fs, D(1).scaleb(-inst.token_dec), D(1), inst.quote_mint, inst.quote_mint,
                   Capabilities('dex', 'spot', 'solana', amount=True), network=inst.genesis_hash,
                   decimals=inst.token_dec, quote_decimals=inst.quote_dec,
                   metadata_revision='frozen:' + deal['id'], legacy_hash=inst.inst_hash())


def reject_unsigned_sol(con, *, deal, clip_id, native, logical):
    """Prove a local pre-submit refusal without fabricating a chain receipt.

    No native attempt is safe only before the common send claim. After a claim,
    require the native journal's explicit unsigned-abandonment proof.
    """
    with exclusive_transaction(con):
        if native.touched(con, logical):
            return False
        attempts = native.attempts(con, logical)
        claims = con.execute("SELECT 1 FROM exec_events WHERE kind='adapter_spot_claimed' "
                             "AND clip_id=? AND json_extract(json,'$.attempt_id')=?",
                             (clip_id, logical)).fetchone()
        if (attempts and any(a['state'] != 'ABANDONED_UNSIGNED' for a in attempts)) or (
                claims and not attempts):
            return False
        done = con.execute("SELECT 1 FROM exec_events WHERE kind='adapter_spot_terminal' "
                           "AND clip_id=? AND json_extract(json,'$.attempt_id')=?",
                           (clip_id, logical)).fetchone()
        if not done:
            store.event(con, 'adapter_spot_terminal', deal_id=deal['id'], clip_id=clip_id,
                        attempt_id=logical, status=Status.REJECTED.value,
                        proof='native_unsigned' if attempts else 'before_common_claim')
        return True


def submit_sol(con, *, deal, clip_id, native, router, decision, request, logical,
               metadata, min_validity_heights, apply, authorize, clock, block_height, registry=None, pair=None,
               compose_context=None):
    """Use the already approved/reselected route; do not choose a second route."""
    from .spot_bindings import solana
    from .contracts import from_raw
    spec = sol_spec(deal, native)
    side = 'BUY' if request.side == 'entry' else 'SELL'
    token, quote_asset = (request.output, request.input) if side == 'BUY' else (request.input, request.output)
    journal = SpotJournal(con, deal, clip_id, spec)
    captured, errors = [], []
    bindings = solana(native, router, token=token, quote_asset=quote_asset, journal=journal,
                      authorize=authorize, con=con, prices=None, hedge=None, resolve_ref=None,
                      read_executions=None, apply=apply, clip_ref=lambda _: str(clip_id),
                      slippage_bps=request.slippage_bps, min_validity_heights=min_validity_heights,
                      clock=clock, approved_selection=(request, decision), metadata=metadata,
                      block_height=block_height)
    send = bindings.submit
    def submit(leg, prepared):
        try:
            outcome = send(leg, prepared)
            captured.append(outcome)
            return outcome
        except Exception as exc:
            errors.append(exc)
            raise
    bindings.submit = submit
    bindings._captured_by_attempt = {logical: captured}
    if compose_context is not None and pair is None:
        from .execution import compose_clip_pair
        pair = compose_clip_pair(compose_context['registry'], spot_spec=spec, spot_bindings=bindings,
                                 perp_spec=compose_context['perp_spec'], perp_bindings=compose_context['perp_bindings'])
        compose_context['pair'] = pair
    if pair is not None:
        if pair.first.describe() != spec:
            raise AdapterError(ErrorKind.IDENTITY, 'composed spot leg differs from frozen spec')
        adapter = pair.first
    else:
        adapter = (registry or production_registry()).build(spec, SimpleNamespace(for_leg=lambda _: bindings))
    # Keep the common quote bound tied to the already approved route. BUY has
    # no separate min-out field in the common contract, so its quantity is the
    # route's effective on-chain minimum; SELL keeps the exact token input.
    winner = decision.winner
    minimum_raw = winner.effective_min_out if winner is not None else None
    if minimum_raw is None:
        raise AdapterError(ErrorKind.REJECTED, 'approved Solana route has no minimum receive')
    action = Action(logical, spec.leg_id, side,
                    from_raw(minimum_raw if side == 'BUY' else request.amount_in_raw, spec.decimals))
    bounds = ({'spend': from_raw(request.amount_in_raw, request.input.decimals)} if side == 'BUY' else
              {'min_receive': from_raw(minimum_raw, request.output.decimals)})
    prepared = adapter.prepare(logical, adapter.quote(action, bounds))
    result = adapter.submit(prepared)
    if result.terminal and captured:
        with exclusive_transaction(con):
            store.event(con, 'adapter_spot_terminal', deal_id=deal['id'], clip_id=clip_id,
                        attempt_id=logical, status=result.status.value,
                        native_ref=result.native_ref.id if result.native_ref else None)
        return captured[0]
    # NativeAdapter maps post-claim exceptions to UNKNOWN. An
    # ABANDONED_UNSIGNED attempt is the only safe exception: the native
    # journal proves that no signature existed, so synchronize the common
    # journal as terminal REJECTED and let sol_flow apply its not-sent gate.
    # Expiry or an absent attempt alone never proves this.
    attempts = getattr(native, 'attempts', lambda *_: [])(con, logical)
    unsigned = [a for a in attempts if a.get('state') == 'ABANDONED_UNSIGNED']
    if unsigned and not getattr(native, 'touched', lambda *_: True)(con, logical):
        attempt_id = unsigned[-1]['attempt_id']
        if not any(e.get('attempt_id') == logical for e in journal._event('adapter_spot_terminal')):
            with exclusive_transaction(con):
                store.event(con, 'adapter_spot_terminal', deal_id=deal['id'], clip_id=clip_id,
                            attempt_id=logical, status=Status.REJECTED.value, native_ref=attempt_id)
        from ..sol_exec import PresendRefused
        raise PresendRefused('подпись не создана — своп не отправлялся')
    # Preserve the native presend/no-sign path owned by the caller. A
    # signed/touched attempt remains UNKNOWN there and cannot release reserve.
    if errors:
        raise errors[0]
    if captured:
        return replace(captured[0], state='unknown', reason='common result unresolved')
    raise AdapterError(ErrorKind.UNKNOWN, 'common Solana attempt unresolved')
