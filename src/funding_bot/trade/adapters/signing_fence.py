"""Opt-in durable IOC signing barrier for native HTTP perpetual adapters.

The same SQLite connection owns claim, signature evidence and no-send recovery.
No network read is performed while recovering under the writer transaction.
"""
import json
from types import MethodType
from pathlib import Path
from threading import Lock
from decimal import Decimal as D
from .contracts import AdapterError, ErrorKind
from .. import store

BARRIER = 'mandatory-signed-callback-v1'
_OWNER_LOCK = Lock()
_VIEW_TYPES = {}


class ExecutionView:
    """Connection-local fence; transport state, budgets and caches stay shared."""
    def __new__(cls, native):
        while isinstance(native, ExecutionView):
            native = object.__getattribute__(native, '_native')
        # Native overrides may use super(); their self must remain an instance
        # of the native class. Never initialize/copy another transport.
        native_type = type(native)
        with _OWNER_LOCK:
            view_type = _VIEW_TYPES.get(native_type)
            if view_type is None:
                view_type = type('ExecutionView_' + native_type.__name__, (ExecutionView, native_type), {})
                _VIEW_TYPES[native_type] = view_type
        return object.__new__(view_type)

    def __init__(self, native):
        while isinstance(native, ExecutionView):
            native = object.__getattribute__(native, '_native')
        object.__setattr__(self, '_native', native)
        object.__setattr__(self, '_execution_fence', None)

    def __getattribute__(self, name):
        if name in {'_native', '_execution_fence', '__class__', '__dict__'}:
            return object.__getattribute__(self, name)
        native = object.__getattribute__(self, '_native')
        value = getattr(native, name)
        if isinstance(value, MethodType) and value.__self__ is native:
            return MethodType(value.__func__, self)
        return value

    def __setattr__(self, name, value):
        if name == '_execution_fence':
            object.__setattr__(self, name, value)
        else:
            setattr(self._native, name, value)


class SigningFence:
    def __init__(self, con, *, account, venue):
        self.con, self.account, self.venue = con, account, venue

    def _row(self, cid, symbol):
        row = store.get_perp_order(self.con, cid)
        if row is None or row['symbol'] != symbol or row['venue'] != self.venue:
            raise AdapterError(ErrorKind.IDENTITY, 'native signing attempt scope differs')
        clip = store.get_clip(self.con, row['clip_id'])
        intent = store.get_intent(self.con, clip['intent_id']) if clip else None
        deal = store.get_deal(self.con, intent['deal_id']) if intent else None
        if (deal is None or deal['sim'] or deal['perp_venue'] != self.venue or deal['symbol'] != symbol):
            raise AdapterError(ErrorKind.IDENTITY, 'native signing deal scope differs')
        proofs = self.con.execute(
            "SELECT deal_id,intent_id,clip_id,json FROM exec_events WHERE kind='adapter_perp_prepared' "
            "AND json_extract(json,'$.attempt_id')=?", (cid,)).fetchall()
        if len(proofs) != 1:
            raise AdapterError(ErrorKind.IDENTITY, 'native signing proof missing or ambiguous')
        proof = proofs[0]
        data = json.loads(proof['json'])
        if ((proof['deal_id'], proof['intent_id'], proof['clip_id']) !=
                (deal['id'], intent['id'], clip['id']) or
                data.get('account') != self.account or data.get('instrument') != symbol or
                data.get('send_barrier') != BARRIER or not data.get('spec_hash') or not data.get('quote_hash')):
            raise AdapterError(ErrorKind.IDENTITY, 'native signing prepared proof differs')
        return row

    def callback(self, callback, *, symbol, side, quantity, price, client_id, reduce_only):
        if not callable(callback):
            raise AdapterError(ErrorKind.CONFIG, 'durable IOC signing callback is mandatory')
        if self.con.in_transaction:
            raise AdapterError(ErrorKind.CONFIG, 'IOC cannot join caller transaction')
        def signed(nonce):
            if type(nonce) is not int or nonce < 0 or self.con.in_transaction:
                raise AdapterError(ErrorKind.CONFIG, 'invalid signing evidence or transaction')
            before = self._row(client_id, symbol)
            if before['state'] != 'SENT' or before['sign_nonce'] is not None:
                raise store.StoreError('native signing admission is no longer unused')
            callback(nonce)
            if self.con.in_transaction:
                raise AdapterError(ErrorKind.CONFIG, 'signing callback must commit before send')
            row = self._row(client_id, symbol)
            if (row['state'] != 'SENT' or row['sign_nonce'] != nonce or
                    (row['side'], D(row['qty']), D(row['price']), row['reduce_only'], row['tif']) !=
                    (side, quantity, price, int(reduce_only), 'IOC')):
                raise AdapterError(ErrorKind.IDENTITY, 'durable signing evidence differs from native IOC')
        return signed

    def absent(self, symbol, client_id, account, proof_con):
        if proof_con is not self.con or not proof_con.in_transaction or account != self.account:
            return False
        try:
            row = self._row(client_id, symbol)
            return (row['state'] in ('SENT', 'UNKNOWN') and row['sign_nonce'] is None and
                    all(row[k] is None for k in ('order_id', 'executed_qty', 'cum_quote', 'avg_price', 'resolved_ts')))
        except (AdapterError, KeyError, TypeError, ValueError):
            return False


class JournalBoundIoc:
    """Legacy IOC stays unchanged until core explicitly binds this native instance."""
    def execution_view(self, con, account):
        """Connection-owned signing boundary sharing only the native transport.

        Startup and executor run on different SQLite connections. A
        transport view avoids rebinding a live fence to the other connection;
        native caches/HTTP budget/locks remain shared with the parent.
        """
        view = ExecutionView(self)
        view.bind_execution_journal(con, account)
        return view

    def bind_execution_journal(self, con, account):
        if con.in_transaction:
            raise AdapterError(ErrorKind.CONFIG, 'bind account before opening writer transaction')
        main = next((row[2] for row in con.execute('PRAGMA database_list') if row[1] == 'main'), '')
        identity = ('file', str(Path(main).resolve())) if main else ('memory', id(con))
        with _OWNER_LOCK:
            previous = getattr(self, '_execution_owner_db', None)
            if previous is not None and previous != identity:
                raise AdapterError(ErrorKind.IDENTITY, 'native execution owner cannot switch database')
            self._execution_owner_db = identity
        # Gate performs an authenticated account read here, never in absent().
        if not account or self.history_account() != account:
            raise AdapterError(ErrorKind.IDENTITY, 'native execution account is unproven')
        old = getattr(self, '_execution_fence', None)
        if old is not None and (old.con is not con or old.account != account or old.venue != self.venue):
            raise AdapterError(ErrorKind.IDENTITY, 'native execution journal binding is immutable')
        self._execution_fence = old or SigningFence(con, account=account, venue=self.venue)
        return BARRIER

    def submission_absent(self, symbol, client_id, account, *, proof_con):
        fence = getattr(self, '_execution_fence', None)
        return fence is not None and fence.absent(symbol, client_id, account, proof_con)

    def _ioc_callback(self, callback, **order):
        fence = getattr(self, '_execution_fence', None)
        return callback if fence is None else fence.callback(callback, **order)
