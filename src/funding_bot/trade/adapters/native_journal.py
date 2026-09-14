"""Adapter submit barrier backed by the existing perpetual execution journal.

No parallel attempt/reservation table: perp_orders owns admission, exec_events
stores only immutable request fingerprints. Native signature/fill records remain
unchanged and all transactions finish before a network callback.
"""
import json
from decimal import Decimal as D
from .contracts import AdapterError, ErrorKind
from .. import store


class PerpJournal:
    def __init__(self, con, *, clip_id, fill_venue, spec):
        self.con, self.clip_id, self.fill_venue, self.spec = con, clip_id, fill_venue, spec
        clip = store.get_clip(con, clip_id)
        if clip is None:
            raise store.StoreError('adapter clip is missing')
        self.intent_id = clip['intent_id']
        intent = store.get_intent(con, self.intent_id)
        self.deal_id = intent['deal_id']

    def _proof(self, attempt_id):
        row = self.con.execute(
            "SELECT json FROM exec_events WHERE kind='adapter_perp_prepared' AND clip_id=? "
            "AND json_extract(json,'$.attempt_id')=? ORDER BY ts LIMIT 1",
            (self.clip_id, attempt_id)).fetchone()
        return json.loads(row[0]) if row else None

    def _check(self, prepared):
        a = prepared.quote.action
        if (prepared.spec_hash != self.spec.fingerprint or a.leg_id != self.spec.leg_id
                or a.action_id != prepared.attempt_id):
            raise AdapterError(ErrorKind.IDENTITY, 'prepared perpetual scope changed')
        a.validate(self.spec)
        if self.spec.legacy_hash:
            intent = store.get_intent(self.con, self.intent_id)
            if json.loads(intent['spec_json']).get('inst_hash') != self.spec.legacy_hash:
                raise AdapterError(ErrorKind.IDENTITY, 'native intent instrument differs from frozen leg')
        return dict(attempt_id=prepared.attempt_id, spec_hash=prepared.spec_hash,
                    quote_hash=prepared.quote.fingerprint)

    def prepare(self, prepared):
        a = prepared.quote.action
        price = D(json.loads(prepared.quote.native)['price_cap'])
        with store.tx(self.con):
            proof = self._check(prepared)
            row = store.get_perp_order(self.con, prepared.attempt_id)
            if row is None:
                store.perp_order_intent(self.con, clip_id=self.clip_id, client_id=prepared.attempt_id,
                                        venue=self.fill_venue, symbol=self.spec.instrument, side=a.side,
                                        reduce_only=a.reduce_only, tif='IOC', price=price, qty=a.quantity)
                store.event(self.con, 'adapter_perp_prepared', deal_id=self.deal_id, intent_id=self.intent_id,
                            clip_id=self.clip_id, **proof)
                return
            expected = (self.clip_id, self.fill_venue, self.spec.instrument, a.side, int(a.reduce_only),
                        price, a.quantity)
            actual = (row['clip_id'], row['venue'], row['symbol'], row['side'], row['reduce_only'],
                      D(row['price']), D(row['qty']))
            if actual != expected or self._proof(prepared.attempt_id) != proof:
                raise AdapterError(ErrorKind.IDENTITY, 'existing native attempt differs from prepared request')

    def claim(self, prepared):
        with store.tx(self.con):
            proof = self._check(prepared)
            if self._proof(prepared.attempt_id) != proof:
                raise AdapterError(ErrorKind.IDENTITY, 'native request proof missing or changed')
            if not store.perp_order_sent(self.con, prepared.attempt_id):
                raise AdapterError(ErrorKind.UNKNOWN, 'native attempt already admitted; resolve, never resend')

    def on_signed(self, attempt_id, nonce):
        if type(nonce) is not int or nonce < 0:
            raise store.StoreError('invalid native signing nonce')
        with store.tx(self.con):
            row = store.get_perp_order(self.con, attempt_id)
            if row is None or row['clip_id'] != self.clip_id or row['state'] != store.PerpOrderState.SENT:
                raise store.StoreError('signature has no admitted native attempt')
            if row['sign_nonce'] is not None and int(row['sign_nonce']) != nonce:
                raise store.StoreError('native attempt signing nonce is immutable')
            # This is evidence of signing, not a resolved order result.
            self.con.execute("UPDATE perp_orders SET sign_nonce=? WHERE client_id=? AND state=?",
                             (nonce, attempt_id, str(store.PerpOrderState.SENT)))

    def lookup(self, attempt_id):
        row = store.get_perp_order(self.con, attempt_id)
        if row is None or row['clip_id'] != self.clip_id:
            raise AdapterError(ErrorKind.IDENTITY, 'native attempt is outside this clip')
        proof = self._proof(attempt_id)
        if proof is None or proof['spec_hash'] != self.spec.fingerprint:
            raise AdapterError(ErrorKind.IDENTITY, 'native attempt is outside this leg scope')
        return dict(row, leg_id=self.spec.leg_id)
