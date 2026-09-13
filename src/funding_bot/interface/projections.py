"""Private DTO client. No SQL or trading imports. All pages share one core revision."""
import time
from ..ipc.protocol import Client, RpcError
from ..ipc.values import decode


def fetch_positions(client=None, now=None):
    client = client or Client()
    now = time.time() if now is None else now
    try:
        health = client.call('get_status')
        if not health.get('ready'):
            raise RpcError('core_not_ready')
        for attempt in range(2):
            try:
                first = client.call('list_positions', {'offset':0,'limit':10})
                rows = list(first['deals'])
                offset = first['next_offset']
                while offset is not None:
                    page = client.call('list_positions', {'offset':offset,'limit':10,'revision':first['revision']})
                    rows.extend(page['deals'])
                    offset = page['next_offset']
                first['deals'] = rows
                if not 0 <= now-first['as_of'] <= 30:
                    raise RpcError('projection_stale')
                return decode(first)
            except RpcError as e:
                if e.code != 'snapshot_changed' or attempt:
                    raise
    except (RpcError, KeyError, TypeError, ValueError):
        return dict(now=now, deals=[], drafts=0, err='Торговое ядро недоступно или данные устарели; актуальные позиции не получены')
