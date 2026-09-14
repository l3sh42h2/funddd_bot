"""Read-only switching inventory. A quiet ledger is necessary, not readiness.

This does not contact exchanges or assert external position equality. The deploy
job must separately prove drain, execution ownership, reader compatibility and
fresh recovery. Missing schema is a refusal, never an empty trading account.
"""
from pathlib import Path
import sqlite3
from contextlib import contextmanager


@contextmanager
def existing_database(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError('trade database is not a file')
    con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute('PRAGMA query_only=ON')
        yield con
    finally:
        con.close()


def inventory(con):
    """One SQLite read snapshot; no SELECT * or private identifiers in result."""
    owned = not con.in_transaction
    if owned:
        con.execute('BEGIN')
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {'schema_version', 'deals', 'intents', 'clips', 'operations', 'dex_txs',
                    'perp_orders', 'sol_tx_attempts', 'sol_receipts', 'hl_order_attempts'}
        if required - tables:
            raise ValueError('missing required trading schema: ' + ','.join(sorted(required - tables)))
        schema = con.execute('SELECT version,min_reader FROM schema_version WHERE id=1').fetchone()
        if schema is None:
            raise ValueError('missing trading schema version')
        queries = {
            'intents': "SELECT count(*) FROM intents WHERE status IN ('approved','running')",
            'clips': "SELECT count(*) FROM clips WHERE state IS NULL OR state NOT IN "
                     "('PLANNED','DEX_OK','DEX_REVERTED','BALANCED')",
            'evm': "SELECT count(*) FROM dex_txs WHERE state IS NULL OR state NOT IN "
                   "('MINED_OK','MINED_REVERTED','REPLACED','DROPPED')",
            'perp': "SELECT count(*) FROM perp_orders WHERE state IS NULL OR state NOT IN "
                    "('FILLED','PARTIALLY_FILLED','EXPIRED','REJECTED','NOT_PLACED')",
            'operations': "SELECT count(*) FROM operations WHERE state IN ('APPROVED','RUNNING','PAUSED_UNKNOWN') "
                          "OR reserved_raw <> '0'",
            'hyperliquid': "SELECT count(*) FROM hl_order_attempts WHERE state IN ('PREPARED','SIGNED','UNKNOWN')",
            'solana': "SELECT count(*) FROM sol_tx_attempts a WHERE a.state IN "
                      "('VALIDATED','SIGNED_DURABLE','BROADCAST_ATTEMPTED','UNKNOWN','CONFIRMED_OK','CONFIRMED_ERR') "
                      "OR (a.state IN ('FINALIZED_OK','FINALIZED_ERR') AND NOT EXISTS "
                      "(SELECT 1 FROM sol_receipts r WHERE r.network=a.network AND r.signature=a.signature "
                      "AND r.commitment='finalized'))",
        }
        # M3 barriers cannot yet prove settlement by themselves. Any populated
        # barrier conservatively blocks until the operation mapper resolves it.
        if 'core_leg_attempts' in tables:
            queries['adapter_attempts'] = 'SELECT count(*) FROM core_leg_attempts'
        if 'core_requests' in tables:
            queries['requests'] = "SELECT count(*) FROM core_requests WHERE state IN ('queued','running')"
        counts = {name: int(con.execute(sql).fetchone()[0]) for name, sql in queries.items()}
        states = dict(con.execute('SELECT state,count(*) FROM deals GROUP BY state'))
        return dict(schema_version=int(schema[0]), min_reader=int(schema[1]), pending=counts,
                    ledger_quiet=not any(counts.values()), deal_states=states)
    finally:
        if owned:
            con.execute('ROLLBACK')
