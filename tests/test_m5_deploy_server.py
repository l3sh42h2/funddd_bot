import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import sqlite3

import pytest

M5 = Path(__file__).resolve().parents[1] / 'deploy/migration'
sys.path.insert(0, str(M5))
import artifacts as af
import deploy_ipc
import server_job as job


def safe_state(epoch='epoch-1', safe=True):
    return {'drain': True, 'drain_epoch': epoch, 'state_revision': 3,
            'recovery': {'complete': safe, 'checked_at': time.time(), 'evidence_revision': 3},
            'execution': {'busy': False, 'lock_held': True, 'owner_pid': 7, 'owner_boot_id': 'boot'},
            'inventory': {'pending_requests': 0, 'operations': 0, 'clips': 0, 'perp': 0,
                          'unresolved': 0 if safe else 1},
            'safe_to_switch': safe, 'blockers': [] if safe else ['journal_unresolved']}


def test_drain_waits_for_fresh_safe_epoch_without_killing_executor():
    class Client:
        def __init__(self): self.values = [safe_state(safe=False), safe_state()]
        def call(self, method, payload):
            assert method == 'get_drain_state' and payload == {'drain_epoch': 'epoch-1'}
            return self.values.pop(0)
    sleeps = []
    value = job.wait_drain(Client(), 'release-x', 'epoch-1', sleep=sleeps.append)
    assert value['safe_to_switch'] and sleeps == [2]
    bad = safe_state(); bad['execution']['lock_held'] = False
    with pytest.raises(deploy_ipc.IpcRefused, match='ownership'):
        deploy_ipc.validate_drain(bad, release_id='release-x', epoch='epoch-1', require_safe=True)


def test_core_readiness_triggers_recovery_before_waiting_for_complete():
    manifest = {'release_id': 'release-x', 'source_sha256': 's', 'artifact_sha256': 'a',
                'ipc_version': 1, 'schema_version': 2}
    calls, recovered = [], {'value': False}
    class Client:
        def call(self, method, payload):
            calls.append(method)
            if method == 'get_drain_state':
                recovered['value'] = True
                return safe_state('epoch-x')
            if method == 'get_status':
                return {'ready': True, 'release_id': 'release-x', 'source_sha256': 's',
                        'artifact_sha256': 'a', 'ipc_version': 1, 'schema_version': 2,
                        'drain': True, 'drain_epoch': 'epoch-x', 'recovery_complete': recovered['value'],
                        'execution_lock_held': True}
            raise AssertionError(method)
    value = job.wait_core_ready(Client(), manifest, sleep=lambda _: pytest.fail('must not spin'))
    assert value['recovery_complete'] and calls == ['get_status', 'get_drain_state', 'get_status']


def test_core_readiness_refuses_immediately_when_service_exited():
    manifest = {'release_id': 'release-x', 'source_sha256': 's', 'artifact_sha256': 'a',
                'ipc_version': 1, 'schema_version': 2}
    class Client:
        def call(self, method, payload):
            raise deploy_ipc.IpcRefused('socket absent')
    class Commands:
        def run(self, argv, **kwargs):
            assert argv[:3] == ['systemctl', 'is-active', '--quiet']
            return subprocess.CompletedProcess(argv, 3, '')
    with pytest.raises(job.DeployFailure, match='exited before readiness'):
        job.wait_core_ready(Client(), manifest, commands=Commands(), timeout=180,
                            sleep=lambda _: pytest.fail('dead service must not spin'))


def test_component_health_uses_private_interface_identity_and_public_collector(tmp_path):
    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', tmp_path / 'legacy')
    manifest = {'release_id': 'release-x', 'source_sha256': 's', 'artifact_sha256': 'a'}
    collector = {'ready': True, 'pid': 41, 'boot_id': 'collector-boot', 'updated_at': time.time(), **manifest}
    interface = {'ready': True, 'pid': 42, 'boot_id': 'interface-boot', 'updated_at': time.time(), **manifest}
    (paths.state / 'collector/public').mkdir(parents=True)
    (paths.state / 'interface').mkdir(parents=True)
    af.atomic_json(paths.state / 'collector/public/collector_health.json', collector)
    af.atomic_json(paths.state / 'interface/interface_health.json', interface)
    class Commands:
        def run(self, argv, **kwargs):
            if argv[:3] == ['systemctl', 'is-active', '--quiet']:
                return subprocess.CompletedProcess(argv, 0, '')
            if argv[:3] == ['systemctl', 'show', '-p']:
                pid = '41\n' if 'collector' in argv[-1] else '42\n'
                return subprocess.CompletedProcess(argv, 0, pid)
            if argv[0] == 'curl':
                return subprocess.CompletedProcess(argv, 0, json.dumps(collector))
            raise AssertionError(argv)
    result = job.wait_components(paths, Commands(), manifest,
                                 sleep=lambda _: pytest.fail('valid health must pass'))
    assert result['collector']['pid'] == 41 and result['interface']['pid'] == 42


def test_missing_database_and_stale_base_cause_no_service_mutation(tmp_path, monkeypatch):
    missing = tmp_path / 'typo.db'
    with pytest.raises(FileNotFoundError):
        job.database_info(missing)
    assert not missing.exists()

    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', tmp_path / 'legacy')
    paths.lock = tmp_path / 'deploy.lock'
    expected = tmp_path / 'expected.json'; af.atomic_json(expected, {'release': 'old'})
    monkeypatch.setattr(job, 'verify_bundle', lambda *_: ({}, {'release_id': 'release-new'}))
    monkeypatch.setattr(job, 'current_identity', lambda *_: {'release': 'other'})
    class Commands:
        def __init__(self): self.calls = []
        def run(self, argv, **kwargs): self.calls.append(argv); return subprocess.CompletedProcess(argv, 0, '')
    commands = Commands()
    with pytest.raises(af.Refused, match='STALE_BASE'):
        job.Job(paths, commands).install(tmp_path / 'a', tmp_path / 'r', expected)
    assert commands.calls == [] and not paths.state.exists() and not paths.opt.exists()


def test_ui_only_requires_identical_core_collector_runtime_and_protocol():
    new = {'component_hashes': {'core': 'c', 'collector': 'k', 'interface': 'new'},
           'dependencies': {'d': 1}, 'ipc_version': 1, 'dto_version': 1, 'schema_version': 2, 'min_reader': 2}
    old = {**new, 'component_hashes': {'core': 'c', 'collector': 'k', 'interface': 'old'}}
    assert job.ui_only(old, new)
    for key in ('dependencies', 'ipc_version', 'schema_version'):
        changed = dict(old); changed[key] = {'different': True}
        assert not job.ui_only(changed, new)


def test_code_rollback_switches_reader_only_and_never_restores_database(tmp_path, monkeypatch):
    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', tmp_path / 'legacy')
    paths.execution_lock = tmp_path / 'execution.lock'
    previous = tmp_path / 'old'; previous.mkdir(parents=True)
    manifest = {'release_id': 'old-release', 'source_sha256': 's', 'artifact_sha256': 'a',
                'schema_version': 2, 'compatible_readers': [2]}
    calls = []
    monkeypatch.setattr(job, 'database_info', lambda _: {'schema_version': 2, 'min_reader': 2})
    monkeypatch.setattr(job, 'wait_drain', lambda *a, **k: safe_state())
    monkeypatch.setattr(job, '_wait_inactive', lambda *a: None)
    monkeypatch.setattr(job, 'execution_lock_free', lambda *a: calls.append('lock_free'))
    monkeypatch.setattr(job, 'install_units', lambda *a: calls.append('units'))
    monkeypatch.setattr(job, 'switch_link', lambda *a: calls.append('switch_code'))
    monkeypatch.setattr(job, 'wait_core_ready', lambda *a, **k: {
        'drain': True, 'drain_epoch': 'epoch-1', 'release_id': 'old-release'})
    monkeypatch.setattr(job, 'wait_components', lambda *a, **k: {})
    class Client:
        def call(self, method, payload):
            if method == 'get_status': return {'drain': True, 'drain_epoch': 'epoch-1', 'release_id': 'new'}
            if method == 'get_drain_state': return safe_state()
            if method == 'end_drain': return {'drain': False}
            raise AssertionError(method)
    class Commands:
        def run(self, argv, **kwargs): calls.append(tuple(map(str, argv))); return subprocess.CompletedProcess(argv, 0, '')
    monkeypatch.setattr(af, 'backup_database', lambda *a, **k: pytest.fail('rollback must not restore/copy a DB'))
    job.rollback_release(paths, Commands(), Client, previous, manifest)
    assert 'switch_code' in calls and 'lock_free' in calls


def test_service_templates_keep_single_executor_and_public_port():
    root = Path(__file__).resolve().parents[1]
    core = (root / 'deploy/migration/funding_bot-core.service').read_text()
    interface = (root / 'deploy/migration/funding_bot-interface.service').read_text()
    entry = (root / 'deploy/deploy.sh').read_text()
    assert 'Conflicts=funding_bot-trader.service' in core
    assert 'TimeoutStopSec=infinity' in core
    assert 'interface --port 8792 --host 127.0.0.1' in interface
    assert 'systemd-run' in entry and 'TimeoutStartSec=infinity' in entry


def test_first_transition_migrates_latest_state_offsets_and_secrets_privately(tmp_path, monkeypatch):
    legacy = tmp_path / 'legacy'; runtime = legacy / 'runtime'; runtime.mkdir(parents=True)
    con = sqlite3.connect(runtime / 'trade.db')
    con.executescript("CREATE TABLE deals(id); CREATE TABLE intents(id); CREATE TABLE flags(k PRIMARY KEY,v);"
                      "CREATE TABLE schema_version(id PRIMARY KEY,version,min_reader);"
                      "INSERT INTO schema_version VALUES(1,2,2); INSERT INTO flags VALUES('tg_offset','417');")
    con.commit(); con.close()
    con = sqlite3.connect(runtime / 'funding_bot.db'); con.execute('CREATE TABLE ticks(x)'); con.commit(); con.close()
    (runtime / 'owner.toml').write_text('schema_version = 2\n')
    (runtime / 'cabinet.env').write_text('CABINET_LOGIN=owner\n')
    (runtime / 'public_url.txt').write_text('https://stable.example\n')
    (runtime / 'okxdex.pace').write_text('17\n')
    keypair = tmp_path / 'wallet.json'; keypair.write_text('[private fixture]')
    (legacy / '.env').write_text('TG_BOT_TOKEN=fixture-token\nOKX_DEX_API_KEY=fixture-key\n'
                                 f'SOLANA_KEYPAIR_FILE={keypair}\n')
    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', legacy)
    monkeypatch.setattr(job.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=1234))
    class Commands:
        def run(self, argv, **kwargs): return subprocess.CompletedProcess(argv, 0, '')
    info, backup = job.migrate_legacy_runtime(paths, Commands(), 'release-12345678')
    assert info['tg_offset'] == 417 and backup.is_file()
    assert json.loads((paths.state / 'interface/state.json').read_text())['offset'] == 417
    assert (paths.state / 'core/trade.db').is_file() and (runtime / 'trade.db').is_file()
    assert (paths.state / 'shared/okxdex.pace').read_text() == '17\n'
    assert oct((paths.state / 'shared/okxdex.pace').stat().st_mode & 0o777) == '0o660'
    assert oct((paths.state / 'shared/trading.busy').stat().st_mode & 0o777) == '0o660'
    assert (paths.state / 'interface/public_url.txt').read_text() == 'https://stable.example\n'
    core_env = (paths.state / 'secrets/core.env').read_text()
    assert str(paths.state / 'core/keys/solana_keypair_file.json') in core_env
    assert 'FUNDING_START_DRAINED="1"' in core_env
    assert 'TG_BOT_TOKEN' not in core_env
    assert 'TG_BOT_TOKEN=fixture-token' in (paths.state / 'secrets/interface.env').read_text()
    assert oct((paths.state / 'secrets/core.env').stat().st_mode & 0o777) == '0o600'


def test_full_update_orders_drain_stop_backup_switch_readiness_and_release_state(tmp_path, monkeypatch):
    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', tmp_path / 'legacy')
    paths.lock = tmp_path / 'deploy.lock'; paths.execution_lock = paths.state / 'core/execution.lock'
    old_manifest = {'release_id': 'old-release', 'source_revision': '1' * 40,
                    'source_sha256': 'o' * 64, 'artifact_sha256': 'a' * 64,
                    'verification_identity_sha256': 'v' * 64,
                    'component_hashes': {'core': 'old', 'collector': 'old', 'interface': 'old'},
                    'dependencies': {'old': 1}, 'ipc_version': 1, 'dto_version': 1,
                    'schema_version': 2, 'min_reader': 2, 'compatible_readers': [2]}
    old_release = paths.releases / 'old-release'; old_release.mkdir(parents=True)
    af.atomic_json(old_release / 'release-manifest.json', old_manifest)
    old_state = {'format_version': 1, 'generation': 7, **{k: old_manifest[k] for k in (
        'release_id', 'source_revision', 'source_sha256', 'artifact_sha256', 'ipc_version',
        'dto_version', 'schema_version', 'min_reader')},
        'components': {'collector': 'old-release', 'core': 'old-release', 'interface': 'old-release'}}
    af.atomic_json(paths.release_state, old_state)
    expected_value = {'base': 'exact'}; expected = tmp_path / 'expected.json'; af.atomic_json(expected, expected_value)
    new_manifest = dict(old_manifest, release_id='new-release', source_revision='2' * 40,
                        source_sha256='n' * 64, artifact_sha256='b' * 64,
                        verification_identity_sha256='w' * 64,
                        component_hashes={'core': 'new', 'collector': 'new', 'interface': 'new'},
                        dependencies={'new': 1})
    events = []
    monkeypatch.setattr(job, 'verify_bundle', lambda *a: ({'identity': {}}, dict(new_manifest)))
    monkeypatch.setattr(job, 'current_identity', lambda *_: expected_value)
    monkeypatch.setattr(job, 'database_info', lambda *_: {'schema_version': 2, 'min_reader': 2})
    monkeypatch.setattr(job, 'wait_drain', lambda *a, **k: events.append('safe_old'))
    monkeypatch.setattr(job, '_wait_inactive', lambda *a: events.append('old_stopped'))
    monkeypatch.setattr(job, 'provision_accounts', lambda *a: events.append('accounts'))
    def backup(src, dst):
        Path(dst).parent.mkdir(parents=True, exist_ok=True); Path(dst).write_bytes(b'backup'); events.append('backup'); return 'b' * 64
    monkeypatch.setattr(af, 'backup_database', backup)
    def unlocked(path):
        Path(path).parent.mkdir(parents=True, exist_ok=True); Path(path).touch(); events.append('lock_free')
    monkeypatch.setattr(job, 'execution_lock_free', unlocked)
    release = paths.releases / 'new-release'; release.mkdir()
    monkeypatch.setattr(job, 'install_release', lambda *a: (release, dict(new_manifest)))
    monkeypatch.setattr(job, 'migration_dry_run', lambda *a: events.append('dry_migration'))
    monkeypatch.setattr(job, 'install_units', lambda *a: events.append('units'))
    monkeypatch.setattr(job, 'switch_link', lambda *a: events.append('switch'))
    monkeypatch.setattr(job, 'wait_core_ready', lambda *a, **k: {
        'drain': True, 'drain_epoch': 'epoch-new', 'release_id': 'new-release'})
    monkeypatch.setattr(job, 'wait_components', lambda *a, **k: {'all': 'ready'})
    class Client:
        def call(self, method, payload):
            events.append(method)
            if method == 'get_status': return {'state_revision': 4}
            if method == 'begin_drain': return {'drain': True, 'drain_epoch': 'epoch-old', 'state_revision': 5,
                                                'release_id': 'new-release'}
            if method == 'get_drain_state': return safe_state('epoch-new')
            if method == 'end_drain': return {'drain': False}
            raise AssertionError(method)
    class Commands:
        def run(self, argv, **kwargs):
            events.append(' '.join(map(str, argv)))
            code = 1 if list(map(str, argv))[:3] == ['systemctl', 'is-active', '--quiet'] else 0
            return subprocess.CompletedProcess(argv, code, '')
    report = job.Job(paths, Commands(), Client).install(tmp_path / 'artifact', tmp_path / 'receipt', expected)
    state = json.loads(paths.release_state.read_text())
    assert report['status'] == 'healthy' and state['release_id'] == 'new-release' and state['generation'] == 8
    assert events.index('safe_old') < events.index('old_stopped') < events.index('backup') < events.index('switch')
    assert events.index('switch') < events.index('end_drain')
