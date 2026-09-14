"""Durable deployment fence; epochs cannot be reused by stale deploy jobs."""
import json
import time
import uuid
from ..ipc.protocol import RpcError


class Drain:
    def __init__(self, conns, event, *, start_drained=False, release_id=None):
        self.conns, self.event = conns, event
        self.state = self._read()
        if self.state['drain']:
            event.set()
        elif start_drained:
            self.begin({'release_id': release_id, 'expected_state_revision': self.state['state_revision']})

    def _read(self):
        row = self.conns.get().execute("SELECT value FROM core_meta WHERE key='deployment_drain'").fetchone()
        if row is None:
            return dict(drain=False, drain_epoch=None, release_id=None, state_revision=0)
        state = json.loads(row[0])
        if (type(state.get('drain')) is not bool or type(state.get('state_revision')) is not int
                or state['state_revision'] < 0 or (state['drain'] and not state.get('drain_epoch'))):
            raise ValueError('invalid durable drain state')
        return state

    def _write(self, state):
        con = self.conns.get()
        if con.in_transaction:
            raise RpcError('drain_transaction_busy')
        con.execute('BEGIN IMMEDIATE')
        try:
            if self._read() != self.state:
                raise RpcError('stale_drain_state')
            con.execute("INSERT OR REPLACE INTO core_meta VALUES('deployment_drain',?)",
                        (json.dumps(state, sort_keys=True),))
            con.commit()
        except BaseException:
            con.rollback()
            raise
        self.state = state

    def begin(self, payload):
        rid = payload.get('release_id')
        rev = payload.get('expected_state_revision')
        if not isinstance(rid, str) or not rid or len(rid) > 200 or type(rev) is not int:
            raise RpcError('invalid_drain_request')
        if self.state['drain'] and self.state['release_id'] == rid:
            if rev not in (self.state['state_revision'], self.state['state_revision']-1):
                raise RpcError('stale_state_revision')
            return dict(self.state)
        if self.state['drain']:
            raise RpcError('drain_owned_by_other_release')
        if rev != self.state['state_revision']:
            raise RpcError('stale_state_revision')
        # Fence first. A failed journal write must not leave execution open.
        self.event.set()
        self._write(dict(drain=True, drain_epoch=uuid.uuid4().hex, release_id=rid,
                         accepted_at=time.time(), state_revision=rev+1))
        return dict(self.state)

    def end(self, payload, *, running_release):
        if (not payload.get('drain_epoch') or payload.get('drain_epoch') != self.state['drain_epoch']
                or payload.get('expected_release_id') != running_release or not running_release):
            raise RpcError('stale_drain_owner')
        if not self.state['drain']:
            return dict(self.state)
        self._write(dict(self.state, drain=False, released_at=time.time(),
                         state_revision=self.state['state_revision']+1))
        self.event.clear()
        return dict(self.state)
