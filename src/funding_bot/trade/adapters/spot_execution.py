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
               amount_raw, clock, authorize, registry=None):
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
