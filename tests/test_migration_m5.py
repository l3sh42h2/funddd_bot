"""M5 preparation checks; these tests do not install or restart production."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import pytest
from funding_bot.core.preflight import existing_database, inventory
from funding_bot.trade import store

p = Path(__file__).resolve().parents[1] / 'deploy/migration/artifacts.py'
spec = importlib.util.spec_from_file_location('migration_artifacts', p)
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def test_missing_database_never_created(tmp_path):
    missing = tmp_path / 'typo.db'
    with pytest.raises(FileNotFoundError):
        with existing_database(missing):
            pass
    with pytest.raises(FileNotFoundError):
        a.backup_database(missing, tmp_path / 'backup.db')
    assert not missing.exists() and not (tmp_path / 'backup.db').exists()


def test_empty_database_is_not_quiet_trading_state(tmp_path):
    p = tmp_path / 'empty.db'
    sqlite3.connect(p).close()
    with existing_database(p) as con:
        with pytest.raises(ValueError, match='missing required'):
            inventory(con)
        assert not con.in_transaction


def test_inventory_consistent_read_only_and_unknown(journal):
    # Importing the M4 fixture below shares no data between tests.
    con, oid, cid = journal
    before = con.total_changes
    assert not inventory(con)['ledger_quiet']  # RUNNING operation, before any send
    assert con.total_changes == before
    store.set_operation_state(con, oid, store.OpState.STOPPED)
    assert inventory(con)['ledger_quiet']
    store.set_clip_state(con, cid, store.ClipState.DEX_SENT)
    assert inventory(con)['pending']['clips'] == 1
    store.set_clip_state(con, cid, store.ClipState.DEX_UNKNOWN)
    assert not inventory(con)['ledger_quiet']


from test_migration_m4 import journal


def test_backup_committed_wal_no_overwrite(journal, tmp_path):
    con, oid, cid = journal
    source = con.execute('PRAGMA database_list').fetchone()[2]
    dest = tmp_path / 'backup.db'
    checksum = a.backup_database(source, dest)
    assert checksum == a.digest(dest) and dest.stat().st_mode & 0o777 == 0o600
    with existing_database(dest) as copy:
        assert copy.execute('SELECT count(*) FROM operations').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            copy.execute('DELETE FROM operations')
    with pytest.raises(FileExistsError):
        a.backup_database(source, dest)
    assert a.digest(dest) == checksum


def test_receipt_exact_runtime_dependency_profile_and_artifact(tmp_path):
    artifact = tmp_path / 'artifact.tar'
    artifact.write_bytes(b'fixture immutable artifact')
    ident = a.verification_identity(artifact=artifact, sources={'src/a.py': 'abc'},
                                   dependencies={'lock': 'xyz', 'installed': 'xyz'},
                                   runtime=a.runtime_fingerprint(), profile={'argv': ['pytest', 'tests'], 'version': 1})
    receipt = a.verified_receipt(ident, exit_code=0, passed=1, failed=0, evidence_sha256='a' * 64)
    a.require_verified(receipt, ident)
    for key in ('artifact_sha256', 'sources', 'dependencies', 'runtime', 'profile'):
        changed = dict(ident, **{key: 'changed'})
        with pytest.raises(a.Refused, match='VERIFICATION_MISMATCH'):
            a.require_verified(receipt, changed)
    for bad in ({'exit_code': 1}, {'passed': 0}, {'evidence_sha256': ''}):
        with pytest.raises(a.Refused):
            a.require_verified({**receipt, **bad}, ident)
    # Retaining a mutable caller dictionary cannot mutate the receipt.
    ident['sources']['src/a.py'] = 'mutated'
    assert receipt['identity']['sources']['src/a.py'] == 'abc'


def test_stale_base_and_stable_flock(tmp_path):
    path = tmp_path / 'deploy.lock'
    with a.deploy_lock(path):
        inode = path.stat().st_ino
        with pytest.raises(a.Refused, match='DEPLOY_BUSY'):
            with a.deploy_lock(path):
                pytest.fail('second deploy got lock')
        with pytest.raises(a.Refused, match='STALE_BASE'):
            a.require_base({'release': 'new'}, {'release': 'old'})
    with a.deploy_lock(path):
        assert path.stat().st_ino == inode
        a.require_base({'release': 'new'}, {'release': 'new'})


def test_source_paths_and_atomic_state(tmp_path):
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src/a.py').write_text('fixture')
    assert a.source_manifest(tmp_path, ['src/a.py']) == {'src/a.py': a.digest(tmp_path / 'src/a.py')}
    for names in ([], ['../escape'], ['src/a.py', 'src/a.py'], ['missing']):
        with pytest.raises(a.Refused):
            a.source_manifest(tmp_path, names)
    (tmp_path / 'link').symlink_to(tmp_path / 'src', target_is_directory=True)
    with pytest.raises(a.Refused):
        a.source_manifest(tmp_path, ['link/a.py'])
    state = tmp_path / 'release-state.json'
    a.atomic_json(state, {'release': 'one'})
    a.atomic_json(state, {'release': 'two'})
    assert json.loads(state.read_text()) == {'release': 'two'}
    assert state.stat().st_mode & 0o777 == 0o600


def test_unknown_future_order_state_blocks_switch(journal):
    con, oid, cid = journal
    store.set_operation_state(con, oid, store.OpState.STOPPED)
    con.execute("INSERT INTO perp_orders(client_id,state) VALUES('future-fixture','NEW_UNKNOWN_STATE')")
    report = inventory(con)
    assert not report['ledger_quiet'] and report['pending']['perp'] == 1
