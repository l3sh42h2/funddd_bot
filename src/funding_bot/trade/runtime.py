"""Выбор ног по сделке и профилю (ТЗ §3.2, CODE_INTEGRATION §2): resolve(сделка) → ноги ЕЁ связки, а не legs(sim).

- Связка сделки — из её замороженной спецификации (deals.inst_json schema 2: profile_id); сделка без неё —
  bsc_okx_aster (DQA9Q и все сделки до SOL). Сделка Solana без schema 2 BSC/Aster-ног не получит никогда.
- RuntimeRegistry вызывается как прежний legs(sim) — это ровно старая связка (побайтно прежний BSC-путь); ноги
  прочих связок — for_profile / for_deal, лениво: сборка SOL/HL — при первой нужде, её сбой — ProfileDown этой
  связки (BSC не падает), а сделка SOL с позицией остаётся видна в сверке как «не сверена», а не как «флэт».
- Сборка боевых ног SOL/HL (build_sol_legs) не читает окружение в dry, не трогает ключи Aster/EVM (M02) и ключи
  связки загружает один раз на процесс (load_sol_hl стирает приватные переменные из окружения).
"""
from __future__ import annotations
import json, logging, threading, time
from types import SimpleNamespace
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Callable, Mapping
from .keys import redact
from .owner import EVM_PROFILE_OF, LEGACY_PROFILE, RH_GATE, SOL_HL

log = logging.getLogger(__name__)
D = Decimal
SOL_CHAINS = frozenset({"sol", "solana", "solana-mainnet"})
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"     # только чтения dry (котировки без сборки): не боевой узел


def profile_of_inst_json(raw: Any) -> str | None:
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(d, dict) and d.get("schema") == 2 and isinstance(d.get("profile_id"), str):
        return d["profile_id"]
    return None


def profile_of_deal(deal: Mapping) -> str:
    """Связка сделки по её строке: schema 2 — profile_id спецификации; сеть Solana без неё — всё равно SOL (такую
    сделку store не создаёт; строка из чужих рук не должна получить ноги BSC/Aster)."""
    d = dict(deal)
    p = profile_of_inst_json(d.get("inst_json"))
    if p is not None:
        return p
    if str(d.get("chain") or "").strip().lower() in SOL_CHAINS:
        return SOL_HL
    # EVM-связки (спот OKX DEX × перп): связка — по (сеть, площадка перпа) сделки; прочее — bsc_okx_aster, как раньше
    return EVM_PROFILE_OF.get((str(d.get("chain") or "").strip().lower(), str(d.get("perp_venue") or "").strip().lower()),
                              LEGACY_PROFILE)


def is_sol_deal(deal: Mapping) -> bool:
    return profile_of_deal(deal) == SOL_HL


def hl_account_id(network: str, master: str, account: str, dex: str) -> str:
    """Scope счёта перпа (ТЗ §10): площадка/сеть/мастер/счёт/dex. Адреса HL — EVM, нижним регистром."""
    return ":".join(("hyperliquid", network, master.lower(), account.lower(), dex))


class ProfileDown(RuntimeError):
    """Ноги связки не собраны (конфигурация, ключи, сеть). Другие связки это не останавливает."""

    def __init__(self, profile: str, reason: str):
        super().__init__(f"{profile}: {reason}")
        self.profile, self.reason = profile, reason


@dataclass
class SolLegs:
    """Ноги связки sol_best_hyperliquid одной «правды»: sim=True — симуляция, False — боевые (или только чтение)."""
    spot: Any                         # sol_exec.SolanaExecutor | sim.SimSolSpot — один кошелёк, одна книга
    perp: Any                         # hyperliquid_trade.HyperliquidTrade | sim.SimHlPerp
    router: Any                       # spot_router.SpotRouter (или совместимый: select / presign_check)
    sim: bool
    account_id: str                   # scope счёта перпа (runtime.hl_account_id)
    wallet: str                       # кошелёк Solana (base58 как есть)
    native_px: Callable[[], D | None] = lambda: None     # SOL в USDC — для текстов
    native_obs: Callable[[], Any] = lambda: None         # fees.PriceObs SOL→USDC — для оценки расходов
    fee_rate: Callable[[], D | None] = lambda: None      # применимая taker-ставка HL; None — неизвестна (не 0)
    can_send: bool = False
    profile: str = SOL_HL
    block_height: Callable[[], int | None] = lambda: None   # высота блока Solana (срок blockhash перед подписью)

    @property
    def fill_venue(self) -> str:
        return ("sim:" if self.sim else "") + self.perp.venue


class RuntimeRegistry:
    """Реестр ног по связкам. __call__(sim) — прежний контракт legs(sim) (только bsc_okx_aster); for_deal — ноги
    связки сделки; фабрики новых связок ленивые, успешная сборка кэшируется, сбой — ProfileDown (повтор при
    следующем обращении)."""

    def __init__(self, legacy: Callable[[bool], Any] | None,
                 factories: Mapping[str, Callable[[bool], Any]] | None = None,
                 component_factories: Mapping[str, Mapping[str, Callable[[bool], Any]]] | None = None):
        self.legacy = legacy
        self.factories = dict(factories or {})
        self.component_factories = {p: dict(v) for p, v in (component_factories or {}).items()}
        self._cache: dict[tuple[str, bool], Any] = {}
        self._lock = threading.Lock()
        self.last_error: dict[str, str] = {}
        from .adapters.registry import production_registry
        self.adapters = production_registry()

    def __call__(self, sim: bool):
        if self.component_factories:
            return self.for_profile(LEGACY_PROFILE, bool(sim))
        return None if self.legacy is None else self.legacy(sim)

    def for_profile(self, profile: str, sim: bool):
        components = self.component_factories.get(profile)
        if components is not None:
            key = (profile, bool(sim))
            with self._lock:
                if key in self._cache:
                    return self._cache[key]
            try:
                spot = components['spot'](bool(sim))
                perp = components['perp'](bool(sim))
                if spot is None or perp is None:
                    return None
                legs = self._pair_components(profile, bool(sim), spot, perp)
            except Exception as e:
                why = f"{type(e).__name__}: {redact(e)}"[:300]
                self.last_error[profile] = why
                raise ProfileDown(profile, why) from None
            with self._lock:
                self._cache[key] = legs
                self.last_error.pop(profile, None)
            return legs
        if profile == LEGACY_PROFILE:
            if self.legacy is None:
                raise ProfileDown(profile, "связка не собрана в этом процессе")
            return self.legacy(bool(sim))
        f = self.factories.get(profile)
        if f is None:
            raise ProfileDown(profile, "связка не подключена в этом процессе")
        key = (profile, bool(sim))
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            try:
                legs = f(bool(sim))
            except Exception as e:     # noqa — сбой одной связки не роняет другие
                why = f"{type(e).__name__}: {redact(e)}"[:300]
                self.last_error[profile] = why
                log.warning("связка %s не собрана: %s", profile, why)
                raise ProfileDown(profile, why) from None
            if legs is not None:
                self._cache[key] = legs
                self.last_error.pop(profile, None)
            return legs

    def _pair_components(self, profile, sim, spot, perp):
        """Turn independent venue components into the legacy leg DTO."""
        if profile != SOL_HL:
            from .engine import Legs, NativePrice
            from .sim import SimPerp, SimSpot
            native = getattr(spot, 'native_usd', None)
            native = native or (lambda: None)
            s, p = (SimSpot(spot, native_px=native), SimPerp(perp)) if sim else (spot, perp)
            can_send = (not sim and getattr(spot, '_loaded_mode', 'dry') == 'live'
                        and getattr(perp, '_loaded_mode', 'dry') == 'live'
                        and getattr(spot, 'sender', None) is not None)
            return Legs(s, p, sim, native, can_send=can_send)
        ex, router, chain = spot
        hl, fee_rate = perp
        cfg = None
        # Component factories retain their owner loader for identity; use it
        # only for constructing the compatibility SolLegs envelope.
        owner = self.component_factories[profile]['spot']
        cfg = owner.owner_loader()
        wallet = cfg.get('wallets.sol_hl.solana_address')
        user = cfg.get('wallets.sol_hl.hl_user_address')
        account = cfg.get('wallets.sol_hl.hl_account_address')
        dex = cfg.get(f'profiles.{SOL_HL}.perp_dex') or 'para'
        network = cfg.get(f'profiles.{SOL_HL}.perp_network') or 'mainnet'
        from . import store
        from .fees import NATIVE_SOL, PriceObs
        from .hl_rules import to_dec
        from .sim import SimHlPerp, SimSolSpot
        from .solana import USDC_MINT
        def native_obs():
            try:
                mids = hl.http.info({'type': 'allMids'})
                return PriceObs(NATIVE_SOL, USDC_MINT, to_dec(mids['SOL'], 'SOL'), time.monotonic(), 'HL allMids SOL')
            except Exception:
                return None
        npx = lambda: (lambda o: None if o is None else o.price)(native_obs())
        acct = hl_account_id(network, user, account, dex)
        if sim:
            return SolLegs(SimSolSpot(ex, wallet=wallet), SimHlPerp(hl), router, True, acct, wallet,
                           npx, native_obs, fee_rate, False, block_height=chain.block_height)
        return SolLegs(ex, hl, router, False, acct, wallet, npx, native_obs, fee_rate,
                       can_send=(getattr(ex, '_loaded_mode', 'dry') == 'live'
                                 and getattr(hl, '_loaded_mode', 'dry') == 'live'
                                 and getattr(ex, 'signer', None) is not None
                                 and getattr(hl, 'signer', None) is not None),
                       block_height=chain.block_height)

    def for_deal(self, deal: Mapping):
        return self.for_profile(profile_of_deal(deal), bool(dict(deal)["sim"]))

    def compose(self, first_spec, second_spec, bindings_by_leg):
        """Compose exactly the two requested production adapters.

        Bindings are keyed by frozen ``LegSpec.leg_id``.  The peer is never
        passed to either factory; this keeps venue credentials and transports
        scoped to the leg that uses them.
        """
        from .adapters.context import AdapterContext
        context = AdapterContext()
        for spec in (first_spec, second_spec):
            try:
                binding = bindings_by_leg[spec.leg_id]
            except (KeyError, TypeError):
                try:
                    binding = bindings_by_leg[spec]
                except (KeyError, TypeError):
                    binding = None
            if binding is None:
                raise ProfileDown(getattr(spec, 'adapter_id', 'unknown'),
                                  'binding for requested leg is missing') from None
            context.add(spec, binding)
        return self.adapters.compose(first_spec, second_spec, context)

    def health(self) -> dict[str, str | None]:
        return {p: self.last_error.get(p) for p in (LEGACY_PROFILE, *self.factories)}


def legs_of(legs_fn: Any, deal: Mapping):
    """Ноги сделки для вызывающих со старым legs(sim): BSC-сделка — ровно legs_fn(sim), как раньше; сделка другой
    связки — только через реестр (без него — None: «не сверена», а не ноги BSC для чужой сделки)."""
    prof = profile_of_deal(deal)
    if prof == LEGACY_PROFILE:
        return legs_fn(bool(dict(deal)["sim"]))
    fn = getattr(legs_fn, "for_deal", None)   # сделка другой связки (SOL/HL, Robinhood × Gate) — ноги BSC ей не даём
    if fn is None:
        raise ProfileDown(prof, "связка не подключена в этом процессе")
    return fn(deal)


# --- боевая сборка ног SOL × HL ---------------------------------------------------------------------------------
class EvmGateFactory:
    """Compatibility alias for Robinhood spot + Gate. Production uses CredentialProvider;
    legacy_rt is retained only for older offline callers. Each leg keeps its loaded mode ceiling.
    """

    def __init__(self, owner_loader: Callable[[], Any], conns, holder, legacy_rt, *, environ=None, okx=None, rpc=None,
                 gate=None, credentials=None, keys_mode=None):
        self.owner_loader, self.conns, self.holder, self.rt = owner_loader, conns, holder, legacy_rt
        self.environ, self.okx, self.rpc, self.gate = environ, okx, rpc, gate
        self.credentials, self.keys_mode = credentials, keys_mode
        self._lock = threading.Lock()
        self._built: dict[bool, Any] = {}

    def _mode(self, cfg) -> str:
        from .keys import effective_mode
        rt_mode = self.keys_mode if self.credentials is not None else self.rt.mode if (self.rt is not None and getattr(self.rt, "keys", None) is not None) else "dry"
        return effective_mode(cfg.profile_mode(RH_GATE), rt_mode)

    def __call__(self, sim: bool):
        cfg = self.owner_loader()
        mode = self._mode(cfg)
        if not sim and mode == "dry":
            return None                  # боевых ног в dry нет (как legs(False) у BSC)
        with self._lock:
            if sim in self._built:
                return self._built[sim]
        legs = self._build(cfg, sim, mode)
        with self._lock:
            self._built[sim] = legs
        return legs

    def _build(self, cfg, sim: bool, mode: str):
        from ..okxdex import OkxDex
        from . import store, tconfig
        from .engine import Legs, NativePrice
        from .evm import EvmRpc, EvmWallet
        from .evm_swap import OkxEvmSpot
        from .gate_trade import GateTrade
        from .sim import SimPerp, SimSpot
        chain = "robinhood"
        okx = self.okx if self.okx is not None else OkxDex()
        rpc = self.rpc if self.rpc is not None else EvmRpc(tconfig.rpc_urls(chain, self.environ))
        native_px = NativePrice(okx, tconfig.chain_index(chain))
        wallet = cfg.get("wallets.rh_gate.evm_address")
        loader, conns = self.owner_loader, self.conns

        def mode_state():
            from .keys import effective_mode
            return effective_mode(loader().profile_mode(RH_GATE), mode), store.execution_paused(conns.get())

        if sim:
            perp_ro = self.gate if self.gate is not None else self._gate_reader(mode, mode_state)
            spot_ro = OkxEvmSpot(okx, rpc, self.holder, chain=chain, wallet=wallet or "0x" + "0" * 40,
                                 native_usd=native_px)
            return Legs(SimSpot(spot_ro, native_px=native_px, wallet_known=bool(wallet)), SimPerp(perp_ro), True,
                        native_px)
        k = self.credentials.evm(mode, wallet) if self.credentials is not None else getattr(self.rt, "keys", None)
        evm = getattr(k, "evm", None) if k is not None else None
        addr = getattr(k, "evm_address", None) if k is not None else None
        if mode == "live" and evm is None:
            raise RuntimeError("EVM-ключ старой связки не загружен — спот Robinhood подписать нечем")
        if not wallet:
            raise RuntimeError("wallets.rh_gate.evm_address не задан — не собираю")
        if (evm is not None or addr) and str(addr or "").lower() != str(wallet).lower():
            raise RuntimeError("wallets.rh_gate.evm_address не совпадает с адресом EVM-ключа (DEX_EVM_KEY) — не собираю")
        perp = self.gate if self.gate is not None else self._gate_trade(mode_state)
        clock = getattr(perp, "check_clock", None)
        if callable(clock):
            clock()                              # часы расходятся с Gate — связка не собирается (как у Aster на старте)
        sender = None
        if evm is not None:                      # readonly без EVM-ключа — ноги только для чтения (как build_runtime)

            def gate(in_flight: bool) -> None:
                k.gate(loader().profile_mode(RH_GATE), "send", paused=store.execution_paused(conns.get()), hedge=in_flight)

            sender = EvmWallet(rpc, tconfig.CHAIN_IDS[chain], evm, lambda row: store.dex_tx_signed(conns.get(), **row),
                               gate=gate, on_sent=lambda h: store.dex_tx_sent(conns.get(), h),
                               on_resolved=lambda h, st, info: store.dex_tx_resolve(conns.get(), h, st, **info),
                               chain=chain)
        spot = OkxEvmSpot(okx, rpc, self.holder, chain=chain, wallet=wallet, sender=sender, native_usd=native_px)
        return Legs(spot, perp, False, native_px, can_send=mode == "live" and sender is not None)

    def _gate_trade(self, mode_state):
        from .gate_trade import GateTrade
        if self.credentials is None:
            return GateTrade.from_env(mode_state, self.environ)
        key, secret = self.credentials.gate()
        return GateTrade(key.reveal(), secret.reveal(), mode_state=mode_state)

    def _gate_reader(self, mode: str, mode_state):
        """Нога Gate под симуляцию: в readonly/live — с ключами (подписанные чтения: маржа, позиция), как у BSC
        AsterTrade.from_keys; ключей нет или dry — только публичное."""
        from .gate_trade import GateTrade
        if mode != "dry":
            try:
                return self._gate_trade(mode_state)
            except Exception as e:             # noqa — без ключей симуляция всё равно строится, маржа «не прочитана»
                log.warning("Gate без ключей для симуляции: %s", redact(e))
        return GateTrade(mode_state=mode_state)


class EvmSpotFactory:
    """Independent EVM spot leg.  It never asks for a perpetual credential."""

    def __init__(self, owner_loader, conns=None, holder=None, *, profile=RH_GATE, chain='robinhood', credentials=None, keys_mode=None,
                 environ=None, okx=None, rpc=None, clock=time.time):
        self.owner_loader, self.conns, self.holder = owner_loader, conns, holder
        self.profile, self.chain = profile, chain
        self.credentials, self.keys_mode, self.environ = credentials, keys_mode, environ
        self.okx, self.rpc, self.clock = okx, rpc, clock
        self._built = {}

    def __call__(self, sim):
        cfg = self.owner_loader()
        mode = cfg.profile_mode(self.profile)
        if not sim and mode == 'dry':
            return None
        if sim in self._built:
            return self._built[sim]
        from ..okxdex import OkxDex
        from . import tconfig
        from .evm import EvmRpc, EvmWallet
        from .evm_swap import OkxEvmSpot
        from .engine import NativePrice
        okx = self.okx or OkxDex()
        rpc = self.rpc or EvmRpc(tconfig.rpc_urls(self.chain, self.environ))
        from . import owner as owner_mod, store
        wallet = cfg.get(owner_mod.EVM_WALLET_KEY[self.chain])
        native = NativePrice(okx, tconfig.chain_index(self.chain))
        loader, conns = self.owner_loader, self.conns
        def mode_state():
            from .keys import effective_mode
            loaded = getattr(self, '_loaded_mode', mode)
            paused = store.execution_paused(conns.get()) if conns is not None else False
            return effective_mode(loader().profile_mode(self.profile), loaded), paused
        sender = None
        if not sim and self.credentials is not None:
            cred = self.credentials.evm(mode, wallet)
            if mode == 'live' and cred.evm is not None:
                def gate(in_flight: bool):
                    cred.gate(loader().profile_mode(self.profile), 'send',
                              paused=mode_state()[1], hedge=in_flight)
                sender = EvmWallet(
                    rpc, tconfig.CHAIN_IDS[self.chain], cred.evm,
                    lambda row: store.dex_tx_signed(conns.get(), **row),
                    gate=gate,
                    on_sent=lambda h: store.dex_tx_sent(conns.get(), h),
                    on_resolved=lambda h, st, info: store.dex_tx_resolve(conns.get(), h, st, **info),
                    chain=self.chain)
        spot_mode = mode
        setattr(self, '_loaded_mode', spot_mode)
        spot = OkxEvmSpot(okx, rpc, self.holder, chain=self.chain,
                          wallet=wallet or '0x' + '0' * 40, sender=sender, native_usd=native)
        self._built[sim] = spot
        setattr(spot, '_loaded_mode', spot_mode)
        return spot


class SolSpotFactory:
    """Independent Solana spot leg; the HL credential scope is never touched."""

    def __init__(self, owner_loader, conns=None, *, credentials=None, keys_mode=None, environ=None,
                 rpc_session=None, jup_session=None, okx=None, clock=time.time,
                 mono=time.monotonic, sleep=time.sleep):
        self.owner_loader, self.conns = owner_loader, conns
        self.credentials, self.keys_mode, self.environ = credentials, keys_mode, environ
        self.rpc_session, self.jup_session, self.okx = rpc_session, jup_session, okx
        self.clock, self.mono, self.sleep = clock, mono, sleep
        self._built = {}

    def __call__(self, sim):
        cfg = self.owner_loader()
        mode = cfg.profile_mode(SOL_HL)
        if not sim and mode == 'dry':
            return None
        if sim in self._built:
            return self._built[sim]
        keys = None if mode == 'dry' else (self.credentials.solana(cfg, mode) if self.credentials else None)
        from . import store
        from .keys import effective_mode
        state = lambda: (effective_mode(self.owner_loader().profile_mode(SOL_HL), mode),
                         store.execution_paused(self.conns.get()) if self.conns is not None else False)
        result = build_sol_component(cfg, mode=mode, keys=keys,
                                     mode_state=state, rpc_session=self.rpc_session,
                                     jup_session=self.jup_session, okx=self.okx, clock=self.clock,
                                     mono=self.mono, sleep=self.sleep)
        if isinstance(result, tuple) and result:
            setattr(result[0], '_loaded_mode', mode)
        self._built[sim] = result
        return result


class AsterPerpFactory:
    """Independent Aster perpetual leg; EVM spot credentials are not loaded."""

    def __init__(self, owner_loader, conns=None, *, credentials=None, keys_mode=None, session=None,
                 clock=time.time, sleep=time.sleep):
        self.owner_loader, self.conns = owner_loader, conns
        self.credentials, self.keys_mode, self.session = credentials, keys_mode, session
        self.clock, self.sleep = clock, sleep
        self._built = {}

    def __call__(self, sim):
        cfg = self.owner_loader()
        mode = cfg.profile_mode(LEGACY_PROFILE)
        if not sim and mode == 'dry':
            return None
        if sim in self._built:
            return self._built[sim]
        from .aster_trade import AsterTrade
        from . import store
        from .keys import effective_mode
        state = lambda: (effective_mode(self.owner_loader().profile_mode(LEGACY_PROFILE), mode),
                         store.execution_paused(self.conns.get()) if self.conns is not None else False)
        if mode == 'dry' or self.credentials is None:
            perp = AsterTrade(mode_state=state, session=self.session, now=self.clock, sleep=self.sleep)
        else:
            a = self.credentials.aster(cfg, mode)
            perp = AsterTrade.from_keys(SimpleNamespace(mode=a.mode, aster_user=a.aster_user,
                                                        aster_signer=a.aster_signer, aster=a.aster,
                                                        gate=a.gate),
                                        state, session=self.session, now=self.clock, sleep=self.sleep)
        setattr(perp, '_loaded_mode', mode)
        self._built[sim] = perp
        return perp


class GatePerpFactory:
    """Independent Gate perpetual leg; no EVM, Aster, SOL or HL credentials."""

    def __init__(self, owner_loader, conns=None, *, credentials=None, keys_mode=None, session=None,
                 clock=time.time, sleep=time.sleep):
        self.owner_loader, self.conns = owner_loader, conns
        self.credentials, self.keys_mode, self.session = credentials, keys_mode, session
        self.clock, self.sleep = clock, sleep
        self._built = {}

    def __call__(self, sim):
        cfg = self.owner_loader()
        mode = cfg.profile_mode(RH_GATE)
        if not sim and mode == 'dry':
            return None
        if sim in self._built:
            return self._built[sim]
        from .gate_trade import GateTrade
        from . import store
        from .keys import effective_mode
        state = lambda: (effective_mode(self.owner_loader().profile_mode(RH_GATE), mode),
                         store.execution_paused(self.conns.get()) if self.conns is not None else False)
        if mode == 'dry' or self.credentials is None:
            perp = GateTrade(mode_state=state, session=self.session, now=self.clock, sleep=self.sleep)
        else:
            key, secret = self.credentials.gate()
            perp = GateTrade(key.reveal(), secret.reveal(), mode_state=state, session=self.session,
                             now=self.clock, sleep=self.sleep)
        setattr(perp, '_loaded_mode', mode)
        self._built[sim] = perp
        return perp


class HlPerpFactory:
    """Independent Hyperliquid perpetual leg; no Solana secret is required."""

    def __init__(self, owner_loader, conns=None, *, credentials=None, keys_mode=None, session=None,
                 clock=time.time, sleep=time.sleep):
        self.owner_loader, self.conns = owner_loader, conns
        self.credentials, self.keys_mode, self.session = credentials, keys_mode, session
        self.clock, self.sleep = clock, sleep
        self._built = {}

    def __call__(self, sim):
        cfg = self.owner_loader()
        mode = cfg.profile_mode(SOL_HL)
        if not sim and mode == 'dry':
            return None
        if sim in self._built:
            return self._built[sim]
        keys = None if mode == 'dry' else (self.credentials.hyperliquid(cfg, mode) if self.credentials else None)
        from . import store
        from .keys import effective_mode
        state = lambda: (effective_mode(self.owner_loader().profile_mode(SOL_HL), mode),
                         store.execution_paused(self.conns.get()) if self.conns is not None else False)
        result = build_hl_component(cfg, self.conns, mode=mode, keys=keys,
                                    mode_state=state, hl_session=self.session,
                                    clock=self.clock, sleep=self.sleep)
        setattr(result[0], '_loaded_mode', mode)
        self._built[sim] = result
        return result


class SolFactory:
    """Ленивая сборка ног связки для RuntimeRegistry. Ключи (readonly/live) грузятся один раз на процесс."""

    def __init__(self, owner_loader: Callable[[], Any], conns, *, keys_mode: str | None, environ=None,
                 hl_session=None, rpc_session=None, jup_session=None, okx=None, clock=time.time,
                 mono=time.monotonic, sleep=time.sleep, paused: Callable[[], bool] | None = None, credentials=None):
        self.owner_loader, self.conns, self.keys_mode, self.environ = owner_loader, conns, keys_mode, environ
        self.hl_session, self.rpc_session, self.jup_session, self.okx = hl_session, rpc_session, jup_session, okx
        self.clock, self.mono, self.sleep = clock, mono, sleep
        self.paused = paused
        self.credentials = credentials
        self._keys: Any = None
        self._keys_loaded = False
        self._lock = threading.Lock()
        self._built: dict[bool, SolLegs] = {}

    def _mode(self, cfg) -> str:
        from .keys import effective_mode
        return effective_mode(cfg.profile_mode(SOL_HL), self.keys_mode) if self.keys_mode else "dry"

    def keys(self, cfg):
        from . import keys as K
        with self._lock:
            if not self._keys_loaded:
                mode = self._mode(cfg)
                if mode == "dry":
                    self._keys = None
                elif self.credentials is not None:
                    # Load the two scopes independently.  ``sol_hl`` remains a
                    # compatibility alias, but production assembly must not
                    # make one component depend on that aggregate loader.
                    sol = self.credentials.solana(cfg, mode)
                    hl = self.credentials.hyperliquid(cfg, mode)
                    self._keys = K.SolHlKeys(
                        mode=K.effective_mode(sol.mode, hl.mode),
                        solana_address=sol.solana_address,
                        hl_user=hl.hl_user, hl_account=hl.hl_account,
                        hl_vault=hl.hl_vault, hl_agent_address=hl.hl_agent_address,
                        sol=sol.sol, hl=hl.hl, jupiter=sol.jupiter, okx=sol.okx,
                        rpc_primary=sol.rpc_primary, rpc_secondary=sol.rpc_secondary,
                        rpc_ws=sol.rpc_ws)
                else:
                    self._keys = K.load_sol_hl(cfg, mode, self.environ)
                self._keys_loaded = True
            return self._keys

    def __call__(self, sim: bool) -> SolLegs | None:
        cfg = self.owner_loader()
        mode = self._mode(cfg)
        if not sim and mode == "dry":
            return None                  # боевых ног в dry нет (как legs(False) у BSC)
        with self._lock:
            if sim in self._built:
                return self._built[sim]
        legs = build_sol_legs(cfg, self.conns, sim=sim, mode=mode, keys=self.keys(cfg), owner_loader=self.owner_loader,
                              hl_session=self.hl_session, rpc_session=self.rpc_session, jup_session=self.jup_session,
                              okx=self.okx, clock=self.clock, mono=self.mono, sleep=self.sleep, paused=self.paused)
        with self._lock:
            self._built[sim] = legs
        return legs


def _routing(cfg, profile=SOL_HL):
    from .spot_router import RouteLimits, RoutingPolicy
    sec = lambda p: {k[len(p):]: v for k, v in cfg.values.items() if k.startswith(p) and v is not None}  # noqa: E731
    pol = RoutingPolicy.from_config(sec("routing.solana."))
    raw = {k: (int(v) if isinstance(v, D) and v == v.to_integral_value() else v)
           for k, v in sec(f"limits.{profile}.").items()}
    lim = RouteLimits.from_config({k: v for k, v in raw.items() if k in RouteLimits.__dataclass_fields__})
    return pol, lim


def build_hl_component(cfg, conns, *, mode, keys, mode_state, profile=SOL_HL,
                       public_identity=None, public_scope=None, profile_params=None,
                       network=None, account=None,
                       instrument_registry=None, allowed_instruments=None,
                       hl_session=None, clock=time.time, sleep=time.sleep):
    """One perpetual leg. Does not require a Solana key, wallet, router or executor."""
    from . import store
    from . import hl_rules as R
    from .hyperliquid_trade import NETWORKS as HL_NETWORKS, HlJournal, HlSigner, HyperliquidTrade
    wallet_section = "sol_hl" if profile == SOL_HL else profile
    w = lambda k: cfg.get(f"wallets.{wallet_section}.{k}")
    identity = dict(profile_params or {})
    identity.update(public_scope or {})
    identity.update(public_identity or {})
    user = identity.get('user') or identity.get('hl_user') or w("hl_user_address")
    account = account or identity.get('account') or identity.get('hl_account') or w("hl_account_address")
    if not user or not account:
        raise ValueError("Hyperliquid account identity missing")
    dex = identity.get('dex') or cfg.get(f"profiles.{profile}.perp_dex") or "para"
    network = network or identity.get('network') or cfg.get(f"profiles.{profile}.perp_network") or "mainnet"
    from . import instruments as I
    registry_value = instrument_registry if instrument_registry is not None else cfg.get(f"profiles.{profile}.instrument_registry")
    reg = I.load_registry(I.registry_path(registry_value))
    allowed = tuple(allowed_instruments if allowed_instruments is not None else
                    (cfg.get(f"profiles.{profile}.allowed_instruments") or ()))
    fulls = sorted({s.perp.fullcoin for s in reg.latest() if s.profile_id == profile and s.perp.dex == dex
                    and s.identity.status != "revoked" and (not allowed or s.instrument_id in allowed)})
    if len(fulls) != 1:
        raise ValueError(f"реестр {reg.path}: рынков профиля на dex {dex} — {len(fulls)} (первый выпуск: ровно один)")
    live = mode == "live" and keys is not None and keys.hl is not None
    signer = journal = None
    if live:
        signer = HlSigner(keys.hl, agent=keys.hl_agent_address, master=user, account=account, network=network)
        journal = HlJournal(store.connect(getattr(conns, "path", None)), now=clock)
    hl = HyperliquidTrade(account, fullcoin=fulls[0], master=user, network=network, signer=signer,
                          journal=journal, keys=keys, mode_state=mode_state, session=hl_session,
                          base=cfg.get("perp.hyperliquid.api_base") or HL_NETWORKS[network], now=clock, sleep=sleep)
    fees_cache: dict = {}

    def fee_rate() -> D | None:
        try:
            if "cross" not in fees_cache:
                fees_cache["cross"] = hl.acct.user_fees()["cross"]
            return R.taker_rate_estimate(fees_cache["cross"], hl.identity())
        except Exception as e:           # noqa — ставка неизвестна: кандидаты не проходят в live (perp_fee_unknown)
            log.warning("sol-hl: ставка HL не прочитана: %s", type(e).__name__)
            return None

    return hl, fee_rate


def build_sol_component(cfg, *, mode, keys, mode_state, profile=SOL_HL,
                        public_identity=None, public_scope=None, profile_params=None,
                        wallet=None, network=None, genesis=None,
                        rpc_session=None, jup_session=None, okx=None,
                        clock=time.time, mono=time.monotonic, sleep=time.sleep):
    """One Solana spot leg. Futures account/venue is not an input."""
    from .jupiter_spot import BASE as JUP_BASE, JupiterSpot
    from .okx_sol_spot import OkxSolSpot
    from .sol_exec import RpcChain, SolanaExecutor
    from .sol_route_validator import solana_tools
    from .solana import MAINNET_GENESIS, USDC_MINT
    from .solana.rpc import RpcPool, SolanaRpc
    from .spot_router import SolanaTools, SpotRouter
    identity = dict(profile_params or {})
    identity.update(public_scope or {})
    identity.update(public_identity or {})
    wallet = wallet or identity.get('wallet') or identity.get('solana_address') or cfg.get(f"wallets.{profile}.solana_address")
    if not wallet:
        raise ValueError("Solana wallet identity missing")
    genesis = genesis or identity.get('genesis') or cfg.get("spot.solana.expected_genesis_hash") or MAINNET_GENESIS
    live = mode == "live" and keys is not None and keys.sol is not None
    urls = [u for u in ((keys.rpc_primary, keys.rpc_secondary) if keys is not None else ()) if u]
    rpcs = [SolanaRpc(u, name=n, expected_genesis=genesis, session=rpc_session, sleep=sleep)
            for u, n in zip(urls, ("primary", "secondary"))] or \
        [SolanaRpc(PUBLIC_RPC, name="public", expected_genesis=genesis, session=rpc_session, sleep=sleep)]
    pool = RpcPool(rpcs)
    policy, limits = _routing(cfg, profile)
    chain = RpcChain(pool)
    tools, validator = SolanaTools(), None
    if urls:                               # сборка и симуляция — только на своих узлах (не публичном)
        lam = limits.max_network_fee_lamports_per_tx
        rent = limits.max_rent_locked_lamports
        tools, validator = solana_tools(pool, chain, limits=limits, policy=policy,
                                        native_budget_lamports=None if lam is None or rent is None else lam + rent)
    jrps, orps = cfg.get("providers.jupiter.main_rps"), cfg.get("providers.okx.rps")
    jup = JupiterSpot(jup_session, api_key=keys.jupiter.reveal() if keys is not None and keys.jupiter else "",
                      base=cfg.get("providers.jupiter.api_base") or JUP_BASE, rps=float(jrps) if jrps else None,
                      policy=policy, tools=tools, clock=mono, wall=clock, environ={})
    if okx is None:
        from ..okxdex import OkxDex
        o = keys.okx if keys is not None else None
        okx = OkxDex(key=o.key.reveal() if o else "", secret=o.secret.reveal() if o else "",
                     passphrase=o.passphrase.reveal() if o else "", rps=float(orps) if orps else None)
    router = SpotRouter([jup, OkxSolSpot(okx, policy=policy, limits=limits, tools=tools, clock=mono, wall=clock)],
                        policy=policy, limits=limits, clock=mono, wall=clock)
    ex = SolanaExecutor(wallet=wallet, genesis=genesis, signer=keys.sol if live else None, endpoints=pool.endpoints,
                        chain=chain, validator=validator, mode_state=mode_state, clock=clock, sleep=sleep,
                        tip_accounts=policy.tip_recipients, keys=keys)
    return ex, router, chain


def build_sol_legs(cfg, conns, *, sim: bool, mode: str, keys, owner_loader: Callable[[], Any], hl_session=None,
                   rpc_session=None, jup_session=None, okx=None, clock=time.time, mono=time.monotonic,
                   sleep=time.sleep, paused: Callable[[], bool] | None = None) -> SolLegs:
    """Ноги связки из owner.toml и ключей. Сеть при сборке не читается. Нет адресов [wallets.sol_hl] — отказ
    (ProfileDown выше): даже симуляция считает маржу и балансы ЭТОГО счёта, а не пустого адреса."""
    from . import store
    from .fees import NATIVE_SOL, PriceObs
    from . import hl_rules as R
    from .sim import SimHlPerp, SimSolSpot
    from .solana import MAINNET_GENESIS, USDC_MINT
    w = lambda k: cfg.get(f"wallets.sol_hl.{k}")          # noqa: E731
    wallet, user, account = w("solana_address"), w("hl_user_address"), w("hl_account_address")
    if not (wallet and user and account):
        raise ValueError("в owner.toml [wallets.sol_hl] не заданы solana_address / hl_user_address / "
                         "hl_account_address")
    dex = cfg.get(f"profiles.{SOL_HL}.perp_dex") or "para"
    network = cfg.get(f"profiles.{SOL_HL}.perp_network") or "mainnet"
    genesis = cfg.get("spot.solana.expected_genesis_hash") or MAINNET_GENESIS
    loader = owner_loader

    def mode_state():
        return loader().profile_mode(SOL_HL), bool(paused()) if paused is not None else \
            store.execution_paused(conns.get())

    hl, fee_rate = build_hl_component(cfg, conns, mode=mode, keys=keys, mode_state=mode_state,
                                     hl_session=hl_session, clock=clock, sleep=sleep)
    ex, router, chain = build_sol_component(cfg, mode=mode, keys=keys, mode_state=mode_state,
                                           rpc_session=rpc_session, jup_session=jup_session, okx=okx,
                                           clock=clock, mono=mono, sleep=sleep)
    live = mode == "live" and keys is not None and keys.hl is not None and keys.sol is not None
    def native_obs():
        try:
            mids = hl.http.info({"type": "allMids"})
            return PriceObs(NATIVE_SOL, USDC_MINT, R.to_dec(mids["SOL"], "SOL"), mono(), "HL allMids SOL")
        except Exception as e:           # noqa — нет цены: расходы в SOL «неизвестны», не 0
            log.warning("sol-hl: цена SOL не получена: %s", type(e).__name__)
            return None

    npx = lambda: (lambda o: None if o is None else o.price)(native_obs())      # noqa: E731
    acct = hl_account_id(network, user, account, dex)
    if sim:
        return SolLegs(SimSolSpot(ex, wallet=wallet), SimHlPerp(hl), router, True, acct, wallet, npx, native_obs,
                       fee_rate, False, block_height=chain.block_height)
    return SolLegs(ex, hl, router, False, acct, wallet, npx, native_obs, fee_rate, can_send=live,
                   block_height=chain.block_height)
