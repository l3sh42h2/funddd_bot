import importlib.util
import json
from pathlib import Path
import sys
import tarfile

import pytest

M5 = Path(__file__).resolve().parents[1] / 'deploy/migration'
sys.path.insert(0, str(M5))
import artifacts as af
import build_verified as build
import server_job as job


def test_tree_profile_and_dependency_fingerprints_are_complete(tmp_path):
    (tmp_path / 'src').mkdir(); (tmp_path / 'tests').mkdir(); (tmp_path / 'wheels').mkdir()
    (tmp_path / 'src/a.py').write_text('a')
    (tmp_path / 'tests/test_a.py').write_text('def test_a(): pass')
    (tmp_path / 'requirements.lock').write_text('x==1\n')
    (tmp_path / 'wheels/x-1-py3-none-any.whl').write_bytes(b'wheel')
    tree = af.tree_manifest(tmp_path, excluded={'wheels'})
    assert set(tree) == {'requirements.lock', 'src/a.py', 'tests/test_a.py'}
    profile = af.profile_fingerprint({'version': 1, 'argv': ['-m', 'pytest'], 'paths': ['tests']}, tmp_path)
    assert set(profile['inputs']) == {'tests/test_a.py'}
    deps = af.dependency_fingerprint(tmp_path / 'requirements.lock', tmp_path / 'wheels')
    assert deps['lock_sha256'] == af.digest(tmp_path / 'requirements.lock')
    (tmp_path / 'src/link').symlink_to(tmp_path / 'src/a.py')
    with pytest.raises(af.Refused, match='symlink'):
        af.tree_manifest(tmp_path)


def test_server_recomputes_artifact_identity_and_runner(tmp_path):
    source = {'deploy/migration/server_job.py': af.digest(M5 / 'server_job.py')}
    template = {
        'format_version': 1, 'release_id': 'release-12345678', 'source_revision': 'a' * 40,
        'source_sha256': af.value_digest(source), 'sources': source,
        'dependencies': {'lock_sha256': 'b' * 64, 'wheels': {'x.whl': 'c' * 64},
                         'wheelset_sha256': 'd' * 64, 'installed': {'packages': [], 'sha256': af.value_digest([])}},
        'runtime': {'platform': 'linux'}, 'test_profile': {'definition_sha256': 'e' * 64},
        'component_hashes': {'core': 'f' * 64, 'collector': 'f' * 64, 'interface': '1' * 64},
        'ipc_version': 1, 'dto_version': 1, 'schema_version': 2, 'min_reader': 2,
        'compatible_readers': [2], 'patchnote': 'PATCHNOTES/x.md',
    }
    release = tmp_path / 'release'; (release / 'deploy/migration').mkdir(parents=True)
    (release / 'deploy/migration/server_job.py').write_bytes((M5 / 'server_job.py').read_bytes())
    af.atomic_json(release / 'release-manifest.template.json', template)
    artifact = tmp_path / 'release.tar'; build.deterministic_tar(release, artifact)
    identity = af.verification_identity(artifact=artifact, sources=source,
                                        dependencies=template['dependencies'], runtime=template['runtime'],
                                        profile=template['test_profile'])
    receipt = af.verified_receipt(identity, exit_code=0, passed=3, failed=0, evidence_sha256='2' * 64)
    receipt.update(release_id=template['release_id'], source_revision=template['source_revision'],
                   source_sha256=template['source_sha256'], patchnote=template['patchnote'])
    receipt_path = tmp_path / 'receipt.json'; af.atomic_json(receipt_path, receipt)
    assert job.verify_bundle(artifact, receipt_path)[1]['release_id'] == template['release_id']
    artifact.write_bytes(artifact.read_bytes() + b'tamper')
    with pytest.raises(af.Refused, match='VERIFICATION_MISMATCH'):
        job.verify_bundle(artifact, receipt_path)


def test_deterministic_tar_is_byte_stable(tmp_path):
    root = tmp_path / 'r'; root.mkdir(); (root / 'b').write_text('B'); (root / 'a').write_text('A')
    one, two = tmp_path / 'one.tar', tmp_path / 'two.tar'
    build.deterministic_tar(root, one); build.deterministic_tar(root, two)
    assert af.digest(one) == af.digest(two)


def test_safe_extract_refuses_links_and_escape(tmp_path):
    archive = tmp_path / 'bad.tar'
    with tarfile.open(archive, 'w') as tf:
        info = tarfile.TarInfo('../escape'); info.size = 0; tf.addfile(info)
    with pytest.raises(af.Refused):
        build.safe_extract(archive, tmp_path / 'out')
