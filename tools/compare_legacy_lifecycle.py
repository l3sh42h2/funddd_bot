#!/usr/bin/env python3
"""Offline differential replay of accepted fixtures against two source trees.

Both subprocesses load exactly the same fixture files from --fixtures. Real
network connects are forbidden. Only normalized financial/state evidence is
compared; generated IDs, clocks and message wording are excluded.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def probe(source, fixtures):
    import socket
    def no_network(*args, **kwargs):
        raise AssertionError('differential fixture attempted network I/O')
    socket.create_connection = no_network
    socket.socket.connect = no_network
    sys.path[:0] = [str(Path(source) / 'src'), str(Path(fixtures) / 'tests')]
    from decimal import Decimal as D
    from funding_bot.trade import store
    from funding_bot.trade.engine import deal_book
    import test_trade_engine as E
    import sol_c2_world as S

    def snapshot(con, did):
        deal = store.get_deal(con, did)
        rows = con.execute('SELECT i.kind,c.state,c.planned_in,c.dex_in,c.dex_out,c.perp_qty,c.perp_quote '
                           'FROM clips c JOIN intents i ON c.intent_id=i.id WHERE i.deal_id=? ORDER BY c.id', (did,))
        operations = con.execute('SELECT side,state,target_kind,target_asset,target_decimals,target_raw,confirmed_raw,reserved_raw '
                                 'FROM operations WHERE deal_id=? ORDER BY created,rowid', (did,))
        fees = con.execute('SELECT kind,asset,decimals,amount_raw,included,estimated,refundable,superseded '
                           'FROM fee_events WHERE deal_id=? ORDER BY id', (did,))
        orders = con.execute('SELECT side,state,qty,executed_qty,cum_quote,reduce_only FROM perp_orders '
                             'WHERE client_id LIKE ? ORDER BY id', (f'fb-{did}-%',))
        book = deal_book(con, did)
        return dict(state=deal['state'], carry=deal['carry'], dust=deal['dust'],
                    book=dict(tokens_raw=book.tokens_raw, short=str(book.short)),
                    clips=[tuple(r) for r in rows], roots=[tuple(r) for r in operations],
                    fees=[tuple(r) for r in fees], orders=[tuple(r) for r in orders])

    output = {}
    with tempfile.TemporaryDirectory(prefix='funding-legacy-replay-') as folder:
        path = Path(folder)
        ep = path / 'evm'
        ep.mkdir()
        e = E.live_env(ep)
        p = e.desk.propose_entry('AIW3', 'okx·bsc', 'aster', D(200), chat=E.OWNER)
        E.run_approved(e, p)
        evm = [snapshot(e.con, p.deal_id)]
        x = e.desk.propose_exit(p.deal_id, D(100), False, chat=E.OWNER)
        E.run_approved(e, x)
        evm.append(snapshot(e.con, p.deal_id))
        x = e.desk.propose_exit(p.deal_id, None, False, chat=E.OWNER)
        E.run_approved(e, x)
        evm.append(snapshot(e.con, p.deal_id))
        output['evm_entry_partial_full_exit'] = evm
        sp = path / 'sol'
        sp.mkdir()
        w = S.make_world(sp)
        p = S.enter(w)
        sol = [snapshot(w.con, p.deal_id)]
        x = w.desk.propose_exit(p.deal_id, None, False, chat=None)
        S.approve_run(w, x)
        sol.append(snapshot(w.con, p.deal_id))
        output['sol_entry_full_exit'] = sol
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline')
    parser.add_argument('--candidate')
    parser.add_argument('--fixtures', required=True)
    parser.add_argument('--probe')
    parser.add_argument('--output')
    args = parser.parse_args()
    if args.probe:
        print(json.dumps(probe(args.probe, args.fixtures), sort_keys=True))
        return
    if not args.baseline or not args.candidate:
        parser.error('baseline and candidate required')
    def fingerprint(root, directory):
        base = Path(root) / directory
        manifest = {str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(base.rglob('*.py'))}
        return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    snapshots = [fingerprint(root, 'src') for root in (args.baseline, args.candidate)]
    fixtures_hash = fingerprint(args.fixtures, 'tests')
    observations = []
    for root in (args.baseline, args.candidate):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        env.pop('PYTHONPATH', None)
        result = subprocess.run([sys.executable, __file__, '--probe', root, '--fixtures', args.fixtures],
                                capture_output=True, text=True, env=env)
        if result.returncode:
            raise SystemExit(f'Fixture replay failed for {root}:\n{result.stderr}')
        observations.append(json.loads(result.stdout))
    if snapshots != [fingerprint(root, 'src') for root in (args.baseline, args.candidate)] or (
            fixtures_hash != fingerprint(args.fixtures, 'tests')):
        raise SystemExit('Source or fixtures changed during replay; evidence discarded')
    report = dict(equal=observations[0] == observations[1], network_allowed=False,
                  baseline=str(Path(args.baseline).resolve()), candidate=str(Path(args.candidate).resolve()),
                  fixtures=str(Path(args.fixtures).resolve()), scenarios=list(observations[0]),
                  source_sha256=dict(baseline=snapshots[0], candidate=snapshots[1]),
                  fixtures_sha256=fixtures_hash,
                  observations=dict(baseline=observations[0], candidate=observations[1]))
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'observations'}, sort_keys=True))
    if not report['equal']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
