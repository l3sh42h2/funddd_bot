"""Composition of independent legs. An adapter factory never receives its peer."""
from dataclasses import dataclass
from typing import Callable
from .contracts import AdapterError, ErrorKind, LegSpec


@dataclass(frozen=True)
class Pair:
    first: object
    second: object


class AdapterRegistry:
    def __init__(self):
        self._factories: dict[str, Callable] = {}

    def register(self, adapter_id, factory):
        if not adapter_id or adapter_id in self._factories:
            raise AdapterError(ErrorKind.CONFIG, 'duplicate or empty adapter registration')
        self._factories[adapter_id] = factory

    def build(self, spec: LegSpec, context):
        factory = self._factories.get(spec.adapter_id)
        if factory is None:
            raise AdapterError(ErrorKind.UNSUPPORTED, 'adapter is not registered')
        adapter = factory(spec, context)
        if adapter.describe() != spec or adapter.capabilities() != spec.capabilities:
            raise AdapterError(ErrorKind.IDENTITY, 'adapter description differs from frozen leg')
        return adapter

    def compose(self, first: LegSpec, second: LegSpec, context):
        if first.leg_id == second.leg_id or first.scope == second.scope:
            raise AdapterError(ErrorKind.INVALID, 'two distinct leg scopes required')
        if first.asset_id != second.asset_id or first.direction == second.direction:
            raise AdapterError(ErrorKind.IDENTITY, 'hedge needs proven same asset and opposite exposures')
        return Pair(self.build(first, context), self.build(second, context))


def production_registry():
    from .native import CexSpotAdapter, EvmSpotAdapter, SolanaSpotAdapter, FuturesAdapter
    registry = AdapterRegistry()
    # "binance"/"binance_spot" (18.09): first real (non-example) CEX legs — no profile references them yet
    # (owner.toml has no matching profile/limits section for a CEX-spot × CEX-perp pair), so registering the
    # factory here is inert by ADAPTER_GUIDE.md's own rule: a registry entry is not a live permission.
    for name, cls in (('okx_evm', EvmSpotAdapter), ('sol_best', SolanaSpotAdapter),
                      ('aster', FuturesAdapter), ('gate', FuturesAdapter), ('hyperliquid', FuturesAdapter),
                      ('binance', FuturesAdapter), ('binance_spot', CexSpotAdapter)):
        registry.register(name, lambda spec, context, cls=cls: cls(spec, context.for_leg(spec)))
    return registry
