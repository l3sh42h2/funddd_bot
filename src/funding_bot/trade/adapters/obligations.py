"""Read durable unresolved obligations; absence of a reserve is not finality.

This reader never loads signing payloads, performs network calls or changes a
journal. Network-specific evidence remains in the adapter boundary.
"""
import json
from .. import store


def _instrument(deal):
    """Read optional native identity without turning corrupt metadata into a crash."""
    try:
        inst = json.loads(deal.get('inst_json') or '{}')
    except (TypeError, ValueError):
        return {}
    return inst if isinstance(inst, dict) else {}


def unresolved(con, deal, *, allow_triggered_native_stop: bool = False, allow_native_stop: bool = False):
    did = deal['id']
    reasons = []
    if con.execute("SELECT 1 FROM clips c JOIN intents i ON i.id=c.intent_id WHERE i.deal_id=? "
                   "AND c.state IN ('DEX_SENT','DEX_UNKNOWN') LIMIT 1", (did,)).fetchone():
        reasons.append('spot_unresolved')
    if con.execute("SELECT 1 FROM perp_orders p JOIN clips c ON c.id=p.clip_id "
                   "JOIN intents i ON i.id=c.intent_id WHERE i.deal_id=? "
                   "AND p.state IN ('INTENT','SENT','UNKNOWN') LIMIT 1", (did,)).fetchone():
        reasons.append('perp_unresolved')
    # A native conditional is a durable external obligation although it has no clip.
    # It must fence every new operation until a terminal outcome is recorded.
    if not allow_native_stop:
        native_states = "('INTENT','SENT','OPEN','UNKNOWN')" if allow_triggered_native_stop else \
                        "('INTENT','SENT','OPEN','UNKNOWN','TRIGGERED')"
        if con.execute("SELECT 1 FROM native_stops WHERE deal_id=? AND state IN " + native_states + " LIMIT 1", (did,)).fetchone():
            reasons.append('native_stop_unresolved')
    if con.execute("SELECT 1 FROM operations WHERE deal_id=? AND reserved_raw<>'0' LIMIT 1", (did,)).fetchone():
        reasons.append('reserved_input')
    # A native approval can exist before there is a clip. Match its frozen wallet
    # and chain, not a symbol or a presumed current global account.
    if not deal.get('sim'):
        from ..owner import OwnerCfg, EVM_WALLET_KEY
        cfg = OwnerCfg.from_frozen(deal['owner_json'])
        chain = deal.get('chain')
        key = EVM_WALLET_KEY.get(chain)
        wallet = cfg.get(key) if key else None
        if wallet and con.execute("SELECT 1 FROM dex_txs WHERE chain=? AND wallet=? "
                                  "AND state IN ('SIGNED','SENT','UNKNOWN') LIMIT 1",
                                  (chain, wallet.lower())).fetchone():
            reasons.append('wallet_unresolved')
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'hl_order_attempts' in tables:
        from ..owner import OwnerCfg
        inst = _instrument(deal)
        cfg = OwnerCfg.from_frozen(deal['owner_json'])
        scope = str(inst.get('perp_account') or '').split(':')
        account = scope[3] if (deal.get('perp_venue') == 'hyperliquid' and len(scope) == 5
                               and scope[0] == 'hyperliquid' and scope[1] == inst.get('perp_network')
                               and scope[4] == inst.get('perp_dex')) else None
        # Margin/leverage setup precedes the first clip and historically has no
        # deal_id. Its frozen account+network+market still owns the obligation.
        if con.execute(
                "SELECT 1 FROM hl_order_attempts WHERE state IN ('PREPARED','SIGNED','UNKNOWN') "
                "AND (deal_id=? OR (?=0 AND account=? AND network=? AND fullcoin=?)) LIMIT 1",
                (did, int(bool(deal.get('sim'))), account.lower() if account else None,
                 inst.get('perp_network'), inst.get('perp_symbol'))).fetchone():
            reasons.append('perp_native_unresolved')
    if 'sol_tx_attempts' in tables:
        from ..solana.journal import unresolved as sol_unresolved
        clips = {str(r[0]) for r in con.execute(
            "SELECT c.id FROM clips c JOIN intents i ON i.id=c.intent_id WHERE i.deal_id=?", (did,))}
        inst = _instrument(deal)
        # Native attempts with a clip use the existing durable relation. Wallet
        # prerequisites use the same frozen identity; no peer account is inferred.
        from ..owner import OwnerCfg
        wallet = OwnerCfg.from_frozen(deal['owner_json']).get('wallets.sol_hl.solana_address')
        for row in sol_unresolved(con):
            if str(row.get('clip_ref')) in clips or (
                    not deal.get('sim') and wallet and row['wallet'] == wallet
                    and row['network'] == inst.get('genesis_hash')):
                reasons.append('spot_native_unresolved')
                break
    return tuple(dict.fromkeys(reasons))


def require_resolved(con, deal, *, allow_triggered_native_stop: bool = False, allow_native_stop: bool = False):
    reasons = unresolved(con, deal, allow_triggered_native_stop=allow_triggered_native_stop,
                         allow_native_stop=allow_native_stop)
    if reasons:
        raise store.StoreError('unresolved execution blocks new operation: ' + ', '.join(reasons))
