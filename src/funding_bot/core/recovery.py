"""External evidence for a drained core, using native recovery journals."""
from ..trade import reconcile, sol_flow, store
from ..trade.runtime import is_sol_deal


def check(con, legs_fn, *, resolve=True):
    blockers = []
    live = legs_fn(False) if resolve else None
    if live is not None:
        blockers.extend('wallet_unresolved' for _ in reconcile.resolve_wallet_txs(con, live.spot))
    if resolve:
        blockers.extend('wallet_unresolved' for _ in reconcile._other_evm_wallet_txs(con, legs_fn))
    for deal in store.active_deals(con):
        if is_sol_deal(deal):
            legs, down = reconcile._sol_legs(legs_fn, deal)
            result = sol_flow.check_deal(con, deal, legs, resolve=resolve, down=down)
            from ..trade import sol_ledger
            if sol_ledger.ledger(con, deal, fee_rate=None).foreign_fills:
                blockers.append("position_unverified")
        else:
            legs = reconcile._evm_legs(legs_fn, deal)
            result = reconcile.check_deal(con, deal, legs, resolve=resolve)
        if result.matched is not True:
            blockers.append('position_unverified')
        if result.hedged is not True:
            blockers.append('hedge_unverified')
    return sorted(set(blockers))
