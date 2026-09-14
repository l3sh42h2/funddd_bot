"""M5 artifact verification primitives (not a live installer).

A receipt is valid only for the exact tested bytes and runtime/profile. These
helpers intentionally cannot start services, run migrations or declare a release
healthy. Receipts/state must be owned by the deploy account, not the UI/core.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
from contextlib import contextmanager

HASH_LEN = 64


class Refused(RuntimeError):
    pass


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def value_digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def require_sha256(value, label='sha256'):
    if not isinstance(value, str) or len(value) != HASH_LEN or any(c not in '0123456789abcdef' for c in value):
        raise Refused(f'invalid {label}')
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def runtime_fingerprint():
    return dict(python=list(sys.version_info[:3]), implementation=platform.python_implementation(),
                platform=sys.platform, machine=platform.machine(), libc=list(platform.libc_ver()),
                sqlite=sqlite3.sqlite_version, executable_hash=digest(sys.executable))


def source_manifest(root, paths):
    root = Path(root).resolve(strict=True)
    out = {}
    for name in sorted(paths):
        p = Path(name)
        if p.is_absolute() or '..' in p.parts or str(p) != name or name in out:
            raise Refused('invalid/duplicate source path')
        target = root / p
        if any((root / Path(*p.parts[:i])).is_symlink() for i in range(1, len(p.parts) + 1)):
            raise Refused('source symlinks are not supported')
        if not target.is_file():
            raise Refused('source file missing')
        out[name] = digest(target)
    if not out:
        raise Refused('empty source manifest')
    return out


def tree_manifest(root, *, excluded=(), excluded_components=()):
    """Hash every regular file below root; refuse links and special files.

    ``excluded`` contains top-level names.  It is intended for non-source state
    such as .git/.venv/runtime, never for selecting a convenient source subset.
    """
    root = Path(root).resolve(strict=True)
    skip = set(excluded)
    names = []
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root)
        if rel.parts and (rel.parts[0] in skip or set(rel.parts) & set(excluded_components)):
            continue
        if p.is_symlink():
            raise Refused(f'source symlink: {rel}')
        if p.is_dir():
            continue
        if not p.is_file():
            raise Refused(f'non-regular source path: {rel}')
        names.append(rel.as_posix())
    return source_manifest(root, names)


def dependency_fingerprint(lockfile, wheelhouse):
    wheelhouse = Path(wheelhouse).resolve(strict=True)
    wheels = tree_manifest(wheelhouse)
    if not wheels or any(not name.endswith('.whl') for name in wheels):
        raise Refused('wheelhouse must contain wheels only')
    return {'lock_sha256': digest(lockfile), 'wheels': wheels,
            'wheelset_sha256': value_digest(wheels)}


def profile_fingerprint(profile, root):
    """Bind the test argv/config plus every named test or fixture byte."""
    if not isinstance(profile, dict) or type(profile.get('version')) is not int:
        raise Refused('invalid test profile')
    argv, paths = profile.get('argv'), profile.get('paths')
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
        raise Refused('invalid test argv')
    if not isinstance(paths, list) or not paths:
        raise Refused('test profile paths required')
    files = []
    base = Path(root).resolve(strict=True)
    for name in paths:
        p = Path(name)
        if p.is_absolute() or '..' in p.parts or str(p) != name:
            raise Refused('invalid test profile path')
        target = base / p
        if target.is_dir():
            files.extend(x.relative_to(base).as_posix() for x in sorted(target.rglob('*')) if x.is_file())
        elif target.is_file():
            files.append(name)
        else:
            raise Refused('test profile path missing')
    manifest = source_manifest(base, sorted(set(files)))
    body = json.loads(canonical(profile))
    return {'definition': body, 'definition_sha256': value_digest(body),
            'inputs': manifest, 'inputs_sha256': value_digest(manifest)}


def verification_identity(*, artifact, sources, dependencies, runtime, profile):
    if not sources or not dependencies or not runtime or not profile:
        raise Refused('incomplete verification identity')
    return dict(version=1, artifact_sha256=digest(artifact), sources=sources,
                dependencies=dependencies, runtime=runtime, profile=profile)


def verified_receipt(identity, *, exit_code, passed, failed, evidence_sha256):
    if (type(exit_code) is not int or exit_code != 0 or type(passed) is not int or passed <= 0
            or type(failed) is not int or failed != 0):
        raise Refused('test profile did not pass')
    require_sha256(evidence_sha256, 'test output fingerprint')
    return dict(identity=json.loads(canonical(identity)), passed=passed, failed=0,
                exit_code=0, evidence_sha256=evidence_sha256)


def require_verified(receipt, expected):
    if not isinstance(receipt, dict):
        raise Refused('verification receipt missing')
    if receipt.get('identity') != expected:
        raise Refused('VERIFICATION_MISMATCH')
    # Validate evidence/result as strictly when reusing as when creating.
    try:
        verified_receipt(expected, exit_code=receipt['exit_code'], passed=receipt['passed'],
                         failed=receipt['failed'], evidence_sha256=receipt['evidence_sha256'])
    except (KeyError, TypeError, ValueError) as e:
        raise Refused('invalid verification receipt') from e


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = canonical(value).encode() + b'\n'
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def deploy_lock(path):
    # Stable inode: never unlink this file, including after failure.
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('DEPLOY_BUSY') from None
        yield
    finally:
        os.close(fd)


def require_base(current, expected):
    """Call under deploy_lock, before any live mutation. Initial base uses full manifest."""
    if not current or not expected or current != expected:
        raise Refused('STALE_BASE')


def backup_database(source, destination):
    """SQLite backup includes committed WAL; never restores or overwrites anything."""
    source = Path(source).resolve(strict=True)
    destination = Path(destination)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        src = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
        try:
            dst = sqlite3.connect(destination)
            try:
                src.backup(dst)
                if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise Refused('backup integrity failed')
            finally:
                dst.close()
        finally:
            src.close()
        with destination.open('rb') as f:
            os.fsync(f.fileno())
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return digest(destination)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
