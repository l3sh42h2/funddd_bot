"""Verify the loaded release once, before opening the execution service."""
import hashlib
import json
from pathlib import Path


def load_release(path=None):
    root = Path(__file__).resolve().parents[3]
    path = Path(path) if path else root / 'release-manifest.json'
    if not path.exists():
        return {}
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError('release manifest must belong to loaded release')
    value = json.loads(path.read_text())
    for key in ('release_id', 'source_sha256', 'artifact_sha256', 'verification_identity_sha256'):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError('incomplete release identity: ' + key)
    sources = value.get('sources')
    if not isinstance(sources, dict) or not sources:
        raise ValueError('empty release sources')
    canonical = json.dumps(sources, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    if hashlib.sha256(canonical.encode()).hexdigest() != value['source_sha256']:
        raise ValueError('release source identity mismatch')
    for name, expected in sources.items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or str(relative) != name:
            raise ValueError('invalid release source path')
        if any((root / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts)+1)):
            raise ValueError('symlink in release source')
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
            raise ValueError('release source changed: ' + name)
    # Omitted Python modules would otherwise execute without participating in identity.
    actual = {str(p.relative_to(root)) for p in (root/'src'/'funding_bot').rglob('*.py')}
    if not actual.issubset(sources):
        raise ValueError('release manifest omits executable modules')
    return value
