"""Source fingerprint captured once per process, never re-read from a switched symlink."""
import hashlib
from pathlib import Path


def _identity():
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for p in sorted(root.rglob('*.py')):
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()

BUILD_ID = _identity()
