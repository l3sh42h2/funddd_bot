"""Executable synthetic evm_spot adapter; native transport is supplied by the test.

Register this one factory and its LegSpec. It receives only its own bindings.
This example never constructs credentials or enables live execution.
"""
from funding_bot.trade.adapters.native import NativeAdapter
from funding_bot.trade.adapters.contracts import Result, AdapterError, ErrorKind


class EvmSpot(NativeAdapter):
    market_kind = 'spot'
    network_family = 'evm'

    def normalize(self, native, side):
        if (not isinstance(native, Result) or native.version != 2 or
                native.spec_hash != self.spec.fingerprint or native.scope != self.spec.scope or
                native.leg_id != self.spec.leg_id):
            raise AdapterError(ErrorKind.IDENTITY, 'receipt differs from frozen leg')
        return native


def register(registry, key):
    registry.register(key, lambda spec, context: EvmSpot(spec, context.for_leg(spec)))

