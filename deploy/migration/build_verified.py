#!/usr/bin/env python3
"""Build and test one immutable M5 artifact in its target Linux runtime.

This module is an implementation detail of deploy/deploy.sh.  It deliberately
has no install or service operations.  The test receipt binds the complete
tracked source tree, dependency lock and wheels, runtime, profile inputs and
the final tar bytes.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import venv

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import artifacts as af  # noqa: E402

EXCLUDED_TOP = {'.git', '.venv', '.pytest_cache', 'runtime', 'logs'}


def _run(argv, *, cwd=None, env=None, capture=False):
    return subprocess.run(argv, cwd=cwd, env=env, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.STDOUT if capture else None)


def tracked_paths(root):
    raw = _run(['git', 'ls-files', '-z'], cwd=root, capture=True).stdout
    paths = [p for p in raw.split('\0') if p]
    if not paths:
        raise af.Refused('no tracked sources')
    dirty = _run(['git', 'status', '--porcelain=v1', '--untracked-files=no'], cwd=root, capture=True).stdout
    if dirty:
        raise af.Refused('tracked source tree is dirty; commit before verification')
    return paths


def copy_sources(root, destination, paths):
    root, destination = Path(root), Path(destination)
    for name in paths:
        p = Path(name)
        src = root / p
        if src.is_symlink() or not src.is_file():
            raise af.Refused(f'non-regular tracked source: {name}')
        dst = destination / p
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def deterministic_tar(source, destination):
    source, destination = Path(source), Path(destination)
    with tarfile.open(destination, 'w', format=tarfile.PAX_FORMAT) as tf:
        for path in sorted(source.rglob('*')):
            rel = path.relative_to(source)
            if path.is_symlink():
                raise af.Refused(f'artifact symlink refused: {rel}')
            info = tf.gettarinfo(str(path), arcname=rel.as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mtime = 0
            info.mode = 0o755 if path.is_dir() or os.access(path, os.X_OK) else 0o644
            if path.is_file():
                with path.open('rb') as f:
                    tf.addfile(info, f)
            else:
                tf.addfile(info)


def safe_extract(archive, destination):
    destination = Path(destination).resolve()
    with tarfile.open(archive, 'r') as tf:
        for member in tf.getmembers():
            p = Path(member.name)
            if p.is_absolute() or '..' in p.parts or member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise af.Refused('unsafe artifact member')
        af.extract_regular_files(tf, destination)


def make_venv(python, root, wheelhouse, lockfile):
    _run([python, '-m', 'venv', str(root)])
    py = Path(root) / 'bin/python'
    _run([str(py), '-m', 'pip', 'install', '--no-index', '--find-links', str(wheelhouse),
          '--no-deps', '-r', str(lockfile)])
    _run([str(py), '-m', 'pip', 'check'])
    return py


def runtime_for(python):
    code = "import json,platform,sqlite3,sys,hashlib; p=sys.executable; print(json.dumps({'python':list(sys.version_info[:3]),'implementation':platform.python_implementation(),'platform':sys.platform,'machine':platform.machine(),'libc':list(platform.libc_ver()),'sqlite':sqlite3.sqlite_version,'executable_hash':hashlib.sha256(open(p,'rb').read()).hexdigest()},sort_keys=True))"
    return json.loads(_run([str(python), '-c', code], capture=True).stdout)


def installed_for(python):
    lines = sorted(x.strip() for x in _run([str(python), '-m', 'pip', 'freeze', '--all'], capture=True).stdout.splitlines() if x.strip())
    return {'packages': lines, 'sha256': af.value_digest(lines)}


def component_hashes(sources):
    interface_only = ('src/funding_bot/interface/', 'src/funding_bot/serve.py',
                      'src/funding_bot/cabinet.py', 'src/funding_bot/cabinet_text.py',
                      'deploy/migration/funding_bot-interface.service',
                      'deploy/migration/funding_bot-tunnel.service')
    runtime_items = {k: v for k, v in sources.items()
                     if k.startswith(('src/funding_bot/', 'deploy/')) or k == 'pyproject.toml'}
    interface = {k: v for k, v in runtime_items.items() if k.startswith(interface_only)}
    # Shared modules are conservatively assigned to both core and collector.  A
    # release is UI-only only when their code, packaging and deploy paths are equal.
    shared = {k: v for k, v in runtime_items.items() if k not in interface}
    return {'interface': af.value_digest(interface), 'core': af.value_digest(shared),
            'collector': af.value_digest(shared)}


def build(root, output, *, profile_path, compatibility_path, patchnote, python=sys.executable):
    root, output = Path(root).resolve(), Path(output).resolve()
    if sys.platform != 'linux':
        raise af.Refused('verified artifact must be built and tested on Linux')
    started = time.time()
    paths = tracked_paths(root)
    if patchnote not in paths or not patchnote.startswith('PATCHNOTES/'):
        raise af.Refused('tracked patchnote required')
    source = af.source_manifest(root, paths)
    source_sha = af.value_digest(source)
    revision = _run(['git', 'rev-parse', 'HEAD'], cwd=root, capture=True).stdout.strip()
    profile = json.loads(Path(profile_path).read_text())
    if profile.get('platform') != 'linux':
        raise af.Refused('Linux test profile required')
    profile_fp = af.profile_fingerprint(profile, root)
    compatibility = json.loads(Path(compatibility_path).read_text())
    release_id = f"{revision[:12]}-{source_sha[:12]}"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='funding-m5-build-') as td:
        td = Path(td)
        release = td / 'release'
        release.mkdir()
        copy_sources(root, release, paths)
        wheels = release / 'wheelhouse'
        wheels.mkdir()
        _run([python, '-m', 'pip', 'download', '--only-binary=:all:', '--no-deps',
              '-r', str(root / 'deploy/requirements.lock'), '-d', str(wheels)])
        deps = af.dependency_fingerprint(root / 'deploy/requirements.lock', wheels)
        probe = make_venv(python, td / 'probe-venv', wheels, root / 'deploy/requirements.lock')
        runtime = runtime_for(probe)
        deps['installed'] = installed_for(probe)
        template = {
            'format_version': 1, 'release_id': release_id, 'source_revision': revision,
            'source_sha256': source_sha, 'sources': source, 'patchnote': patchnote,
            'dependencies': deps, 'runtime': runtime, 'test_profile': profile_fp,
            'component_hashes': component_hashes(source),
            **compatibility,
        }
        af.atomic_json(release / 'release-manifest.template.json', template)
        deterministic_tar(release, output)

        exact = td / 'exact'
        exact.mkdir()
        safe_extract(output, exact)
        test_py = make_venv(python, td / 'test-venv', exact / 'wheelhouse', exact / 'deploy/requirements.lock')
        if runtime_for(test_py) != runtime or installed_for(test_py) != deps['installed']:
            raise af.Refused('fresh verification environment differs from fingerprint')
        env = os.environ.copy()
        env.update(profile.get('environment') or {})
        env['PYTHONPATH'] = str(exact / 'src')
        result = subprocess.run([str(test_py), *profile['argv']], cwd=exact, env=env,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        evidence = output.with_suffix(output.suffix + '.tests.txt')
        evidence.write_text(result.stdout)
        match = re.search(r'(?m)(\d+) passed(?:,| in)', result.stdout)
        passed = int(match.group(1)) if match else 0
        ident = af.verification_identity(artifact=output, sources=source, dependencies=deps,
                                         runtime=runtime, profile=profile_fp)
        receipt = af.verified_receipt(ident, exit_code=result.returncode, passed=passed,
                                      failed=0 if result.returncode == 0 else 1,
                                      evidence_sha256=af.digest(evidence))
        receipt.update(release_id=release_id, source_revision=revision, source_sha256=source_sha,
                       patchnote=patchnote, built_at=time.time(), duration_s=round(time.time()-started, 3))
        receipt_path = output.with_suffix(output.suffix + '.receipt.json')
        af.atomic_json(receipt_path, receipt)
    return receipt_path


def main(argv=None):
    if os.environ.get('FUNDING_M5_ENTRY') != 'deploy/deploy.sh':
        raise SystemExit('enter M5 only through deploy/deploy.sh')
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True); ap.add_argument('--output', required=True)
    ap.add_argument('--profile', required=True); ap.add_argument('--compatibility', required=True)
    ap.add_argument('--patchnote', required=True); ap.add_argument('--python', default=sys.executable)
    a = ap.parse_args(argv)
    print(build(a.root, a.output, profile_path=a.profile, compatibility_path=a.compatibility,
                patchnote=a.patchnote, python=a.python))


if __name__ == '__main__':
    main()
