"""C1 checks for the independent composition boundary."""
from types import SimpleNamespace as NS
from decimal import Decimal as D

import pytest

from funding_bot.trade.runtime import RuntimeRegistry, ProfileDown
from funding_bot.trade.adapters.contracts import Capabilities, LegSpec


def _spec(adapter, leg, direction):
    spot = adapter == "okx_evm"
    return LegSpec(leg, "inventory" if spot else "hedge", direction, adapter, adapter,
                   "acct:" + adapter, "ASSET", "asset:ASSET", "identity:verified", D(1), D(".01"), D(".001"),
                   "USDC", "USDC", Capabilities("dex" if spot else "cex",
                   "spot" if spot else "perpetual", "evm" if spot else None,
                   short=not spot, reduce_only=not spot),
                   network="chain:1" if spot else None, decimals=6, metadata_revision="r1")


def test_runtime_compose_registers_only_requested_leg_bindings():
    # Native adapters only need their own binding.  The peer's venue methods
    # are intentionally absent, proving compose does not create a shared bag.
    from test_migration_m3 import Context
    first, second = _spec("okx_evm", "spot", "long"), _spec("gate", "perp", "short")
    registry = RuntimeRegistry(None)
    context = Context()
    pair = registry.compose(first, second, {first.leg_id: context.for_leg(first), second.leg_id: context.for_leg(second)})
    assert pair.first.describe() == first
    assert pair.second.describe() == second


def test_runtime_compose_refuses_missing_scoped_binding():
    registry = RuntimeRegistry(None)
    first, second = _spec("okx_evm", "spot", "long"), _spec("gate", "perp", "short")
    with pytest.raises(ProfileDown, match="binding"):
        registry.compose(first, second, {first.leg_id: NS()})


def test_sol_and_gate_factories_do_not_load_peer_credentials(monkeypatch):
    from funding_bot.trade import runtime

    class Cfg:
        mode = "readonly"
        def profile_mode(self, _profile): return "readonly"
        def get(self, key):
            return {"wallets.sol_hl.solana_address": "sol-wallet"}.get(key)

    class Credentials:
        def __init__(self): self.calls = []
        def solana(self, cfg, mode): self.calls.append("solana"); return NS(mode=mode, sol="sol")
        def hyperliquid(self, cfg, mode): self.calls.append("hyperliquid"); raise AssertionError("HL peer loaded")
        def gate(self): self.calls.append("gate"); return (NS(reveal=lambda: "key"), NS(reveal=lambda: "secret"))

    credentials = Credentials()
    monkeypatch.setattr(runtime, "build_sol_component", lambda *a, **kw: "SOL-SPOT")
    assert runtime.SolSpotFactory(lambda: Cfg(), credentials=credentials)(True) == "SOL-SPOT"
    assert credentials.calls == ["solana"]

    gate = runtime.GatePerpFactory(lambda: Cfg(), credentials=credentials, session=NS(headers={}))
    gate(True)
    assert credentials.calls == ["solana", "gate"]


def test_aster_factory_preserves_loaded_mode_gate_before_network_call():
    from funding_bot.trade import keys as K, runtime

    class Cfg:
        def profile_mode(self, _profile): return "readonly"

    class Credentials:
        def aster(self, cfg, mode):
            def gate(owner_mode, action, paused=False, hedge=False):
                K.gate(K.effective_mode(owner_mode, mode), action,
                       paused=paused, hedge=hedge)
            return NS(mode=mode, aster_user="0x" + "1" * 40,
                      aster_signer="0x" + "2" * 40,
                      aster=NS(address="0x" + "2" * 40), gate=gate)

    perp = runtime.AsterPerpFactory(lambda: Cfg(), credentials=Credentials())(False)
    with pytest.raises(K.ModeForbidden):
        perp._gate("send", False)
