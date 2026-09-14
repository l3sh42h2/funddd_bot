"""Public atomic market snapshot contract. Missing/incompatible data is never fresh."""
import json
import math
import os
import time
from pathlib import Path
from . import config
from .build_info import BUILD_ID
from .core.release import load_release

SCHEMA = 1
HEALTH_MAX = 16384
HEALTH_BOOT_ID = os.urandom(12).hex()
RELEASE = load_release()


def release_identity():
    return {key: RELEASE.get(key) for key in ('release_id', 'source_sha256', 'artifact_sha256')}


def atomic_json(path, value, mode=0o640):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    body = json.dumps(value, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def load_market_table(path=None):
    try:
        with open(path or config.TABLE_PATH) as f:
            t = json.load(f)
        if not isinstance(t, dict) or t.get('schema_version') not in (None, SCHEMA):
            return {}
        if t.get('schema_version') == SCHEMA and (
                not isinstance(t.get('snapshot_id'), str) or not t['snapshot_id']
                or not isinstance(t.get('sf_rows'), list) or not isinstance(t.get('ff_rows'), list)):
            return {}
        # Legacy snapshots remain readable during M1/M2; original timestamps are preserved.
        return t
    except (OSError, ValueError):
        return {}


def health_path(table_path=None):
    p = Path(table_path or config.TABLE_PATH)
    return p.with_name('collector_health.json')


def publish_health(table, table_path=None):
    keep = ('schema_version','snapshot_id','generated_at','tick_ts','pid','started_ts','n_ff','n_sf',
            'n_stale_ff','n_stale_sf','src_age','backfill')
    h = {k:table.get(k) for k in keep}
    h["build_id"] = BUILD_ID
    h.update(release_identity(), boot_id=HEALTH_BOOT_ID, updated_at=time.time())
    tick = h.get('tick_ts')
    h['ready'] = isinstance(tick, (int, float)) and 0 <= time.time() - tick <= config.STALE_S
    # Provider notes/error strings may grow without bound; not included in health.
    if len(json.dumps(h).encode()) > HEALTH_MAX:
        h.pop('src_age', None)
        h.pop('backfill', None)
        h['details_omitted'] = True
    atomic_json(health_path(table_path), h, 0o644)


def load_health(path=None, now=None):
    now = time.time() if now is None else now
    try:
        with open(path or health_path(), 'rb') as f:
            body = f.read(HEALTH_MAX+1)
        if len(body)>HEALTH_MAX:
            raise ValueError()
        h = json.loads(body)
        if h.get('schema_version') != SCHEMA:
            raise ValueError()
        tick = h.get('tick_ts')
        age = now-tick if isinstance(tick,(int,float)) and math.isfinite(tick) else None
        return {**h, 'age_s':age, 'ready': age is not None and 0 <= age <= config.STALE_S}
    except (OSError, ValueError, TypeError, AttributeError):
        return {'schema_version':SCHEMA,'age_s':None,'ready':False,'error':'collector_status_unavailable'}
