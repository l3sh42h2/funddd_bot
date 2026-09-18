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
    from .native import EvmSpotAdapter, SolanaSpotAdapter, FuturesAdapter
    registry = AdapterRegistry()
    # 'lighter' зарегистрирован (PATCHNOTES/lighter-futures-adapter-20260918.md) — регистрация сама по себе не
    # разрешает live (см. docs/migration/ADAPTER_GUIDE.md): нужны профиль/лимиты и Bindings поверх реального
    # trade/lighter_trade.LighterTrade, а его подписанные операции сами отказывают до решения владельца о
    # подписи Lighter (SigningNotAvailable). Ни один профиль/фабрика в runtime.py на 'lighter' пока не ссылается.
    for name, cls in (('okx_evm', EvmSpotAdapter), ('sol_best', SolanaSpotAdapter),
                      ('aster', FuturesAdapter), ('gate', FuturesAdapter), ('hyperliquid', FuturesAdapter),
                      ('lighter', FuturesAdapter)):
        registry.register(name, lambda spec, context, cls=cls: cls(spec, context.for_leg(spec)))
    return registry
