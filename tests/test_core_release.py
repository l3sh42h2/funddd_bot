"""A release identity cannot cover just a chosen subset of executable files."""
import hashlib
import json
import pytest
from funding_bot.core import release


def manifest(tmp_path, monkeypatch):
    module=tmp_path/'src/funding_bot/core/release.py'
    module.parent.mkdir(parents=True)
    module.write_text('# fixture code')
    monkeypatch.setattr(release, '__file__', str(module))
    sources={'src/funding_bot/core/release.py':hashlib.sha256(module.read_bytes()).hexdigest()}
    digest=hashlib.sha256(json.dumps(sources,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    value=dict(release_id='R1',source_sha256=digest,artifact_sha256='a'*64,
               verification_identity_sha256='b'*64,sources=sources)
    path=tmp_path/'release-manifest.json';path.write_text(json.dumps(value))
    return path, module, value


def test_loaded_release_identity_and_tampering(tmp_path, monkeypatch):
    path, module, value=manifest(tmp_path,monkeypatch)
    assert release.load_release()==value
    module.write_text('# changed code')
    with pytest.raises(ValueError,match='source changed'):
        release.load_release()


def test_omitted_executable_does_not_pass_identity(tmp_path, monkeypatch):
    path,module,value=manifest(tmp_path,monkeypatch)
    module.with_name('omitted.py').write_text('# executable omitted from manifest')
    with pytest.raises(ValueError,match='omits executable'):
        release.load_release()


def test_missing_manifest_is_not_a_release(tmp_path, monkeypatch):
    path,module,value=manifest(tmp_path,monkeypatch)
    path.unlink()
    assert release.load_release()=={}
    path.write_text('broken')
    with pytest.raises(ValueError):
        release.load_release()
