"""Durable M3 submit barrier; core supplies its connection, no independent DB writer.

A claimed attempt is NEVER submitted again, including after a process crash. Native
journals remain authoritative for signatures/fills. M4 maps these references into operations.
"""
import time
from .contracts import AdapterError, ErrorKind, Prepared


class AttemptJournal:
    def __init__(self, connection):
        self.con = connection
        self._outside_transaction()
        self.con.execute('''CREATE TABLE IF NOT EXISTS core_leg_attempts (
            attempt_id TEXT PRIMARY KEY, action_id TEXT NOT NULL UNIQUE, leg_id TEXT NOT NULL, quote_hash TEXT NOT NULL, spec_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('PREPARED','CLAIMED')), created REAL NOT NULL)''')
        self.con.commit()

    def _outside_transaction(self):
        if self.con.in_transaction:
            raise AdapterError(ErrorKind.CONFIG, "adapter journal requires its own short transaction")

    def prepare(self, prepared: Prepared):
        self._outside_transaction()
        p = prepared
        with self.con:
            self.con.execute('INSERT OR IGNORE INTO core_leg_attempts VALUES (?,?,?,?,?,?,?)',
                             (p.attempt_id, p.quote.action.action_id, p.quote.action.leg_id, p.quote.fingerprint, p.spec_hash, 'PREPARED', time.time()))
            row = self.con.execute('SELECT leg_id,quote_hash,spec_hash FROM core_leg_attempts WHERE attempt_id=?',
                                   (p.attempt_id,)).fetchone()
            if row is None or tuple(row) != (p.quote.action.leg_id, p.quote.fingerprint, p.spec_hash):
                raise AdapterError(ErrorKind.IDENTITY, 'attempt id reused with changed request')

    def claim(self, prepared: Prepared):
        self._outside_transaction()
        with self.con:
            row = self.con.execute('UPDATE core_leg_attempts SET state=? WHERE attempt_id=? AND leg_id=? '
                                   'AND quote_hash=? AND spec_hash=? AND state=?',
                                   ('CLAIMED', prepared.attempt_id, prepared.quote.action.leg_id,
                                    prepared.quote.fingerprint, prepared.spec_hash, 'PREPARED'))
            if row.rowcount != 1:
                raise AdapterError(ErrorKind.UNKNOWN, 'attempt absent or already claimed; resolve, do not resend')
