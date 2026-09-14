"""Prepare M2 isolated layout; no service starts, no live DB copy, no deployment bypass.

Run by the future deploy job against its staging directory. Existing files cause refusal.
Values are copied verbatim in EnvironmentFile syntax and never printed.
"""
import argparse
import os
from pathlib import Path
import re

DATA_KEYS = {'OKX_DEX_API_KEY','OKX_DEX_SECRET','OKX_DEX_PASSPHRASE','OKX_DEX_PROJECT'}
UI_KEYS = {'TG_BOT_TOKEN','CABINET_LOGIN','CABINET_PASS_HASH'}


def parse_env(path, *, contents=None):
    out = {}
    text = Path(path).read_text() if contents is None else contents.decode('utf-8')
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        m = re.fullmatch(r'([A-Z][A-Z0-9_]*)=(.*)', line)
        if m is None or m[1] in out:
            raise ValueError('invalid/duplicate environment entry (value omitted)')
        out[m[1]] = line
    return out


def prepare(root, secret_env, cabinet_env=None, *, final_root='/var/lib/funding-bot',
            interface_uid=None, deploy_uid=0, secret_content=None, cabinet_content=None):
    root = Path(root)
    if root.exists():
        raise ValueError('staging directory already exists')
    values = parse_env(secret_env, contents=secret_content)
    if cabinet_env or cabinet_content is not None:
        extra = parse_env(cabinet_env, contents=cabinet_content)
        if set(extra) - UI_KEYS:
            raise ValueError('unexpected cabinet environment keys')
        values.update(extra)
    # Runtime routing must be generated, never inherited from an old mixed .env.
    if any(k.startswith('FUNDING_') for k in values):
        raise ValueError('legacy FUNDING_* routing must be migrated explicitly')
    if interface_uid is None:
        raise ValueError('explicit interface UID required')
    root.mkdir(mode=0o755, parents=True)
    for name in ('core','collector','interface','secrets'):
        (root/name).mkdir(mode=0o700)
    (root/'shared').mkdir(mode=0o770)
    (root/'collector'/'public').mkdir(mode=0o750)
    base=Path(final_root)
    common = {
        'FUNDING_TABLE_PATH': str(base/'collector/public/table.json'),
        'FUNDING_CORE_SOCKET': '/run/funding-bot/core.sock',
    }
    for role in ('core','collector','interface'):
        selected = UI_KEYS if role=='interface' else DATA_KEYS if role=='collector' else set(values)-UI_KEYS
        lines = [values[k] for k in sorted(selected & values.keys())]
        routing = dict(common, FUNDING_BOT_RUNTIME=str(base/role), FUNDING_PROCESS=role)
        if role=='core':
            routing.update(FUNDING_TRADE_DB=str(base/'core/trade.db'),
                           FUNDING_OWNER_PATH=str(base/'core/owner.toml'),
                           FUNDING_INTERFACE_UID=str(interface_uid), FUNDING_DEPLOY_UID=str(deploy_uid),
                           FUNDING_IPC_GROUP='funding-ipc',
                           FUNDING_EXECUTION_LOCK=str(base/'core/execution.lock'))
        if role in ('core','collector'):
            routing.update(FUNDING_OKX_PACE=str(base/'shared/okxdex.pace'),
                           FUNDING_TRADING_BUSY=str(base/'shared/trading.busy'))
        if role=='interface':
            routing['FUNDING_INTERFACE_STATE']=str(base/'interface/state.json')
        # Quote routing values for systemd, without shell evaluation.
        lines += [k+'="'+v.replace('\\','\\\\').replace('"','\\"')+'"' for k,v in routing.items()]
        p=root/'secrets'/f'{role}.env'
        fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write('\n'.join(lines)+'\n')
    return root


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--staging',required=True);ap.add_argument('--env',required=True)
    ap.add_argument('--cabinet-env');ap.add_argument('--interface-uid',required=True,type=int)
    ap.add_argument('--final-root',default='/var/lib/funding-bot')
    a=ap.parse_args()
    prepare(a.staging,a.env,a.cabinet_env,final_root=a.final_root,interface_uid=a.interface_uid)
    print('Staged isolated layout; no DB migrated, no services changed.')
