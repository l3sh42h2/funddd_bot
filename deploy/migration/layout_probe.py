"""Read-only pre-switch checks under each service's actual UID and sandbox.

No clients, signatures, transaction submission or database migrations are created.
Only named files are opened; no private contents are printed.
"""
import json
import os
import pwd
from pathlib import Path
import sys


def readable(path):
    with Path(path).open('rb') as stream:
        stream.read(1)


def denied(path):
    try:
        readable(path)
    except PermissionError:
        return
    raise RuntimeError('cross-role file is accessible')


def probe(role, root):
    root = Path(root)
    if os.geteuid() != pwd.getpwnam('funding-' + role).pw_uid:
        raise RuntimeError('probe did not run under target identity')
    if role == 'core':
        from funding_bot.trade import owner
        cfg = owner.load(root / 'core/owner.toml')
        readable(root / 'core/trade.db')
        if cfg.sha256 is None:
            raise RuntimeError('owner configuration missing')
        for name, registry in cfg.values.items():
            if name.endswith('.instrument_registry') and registry:
                readable(root / 'core' / registry)
        if cfg.get(f'profiles.{owner.SOL_HL}.enabled'):
            from funding_bot.trade.instruments import registry_path
            readable(registry_path(cfg.get(f'profiles.{owner.SOL_HL}.instrument_registry')))
        # Same config indirection as native Solana key loader; never decode/sign.
        names = {name for name in os.environ if name.endswith('KEYPAIR_FILE')}
        configured = cfg.get('spot.solana.keypair_file_env')
        if configured:
            names.add(configured)
        for name in names:
            if os.environ.get(name):
                readable(os.environ[name])
        denied(root / 'interface/state.json')
        with (root / 'shared/okxdex.pace').open('r+b'):
            pass
    elif role == 'collector':
        denied(root / 'core/trade.db')
        denied(root / 'interface/state.json')
        with (root / 'shared/okxdex.pace').open('r+b'):
            pass
        if (root / 'collector/funding_bot.db').exists():
            readable(root / 'collector/funding_bot.db')
    elif role == 'interface':
        denied(root / 'core/trade.db')
        denied(root / 'collector/funding_bot.db')
        readable(root / 'interface/state.json')
    else:
        raise RuntimeError('unknown role')
    # Both consumer roles must be able to traverse the market-public directory.
    if role in ('core', 'interface'):
        public = root / 'collector/public'
        if not os.access(public, os.R_OK | os.X_OK):
            raise RuntimeError('public market directory is inaccessible')
    return {'role': role, 'uid': os.geteuid(), 'ok': True}


if __name__ == '__main__':
    try:
        print(json.dumps(probe(sys.argv[1], sys.argv[2])))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__}))
        sys.exit(1)
