import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import sqlite3
import os

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


def release_manifest(release_id):
    return {'release_id': release_id, 'source_revision': '1' * 40, 'source_sha256': 's' * 64,
            'artifact_sha256': 'a' * 64, 'verification_identity_sha256': 'v' * 64,
            'component_hashes': {'core': 'c', 'collector': 'k', 'interface': 'i'},
            'dependencies': {}, 'runtime': {}, 'ipc_version': 1, 'dto_version': 1,
            'schema_version': 2, 'min_reader': 2, 'compatible_readers': [2]}


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


def test_end_drain_refreshes_after_ui_ack_and_retries_lost_reply():
    manifest = {'release_id': 'release-x', 'source_sha256': 's', 'artifact_sha256': 'a',
                'ipc_version': 1, 'schema_version': 2}
    state = {'ended': False, 'end_calls': 0, 'drain_checks': 0}
    class Client:
        def call(self, method, payload):
            if method == 'get_status':
                return {'ready': True, 'release_id': 'release-x', 'source_sha256': 's',
                        'artifact_sha256': 'a', 'ipc_version': 1, 'schema_version': 2,
                        'drain': not state['ended'], 'drain_epoch': 'epoch-1',
                        'recovery_complete': True, 'execution_lock_held': True}
            if method == 'get_drain_state':
                state['drain_checks'] += 1
                if state['drain_checks'] == 1:
                    raise deploy_ipc.IpcRefused('drain_not_ready after UI ACK commit')
                return safe_state('epoch-1')
            if method == 'end_drain':
                state['end_calls'] += 1
                state['ended'] = True
                raise deploy_ipc.IpcRefused('reply lost')
            raise AssertionError(method)
    result = job.end_drain_verified(Client, manifest, 'epoch-1', attempts=3, sleep=lambda _: None)
    assert result['drain'] is False and state['end_calls'] == 1 and state['drain_checks'] == 2


def test_end_drain_rejects_undrained_status_from_wrong_epoch():
    manifest = {'release_id': 'release-x', 'source_sha256': 's', 'artifact_sha256': 'a',
                'ipc_version': 1, 'schema_version': 2}
    class Client:
        def call(self, method, payload):
            return {'ready': True, 'release_id': 'release-x', 'source_sha256': 's',
                    'artifact_sha256': 'a', 'ipc_version': 1, 'schema_version': 2,
                    'drain': False, 'drain_epoch': 'other', 'recovery_complete': True,
                    'execution_lock_held': True}
    with pytest.raises(job.DeployFailure, match='outcome unknown: drain epoch changed'):
        job.end_drain_verified(Client, manifest, 'expected', attempts=1, sleep=lambda _: None)


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


def test_old_core_restore_keeps_target_owned_drain_until_old_code_is_ready():
    manifest = {'release_id': 'release-old', 'source_sha256': 's', 'artifact_sha256': 'a',
                'ipc_version': 1, 'schema_version': 2}
    class Client:
        def call(self, method, payload):
            if method == 'get_status':
                return {'ready': True, 'release_id': 'release-old', 'source_sha256': 's',
                        'artifact_sha256': 'a', 'ipc_version': 1, 'schema_version': 2,
                        'drain': True, 'drain_epoch': 'epoch-1', 'recovery_complete': True,
                        'execution_lock_held': True}
            if method == 'get_drain_state':
                return dict(safe_state('epoch-1'), release_id='release-target')
            raise AssertionError(method)
    value = job.wait_core_ready(Client(), manifest, drain_release_id='release-target',
                                sleep=lambda _: pytest.fail('valid restored core must pass'))
    assert value['release_id'] == 'release-old'


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


def test_transition_identity_prevents_stale_release_state_after_link_switch(tmp_path):
    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', tmp_path / 'legacy')
    target = paths.releases / 'release-new'; target.mkdir(parents=True)
    paths.opt.mkdir(exist_ok=True)
    paths.current.symlink_to(target)
    af.atomic_json(paths.transition, {'operation_id': 'op-1', 'phase': 'switched',
                                      'target_release_id': 'release-new', 'db_authority': 'state',
                                      'previous_identity': {'release_id': 'release-old'}})
    identity = job.current_identity(paths)
    assert identity == {'kind': 'transition', 'operation_id': 'op-1', 'phase': 'switched',
                        'target_release_id': 'release-new', 'db_authority': 'state',
                        'current_release_id': 'release-new',
                        'previous_identity': {'release_id': 'release-old'}}


def test_interrupted_first_preswitch_resumes_legacy_and_discards_copied_state(tmp_path, monkeypatch):
    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', tmp_path / 'legacy')
    paths.opt.mkdir(parents=True); paths.state.mkdir(parents=True)
    (paths.state / 'copied').write_text('not authoritative')
    transition = {'operation_id': 'op', 'phase': 'state_ready', 'target_release_id': 'release-new',
                  'db_authority': 'legacy_copied', 'previous_state': None, 'ui_only': False}
    af.atomic_json(paths.transition, transition)
    resumed = []
    monkeypatch.setattr(job, 'resume_legacy', lambda *a: resumed.append(True))
    with pytest.raises(job.DeployFailure, match='RESNAPSHOT'):
        job.recover_transition(paths, object(), None, transition)
    assert resumed == [True] and not paths.state.exists() and not paths.transition.exists()


@pytest.mark.parametrize('phase', ['prepared', 'switched'])
def test_transition_with_undrained_old_core_only_clears_journal(tmp_path, monkeypatch, phase):
    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', tmp_path / 'legacy')
    old = paths.releases / 'release-old'; old.mkdir(parents=True)
    previous = {'release_id': 'release-old'}
    manifest = release_manifest('release-old')
    af.atomic_json(old / 'release-manifest.json', manifest)
    af.atomic_json(paths.release_state, previous)
    transition = {'operation_id': 'op', 'phase': phase, 'target_release_id': 'release-new',
                  'db_authority': 'state', 'previous_state': previous, 'ui_only': False}
    af.atomic_json(paths.transition, transition)
    class Commands:
        def run(self, argv, **kwargs): return subprocess.CompletedProcess(argv, 0, '')
    class Client:
        def call(self, method, payload):
            assert method == 'get_status'
            return {'ready': True, 'release_id': 'release-old',
                    'source_sha256': manifest['source_sha256'], 'artifact_sha256': manifest['artifact_sha256'],
                    'ipc_version': 1, 'schema_version': 2, 'drain': False,
                    'drain_epoch': 'old-epoch', 'recovery_complete': True,
                    'execution_lock_held': True}
    monkeypatch.setattr(job, 'restore_before_switch', lambda *a, **k: pytest.fail('old core is already healthy'))
    verified = []
    monkeypatch.setattr(job, 'wait_components', lambda *a, **k: verified.append(True))
    with pytest.raises(job.DeployFailure, match='RESNAPSHOT'):
        job.recover_transition(paths, Commands(), Client, transition)
    assert verified == [True] and not paths.transition.exists()
    assert json.loads(paths.release_state.read_text()) == previous


def test_committed_healthy_target_wins_over_leftover_transition(tmp_path, monkeypatch):
    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', tmp_path / 'legacy')
    target = paths.releases / 'release-new'; target.mkdir(parents=True)
    paths.current.symlink_to(target)
    committed = {'release_id': 'release-new', 'status': 'healthy', 'operation_id': 'op'}
    af.atomic_json(paths.release_state, committed)
    transition = {'operation_id': 'op', 'phase': 'switched', 'target_release_id': 'release-new',
                  'db_authority': 'state', 'previous_state': {'release_id': 'release-old'}, 'ui_only': False}
    af.atomic_json(paths.transition, transition)
    monkeypatch.setattr(job, 'rollback_release', lambda *a: pytest.fail('committed target must not roll back'))
    with pytest.raises(job.DeployFailure, match='FINALIZED_RESNAPSHOT'):
        job.recover_transition(paths, object(), object(), transition)
    assert not paths.transition.exists() and json.loads(paths.release_state.read_text()) == committed


def test_same_release_prior_state_cannot_finalize_new_transition_operation(tmp_path, monkeypatch):
    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', tmp_path / 'legacy')
    release = paths.releases / 'release-same'; release.mkdir(parents=True)
    manifest = release_manifest('release-same')
    af.atomic_json(release / 'release-manifest.json', manifest)
    paths.current.symlink_to(release)
    previous = {'release_id': 'release-same', 'status': 'healthy', 'operation_id': 'old-op'}
    af.atomic_json(paths.release_state, previous)
    transition = {'operation_id': 'new-op', 'phase': 'switching', 'target_release_id': 'release-same',
                  'db_authority': 'state', 'previous_state': previous, 'ui_only': True}
    af.atomic_json(paths.transition, transition)
    rolled = []
    monkeypatch.setattr(job, 'rollback_ui', lambda *a: rolled.append(True))
    with pytest.raises(job.DeployFailure, match='ROLLED_BACK_RESNAPSHOT'):
        job.recover_transition(paths, object(), object(), transition)
    assert rolled == [True] and not paths.transition.exists()


def test_mixed_ui_components_are_used_for_prepared_full_recovery(tmp_path, monkeypatch):
    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', tmp_path / 'legacy')
    release_a = paths.releases / 'release-A'; release_a.mkdir(parents=True)
    release_b = paths.releases / 'release-B'; release_b.mkdir(parents=True)
    manifest_a, manifest_b = release_manifest('release-A'), release_manifest('release-B')
    af.atomic_json(release_a / 'release-manifest.json', manifest_a)
    af.atomic_json(release_b / 'release-manifest.json', manifest_b)
    paths.current.symlink_to(release_b)
    previous = {'release_id': 'release-B', 'status': 'healthy', 'drain': False,
                'components': {'core': 'release-A', 'collector': 'release-A', 'interface': 'release-B'}}
    af.atomic_json(paths.release_state, previous)
    transition = {'operation_id': 'op-C', 'phase': 'prepared', 'target_release_id': 'release-C',
                  'db_authority': 'state', 'previous_state': previous, 'ui_only': False}
    af.atomic_json(paths.transition, transition)
    class Client:
        def call(self, method, payload):
            assert method == 'get_status'
            return {'ready': True, 'release_id': 'release-A',
                    'source_sha256': manifest_a['source_sha256'],
                    'artifact_sha256': manifest_a['artifact_sha256'], 'ipc_version': 1,
                    'schema_version': 2, 'drain': False, 'drain_epoch': 'epoch-A',
                    'recovery_complete': True, 'execution_lock_held': True}
    class Commands:
        def run(self, argv, **kwargs): return subprocess.CompletedProcess(argv, 0, '')
    checked = []
    def check_components(paths_arg, commands, fallback, **kwargs):
        checked.append((kwargs['collector_manifest']['release_id'],
                        kwargs['interface_manifest']['release_id']))
    monkeypatch.setattr(job, 'wait_components', check_components)
    with pytest.raises(job.DeployFailure, match='RECOVERED_RESNAPSHOT'):
        job.recover_transition(paths, Commands(), Client, transition)
    assert checked == [('release-A', 'release-B')]
    assert json.loads(paths.release_state.read_text())['components'] == previous['components']


def test_mixed_ui_components_normalize_after_drained_full_restore(tmp_path, monkeypatch):
    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', tmp_path / 'legacy')
    release_b = paths.releases / 'release-B'; release_b.mkdir(parents=True)
    manifest_b = release_manifest('release-B')
    af.atomic_json(release_b / 'release-manifest.json', manifest_b)
    paths.current.symlink_to(release_b)
    previous = {'release_id': 'release-B', 'status': 'healthy', 'drain': False,
                'components': {'core': 'release-A', 'collector': 'release-A', 'interface': 'release-B'}}
    af.atomic_json(paths.release_state, previous)
    transition = {'operation_id': 'op-C', 'phase': 'fenced', 'target_release_id': 'release-C',
                  'db_authority': 'state', 'previous_state': previous, 'ui_only': False}
    af.atomic_json(paths.transition, transition)
    class Commands:
        def run(self, argv, **kwargs): return subprocess.CompletedProcess(argv, 0, '')
    class Client:
        def call(self, method, payload): return {'drain': True}
    normalized = {name: 'release-B' for name in ('collector', 'core', 'interface')}
    monkeypatch.setattr(job, 'restore_before_switch', lambda *a, **k: normalized)
    with pytest.raises(job.DeployFailure, match='RECOVERED_RESNAPSHOT'):
        job.recover_transition(paths, Commands(), Client, transition)
    assert json.loads(paths.release_state.read_text())['components'] == normalized


def test_inactive_first_target_without_core_meta_is_fenced_by_start_config_and_lock(tmp_path):
    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', tmp_path / 'legacy')
    paths.execution_lock = paths.state / 'core/execution.lock'
    target = paths.releases / 'release-new'; target.mkdir(parents=True)
    manifest = release_manifest('release-new')
    af.atomic_json(target / 'release-manifest.json', manifest)
    paths.current.symlink_to(target)
    (paths.state / 'core').mkdir(parents=True)
    sqlite3.connect(paths.state / 'core/trade.db').close()
    (paths.state / 'secrets').mkdir()
    (paths.state / 'secrets/core.env').write_text(
        'FUNDING_START_DRAINED="1"\nFUNDING_DRAIN_RELEASE_ID="release-new"\n')
    transition = {'operation_id': 'op', 'phase': 'switched', 'target_release_id': 'release-new',
                  'db_authority': 'state', 'previous_state': None, 'ui_only': False}
    af.atomic_json(paths.transition, transition)
    class Commands:
        def run(self, argv, **kwargs): return subprocess.CompletedProcess(argv, 3, '')
    with pytest.raises(job.DeployFailure, match='FENCED_RESNAPSHOT'):
        job.recover_transition(paths, Commands(), None, transition)
    state = json.loads(paths.release_state.read_text())
    assert state['release_id'] == 'release-new' and state['drain'] is True
    assert not paths.transition.exists()


def test_offline_fence_cancels_systemd_restart_backoff_before_install(tmp_path, monkeypatch):
    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', tmp_path / 'legacy')
    events = []
    class Commands:
        def run(self, argv, **kwargs):
            events.append(' '.join(map(str, argv)))
            return subprocess.CompletedProcess(argv, 0, '')
    monkeypatch.setattr(job, '_wait_inactive', lambda *a: events.append('inactive_verified'))
    monkeypatch.setattr(job, 'execution_lock_free', lambda *a: events.append('lock_free'))
    job.cancel_pending_core_restart(paths, Commands())
    assert events == ['systemctl stop funding_bot-core.service', 'inactive_verified', 'lock_free']


def test_partial_rollback_records_actual_old_link_and_retains_transition(tmp_path):
    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', tmp_path / 'legacy')
    old = paths.releases / 'release-old'; old.mkdir(parents=True)
    paths.current.symlink_to(old)
    previous = {'format_version': 1, 'generation': 3, 'release_id': 'release-old',
                'source_revision': '0' * 40, 'source_sha256': 'o' * 64,
                'artifact_sha256': 'b' * 64, 'components': {}, 'ipc_version': 1,
                'dto_version': 1, 'schema_version': 2, 'min_reader': 2}
    af.atomic_json(paths.transition, {'operation_id': 'op'})
    selected = job.record_failed_selection(paths, None, previous, release_manifest('release-new'),
                                           tmp_path / 'failed.json')
    state = json.loads(paths.release_state.read_text())
    assert selected == 'release-old' and state['release_id'] == 'release-old'
    assert state['status'] == 'rollback_incomplete' and paths.transition.exists()


def test_restore_before_switch_restarts_and_verifies_both_readers_before_end(tmp_path):
    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', tmp_path / 'legacy')
    old = paths.releases / 'release-old'; (old / 'deploy/migration').mkdir(parents=True)
    manifest = release_manifest('release-old')
    now = time.time()
    collector = {'ready': True, 'pid': 41, 'boot_id': 'cb', 'updated_at': now,
                 **{k: manifest[k] for k in ('release_id', 'source_sha256', 'artifact_sha256')}}
    interface = {'ready': True, 'pid': 42, 'boot_id': 'ib', 'updated_at': now,
                 **{k: manifest[k] for k in ('release_id', 'source_sha256', 'artifact_sha256')}}
    (paths.state / 'collector/public').mkdir(parents=True)
    (paths.state / 'interface').mkdir(parents=True)
    af.atomic_json(paths.state / 'collector/public/collector_health.json', collector)
    af.atomic_json(paths.state / 'interface/interface_health.json', interface)
    events, state = [], {'ended': False}
    class Client:
        def call(self, method, payload):
            events.append(method)
            if method == 'get_status':
                return {'ready': True, 'release_id': 'release-old',
                        'source_sha256': manifest['source_sha256'], 'artifact_sha256': manifest['artifact_sha256'],
                        'ipc_version': 1, 'schema_version': 2, 'drain': not state['ended'],
                        'drain_epoch': 'epoch', 'recovery_complete': True, 'execution_lock_held': True}
            if method == 'get_drain_state':
                return dict(safe_state('epoch'), release_id='release-target')
            if method == 'end_drain':
                state['ended'] = True; return {'drain': False}
            raise AssertionError(method)
    class Commands:
        def __init__(self): self.inactive = set()
        def run(self, argv, **kwargs):
            events.append(' '.join(map(str, argv)))
            if argv[:2] == ['systemctl', 'stop']:
                self.inactive.add(argv[2]); return subprocess.CompletedProcess(argv, 0, '')
            if argv[:2] == ['systemctl', 'start']:
                self.inactive.discard(argv[2]); return subprocess.CompletedProcess(argv, 0, '')
            if argv[:3] == ['systemctl', 'is-active', '--quiet']:
                return subprocess.CompletedProcess(argv, 3 if argv[3] in self.inactive else 0, '')
            if argv[:3] == ['systemctl', 'show', '-p']:
                return subprocess.CompletedProcess(argv, 0, '41\n' if 'collector' in argv[-1] else '42\n')
            if argv[0] == 'curl': return subprocess.CompletedProcess(argv, 0, json.dumps(collector))
            return subprocess.CompletedProcess(argv, 0, '')
    job.restore_before_switch(paths, Commands(), Client, old, manifest,
                              drain_release_id='release-target')
    assert state['ended'] is True
    assert events.index('systemctl start funding_bot-collector.service') < events.index('end_drain')
    assert events.index('systemctl start funding_bot-interface.service') < events.index('end_drain')


def test_first_preswitch_failure_report_does_not_recreate_state_root(tmp_path):
    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', tmp_path / 'legacy')
    assert job.failure_report_root(paths, None, False) == paths.bootstrap_reports
    assert job.failure_report_root(paths, None, True) == paths.reports


def test_existing_release_revalidates_complete_installed_identity(tmp_path, monkeypatch):
    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', tmp_path / 'legacy')
    release = paths.releases / 'release-existing'; release.mkdir(parents=True)
    manifest = {'release_id': 'release-existing', 'artifact_sha256': 'x'}
    af.atomic_json(release / 'release-manifest.json', manifest)
    checked = []
    monkeypatch.setattr(job, 'verify_installed_release',
                        lambda *args: checked.append((args[0], args[4])))
    result, loaded = job.install_release(paths, tmp_path / 'artifact', {'identity': {}},
                                         {'release_id': 'release-existing'}, object())
    assert result == release and loaded == manifest and checked == [(release, manifest)]


@pytest.mark.skipif(sys.platform != 'linux' or os.geteuid() != 0,
                    reason='isolated Linux root DAC fixture')
def test_shared_pace_dac_allows_two_uids_but_denies_private_controls(tmp_path):
    gid, uid1, uid2 = 61001, 61002, 61003
    os.chmod(tmp_path, 0o755)
    shared = tmp_path / 'shared'; shared.mkdir(mode=0o770)
    pace = shared / 'okxdex.pace'; pace.write_text('0\n')
    private = tmp_path / 'core.env'; private.write_text('TOKEN=fixture\n')
    os.chown(shared, 0, gid); os.chown(pace, 0, gid); os.chmod(pace, 0o660)
    os.chown(private, 0, 0); os.chmod(private, 0o600)
    for uid in (uid1, uid2):
        pid = os.fork()
        if pid == 0:
            try:
                os.setgroups([gid]); os.setgid(gid); os.setuid(uid)
                fd = os.open(pace, os.O_RDWR); os.close(fd)
                try:
                    os.open(private, os.O_RDONLY)
                except PermissionError:
                    os._exit(0)
                os._exit(2)
            except BaseException:
                os._exit(3)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0


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
    monkeypatch.setattr(job, 'end_drain_verified', lambda *a, **k: calls.append('end_verified'))
    class Client:
        def call(self, method, payload):
            if method == 'get_status': return {'drain': True, 'drain_epoch': 'epoch-1', 'release_id': 'new'}
            if method == 'get_drain_state': return dict(safe_state(), release_id='old-release')
            if method == 'end_drain': return {'drain': False}
            raise AssertionError(method)
    class Commands:
        def run(self, argv, **kwargs): calls.append(tuple(map(str, argv))); return subprocess.CompletedProcess(argv, 0, '')
    monkeypatch.setattr(af, 'backup_database', lambda *a, **k: pytest.fail('rollback must not restore/copy a DB'))
    job.rollback_release(paths, Commands(), Client, previous, manifest)
    assert 'switch_code' in calls and 'lock_free' in calls


def test_link_failure_restores_prior_compatible_owner_without_first_cutover_path(tmp_path, monkeypatch):
    paths = job.Paths(tmp_path / 'opt', tmp_path / 'state', tmp_path / 'legacy')
    old = paths.releases / 'old'; old.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(job, 'restore_before_switch', lambda *a, **k: calls.append('restore_old_owner'))
    monkeypatch.setattr(job, 'rollback_release', lambda *a: pytest.fail('link never switched'))
    job.restore_activation_failure(paths, object(), object(), old, {'release_id': 'old'},
                                   ui_update=False, switched=False)
    assert calls == ['restore_old_owner']


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
    assert oct((paths.state / 'shared/trading.busy').stat().st_mode & 0o777) == '0o640'
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
    monkeypatch.setattr(job, 'end_drain_verified', lambda *a, **k: events.append('end_drain'))
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
            args = list(map(str, argv))
            code = 0 if args[:4] == ['systemctl', 'is-active', '--quiet', 'funding_bot-core.service'] else 0
            return subprocess.CompletedProcess(argv, code, '')
    report = job.Job(paths, Commands(), Client).install(tmp_path / 'artifact', tmp_path / 'receipt', expected)
    state = json.loads(paths.release_state.read_text())
    assert report['status'] == 'healthy' and state['release_id'] == 'new-release' and state['generation'] == 8
    assert events.index('safe_old') < events.index('old_stopped') < events.index('backup') < events.index('switch')
    assert events.index('switch') < events.index('end_drain')
