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
                 factories: Mapping[str, Callable[[bool], Any]] | None = None):
        self.legacy = legacy
        self.factories = dict(factories or {})
        self._cache: dict[tuple[str, bool], Any] = {}
        self._lock = threading.Lock()
        self.last_error: dict[str, str] = {}
        from .adapters.registry import production_registry
        self.adapters = production_registry()

    def __call__(self, sim: bool):
        return None if self.legacy is None else self.legacy(sim)

    def for_profile(self, profile: str, sim: bool):
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

    def for_deal(self, deal: Mapping):
        return self.for_profile(profile_of_deal(deal), bool(dict(deal)["sim"]))

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
            return effective_mode(loader().profile_mode(RH_GATE), mode), store.is_paused(conns.get())

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
                k.gate(loader().profile_mode(RH_GATE), "send", paused=store.is_paused(conns.get()), hedge=in_flight)

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
                self._keys = None if mode == "dry" else (self.credentials.sol_hl(cfg, mode) if self.credentials is not None
                                                       else K.load_sol_hl(cfg, mode, self.environ))
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


def _routing(cfg):
    from .spot_router import RouteLimits, RoutingPolicy
    sec = lambda p: {k[len(p):]: v for k, v in cfg.values.items() if k.startswith(p) and v is not None}  # noqa: E731
    pol = RoutingPolicy.from_config(sec("routing.solana."))
    raw = {k: (int(v) if isinstance(v, D) and v == v.to_integral_value() else v)
           for k, v in sec(f"limits.{SOL_HL}.").items()}
    lim = RouteLimits.from_config({k: v for k, v in raw.items() if k in RouteLimits.__dataclass_fields__})
    return pol, lim


def build_hl_component(cfg, conns, *, mode, keys, mode_state, hl_session=None,
                       clock=time.time, sleep=time.sleep):
    """One perpetual leg. Does not require a Solana key, wallet, router or executor."""
    from . import store
    from . import hl_rules as R
    from .hyperliquid_trade import NETWORKS as HL_NETWORKS, HlJournal, HlSigner, HyperliquidTrade
    w = lambda k: cfg.get(f"wallets.sol_hl.{k}")
    user, account = w("hl_user_address"), w("hl_account_address")
    if not user or not account:
        raise ValueError("Hyperliquid account identity missing")
    dex = cfg.get(f"profiles.{SOL_HL}.perp_dex") or "para"
    network = cfg.get(f"profiles.{SOL_HL}.perp_network") or "mainnet"
    from . import instruments as I
    reg = I.load_registry(I.registry_path(cfg.get(f"profiles.{SOL_HL}.instrument_registry")))
    allowed = tuple(cfg.get(f"profiles.{SOL_HL}.allowed_instruments") or ())
    fulls = sorted({s.perp.fullcoin for s in reg.latest() if s.profile_id == SOL_HL and s.perp.dex == dex
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


def build_sol_component(cfg, *, mode, keys, mode_state, rpc_session=None, jup_session=None,
                        okx=None, clock=time.time, mono=time.monotonic, sleep=time.sleep):
    """One Solana spot leg. Futures account/venue is not an input."""
    from .jupiter_spot import BASE as JUP_BASE, JupiterSpot
    from .okx_sol_spot import OkxSolSpot
    from .sol_exec import RpcChain, SolanaExecutor
    from .sol_route_validator import solana_tools
    from .solana import MAINNET_GENESIS, USDC_MINT
    from .solana.rpc import RpcPool, SolanaRpc
    from .spot_router import SolanaTools, SpotRouter
    wallet = cfg.get("wallets.sol_hl.solana_address")
    if not wallet:
        raise ValueError("Solana wallet identity missing")
    genesis = cfg.get("spot.solana.expected_genesis_hash") or MAINNET_GENESIS
    live = mode == "live" and keys is not None and keys.sol is not None
    urls = [u for u in ((keys.rpc_primary, keys.rpc_secondary) if keys is not None else ()) if u]
    rpcs = [SolanaRpc(u, name=n, expected_genesis=genesis, session=rpc_session, sleep=sleep)
            for u, n in zip(urls, ("primary", "secondary"))] or \
        [SolanaRpc(PUBLIC_RPC, name="public", expected_genesis=genesis, session=rpc_session, sleep=sleep)]
    pool = RpcPool(rpcs)
    policy, limits = _routing(cfg)
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
            store.is_paused(conns.get())

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
