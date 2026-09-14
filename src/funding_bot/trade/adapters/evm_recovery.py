"""Native EVM nonce-group recovery behind the adapter boundary. Read-only network."""
import logging
from dataclasses import replace
from types import SimpleNamespace
from .. import store
from ..store import DexTxState
from ..keys import redact
from .contracts import Status, AdapterError, ErrorKind

log = logging.getLogger(__name__)
UNRESOLVED_TX = (str(DexTxState.SIGNED), str(DexTxState.SENT), str(DexTxState.UNKNOWN))
MINED = (str(DexTxState.MINED_OK), str(DexTxState.MINED_REVERTED))
FOREIGN_NONCE = "nonce занят не нашей транзакцией"


def resolve_receipt(con, spot, row):
    if row['chain'] != spot.chain or row['wallet'].lower() != spot.wallet.lower():
        raise AdapterError(ErrorKind.IDENTITY, 'native EVM receipt belongs to another wallet/network')
    result = spot.resolve(row)
    if result.tx_hash != row['tx_hash']:
        raise AdapterError(ErrorKind.IDENTITY, 'native EVM receipt hash differs from journal')
    if row['kind'] not in ('swap', 'bump') or row['clip_id'] is None:
        return result
    clip = store.get_clip(con, row['clip_id'])
    intent = store.get_intent(con, clip['intent_id']) if clip else None
    deal = store.get_deal(con, intent['deal_id']) if intent else None
    if deal is None:
        raise AdapterError(ErrorKind.IDENTITY, 'EVM receipt has no frozen deal')
    from ... import config
    from .. import tconfig
    from .spot_execution import evm_spec
    from .execution_scope import continuation_identity
    from .outcomes import evm_swap
    stable, dec = config.OKX_DEX_STABLES[tconfig.chain_index(deal['chain'])]
    spec = evm_spec(deal, spot, stable, dec,
                    continuation_evidence=continuation_identity(con, deal, 'recovery'))
    side = 'BUY' if intent['kind'] == 'entry' else 'SELL'
    normalized = evm_swap(result, spec, side)
    if normalized.status == Status.UNKNOWN:
        # Receipt finality is independent of proof that the swap delivered tokens.
        # Keep the mined nonce, but persist the failed economic validation.
        return replace(result, status='unknown', note=getattr(result, 'note', '') or
                       'adapter: incomplete swap settlement evidence')
    return result


def stored_settlement(con, deal, spot, row, kind):
    """Persisted receipt amounts pass the same v2 gate as the immediate result."""
    from ... import config
    from .. import tconfig
    from ..operations import SpotSettlement
    from .spot_execution import evm_spec
    from .execution_scope import continuation_identity
    from .outcomes import evm_swap
    stable, dec = config.OKX_DEX_STABLES[tconfig.chain_index(deal['chain'])]
    spec = evm_spec(deal, spot, stable, dec,
                    continuation_evidence=continuation_identity(con, deal, 'recovery'))
    if row['chain'] != spot.chain or row['wallet'].lower() != spec.account.lower():
        raise AdapterError(ErrorKind.IDENTITY, 'stored receipt scope differs')
    if row['state'] != DexTxState.MINED_OK:
        raise AdapterError(ErrorKind.UNKNOWN, 'stored receipt is not mined successfully')
    if row.get('err'):
        # A mined receipt with a validation error is not a successful swap.
        # Recheck the native evidence, never manufacture status=ok from its logs.
        candidate = dict(row, token_in=stable if kind == 'entry' else deal['token'],
                         token_out=deal['token'] if kind == 'entry' else stable)
        checked = resolve_receipt(con, spot, candidate)
        if checked.status != 'ok' or (('balanceOf' in row['err']) and
                                      getattr(checked, 'balance_check', None) != 'ok'):
            raise AdapterError(ErrorKind.UNKNOWN, 'native receipt validation remains unresolved')
        store.dex_tx_resolve(con, row['tx_hash'], DexTxState.MINED_OK,
                             amount_in=int(checked.amount_in), amount_out=int(checked.amount_out), err='')
        row = store.get_dex_tx(con, row['tx_hash'])
    value = SimpleNamespace(status='ok', tx_hash=row['tx_hash'], block=row['block'], gas_wei=None,
                            amount_in=int(row['amount_in']), amount_out=int(row['amount_out']))
    result = evm_swap(value, spec, 'BUY' if kind == 'entry' else 'SELL')
    if result.status != Status.SETTLED:
        raise AdapterError(ErrorKind.UNKNOWN, 'stored receipt amounts are incomplete')
    return SpotSettlement(True, result.spot_input_raw.raw, result.spot_output_raw.raw)


def _resolve_nonce(con, spot, chain: str, wallet: str, nonce: int, tok: dict) -> str | None:
    """Одна nonce-группа (своп, его bump и cancel): замайниться могла только одна. None — всё решено; иначе текст,
    почему исход неизвестен."""
    group = store.dex_txs_on_nonce(con, chain, wallet, nonce)
    mined_before = [g for g in group if g["state"] in MINED]
    open_ = [g for g in group if g["state"] in UNRESOLVED_TX]
    if not open_:
        return None
    res: dict[str, tuple[dict, Any]] = {}
    for g in open_:
        row = dict(g)
        row["token_in"], row["token_out"] = tok.get(g["clip_id"], (None, None))
        try:
            res[g["tx_hash"]] = (g, resolve_receipt(con, spot, row))
        except Exception as e:                 # noqa — не прочитали: исход неизвестен, ничего не меняем
            return f"tx {g['tx_hash'][:10]}…: чек не прочитан ({redact(e)[:120]})"
    states = {h: str(getattr(r, "tx_state", "") or "") for h, (_g, r) in res.items()}
    mined = [h for h, st in states.items() if st in MINED]
    for h in mined:
        g, r = res[h]
        ok = states[h] == DexTxState.MINED_OK
        swapish = g["kind"] in ("swap", "bump")
        store.dex_tx_resolve(con, h, states[h], block=int(getattr(r, "block", 0) or 0) or None, status=1 if ok else 0,
                             gas_used=getattr(r, "gas_used", None), eff_gas_price=getattr(r, "eff_gas_price", None),
                             amount_in=int(r.amount_in) if (ok and swapish) else None,
                             amount_out=int(r.amount_out) if (ok and swapish) else None,
                             err=(getattr(r, "note", "") or None))
    if mined or mined_before:
        for h in states:
            if h not in mined:
                store.dex_tx_resolve(con, h, DexTxState.REPLACED, err="на nonce замайнена другая наша транзакция")
        return None
    sts = set(states.values())
    if sts == {str(DexTxState.DROPPED)}:
        for h in states:
            store.dex_tx_resolve(con, h, DexTxState.DROPPED)
        return None
    if str(DexTxState.REPLACED) in sts and sts <= {str(DexTxState.REPLACED), str(DexTxState.DROPPED)}:
        for h in states:
            store.dex_tx_resolve(con, h, DexTxState.REPLACED, err=FOREIGN_NONCE)
        return f"nonce {nonce}: {FOREIGN_NONCE} — ключ кошелька у кого-то ещё?"
    for h, st in states.items():                # ещё в пуле или сеть не ответила — ждать, не трогать
        g = res[h][0]
        if st == DexTxState.SENT and g["state"] == DexTxState.SIGNED:
            store.dex_tx_sent(con, h)
        elif st != DexTxState.SENT and g["state"] != DexTxState.UNKNOWN:
            store.dex_tx_resolve(con, h, DexTxState.UNKNOWN)
    return f"nonce {nonce}: транзакция ещё в пуле или сеть не ответила — исход неизвестен"


def _refetch_amounts(con, spot, row: dict, tokens: tuple) -> dict | None:
    """MINED_OK без сумм (упали между чеком и записью сумм): перечитать чек и дописать суммы из логов."""
    r = dict(row)
    r["token_in"], r["token_out"] = tokens
    try:
        res = resolve_receipt(con, spot, r)
    except Exception as e:                     # noqa
        log.warning("сверка: чек %s не перечитан: %s", row["tx_hash"], redact(e))
        return row
    if res.status != 'ok' or str(getattr(res, "tx_state", "")) != DexTxState.MINED_OK or not res.amount_out:
        return row
    store.dex_tx_resolve(con, row["tx_hash"], DexTxState.MINED_OK, amount_in=int(res.amount_in),
                         amount_out=int(res.amount_out))
    return store.get_dex_tx(con, row["tx_hash"])
