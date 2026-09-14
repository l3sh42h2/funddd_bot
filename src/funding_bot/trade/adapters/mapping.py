"""Read-only v1 mapping of historical InstrumentSpec. Never updates its JSON/hash."""
from decimal import Decimal as D
from .contracts import LegSpec, Capabilities, AdapterError, ErrorKind
from ..types import InstrumentSpec


def map_legacy(deal, *, spot_account, perp_account, filters, spot_quote, network,
               spot_tick, metadata_revision, quote_decimals=None):
    inst = InstrumentSpec.from_json(deal['inst_json'])
    if not inst.verified or not (inst.ident_ev or inst.identity_hash):
        raise AdapterError(ErrorKind.IDENTITY, 'historical instrument has no verified identity proof')
    if inst.perp_account and inst.perp_account != perp_account:
        raise AdapterError(ErrorKind.IDENTITY, 'historical perpetual account differs')
    family = 'solana' if inst.chain == 'solana' else 'evm'
    if family == 'solana' and network != inst.genesis_hash:
        raise AdapterError(ErrorKind.IDENTITY, 'Solana genesis differs from frozen record')
    proof = inst.identity_hash or inst.ident_ev
    # Shared identity is scoped to this proven mapping, not the display ticker.
    asset = f'{inst.chain}:{inst.token}'
    h = inst.inst_hash()
    common = dict(asset_id=asset, identity_evidence=proof, legacy_hash=h, metadata_revision=metadata_revision)
    spot = LegSpec(str(deal['id']) + ':spot', 'inventory', 'long',
                   'sol_best' if family == 'solana' else 'okx_evm', 'sol_best' if family == 'solana' else 'okx',
                   spot_account, inst.token, multiplier=inst.fs, step=D(1).scaleb(-inst.token_dec), tick=spot_tick,
                   quote_currency=spot_quote, settlement_currency=spot_quote,
                   capabilities=Capabilities('dex', 'spot', family, amount=True), network=network,
                   decimals=inst.token_dec, quote_decimals=quote_decimals, **common)
    if not inst.quote_asset:
        raise AdapterError(ErrorKind.IDENTITY, 'perpetual quote currency unknown')
    perp = map_perpetual(deal, account=perp_account, filters=filters,
                         metadata_revision=metadata_revision)
    return spot, perp


def map_perpetual(deal, *, account, filters, metadata_revision):
    """Map one frozen leg without requiring a spot wallet, route or network RPC."""
    inst = InstrumentSpec.from_json(deal['inst_json'])
    if deal.get('symbol', inst.perp_symbol) != inst.perp_symbol or deal.get('perp_venue', inst.perp_venue) != inst.perp_venue:
        raise AdapterError(ErrorKind.IDENTITY, 'deal differs from frozen perpetual instrument')
    if not inst.verified or not (inst.ident_ev or inst.identity_hash):
        raise AdapterError(ErrorKind.IDENTITY, 'historical instrument has no verified identity proof')
    if inst.perp_account and inst.perp_account != account:
        raise AdapterError(ErrorKind.IDENTITY, 'historical perpetual account differs')
    if not inst.quote_asset:
        raise AdapterError(ErrorKind.IDENTITY, 'perpetual quote currency unknown')
    common = dict(asset_id=f'{inst.chain}:{inst.token}',
                  identity_evidence=inst.identity_hash or inst.ident_ev,
                  legacy_hash=inst.inst_hash(), metadata_revision=metadata_revision)
    return LegSpec(str(deal['id']) + ':perp', 'hedge', 'short', inst.perp_venue, inst.perp_venue,
                   account, inst.perp_symbol, multiplier=inst.fp, step=filters.step, tick=filters.tick,
                   quote_currency=inst.quote_asset, settlement_currency=inst.quote_asset,
                   margin_currency=inst.quote_asset,
                   capabilities=Capabilities('dex' if inst.perp_venue == 'hyperliquid' else 'cex',
                                             'perpetual', short=True, reduce_only=True),
                   network=inst.perp_network, subaccount=inst.perp_dex, **common)
