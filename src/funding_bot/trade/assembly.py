"""Single compatibility composition root for core and legacy trader.

Profile aliases select configuration and limits. M4 replaces their business flows.
"""
from . import owner as owner_mod
from .engine import build_runtime
from .keys import KeysError, redact
from .runtime import (RuntimeRegistry, SolFactory, EvmGateFactory, ProfileDown,
                      EvmSpotFactory, SolSpotFactory, AsterPerpFactory,
                      GatePerpFactory, HlPerpFactory)
from .adapters.credentials import CredentialProvider


def build_trader_legs(cfg, conns, holder, env, *, build=None, factory=None):
    credentials = CredentialProvider(env)
    custom = build is not None
    build = build or build_runtime
    legacy_on = cfg.profile_enabled(owner_mod.LEGACY_PROFILE)
    sol_on = cfg.profile_enabled(owner_mod.SOL_HL)
    rh_on = cfg.profile_enabled(owner_mod.RH_GATE)
    kw = {} if custom else {'credentials': credentials}
    failure = None
    try:
        rt = build(cfg, conns, holder=holder, environ=env, **({} if legacy_on else {'mode': 'dry'}), **kw)
        if not custom and legacy_on and rt.mode == 'live':
            rt.live.perp.check_clock()
    except Exception as e:
        if not (sol_on or rh_on):
            from .aster_trade import AsterError
            if isinstance(e, AsterError):
                raise KeysError(redact(e)) from None
            raise
        failure = redact(e)
        rt = build(cfg, conns, holder=holder, environ=env, mode='dry', **kw)
    keys_mode = rt.mode if rt.keys is not None else (cfg.mode if (sol_on or rh_on) else None)
    factories = {}
    if sol_on:
        factories[owner_mod.SOL_HL] = (factory or SolFactory)(
            owner_mod.load, conns, keys_mode=keys_mode, environ=env,
            **({} if factory else {'credentials': credentials}))
    if rh_on:
        factories[owner_mod.RH_GATE] = EvmGateFactory(owner_mod.load, conns, holder, rt, environ=env,
                                                   credentials=credentials, keys_mode=cfg.mode)
    def legacy(sim):
        if failure and not sim:
            raise ProfileDown(owner_mod.LEGACY_PROFILE, failure)
        return rt.sim if sim else rt.live
    reg = RuntimeRegistry(legacy, factories)
    # Component factories are exposed for the common coordinator.  The legacy
    # paired runtime above remains the compatibility callable; these factories
    # load only the credential scope owned by their leg.
    component_factories = {
        owner_mod.LEGACY_PROFILE: {
            'spot': EvmSpotFactory(owner_mod.load, conns, holder, profile=owner_mod.LEGACY_PROFILE,
                                   chain='bsc', credentials=credentials, keys_mode=keys_mode, environ=env),
            'perp': AsterPerpFactory(owner_mod.load, conns, credentials=credentials, keys_mode=keys_mode),
        },
        owner_mod.SOL_HL: {
            'spot': SolSpotFactory(owner_mod.load, conns, credentials=credentials, keys_mode=keys_mode, environ=env),
            'perp': HlPerpFactory(owner_mod.load, conns, credentials=credentials, keys_mode=keys_mode),
        },
        owner_mod.RH_GATE: {
            'spot': EvmSpotFactory(owner_mod.load, conns, holder, profile=owner_mod.RH_GATE,
                                   chain='robinhood', credentials=credentials, keys_mode=keys_mode, environ=env),
            'perp': GatePerpFactory(owner_mod.load, conns, credentials=credentials, keys_mode=keys_mode),
        },
    }
    reg.component_factories = ({p: v for p, v in component_factories.items()
                                if cfg.profile_enabled(p) and not (p == owner_mod.LEGACY_PROFILE and failure)}
                               if not custom else {})
    if failure:
        reg.last_error[owner_mod.LEGACY_PROFILE] = failure
    mode = cfg.mode if (sol_on or rh_on) else rt.mode
    return rt, reg, keys_mode, mode, legacy_on
