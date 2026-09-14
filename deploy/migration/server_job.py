#!/usr/bin/env python3
"""Single supervised server-side M5 install/rollback job.

The caller starts this file with systemd-run.  Every live mutation is below one
stable flock and after exact expected-base verification.  A dropped SSH session
therefore cannot create a second execution owner or interrupt cutover.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import pwd
import grp
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import artifacts as af  # noqa: E402
from deploy_ipc import DeployClient, IpcRefused, validate_drain  # noqa: E402
from prepare_layout import parse_env, prepare  # noqa: E402

LEGACY_EXCLUDE = {'.git', '.venv', '.pytest_cache', 'runtime', 'logs'}
SERVICES = ('funding_bot-collector.service', 'funding_bot-core.service', 'funding_bot-interface.service')
OLD_SERVICES = ('funding_bot-trader.service', 'funding_bot-web.service')
TUNNEL_SERVICE = 'funding_bot-tunnel.service'


class DeployFailure(RuntimeError):
    pass


class Paths:
    def __init__(self, opt='/opt/funding-bot', state='/var/lib/funding-bot', legacy='/home/admin/hyper/funding_bot'):
        self.opt, self.state, self.legacy = map(Path, (opt, state, legacy))
        self.releases = self.opt / 'releases'
        self.current = self.opt / 'current'
        self.release_state = self.state / 'deploy/release-state.json'
        self.lock = Path('/run/lock/funding-bot-deploy.lock')
        self.reports = self.state / 'deploy/reports'
        self.backups = self.state / 'backups'
        self.execution_lock = self.state / 'core/execution.lock'
        self.core_socket = Path('/run/funding-bot/core.sock')


class Commands:
    def run(self, argv, *, check=True, env=None):
        return subprocess.run(list(map(str, argv)), check=check, text=True, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _json(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise DeployFailure(f'object required: {path}')
    return value


def _source_sha(root):
    manifest = af.tree_manifest(root, excluded=LEGACY_EXCLUDE)
    return manifest, af.value_digest(manifest)


def database_info(path):
    """Read the authoritative DB without allowing sqlite to create a typo path."""
    path = Path(path).resolve(strict=True)
    con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
    try:
        if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise DeployFailure('trade.db quick_check failed')
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'deals', 'intents', 'flags'} <= tables:
            raise DeployFailure('trade.db missing required tables')
        schema = min_reader = 1
        if 'schema_version' in tables:
            row = con.execute('SELECT version,min_reader FROM schema_version WHERE id=1').fetchone()
            if not row:
                raise DeployFailure('schema version row missing')
            schema, min_reader = map(int, row)
        offset = 0
        row = con.execute("SELECT v FROM flags WHERE k='tg_offset'").fetchone()
        if row:
            offset = int(row[0])
        return {'schema_version': schema, 'min_reader': min_reader, 'tg_offset': offset}
    finally:
        con.close()


def state_identity(state):
    keys = ('format_version', 'generation', 'release_id', 'source_revision', 'source_sha256',
            'artifact_sha256', 'components', 'ipc_version', 'dto_version', 'schema_version', 'min_reader')
    if not all(k in state for k in keys):
        raise DeployFailure('release-state.json incomplete')
    return {k: state[k] for k in keys}


def legacy_identity(paths):
    manifest, source_sha = _source_sha(paths.legacy)
    db = database_info(paths.legacy / 'runtime/trade.db')
    return {'kind': 'legacy', 'source_sha256': source_sha, 'sources': manifest,
            'schema_version': db['schema_version'], 'min_reader': db['min_reader']}


def current_identity(paths):
    if paths.release_state.exists():
        return state_identity(_json(paths.release_state))
    return legacy_identity(paths)


def read_template(artifact):
    with tarfile.open(artifact, 'r') as tf:
        try:
            member = tf.getmember('release-manifest.template.json')
        except KeyError as e:
            raise DeployFailure('artifact manifest missing') from e
        if member.size > 16 * 1024 * 1024 or not member.isfile():
            raise DeployFailure('invalid artifact manifest')
        return json.loads(tf.extractfile(member).read())


def verify_bundle(artifact, receipt_path):
    receipt, template = _json(receipt_path), read_template(artifact)
    required = {'release_id', 'source_revision', 'source_sha256', 'sources', 'dependencies', 'runtime',
                'test_profile', 'component_hashes', 'ipc_version', 'dto_version', 'schema_version', 'min_reader'}
    if not required <= set(template):
        raise DeployFailure('release manifest incomplete')
    if af.value_digest(template['sources']) != template['source_sha256']:
        raise DeployFailure('source manifest fingerprint mismatch')
    expected = af.verification_identity(artifact=artifact, sources=template['sources'],
                                        dependencies=template['dependencies'], runtime=template['runtime'],
                                        profile=template['test_profile'])
    af.require_verified(receipt, expected)
    for key in ('release_id', 'source_revision', 'source_sha256', 'patchnote'):
        if receipt.get(key) != template.get(key):
            raise DeployFailure(f'receipt {key} mismatch')
    own = template['sources'].get('deploy/migration/server_job.py')
    if own != af.digest(__file__):
        raise DeployFailure('server runner differs from verified artifact')
    return receipt, template


def safe_extract(artifact, destination):
    destination = Path(destination).resolve()
    with tarfile.open(artifact, 'r') as tf:
        for m in tf.getmembers():
            p = Path(m.name)
            if p.is_absolute() or '..' in p.parts or m.issym() or m.islnk() or not (m.isfile() or m.isdir()):
                raise DeployFailure('unsafe artifact member')
        tf.extractall(destination, filter='data')


def exact_source_check(release, manifest):
    actual = {}
    for name in manifest:
        path = release / name
        if not path.is_file() or path.is_symlink():
            raise DeployFailure(f'installed source missing: {name}')
        actual[name] = af.digest(path)
    if actual != manifest:
        raise DeployFailure('installed source bytes differ')


def freeze_release(root):
    for path in sorted(Path(root).rglob('*'), reverse=True):
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        os.chmod(path, 0o555 if path.is_dir() or mode & 0o111 else 0o444)
        os.chown(path, 0, 0)
    os.chmod(root, 0o555); os.chown(root, 0, 0)


def compatible_reader(manifest, db):
    readers = manifest.get('compatible_readers')
    return isinstance(readers, list) and db['min_reader'] in readers and manifest.get('schema_version', 0) >= db['min_reader']


def ui_only(previous, new):
    if not previous:
        return False
    return (previous.get('component_hashes', {}).get('core') == new['component_hashes']['core']
            and previous.get('component_hashes', {}).get('collector') == new['component_hashes']['collector']
            and previous.get('dependencies') == new['dependencies']
            and all(previous.get(k) == new.get(k) for k in ('ipc_version', 'dto_version', 'schema_version', 'min_reader')))


def _wait_inactive(commands, service):
    # No timeout: systemd has TimeoutStopSec=infinity in the runtime fence.  An
    # unresolved executor is never killed merely to make deploy progress.
    while commands.run(['systemctl', 'is-active', '--quiet', service], check=False).returncode == 0:
        time.sleep(2)


def fence_legacy(commands):
    drop = Path('/run/systemd/system/funding_bot-trader.service.d/m5-fence.conf')
    drop.parent.mkdir(parents=True, exist_ok=True)
    drop.write_text('[Service]\nRestart=no\nTimeoutStopSec=infinity\n')
    commands.run(['systemctl', 'daemon-reload'])
    commands.run(['systemctl', 'disable', 'funding_bot-trader.service'], check=False)
    commands.run(['systemctl', 'stop', 'funding_bot-trader.service'])
    _wait_inactive(commands, 'funding_bot-trader.service')


def execution_lock_free(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployFailure('execution lock still owned') from None
    finally:
        os.close(fd)


def wait_drain(client, release_id, epoch, *, sleep=time.sleep):
    while True:
        state = validate_drain(client.call('get_drain_state', {'drain_epoch': epoch}),
                               release_id=release_id, epoch=epoch)
        if state.get('safe_to_switch') is True:
            return validate_drain(state, release_id=release_id, epoch=epoch, require_safe=True)
        # UNKNOWN and an in-flight hedge stay under the old executor.  There is
        # deliberately no deadline or kill here; the supervised job keeps lock.
        sleep(2)


def provision_accounts(commands):
    for group in ('funding-ipc', 'funding-market', 'funding-pace'):
        commands.run(['groupadd', '--system', '--force', group])
    for user in ('funding-core', 'funding-collector', 'funding-interface'):
        commands.run(['useradd', '--system', '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', user], check=False)
    commands.run(['usermod', '-a', '-G', 'funding-ipc,funding-market,funding-pace', 'funding-core'])
    commands.run(['usermod', '-a', '-G', 'funding-market,funding-pace', 'funding-collector'])
    commands.run(['usermod', '-a', '-G', 'funding-ipc,funding-market', 'funding-interface'])


def _copy_private(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as out, Path(src).open('rb') as inp:
        shutil.copyfileobj(inp, out)


def migrate_legacy_runtime(paths, commands, release_id):
    """Run only after legacy trader is fully stopped and fenced."""
    legacy_runtime = paths.legacy / 'runtime'
    info = database_info(legacy_runtime / 'trade.db')
    if paths.state.exists():
        raise DeployFailure('new state root exists before first migration')
    uid = pwd.getpwnam('funding-interface').pw_uid
    stage_state = paths.state.with_name('.' + paths.state.name + '-staging-' + uuid.uuid4().hex)
    try:
        prepare(stage_state, paths.legacy / '.env',
                legacy_runtime / 'cabinet.env' if (legacy_runtime / 'cabinet.env').exists() else None,
                final_root=str(paths.state), interface_uid=uid, deploy_uid=0)
        # Keep all non-collector runtime files private to core so owner.toml keypair
        # paths and durable journals retain their relative names.
        collector_names = {'funding_bot.db', 'table.json', 'collector_health.json'}
        shared_names = {'okxdex.pace', 'trading.busy'}
        skip = collector_names | shared_names | {'trade.db', 'trade.db-wal', 'trade.db-shm', 'cabinet.env', 'public_url.txt'}
        for src in sorted(legacy_runtime.rglob('*')):
            if not src.is_file() or src.name in skip or src.is_symlink():
                continue
            _copy_private(src, stage_state / 'core' / src.relative_to(legacy_runtime))
        for name in shared_names:
            if (legacy_runtime / name).is_file():
                _copy_private(legacy_runtime / name, stage_state / 'shared' / name)
        # Keypair file locations are secrets carried by env indirection. Copy the
        # bytes privately and rewrite only their path, never their value/content.
        keyfiles = {}
        for key, line in parse_env(paths.legacy / '.env').items():
            if not key.endswith('KEYPAIR_FILE'):
                continue
            raw = line.split('=', 1)[1].strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
                raw = raw[1:-1]
            source = Path(raw)
            if not source.is_absolute() or source.is_symlink() or not source.is_file():
                raise DeployFailure('configured keypair file missing/unsafe (path omitted)')
            installed_path = paths.state / 'core/keys' / (key.lower() + '.json')
            _copy_private(source, stage_state / 'core/keys' / installed_path.name)
            keyfiles[key] = installed_path
        if keyfiles:
            core_env = stage_state / 'secrets/core.env'
            lines = []
            for line in core_env.read_text().splitlines():
                key = line.split('=', 1)[0]
                lines.append(f'{key}="{keyfiles[key]}"' if key in keyfiles else line)
            core_env.write_text('\n'.join(lines) + '\n')
            core_env.chmod(0o600)
        stage_backups = stage_state / 'backups'
        stage_backups.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        backup_name = f'{stamp}-{release_id}-trade.db'
        af.backup_database(legacy_runtime / 'trade.db', stage_backups / backup_name)
        af.backup_database(legacy_runtime / 'trade.db', stage_state / 'core/trade.db')
        if (legacy_runtime / 'funding_bot.db').exists():
            af.backup_database(legacy_runtime / 'funding_bot.db', stage_state / 'collector/funding_bot.db')
        for name in ('table.json', 'collector_health.json'):
            if (legacy_runtime / name).is_file():
                _copy_private(legacy_runtime / name, stage_state / 'collector/public' / name)
        af.atomic_json(stage_state / 'interface/state.json', {'offset': info['tg_offset'], 'acks': {}})
        if (legacy_runtime / 'public_url.txt').is_file():
            _copy_private(legacy_runtime / 'public_url.txt', stage_state / 'interface/public_url.txt')
        # First core must be drained before it can expose IPC or execute recovery.
        core_env = stage_state / 'secrets/core.env'
        with core_env.open('a') as f:
            f.write(f'FUNDING_START_DRAINED="1"\nFUNDING_DRAIN_RELEASE_ID="{release_id}"\n')
        commands.run(['chown', '-R', 'funding-core:funding-core', stage_state / 'core'])
        commands.run(['chown', '-R', 'funding-collector:funding-market', stage_state / 'collector'])
        commands.run(['chown', '-R', 'funding-interface:funding-interface', stage_state / 'interface'])
        commands.run(['chown', '-R', 'root:root', stage_state / 'secrets'])
        commands.run(['chown', '-R', 'root:funding-pace', stage_state / 'shared'])
        os.chmod(stage_state, 0o755)
        os.chmod(stage_state / 'core', 0o700)
        os.chmod(stage_state / 'collector', 0o710)
        os.chmod(stage_state / 'collector/public', 0o2750)
        os.chmod(stage_state / 'interface', 0o700)
        os.chmod(stage_state / 'shared', 0o2770)
        os.replace(stage_state, paths.state)
        return info, paths.backups / backup_name
    except BaseException:
        shutil.rmtree(stage_state, ignore_errors=True)
        raise


def clear_first_start_drain(path):
    path = Path(path)
    keep = [line for line in path.read_text().splitlines()
            if not line.startswith(('FUNDING_START_DRAINED=', 'FUNDING_DRAIN_RELEASE_ID='))]
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write('\n'.join(keep) + '\n'); f.flush(); os.fsync(f.fileno())
        os.chmod(tmp, 0o600); os.chown(tmp, 0, 0)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def install_release(paths, artifact, receipt, template, commands):
    paths.releases.mkdir(parents=True, exist_ok=True)
    final = paths.releases / template['release_id']
    if final.exists():
        existing = _json(final / 'release-manifest.json')
        if existing.get('artifact_sha256') != af.digest(artifact):
            raise DeployFailure('release id already has different bytes')
        return final, existing
    stage = paths.releases / ('.staging-' + template['release_id'] + '-' + uuid.uuid4().hex)
    stage.mkdir()
    try:
        safe_extract(artifact, stage)
        exact_source_check(stage, template['sources'])
        dependency_bytes = af.dependency_fingerprint(stage / 'deploy/requirements.lock', stage / 'wheelhouse')
        for key in ('lock_sha256', 'wheels', 'wheelset_sha256'):
            if dependency_bytes[key] != template['dependencies'].get(key):
                raise DeployFailure('installed dependency bytes differ')
        # The server install has no network and consumes the exact verified wheel set.
        commands.run([sys.executable, '-m', 'venv', stage / '.venv'])
        py = stage / '.venv/bin/python'
        commands.run([py, '-m', 'pip', 'install', '--no-index', '--find-links', stage / 'wheelhouse',
                      '--no-deps', '-r', stage / 'deploy/requirements.lock'])
        commands.run([py, '-m', 'pip', 'check'])
        commands.run([py, '-c', 'import funding_bot,pytest,sqlite3'],
                     env={**os.environ, 'PYTHONPATH': str(stage / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'})
        runtime_code = ("import json,platform,sqlite3,sys,hashlib; p=sys.executable; "
                        "print(json.dumps({'python':list(sys.version_info[:3]),'implementation':platform.python_implementation(),"
                        "'platform':sys.platform,'machine':platform.machine(),'libc':list(platform.libc_ver()),"
                        "'sqlite':sqlite3.sqlite_version,'executable_hash':hashlib.sha256(open(p,'rb').read()).hexdigest()},sort_keys=True))")
        runtime = json.loads(commands.run([py, '-c', runtime_code]).stdout)
        if runtime != template['runtime']:
            raise DeployFailure('server Linux runtime differs from verified runtime')
        packages = sorted(x.strip() for x in commands.run([py, '-m', 'pip', 'freeze', '--all']).stdout.splitlines() if x.strip())
        installed = {'packages': packages, 'sha256': af.value_digest(packages)}
        if installed != template['dependencies'].get('installed'):
            raise DeployFailure('server installed dependencies differ from verified environment')
        manifest = dict(template, artifact_sha256=af.digest(artifact),
                        verification_identity_sha256=af.value_digest(receipt['identity']))
        af.atomic_json(stage / 'release-manifest.json', manifest)
        freeze_release(stage)
        os.replace(stage, final)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return final, manifest


def migration_dry_run(release, database, commands):
    """Run additive migrations on a disposable SQLite backup before live core."""
    scratch = Path(str(database) + '.migration-check-' + uuid.uuid4().hex)
    af.backup_database(database, scratch)
    try:
        code = ('import sqlite3,sys; from funding_bot.trade import store; '
                'c=store.connect(sys.argv[1]); i=store.schema_info(c); '
                'assert i and i["version"]>=store.SCHEMA_VERSION; '
                'assert c.execute("PRAGMA quick_check").fetchone()[0]=="ok"; c.close()')
        commands.run([release / '.venv/bin/python', '-c', code, scratch],
                     env={**os.environ, 'PYTHONPATH': str(release / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'})
    finally:
        scratch.unlink(missing_ok=True)


def install_units(release, commands):
    for name in SERVICES:
        commands.run(['install', '-o', 'root', '-g', 'root', '-m', '0644',
                      release / 'deploy/migration' / name, Path('/etc/systemd/system') / name])
    commands.run(['install', '-o', 'root', '-g', 'root', '-m', '0644',
                  release / 'deploy/migration' / TUNNEL_SERVICE,
                  Path('/etc/systemd/system') / TUNNEL_SERVICE])
    commands.run(['systemctl', 'daemon-reload'])
    commands.run(['systemctl', 'enable', *SERVICES])
    commands.run(['systemctl', 'enable', TUNNEL_SERVICE])


def switch_link(paths, release):
    tmp = paths.opt / ('.current-' + uuid.uuid4().hex)
    os.symlink(release, tmp)
    os.replace(tmp, paths.current)


def health_matches(value, manifest, *, require_drain=True):
    if not isinstance(value, dict) or value.get('ready') is not True:
        raise DeployFailure('core readiness false')
    if value.get('release_id') != manifest['release_id'] or value.get('source_sha256') != manifest['source_sha256']:
        raise DeployFailure('core loaded unexpected release')
    if value.get('artifact_sha256') != manifest['artifact_sha256'] or value.get('ipc_version') != manifest['ipc_version']:
        raise DeployFailure('core build identity mismatch')
    if value.get('schema_version') != manifest['schema_version']:
        raise DeployFailure('core schema version mismatch')
    if require_drain and (value.get('drain') is not True or not value.get('drain_epoch')):
        raise DeployFailure('new core did not retain drain')
    if value.get('recovery_complete') is not True:
        raise DeployFailure('new core recovery incomplete')
    if value.get('execution_lock_held') is not True:
        raise DeployFailure('new core execution ownership missing')
    return value


def wait_core_ready(client, manifest, *, sleep=time.sleep):
    while True:
        try:
            return health_matches(client.call('get_status', {}), manifest)
        except (IpcRefused, DeployFailure):
            sleep(2)


def _service_pid(commands, name):
    active = commands.run(['systemctl', 'is-active', '--quiet', name], check=False)
    if active.returncode != 0:
        raise DeployFailure(f'{name} inactive')
    value = commands.run(['systemctl', 'show', '-p', 'MainPID', '--value', name]).stdout.strip()
    if not value.isdigit() or int(value) <= 0:
        raise DeployFailure(f'{name} PID missing')
    return int(value)


def _health_identity(value, manifest, pid, component):
    if not isinstance(value, dict) or value.get('ready') is not True or value.get('pid') != pid:
        raise DeployFailure(f'{component} readiness/PID mismatch')
    for key in ('release_id', 'source_sha256', 'artifact_sha256'):
        if value.get(key) != manifest[key]:
            raise DeployFailure(f'{component} {key} mismatch')
    if not isinstance(value.get('boot_id'), str) or not value['boot_id']:
        raise DeployFailure(f'{component} boot identity missing')
    updated = value.get('updated_at')
    if not isinstance(updated, (int, float)) or not 0 <= time.time() - updated < 120:
        raise DeployFailure(f'{component} health stale')


def wait_components(paths, commands, manifest, *, timeout=180, sleep=time.sleep):
    deadline = time.monotonic() + timeout
    last = 'not checked'
    while time.monotonic() < deadline:
        try:
            cp = _service_pid(commands, 'funding_bot-collector.service')
            ip = _service_pid(commands, 'funding_bot-interface.service')
            body = (paths.state / 'collector/public/collector_health.json').read_bytes()
            if len(body) > 16 * 1024:
                raise DeployFailure('collector health exceeds 16 KiB')
            collector = json.loads(body)
            response = commands.run(['curl', '-fsS', '--max-time', '5', 'http://127.0.0.1:8792/status']).stdout.encode()
            if len(response) > 16 * 1024:
                raise DeployFailure('interface status exceeds 16 KiB')
            interface = json.loads(response)
            _health_identity(collector, manifest, cp, 'collector')
            _health_identity(interface, manifest, ip, 'interface')
            return {'collector': collector, 'interface': interface}
        except (OSError, ValueError, json.JSONDecodeError, DeployFailure, subprocess.SubprocessError) as e:
            last = str(e)
            sleep(2)
    raise DeployFailure('component readiness timeout: ' + last)


def resume_legacy(paths, commands):
    for name in ('funding_bot-collector.service', 'funding_bot-web.service',
                 'funding_bot-trader.service', 'funding_bot-tunnel.service'):
        source = paths.legacy / 'deploy' / name
        if source.is_file():
            commands.run(['install', '-o', 'root', '-g', 'root', '-m', '0644', source,
                          Path('/etc/systemd/system') / name])
    Path('/run/systemd/system/funding_bot-trader.service.d/m5-fence.conf').unlink(missing_ok=True)
    commands.run(['systemctl', 'daemon-reload'])
    commands.run(['systemctl', 'enable', *OLD_SERVICES])
    commands.run(['systemctl', 'start', 'funding_bot-web.service'])
    commands.run(['systemctl', 'start', 'funding_bot-trader.service'])


def restore_before_switch(paths, commands, client_factory, previous_release, previous_manifest):
    install_units(previous_release, commands)
    commands.run(['systemctl', 'start', 'funding_bot-core.service'])
    health = wait_core_ready(client_factory(), previous_manifest)
    epoch = health['drain_epoch']
    validate_drain(client_factory().call('get_drain_state', {'drain_epoch': epoch}),
                   release_id=previous_manifest['release_id'], epoch=epoch, require_safe=True)
    ended = client_factory().call('end_drain', {'drain_epoch': epoch,
                                  'expected_release_id': previous_manifest['release_id']})
    if ended.get('drain') is not False:
        raise DeployFailure('old core did not leave pre-switch drain')


def rollback_release(paths, commands, client_factory, previous_release, previous_manifest):
    """Change code only. The current /var/lib trade.db is never replaced."""
    db = database_info(paths.state / 'core/trade.db')
    if not compatible_reader(previous_manifest, db):
        raise DeployFailure('ROLLBACK_READER_INCOMPATIBLE')
    client = client_factory()
    health = client.call('get_status', {})
    if health.get('drain') is True and health.get('drain_epoch'):
        epoch = health['drain_epoch']
    else:
        begun = validate_drain(client.call('begin_drain', {
            'release_id': previous_manifest['release_id'],
            'expected_state_revision': health['state_revision'], 'reason': 'rollback',
        }), release_id=previous_manifest['release_id'])
        epoch = begun['drain_epoch']
    wait_drain(client, health.get('release_id') or previous_manifest['release_id'], epoch)
    commands.run(['systemctl', 'stop', 'funding_bot-core.service'])
    _wait_inactive(commands, 'funding_bot-core.service')
    execution_lock_free(paths.execution_lock)
    install_units(previous_release, commands)
    switch_link(paths, previous_release)
    commands.run(['systemctl', 'start', 'funding_bot-collector.service'])
    commands.run(['systemctl', 'start', 'funding_bot-core.service'])
    old_health = wait_core_ready(client_factory(), previous_manifest)
    old_epoch = old_health['drain_epoch']
    validate_drain(client_factory().call('get_drain_state', {'drain_epoch': old_epoch}),
                   release_id=previous_manifest['release_id'], epoch=old_epoch, require_safe=True)
    commands.run(['systemctl', 'start', 'funding_bot-interface.service'])
    wait_components(paths, commands, previous_manifest)
    ended = client_factory().call('end_drain', {'drain_epoch': old_epoch,
                                  'expected_release_id': previous_manifest['release_id']})
    if ended.get('drain') is not False:
        raise DeployFailure('rollback core did not leave drain')


def release_state(previous, manifest, *, components, status, drain, report):
    return {
        'format_version': 1, 'generation': int((previous or {}).get('generation', 0)) + 1,
        'release_id': manifest['release_id'], 'source_revision': manifest['source_revision'],
        'source_sha256': manifest['source_sha256'], 'artifact_sha256': manifest['artifact_sha256'],
        'verification_identity_sha256': manifest['verification_identity_sha256'],
        'component_hashes': manifest['component_hashes'], 'dependencies': manifest['dependencies'],
        'components': components, 'ipc_version': manifest['ipc_version'], 'dto_version': manifest['dto_version'],
        'schema_version': manifest['schema_version'], 'min_reader': manifest['min_reader'],
        'active_epoch': uuid.uuid4().hex, 'status': status, 'drain': drain,
        'installed_at': time.time(), 'report': str(report),
    }


class Job:
    def __init__(self, paths=None, commands=None, client_factory=None):
        self.paths = paths or Paths()
        self.commands = commands or Commands()
        self.client_factory = client_factory or (lambda: DeployClient(self.paths.core_socket))

    def install(self, artifact, receipt_path, expected_base):
        p = self.paths
        receipt, template = verify_bundle(artifact, receipt_path)
        expected = _json(expected_base)
        report = {'release_id': template['release_id'], 'started_at': time.time(), 'stages': [], 'status': 'running'}
        p.lock.parent.mkdir(parents=True, exist_ok=True)
        with af.deploy_lock(p.lock):
            # Exact STALE_BASE is the first operation under lock.  No release,
            # state, service, DB, credential or unit mutation precedes it.
            af.require_base(current_identity(p), expected)
            report['stages'].append('base_verified')
            previous_state = _json(p.release_state) if p.release_state.exists() else None
            previous_manifest = None
            old_release = None
            if previous_state:
                old_release = p.releases / previous_state['release_id']
                previous_manifest = _json(old_release / 'release-manifest.json')
            is_ui = ui_only(previous_manifest, template)
            db_path = (p.state / 'core/trade.db') if previous_state else (p.legacy / 'runtime/trade.db')
            db_before = database_info(db_path)
            if not compatible_reader(template, db_before):
                raise DeployFailure('new release cannot read current trade.db')

            old_epoch = None
            if previous_state and not is_ui:
                client = self.client_factory()
                begun = validate_drain(client.call('begin_drain', {'release_id': template['release_id'],
                                       'expected_state_revision': client.call('get_status', {})['state_revision'],
                                       'reason': 'deploy'}), release_id=template['release_id'])
                old_epoch = begun['drain_epoch']
                wait_drain(client, template['release_id'], old_epoch)
                report['stages'].append('drained')
                self.commands.run(['systemctl', 'stop', 'funding_bot-core.service'])
                _wait_inactive(self.commands, 'funding_bot-core.service')
            elif not previous_state:
                fence_legacy(self.commands)
                report['stages'].append('legacy_fenced')

            try:
                provision_accounts(self.commands)
                if not previous_state:
                    db_before, backup = migrate_legacy_runtime(p, self.commands, template['release_id'])
                    report['backup'] = {'path': str(backup), 'sha256': af.digest(backup)}
                else:
                    p.backups.mkdir(parents=True, exist_ok=True)
                    backup = p.backups / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + template['release_id'] + '-trade.db')
                    report['backup'] = {'path': str(backup), 'sha256': af.backup_database(db_path, backup)}
                execution_lock_free(p.execution_lock)
                self.commands.run(['chown', 'funding-core:funding-core', p.execution_lock])
                os.chmod(p.execution_lock, 0o600)
                report['stages'].append('execution_released')
                release, manifest = install_release(p, artifact, receipt, template, self.commands)
                migration_dry_run(release, p.state / 'core/trade.db', self.commands)
                report['stages'].append('artifact_installed')
                install_units(release, self.commands)
            except BaseException as pre_switch_failure:
                report['status'] = 'failed_pre_switch'
                report['failure'] = type(pre_switch_failure).__name__ + ': ' + str(pre_switch_failure)
                try:
                    if previous_manifest is None:
                        resume_legacy(p, self.commands)
                    else:
                        restore_before_switch(p, self.commands, self.client_factory, old_release, previous_manifest)
                    report['rollback'] = 'pre_switch_owner_restored'
                except BaseException as restore_failure:
                    report['rollback'] = 'pre_switch_restore_failed: ' + type(restore_failure).__name__ + ': ' + str(restore_failure)
                p.reports.mkdir(parents=True, exist_ok=True)
                failed_path = p.reports / (template['release_id'] + '-failed.json')
                report['completed_at'] = time.time(); af.atomic_json(failed_path, report)
                raise
            switched = False
            try:
                if is_ui:
                    self.commands.run(['systemctl', 'stop', 'funding_bot-interface.service'])
                    switch_link(p, release); switched = True
                    self.commands.run(['systemctl', 'start', 'funding_bot-interface.service'])
                    # UI identity is checked without touching the live core.
                    ip = _service_pid(self.commands, 'funding_bot-interface.service')
                    response = self.commands.run(['curl', '-fsS', '--max-time', '5', 'http://127.0.0.1:8792/status']).stdout.encode()
                    if len(response) > 16 * 1024:
                        raise DeployFailure('interface status exceeds 16 KiB')
                    _health_identity(json.loads(response), manifest, ip, 'interface')
                    components = dict(previous_state['components'], interface=manifest['release_id'])
                    drain = previous_state.get('drain', False)
                    report['stages'].append('ui_only_switched')
                else:
                    self.commands.run(['systemctl', 'stop', 'funding_bot-interface.service'], check=False)
                    self.commands.run(['systemctl', 'stop', 'funding_bot-web.service'], check=False)
                    self.commands.run(['systemctl', 'disable', *OLD_SERVICES], check=False)
                    switch_link(p, release); switched = True
                    self.commands.run(['systemctl', 'start', 'funding_bot-collector.service'])
                    self.commands.run(['systemctl', 'start', 'funding_bot-core.service'])
                    health = wait_core_ready(self.client_factory(), manifest)
                    epoch = health['drain_epoch']
                    validate_drain(self.client_factory().call('get_drain_state', {'drain_epoch': epoch}),
                                   release_id=template['release_id'], epoch=epoch, require_safe=True)
                    self.commands.run(['systemctl', 'start', 'funding_bot-interface.service'])
                    report['health'] = wait_components(p, self.commands, manifest)
                    if previous_state is None:
                        clear_first_start_drain(p.state / 'secrets/core.env')
                    ended = self.client_factory().call('end_drain', {'drain_epoch': epoch,
                                                      'expected_release_id': template['release_id']})
                    if ended.get('drain') is not False:
                        raise DeployFailure('new core did not leave drain')
                    components = {name: manifest['release_id'] for name in ('collector', 'core', 'interface')}
                    drain = False
                    report['stages'].append('three_process_ready')
            except BaseException as failure:
                report['status'] = 'failed'; report['failure'] = type(failure).__name__ + ': ' + str(failure)
                rollback_ok = False
                if switched and previous_manifest is not None:
                    try:
                        if is_ui:
                            switch_link(p, old_release)
                            self.commands.run(['systemctl', 'start', 'funding_bot-interface.service'])
                        else:
                            rollback_release(p, self.commands, self.client_factory, old_release, previous_manifest)
                        report['rollback'] = 'code_only_succeeded'
                        rollback_ok = True
                    except BaseException as rollback_failure:
                        report['rollback'] = 'refused_or_failed: ' + type(rollback_failure).__name__ + ': ' + str(rollback_failure)
                elif not switched and previous_manifest is None:
                    resume_legacy(p, self.commands)
                    report['rollback'] = 'legacy_resumed_before_switch'
                else:
                    # First cutover has no execution-lock-compatible old core.
                    # Keep the new recovery-capable code drained; never copy the
                    # frozen legacy DB back over current state.
                    report['rollback'] = 'first_transition_requires_compatible_fix; current_DB_retained'
                p.reports.mkdir(parents=True, exist_ok=True)
                failed_path = p.reports / (template['release_id'] + '-failed.json')
                report['completed_at'] = time.time(); af.atomic_json(failed_path, report)
                if switched and not rollback_ok:
                    failed_components = {name: manifest['release_id'] for name in ('collector', 'core', 'interface')}
                    af.atomic_json(p.release_state, release_state(previous_state, manifest,
                                   components=failed_components, status='drained_failure', drain=True,
                                   report=failed_path))
                raise

            report['status'] = 'healthy'; report['completed_at'] = time.time()
            p.reports.mkdir(parents=True, exist_ok=True)
            report_path = p.reports / (template['release_id'] + '.json')
            af.atomic_json(report_path, report)
            af.atomic_json(p.release_state, release_state(previous_state, manifest, components=components,
                                                          status='healthy', drain=drain, report=report_path))
            return report


def inspect(paths):
    return current_identity(paths)


def main(argv=None):
    if os.environ.get('FUNDING_M5_ENTRY') != 'deploy/deploy.sh':
        raise SystemExit('enter M5 only through deploy/deploy.sh')
    ap = argparse.ArgumentParser()
    ap.add_argument('--opt-root', default='/opt/funding-bot'); ap.add_argument('--state-root', default='/var/lib/funding-bot')
    ap.add_argument('--legacy-root', default='/home/admin/hyper/funding_bot')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('inspect-base')
    ins = sub.add_parser('install'); ins.add_argument('--artifact', required=True); ins.add_argument('--receipt', required=True)
    ins.add_argument('--expected-base', required=True)
    a = ap.parse_args(argv)
    paths = Paths(a.opt_root, a.state_root, a.legacy_root)
    if a.cmd == 'inspect-base':
        print(af.canonical(inspect(paths)))
        return
    report = Job(paths).install(a.artifact, a.receipt, a.expected_base)
    print(af.canonical(report))


if __name__ == '__main__':
    main()
