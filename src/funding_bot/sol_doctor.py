"""Связка SOL×HL, веха M1 — readonly-доктор: `funding_bot sol-hl doctor | quote-compare | hl-preflight | record`.

Только чтение. Ни одной подписи и отправки: Hyperliquid — только POST /info, Solana — только методы чтения
SolanaRpc (sendTransaction туда не пускается), провайдеры — котировки и инструкции без подписи. Приватные ключи не
загружаются: в readonly keys.load_sol_hl их только стирает из окружения, в dry окружение не читается вовсе. Ключи
провайдеров берутся из окружения и не печатаются: весь вывод проходит keys.redact, RPC показывается хостом. OKX —
существующий okxdex.OkxDex с общим файлом темпа runtime/okxdex.pace (иначе замер сломал бы темп коллектора и
BSC-трейдера). Серия record пишется в отдельный SQLite (runtime/sol_hl_record.sqlite), не в trade.db.

Неизвестное не превращается в ноль: провал чтения — «?» с причиной и причина live_ready=false, в серии — NULL и
текст ошибки. live_ready=true не обещает исполнения: состояние меняется, проверка повторяется перед каждой операцией.
Метки: ✓ проверено · ✗ мешает live · ? не проверено (тоже мешает) · «·» справка.
"""
from __future__ import annotations
import argparse, logging, os, signal, sqlite3, struct, threading, time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Callable, Sequence

from . import config
from .trade import hl_rules as R
from .trade import instruments as I
from .trade import keys as K
from .trade import owner as O
from .trade import spot_router as SR
from .trade.fees import NATIVE_SOL, PriceObs, human
from .trade.hyperliquid_trade import DEFAULT_FULLCOIN, HOUR_MS, NETWORKS, HlAccount, HlHttp, HlMarket
from .trade.jupiter_spot import JUP_PROGRAM
from .trade.okx_sol_spot import OKX_ROUTER
from .trade.solana import ANSEM_MINT, MAINNET_GENESIS, TOKEN_2022_PROGRAM, TOKEN_PROGRAM, USDC_MINT
from .trade.solana import accounts as A
from .trade.solana.b58 import b58encode
from .trade.solana.rpc import GenesisMismatch, RpcError, RpcUnavailable, SolanaRpc

log = logging.getLogger(__name__)
D = Decimal

SOL_HL = O.SOL_HL
DEFAULT_INSTRUMENT = "ansem_sol_para_v1"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"      # только когда RPC владельца не загружены (dry, нет адресов)
# объём показа, пока limits.max_clip_usdc пуст: пилот владельца 13.09 ($150). Не лимит и не разрешение
PILOT_CLIP_USD = D(150)
RECORD_DB = "sol_hl_record.sqlite"
FORBIDDEN_DB = frozenset({"trade.db", "funding_bot.db"})    # базы трейдера и коллектора — серия туда не пишется
BPF_UPGRADEABLE = "BPFLoaderUpgradeab1e11111111111111111111111"
PROGRAMS = (("Jupiter v6", JUP_PROGRAM), ("OKX router", OKX_ROUTER))
READ_DEADLINE_S = 10.0      # срок сбора котировок, пока collection_deadline_ms пуст (техника замера, не деньги)
HL_EVERY_S, ROUTES_EVERY_S, RPC_EVERY_S = 15, 600, 60      # план M1: стакан раз в 10–30 с, пути раз в 10 мин
FUNDING_DAYS = 7
BOOK_LEVELS = 20
SLOT_S = D("0.4")           # номинал слота — только для «≈ дней назад» в справке
INFO = "i"
_MARK = {True: "✓", False: "✗", None: "?", INFO: "·"}
_LIVE_TEXT = {"not_simulated": "не симулирован", "not_validated": "не проверен валидатором",
              "limit_missing": "нет лимита", "hedge_unchecked": "пара не проверена", "rent_unknown": "rent неизвестен",
              "price_impact_unknown": "impact неизвестен", "perp_fee_unknown": "ставка HL неизвестна",
              "margin_unknown": "маржа HL неизвестна", "margin_insufficient": "маржи HL не хватает",
              "block_height_unknown": "высота блока неизвестна", "blockhash_unknown": "blockhash неизвестен",
              "recipient_unverified": "получатель не проверен", "source_unverified": "источник не проверен"}


# --- вывод ----------------------------------------------------------------------------------------
class Report:
    """Секции строк с метками. ✗ и ? — причины live_ready=false; ✓ и «·» — нет."""

    def __init__(self, title: str):
        self.title = title
        self.sections: list[tuple[str, list[tuple[Any, str]]]] = []
        self.blockers: list[str] = []
        self.final = ""

    def section(self, name: str) -> None:
        self.sections.append((name, []))

    def add(self, ok: Any, text: str) -> None:
        self.sections[-1][1].append((ok, text))
        if ok is False or ok is None:
            self.blockers.append(text)

    def check(self, text: str, problems: Sequence[str]) -> None:
        """Строка факта: без проблем — ✓; с проблемами — справкой, а каждая проблема — своей строкой ✗."""
        if not problems:
            self.add(True, text)
            return
        self.add(INFO, text)
        for p in problems:
            self.add(False, p)

    @property
    def ready(self) -> bool:
        return not self.blockers

    def finish(self, what: str = "live_ready") -> None:
        n = len(dict.fromkeys(self.blockers))
        if what == "live_ready":
            self.final = ("live_ready=true — состояние может измениться: проверка повторяется перед каждой операцией"
                          if not n else f"live_ready=false — причин {n} (✗ и ? выше)")
        else:
            self.final = f"{what}: готово" if not n else f"{what}: не готово — причин {n} (✗ и ? выше)"

    def render(self) -> str:
        out = [self.title]
        for name, lines in self.sections:
            if lines:
                out.append(f"— {name}")
                out += [f"{_MARK[ok]} {t}" for ok, t in lines]
        if self.final:
            out.append(self.final)
        return K.redact("\n".join(out))


def _short(a: Any) -> str:
    s = str(a or "")
    return "—" if not s else s if len(s) <= 12 else f"{s[:4]}…{s[-4:]}"


def _err(e: BaseException) -> str:
    return K.redact(f"{type(e).__name__}: {e}")[:200]


def _n(x: Any, places: int | None = None) -> str:
    """Decimal строкой без экспоненты; places — округление показа (не расчёта)."""
    if x is None:
        return "—"
    d = D(x)
    if places is not None:
        d = d.quantize(D(1).scaleb(-places))
        return format(d, "f")
    return format(d.normalize(), "f")


def _money(x: D | None) -> str:
    if x is None:
        return "—"
    a = abs(x)
    if a >= 10 ** 6:
        return f"${_n(x / 10 ** 6, 2)}M"
    if a >= 10 ** 3:
        return f"${_n(x / 10 ** 3, 1)}k"
    return f"${_n(x, 2)}"


def _pct_h(rate: D) -> str:
    return f"{rate * 100:+.4f}%/ч"


def _s(x: Any) -> str | None:
    return None if x is None else str(x)


def _safe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception:       # noqa — справочное чтение: неудача = «неизвестно», не падение доктора
        return None


def usd_raw(usd: D, decimals: int) -> int:
    """USDC человеческие → raw вниз (объём показа; лишнего не просим)."""
    return int((D(usd) * D(10) ** decimals).to_integral_value(rounding=ROUND_DOWN))


def book_depth(book, usd: D, sz_decimals: int) -> dict:
    """Шорт (продажа по бидам) и закрытие (покупка по аскам) на usd: (контракты, VWAP, отход от лучшей цены в бп).
    Глубины 20 уровней не хватает — None, а не «последний уровень бесконечен»."""
    step = R.sz_step(sz_decimals)
    out = {}
    for side, levels in (("sell", book.bids), ("buy", book.asks)):
        if not levels:
            out[side] = None
            continue
        best = levels[0][0]
        q = SR.floor_step(D(usd) / best, step)
        w = SR.walk_book(levels, q) if q > 0 else None
        out[side] = None if w is None else (q, w[0], abs(w[0] - best) / best * 10000)
    return out


def _role(body: Any) -> tuple[str, str | None]:
    """userRole → (роль, чей: мастер агента/субаккаунта). Неизвестная форма — роль строкой, владелец None."""
    if not isinstance(body, dict):
        return str(body)[:30], None
    d = body.get("data") if isinstance(body.get("data"), dict) else {}
    who = d.get("user") or d.get("master")
    return str(body.get("role")), (str(who).lower() if who else None)


def _group_keys(keys: Sequence[str]) -> str:
    by: dict[str, list[str]] = {}
    for k in keys:
        sec, _, name = k.rpartition(".")
        by.setdefault(sec, []).append(name)
    return "; ".join(f"[{sec}] {', '.join(v)}" for sec, v in by.items())


def _reason_text(r: str) -> str:
    code, _, arg = r.partition(":")
    if code == "capability":
        return "только показ"
    t = _LIVE_TEXT.get(code)
    return (f"{t} {arg}" if arg else t) if t else r


# --- контекст -------------------------------------------------------------------------------------
@dataclass
class Deps:
    """Точки подмены для тестов. В бою всё по умолчанию: свои requests.Session, общий файл темпа OKX, часы ОС."""
    hl_session: Any = None
    rpc_session: Any = None
    jup_session: Any = None
    okx_session: Any = None
    okx_pace: Any = None            # None — общий runtime/okxdex.pace (как у коллектора и трейдера); False — процесс
    jup_gate: Any = None
    providers: Sequence | None = None
    registry_path: Any = None
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep


class Ctx:
    """Что доктор читает: owner.toml, реестр, публичные адреса, ключи API (без приватных), клиенты чтения."""

    def __init__(self, cfg: O.OwnerCfg, env, deps: Deps, instrument_id: str = DEFAULT_INSTRUMENT):
        self.cfg, self.deps, self.instrument_id = cfg, deps, instrument_id
        self.mode = cfg.profile_mode(SOL_HL)
        # готовность — ДО загрузки: load_sol_hl стирает приватные переменные из окружения
        self.readiness = K.profile_readiness(cfg, SOL_HL, env)
        self.creds: K.SolHlKeys | None = None
        self.creds_error: str | None = None
        self.okx_project = ""
        if self.mode != "dry":                  # dry: окружение не читается (как keys.load_sol_hl)
            try:
                self.creds = K.load_sol_hl(cfg, "readonly", env)
            except K.KeysError as e:
                self.creds_error = K.redact(str(e))
            self.okx_project = (env.get("OKX_DEX_PROJECT") or "").strip()
        w = lambda k: cfg.get(f"wallets.sol_hl.{k}")        # noqa: E731 — публичные адреса owner.toml
        self.wallet, self.hl_user, self.hl_account = w("solana_address"), w("hl_user_address"), w("hl_account_address")
        self.hl_agent, self.hl_vault = w("hl_agent_address"), w("hl_vault_address")
        self.config_errors: list[str] = []
        self._load_registry()
        self.genesis = cfg.get("spot.solana.expected_genesis_hash") or MAINNET_GENESIS
        self._make_rpcs()
        base = cfg.get("perp.hyperliquid.api_base") or NETWORKS["mainnet"]
        self.http = HlHttp(base, session=deps.hl_session, now=deps.wall, sleep=deps.sleep)
        self.market = HlMarket(self.http, self.fullcoin, now=deps.wall)
        self.account = (HlAccount(self.http, self.hl_account, fullcoin=self.fullcoin, now=deps.wall)
                        if self.hl_account else None)
        self.policy, self.limits = self._routing()
        self._fees: dict | None = None
        self._providers: list | None = None

    # --- реестр ---
    def _load_registry(self) -> None:
        cfg, d = self.cfg, self.deps
        self.spec: I.InstrumentSpec | None = None
        self.registry_error: str | None = None
        self.registry_file: str | None = None
        try:
            path = (Path(d.registry_path) if d.registry_path
                    else I.registry_path(cfg.get(f"profiles.{SOL_HL}.instrument_registry")))
            reg = I.load_registry(path)
            self.registry_file = Path(reg.path).name
            if reg.sha256 is None:
                self.registry_error = f"{self.registry_file}: файла нет"
            else:
                self.spec = reg.get(self.instrument_id)
        except I.RegistryError as e:
            self.registry_error = K.redact(str(e))
        s = self.spec
        # без записи — сверка по фактам ТЗ (mint ANSEM, para:ANSEM, Fs=Fp=1); live без записи невозможен
        self.symbol = s.display_symbol if s else "ANSEM"
        self.mint, self.mint_program, self.mint_decimals = ((s.spot.mint, s.spot.token_program, s.spot.decimals)
                                                            if s else (ANSEM_MINT, TOKEN_2022_PROGRAM, 6))
        self.mint_exts = tuple(s.spot.extensions) if s else I.MINT_EXTENSIONS_V1
        self.quote_mint, self.quote_program, self.quote_decimals = (
            (s.quote.mint, s.quote.token_program, s.quote.decimals) if s else (USDC_MINT, TOKEN_PROGRAM, 6))
        self.fullcoin = s.perp.fullcoin if s else DEFAULT_FULLCOIN
        self.fs, self.fp = (s.units.fs, s.units.fp) if s else (D(1), D(1))

    # --- RPC ---
    def _make_rpcs(self) -> None:
        c = self.creds
        urls = [(u, n) for u, n in ((c.rpc_primary, "primary"), (c.rpc_secondary, "secondary")) if u] if c else []
        self.rpc_public = not urls
        if not urls:
            urls = [(PUBLIC_RPC, "public")]
        self.rpcs: list[SolanaRpc] = []
        self.rpc_errors: list[str] = []
        for u, n in urls:
            try:
                self.rpcs.append(SolanaRpc(u, name=n, expected_genesis=self.genesis, session=self.deps.rpc_session,
                                           clock=self.deps.clock, sleep=self.deps.sleep))
            except ValueError as e:
                self.rpc_errors.append(f"{n}: {e}")
        self._rpc: SolanaRpc | None = None

    def rpc(self) -> SolanaRpc | None:
        """Первый узел с верным genesis (чужая сеть не используется никогда)."""
        if self._rpc is None:
            for r in self.rpcs:
                try:
                    r.check_genesis()
                except (GenesisMismatch, RpcUnavailable, RpcError):
                    continue
                self._rpc = r
                break
        return self._rpc

    # --- политика и лимиты владельца ---
    def _sec(self, prefix: str) -> dict:
        return {k[len(prefix):]: v for k, v in self.cfg.values.items() if k.startswith(prefix) and v is not None}

    def _routing(self) -> tuple[SR.RoutingPolicy, SR.RouteLimits]:
        try:
            pol = SR.RoutingPolicy.from_config(self._sec("routing.solana."))
        except ValueError as e:
            self.config_errors.append(f"[routing.solana]: {e}")
            pol = SR.RoutingPolicy()
        # замер: один раунд сбора — повторная сессия тратила бы квоту OKX, общую с коллектором и трейдером
        pol = replace(pol, max_collection_rounds=1)
        raw = {k: (int(v) if isinstance(v, D) and v == v.to_integral_value() else v)
               for k, v in self._sec(f"limits.{SOL_HL}.").items()}
        try:
            lim = SR.RouteLimits.from_config(raw)
        except ValueError as e:
            self.config_errors.append(f"[limits.{SOL_HL}]: {e}")
            lim = SR.RouteLimits()
        return pol, lim

    def limit(self, name: str) -> Any:
        return self.cfg.get(f"limits.{SOL_HL}.{name}")

    def clip_usd(self, override: D | None = None) -> D:
        if override is not None:
            return D(override)
        v = self.limit("max_clip_usdc")
        return D(v) if v is not None else PILOT_CLIP_USD

    def leverage(self) -> D | None:
        v = self.cfg.get("perp.hyperliquid.leverage")
        return None if v is None else D(v)

    def slippage_bps(self) -> int | None:
        v = self.limit("max_spot_slippage_bps")
        return int(v) if v is not None and D(v) == D(v).to_integral_value() else None

    # --- HL ---
    def ref(self) -> R.AssetRef:
        """Привязка рынка: кэш адаптера RULES_TTL_S (60 с) — серия на сутки видит смену меты, а не первый снимок."""
        return self.market.identity()

    def taker_rate(self) -> D | None:
        if self.account is None:
            return None
        if self._fees is None:
            self._fees = self.account.user_fees()
        return R.taker_rate_estimate(self._fees["cross"], self.ref())

    def margin_view(self) -> R.MarginView | None:
        if self.account is None:
            return None
        return self.account.margin(self.ref().collateral_token)

    def sol_price(self) -> PriceObs | None:
        """SOL в USDC для оценки сети и rent — середина перпа SOL на HL (одна цена на сбор; оценка, не исполнение)."""
        try:
            mids = self.http.info({"type": "allMids"})
            return PriceObs(NATIVE_SOL, self.quote_mint, R.to_dec(mids["SOL"], "SOL"), self.deps.clock(),
                            "HL allMids SOL")
        except Exception as e:      # noqa — нет цены: расходы в SOL останутся «неизвестны», не 0
            log.warning("sol-hl: цена SOL не получена: %s", type(e).__name__)
            return None

    def hedge_factory(self, side: str) -> Callable[[], SR.HedgeContext]:
        """Стакан HL берётся ПОСЛЕ сбора котировок (свежий к оценке пары); маржа — только для входа."""
        def make() -> SR.HedgeContext:
            ref = self.ref()
            book = self.market.book(BOOK_LEVELS)
            fee = _safe(self.taker_rate)
            avail = None
            if side == "buy":
                mv = _safe(self.margin_view)
                avail = mv.available if mv is not None else None
                if avail is not None and avail < 0:
                    avail = D(0)
            params = SR.PairParams(fs=self.fs, fp=self.fp, step=R.sz_step(ref.sz_decimals), perp_fee_rate=fee,
                                   min_notional=R.MIN_NOTIONAL_USD, leverage=self.leverage(),
                                   margin_reserve=self.limit("min_hl_margin_reserve_usdc"), available_margin=avail)
            return SR.HedgeContext(book=book, params=params, now_wall=self.deps.wall())
        return make

    # --- Solana ---
    def ata_rent(self, rpc: SolanaRpc | None, asset: SR.AssetRef) -> int | None:
        """0 — наш ATA есть; число — rent его создания (у узла); None — неизвестно."""
        if rpc is None or not self.wallet:
            return None
        try:
            if A.read_token_account(rpc, A.ata(self.wallet, asset.mint, asset.program)) is not None:
                return 0
            exts = self.mint_exts if asset.mint == self.mint else ()
            return A.ata_rent_lamports(rpc, asset.program, exts)
        except Exception:       # noqa
            return None

    # --- провайдеры ---
    def providers(self) -> list:
        if self.deps.providers is not None:
            return list(self.deps.providers)
        if self._providers is None:
            from .okxdex import OkxDex
            from .trade.jupiter_spot import BASE as JUP_BASE, JupiterSpot
            from .trade.okx_sol_spot import OkxSolSpot
            c, cfg, d = self.creds, self.cfg, self.deps
            jrps, orps = cfg.get("providers.jupiter.main_rps"), cfg.get("providers.okx.rps")
            # api_key "" (не None): адаптер не лезет в окружение сам — в dry ключ не читается
            jup = JupiterSpot(d.jup_session, api_key=c.jupiter.reveal() if c and c.jupiter else "",
                              base=cfg.get("providers.jupiter.api_base") or JUP_BASE,
                              rps=float(jrps) if jrps else None, policy=self.policy, gate=d.jup_gate, clock=d.clock,
                              wall=d.wall, environ={})
            o = c.okx if c else None
            okx = OkxDex(d.okx_session, key=o.key.reveal() if o else "", secret=o.secret.reveal() if o else "",
                         passphrase=o.passphrase.reveal() if o else "", project=self.okx_project,
                         rps=float(orps) if orps else None, pace_path=d.okx_pace)
            self._providers = [jup, OkxSolSpot(okx, policy=self.policy, limits=self.limits, clock=d.clock,
                                               wall=d.wall)]
        return self._providers


# --- сравнение маршрутов ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Compare:
    side: str                               # buy: USDC → токен (вход); sell: токен → USDC (выход)
    amount_raw: int
    slippage_bps: int | None
    req: SR.QuoteRequest | None = None
    decision: SR.Decision | None = None
    error: str | None = None
    price: PriceObs | None = None
    notes: tuple[str, ...] = ()


def compare(ctx: Ctx, side: str, amount_raw: int, *, slippage_bps: int | None = None,
            deadline_s: float | None = None) -> Compare:
    """Три пути на один и тот же exactIn-объём через SpotRouter.select (как будет в движке), один раунд сбора."""
    if side not in ("buy", "sell"):
        raise ValueError(f"side: buy или sell, а не {side!r}")
    slip = slippage_bps if slippage_bps is not None else ctx.slippage_bps()
    cmp = Compare(side, amount_raw, slip)
    if not ctx.wallet:
        return replace(cmp, error="нет адреса кошелька (wallets.sol_hl.solana_address): сборка маршрута просит taker")
    if slip is None:
        return replace(cmp, error=f"не задан limits.{SOL_HL}.max_spot_slippage_bps (целое) — или укажите --slippage-bps")
    q = SR.AssetRef(ctx.quote_mint, ctx.quote_program, ctx.quote_decimals, "USDC")
    t = SR.AssetRef(ctx.mint, ctx.mint_program, ctx.mint_decimals, ctx.symbol)
    inp, out = (q, t) if side == "buy" else (t, q)
    rpc = ctx.rpc()
    rent = tuple((a.mint, ctx.ata_rent(rpc, a)) for a in (inp, out))
    dl = deadline_s if deadline_s is not None else (ctx.limits.collection_deadline_ms / 1000
                                                    if ctx.limits.collection_deadline_ms else READ_DEADLINE_S)
    try:
        req = SR.QuoteRequest(side="entry" if side == "buy" else "exit", input=inp, output=out,
                              amount_in_raw=amount_raw, wallet=ctx.wallet, slippage_bps=slip, genesis_hash=ctx.genesis,
                              deadline_mono=ctx.deps.clock() + dl, purpose="mark",
                              input_account=A.ata(ctx.wallet, inp.mint, inp.program),
                              output_account=A.ata(ctx.wallet, out.mint, out.program), account_rent=rent)
    except ValueError as e:
        return replace(cmp, error=f"запрос не собран: {e}")
    px = ctx.sol_price()
    notes = () if px else ("цены SOL нет — сеть и rent в USDC не оценены",)
    router = SR.SpotRouter(ctx.providers(), policy=ctx.policy, limits=ctx.limits, clock=ctx.deps.clock,
                           wall=ctx.deps.wall)
    bh = (lambda: _safe(lambda: rpc.block_height("confirmed"))) if rpc is not None else None
    dec = router.select(req, prices={NATIVE_SOL: px} if px else {}, block_height=bh, hedge=ctx.hedge_factory(side))
    return replace(cmp, req=req, decision=dec, price=px, notes=notes)


def render_compare(ctx: Ctx, cmp: Compare) -> list[str]:
    buy = cmp.side == "buy"
    in_sym, out_sym = ("USDC", ctx.symbol) if buy else (ctx.symbol, "USDC")
    in_dec = ctx.quote_decimals if buy else ctx.mint_decimals
    head = (f"{'покупка' if buy else 'продажа'} {ctx.symbol}: вход {_n(human(cmp.amount_raw, in_dec))} {in_sym} "
            f"({cmp.amount_raw} raw) · кошелёк {_short(ctx.wallet)} · slippage "
            f"{'—' if cmp.slippage_bps is None else cmp.slippage_bps} бп")
    lines = [head]
    if cmp.error:
        return lines + [f"✗ {cmp.error}"]
    d, now = cmp.decision, ctx.deps.clock()
    for x in d.ranked:
        c = x.cand
        mark = "✓" if x.eligible else "·" if x.previewable else "✗"
        parts = [f"{mark} {c.path}" + (" (только показ)" if c.path == "jupiter_order_v2" else "")]
        parts.append(f"выход {_n(human(c.expected_out_raw, c.output_decimals)) if c.expected_out_raw else '—'} {out_sym}")
        if c.effective_min_out:
            parts.append(f"мин. {_n(human(c.effective_min_out, c.output_decimals))}")
        if x.metric is not None:
            parts.append(f"{_n(x.metric, 8)} USDC за {ctx.symbol}" if buy else f"нетто {_n(x.metric, 6)} USDC")
        if x.valuation is not None:
            parts.append("расходы " + (f"{_n(x.valuation.total, 6)} USDC" if x.valuation.total is not None
                                       else "неизвестны"))
        if c.price_impact_bps is not None:
            parts.append(f"impact {_n(c.price_impact_bps, 1)} бп")
        if x.pair is not None and x.pair.basis_bps is not None:
            parts.append(f"базис к шорту {_n(x.pair.basis_bps, 1)} бп")
        parts.append(f"возраст {int((now - c.received_mono) * 1000)} мс")
        if c.latency_ms is not None:
            parts.append(f"ответ {c.latency_ms} мс")
        lines.append(" · ".join(parts))
        if c.hard_reasons:
            lines.append("   исключён: " + ", ".join(c.hard_reasons))
        live = [r for r in c.reasons if SR.live_only(r)]
        if live:
            lines.append("   для live: " + ", ".join(dict.fromkeys(_reason_text(r) for r in live)))
    for u in d.unavailable:
        lines.append(f"✗ {u.path}: недоступен — {u.reason}" + (f" ({K.redact(u.detail)})" if u.detail else ""))
    w = d.winner or d.preview_winner
    if d.winner is not None:
        lines.append(f"победитель: {d.winner.path} (исполнимый, {d.status})")
    elif w is not None:
        lines.append(f"победитель показа: {w.path} — в live не исполним ({'; '.join(d.reasons) or d.status})")
    else:
        lines.append("победителя нет: " + "; ".join(d.reasons or (d.status,)))
    wr = next((x for x in d.ranked if x.cand is w), None)
    if wr is not None and wr.pair is not None and wr.pair.qty is not None and wr.pair.vwap is not None:
        lines.append(f"   шорт HL: {_n(wr.pair.qty)} контр., VWAP {_n(wr.pair.vwap, 6)}"
                     + (f", разница пары {_n(wr.pair.edge, 4)} USDC" if wr.pair.edge is not None else ""))
    if d.note:
        lines.append("   " + d.note)
    lines += [f"   ! {x}" for x in d.warnings]
    if cmp.price is not None:
        lines.append(f"· SOL {_n(cmp.price.price)} USDC ({cmp.price.source}) — только для оценки сети и rent")
    lines += [f"· {n}" for n in cmp.notes]
    return lines


# --- проверки доктора ----------------------------------------------------------------------------
def _header(ctx: Ctx, what: str) -> str:
    c = ctx.cfg
    src = f"sha256 {c.sha256[:12]}" if c.sha256 else "файла нет"
    return (f"sol-hl {what} · {SOL_HL} · режим {ctx.mode} · связка {'включена' if ctx.readiness.enabled else 'выключена'}"
            f" · owner.toml схема {c.schema_version} ({src}) · trade.db не открывается")


def _check_rpc(ctx: Ctx, rep: Report) -> None:
    rep.section("Solana RPC")
    c, cfg = ctx.creds, ctx.cfg
    if ctx.mode == "dry":
        rep.add(INFO, "режим dry: окружение не читается — RPC владельца и ключи провайдеров не проверены, чтения через "
                      "публичный mainnet-beta")
    else:
        if ctx.creds_error:
            rep.add(False, f"ключи и адреса связки (readonly): {ctx.creds_error}")
        n1, n2 = cfg.env_name("spot.solana.rpc_primary_env"), cfg.env_name("spot.solana.rpc_secondary_env")
        if ctx.rpc_public:
            rep.add(False, f"RPC владельца не загружены ({n1}, {n2}) — чтения через публичный mainnet-beta")
        elif c is not None and c.rpc_secondary is None:
            rep.add(False, f"резервный RPC не задан ({n2}) — неизвестный исход отправки не с чем сверить")
    hosts = [r.host for r in ctx.rpcs]
    if len(set(hosts)) != len(hosts):
        rep.add(False, "основной и резервный RPC на одном хосте — не независимые источники")
    for e in ctx.rpc_errors:
        rep.add(False, f"RPC {e}")
    if not cfg.get("spot.solana.expected_genesis_hash"):
        rep.add(INFO, "expected_genesis_hash в owner.toml пуст — сверка с genesis mainnet-beta")
    for r in ctx.rpcs:
        try:
            g = r.check_genesis()
            slot = r.slot("confirmed")
            ms = r.stats.last_ms
            height = r.block_height("finalized")
        except GenesisMismatch as e:
            rep.add(False, f"{r.label}: {e}")
            continue
        except (RpcUnavailable, RpcError) as e:
            rep.add(None, f"{r.label}: недоступен — {_err(e)}")
            continue
        try:
            healthy, behind = r.health()
        except (RpcUnavailable, RpcError):
            healthy, behind = None, None
        txt = f"{r.label}: genesis {_short(g)} совпал · слот {slot} · высота {height} · {ms} мс"
        if healthy is False:
            rep.add(False, txt + " · узел нездоров" + (f", отстаёт на {behind} слотов" if behind else ""))
        else:
            rep.add(True, txt + ("" if healthy else " · getHealth не ответил"))


def _identity(rep: Report, s: I.InstrumentSpec, now: float) -> None:
    i = s.identity
    if i.status not in I.LIVE_IDENTITY:
        rep.add(False, f"identity {i.status} — для live нужен verified_source или reviewed_override")
    elif i.expires_at is None:
        rep.add(False, f"identity {i.status}: нет срока expires_at")
    elif I.ts_epoch(i.expires_at) <= now:
        rep.add(False, f"identity {i.status}: срок истёк {i.expires_at}")
    else:
        left = int((I.ts_epoch(i.expires_at) - now) // 86400)
        who = f"{i.reviewed_by}: {i.review_reason}" if i.status == "reviewed_override" else "прямое доказательство"
        rep.add(True, f"identity {i.status} ({who}) · до {i.expires_at[:10]}, осталось {left} д")


def _program_deploy(rpc: SolanaRpc, program: str) -> tuple[int | None, str | None, str | None]:
    """(слот последнего деплоя, upgrade authority, примечание) по счетам BPF Upgradeable Loader."""
    v, _ = rpc.account_info(program, encoding="base64")
    if v is None:
        raise A.AccountError("счёта программы нет")
    if v.get("owner") != BPF_UPGRADEABLE:
        return None, None, f"загрузчик {_short(v.get('owner'))} — не обновляемая программа"
    b = A.account_bytes(v)
    if len(b) < 36 or struct.unpack_from("<I", b, 0)[0] != 2:
        raise A.AccountError("счёт программы не Program")
    pd = b58encode(b[4:36])
    res = rpc.call("getAccountInfo", [pd, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 45},
                                           "commitment": "confirmed"}])
    val = res.get("value") if isinstance(res, dict) else None
    if val is None:
        return None, None, "programdata узлом не отдаётся"
    d = A.account_bytes(val)
    if len(d) < 13 or struct.unpack_from("<I", d, 0)[0] != 3:
        raise A.AccountError("programdata не ProgramData")
    auth = b58encode(d[13:45]) if d[12] == 1 and len(d) >= 45 else None
    return struct.unpack_from("<Q", d, 4)[0], auth, None


def _check_instrument(ctx: Ctx, rep: Report) -> None:
    rep.section(f"{ctx.symbol}: реестр и mint")
    s, now = ctx.spec, ctx.deps.wall()
    if s is None:
        rep.add(False, f"реестр: {ctx.registry_error or 'нет записи ' + ctx.instrument_id} — ниже сверка по mint из ТЗ")
    else:
        rep.add(True, f"реестр {ctx.registry_file}: {s.instrument_id} v{s.version} · {_short(s.spot.mint)} ↔ "
                      f"{s.perp.fullcoin} · Fs/Fp {_n(s.units.fs)}/{_n(s.units.fp)}")
        _identity(rep, s, now)
        for b in I.entry_blockers(s, now):
            if not b.startswith("identity"):
                rep.add(False, b)
    rpc = ctx.rpc()
    if rpc is None:
        rep.add(None, "mint не прочитан: нет RPC с верным genesis")
        return
    try:
        mi = A.read_mint(rpc, ctx.mint)
    except (A.AccountError, RpcUnavailable, RpcError, GenesisMismatch) as e:
        rep.add(None, f"mint {_short(ctx.mint)} не прочитан — {_err(e)}")
        return
    names = {TOKEN_PROGRAM: "Token", TOKEN_2022_PROGRAM: "Token-2022"}
    prog = names.get(mi.program, mi.program)
    bad = A.mint_policy(mi)
    if s is not None:
        if mi.program != s.spot.token_program:
            bad.append(f"программа mint {prog} ≠ записи {names.get(s.spot.token_program, s.spot.token_program)}")
        if mi.decimals != s.spot.decimals:
            bad.append(f"decimals mint {mi.decimals} ≠ записи {s.spot.decimals}")
        if sorted(mi.ext_names) != sorted(s.spot.extensions):
            bad.append(f"расширения mint {sorted(mi.ext_names)} ≠ записи {sorted(s.spot.extensions)} — нужна новая "
                       "версия записи")
    auth = "authority нет" if not (mi.mint_authority or mi.freeze_authority) else "есть authority"
    rep.check(f"mint {_short(mi.address)}: {prog}, decimals {mi.decimals}, расширения "
              f"{', '.join(mi.ext_names) or 'нет'}, {auth} · слот {mi.slot}", bad)
    for note in A.mint_notes(mi):
        rep.add(INFO, note)
    try:
        qi = A.read_mint(rpc, ctx.quote_mint)
        qbad = [f"USDC: программа/decimals {qi.program}/{qi.decimals} ≠ {ctx.quote_program}/{ctx.quote_decimals}"
                ] if (qi.program, qi.decimals) != (ctx.quote_program, ctx.quote_decimals) else []
        rep.check(f"USDC {_short(qi.address)}: {names.get(qi.program, qi.program)}, decimals {qi.decimals}",
                  qbad + [f"USDC: {p}" for p in A.mint_policy(qi)])
    except (A.AccountError, RpcUnavailable, RpcError, GenesisMismatch) as e:
        rep.add(None, f"USDC mint не прочитан — {_err(e)}")
    cur = _safe(lambda: rpc.slot("confirmed"))
    for name, prog_id in PROGRAMS:
        try:
            slot, auth_pd, note = _program_deploy(rpc, prog_id)
        except (A.AccountError, RpcUnavailable, RpcError, GenesisMismatch) as e:
            rep.add(INFO, f"{name} {_short(prog_id)}: деплой не прочитан — {_err(e)}")
            continue
        if slot is None:
            rep.add(INFO, f"{name} {_short(prog_id)}: {note}")
            continue
        ago = f" (≈{_n((cur - slot) * SLOT_S / 86400, 1)} дн назад)" if cur and cur >= slot else ""
        rep.add(INFO, f"{name} {_short(prog_id)}: последний деплой — слот {slot}{ago}, "
                      + (f"upgrade authority {_short(auth_pd)}" if auth_pd else "без upgrade authority"))


def _book_line(ctx: Ctx, book, clip: D, ref: R.AssetRef) -> tuple[bool, str]:
    if not book.bids or not book.asks:
        return False, "стакан пуст с одной стороны"
    bid, ask = book.bids[0][0], book.asks[0][0]
    spread = (ask - bid) / ((ask + bid) / 2) * 10000
    t = ctx.market.last_book_ms
    age = f"{_n(D(int(ctx.deps.wall() * 1000) - t) / 1000, 1)} с назад" if t else "время неизвестно"
    d = book_depth(book, clip, ref.sz_decimals)
    txt = f"стакан bid {_n(bid)} / ask {_n(ask)} (спред {_n(spread, 0)} бп) · снимок биржи {age}"
    s, b = d["sell"], d["buy"]
    if s is None:
        return False, txt + f" · бидов на шорт {_n(clip)} USDC не хватает ({BOOK_LEVELS} уровней)"
    txt += f" · шорт на {_n(clip)} USDC: {_n(s[0])} контр., VWAP {_n(s[1], 6)}, −{_n(s[2], 1)} бп от bid"
    txt += (f" · закрытие: VWAP {_n(b[1], 6)}, +{_n(b[2], 1)} бп от ask" if b is not None
            else " · асков на закрытие не хватает")
    return True, txt


def _check_market(ctx: Ctx, rep: Report, clip: D) -> R.AssetRef | None:
    rep.section(f"Hyperliquid {ctx.fullcoin}")
    try:
        ref = ctx.ref()
    except Exception as e:      # noqa — мета не прочитана: рынок неизвестен
        rep.add(None, f"мета HL не прочитана — {_err(e)}")
        return None
    iso = ref.only_isolated or ref.margin_mode in ("noCross", "strictIsolated")
    bad, s = [], ctx.spec
    if s is not None:
        p = s.perp
        if p.asset_id is not None and p.asset_id != ref.asset:
            bad.append(f"asset id в записи {p.asset_id} ≠ {ref.asset} сейчас — нужна новая версия записи")
        if p.sz_decimals is not None and p.sz_decimals != ref.sz_decimals:
            bad.append(f"szDecimals в записи {p.sz_decimals} ≠ {ref.sz_decimals} сейчас")
        if p.collateral_token_id is not None and p.collateral_token_id != ref.collateral_token:
            bad.append(f"коллатераль в записи {p.collateral_token_id} ≠ {ref.collateral_token} сейчас")
    if ref.is_delisted:
        bad.append("рынок снят с торгов (isDelisted)")
    lev = ctx.leverage()
    if lev is not None and lev > ref.max_leverage:
        bad.append(f"плечо owner.toml {_n(lev)}x > maxLeverage {ref.max_leverage}x")
    rep.check(f"asset {ref.asset} (dex {ref.dex} #{ref.dex_index}, позиция {ref.local_index}) · szDecimals "
              f"{ref.sz_decimals} · maxLeverage {ref.max_leverage} · {'только isolated' if iso else 'cross разрешён'}",
              bad)
    if s is not None and s.perp.max_leverage is not None and s.perp.max_leverage != ref.max_leverage:
        rep.add(INFO, f"maxLeverage в записи {s.perp.max_leverage}, сейчас {ref.max_leverage}")
    try:
        oi = ctx.market.oi_state()
        cap, used = oi["cap_usd"], oi["oi_usd"]
        txt = (f"OI {_money(used)} из лимита {_money(cap)} ({_n(used / cap * 100, 0)} %)" if cap
               else f"OI {_money(used)}, лимита рынка в perpDexLimits нет")
        if oi["at_cap"]:
            rep.add(False, txt + " · рынок на лимите OI — новый шорт не откроется")
        else:
            rep.add(True, txt)
    except Exception as e:      # noqa
        rep.add(None, f"лимит OI не прочитан — {_err(e)}")
    try:
        mark, rate, _ = ctx.market.funding()
        now_ms = int(ctx.deps.wall() * 1000)
        page = ctx.market.funding_history(now_ms - FUNDING_DAYS * 24 * HOUR_MS, now_ms)
        rates = [r["rate"] for r in page.rows]
        avg = (f"{_pct_h(sum(rates) / len(rates))} ({len(rates)} ч{'' if page.complete else ', неполно'})"
               if rates else "истории нет")
        rep.add(INFO, f"фандинг сейчас {_pct_h(rate)} · за {FUNDING_DAYS} д в среднем {avg} · марк {_n(mark)}")
    except Exception as e:      # noqa — справка: решение о входе у владельца
        rep.add(INFO, f"фандинг недоступен — {_err(e)}")
    try:
        ok, txt = _book_line(ctx, ctx.market.book(BOOK_LEVELS), clip, ref)
        rep.add(ok, txt)
    except Exception as e:      # noqa
        rep.add(None, f"стакан не прочитан — {_err(e)}")
    return ref


def _check_account(ctx: Ctx, rep: Report, clip: D, ref: R.AssetRef | None) -> None:
    rep.section("Hyperliquid счёт")
    acc = ctx.account
    if acc is None:
        rep.add(False, "wallets.sol_hl.hl_account_address не задан — счёт не проверен")
        return
    user = (ctx.hl_user or "").lower() or None
    account, agent = acc.account, ctx.hl_agent
    sub = user is not None and account != user
    rep.add(INFO, f"мастер {_short(user)} · счёт {_short(account)}{' (субаккаунт)' if sub else ''} · агент "
                  f"{_short(agent)}" + (f" · vault {_short(ctx.hl_vault)}" if ctx.hl_vault else ""))
    try:
        m = acc.abstraction()
        if m.trade_supported:
            rep.add(True, f"режим счёта {m.raw} → Standard: поддержан")
        elif m.mode == "unified":
            rep.add(False, "режим счёта unifiedAccount — v1 торгует только Standard: переключите счёт в Standard "
                           "(до этого — честный отказ до свопа)")
        else:
            rep.add(False, f"режим счёта {str(m.raw)[:40]} → {m.mode}: не поддержан ({m.note})")
    except Exception as e:      # noqa
        rep.add(None, f"режим счёта не прочитан — {_err(e)}")
    try:
        r, who = _role(acc.user_role(account))
        ok = (r == "subAccount" and who == user) if sub else r == "user"
        rep.add(ok, f"userRole счёта: {r}" + (f" мастера {_short(who)}" if who else "")
                + ("" if ok else " — ожидался " + ("subAccount этого мастера" if sub else "user (мастер)")))
    except Exception as e:      # noqa
        rep.add(None, f"userRole счёта не прочитан — {_err(e)}")
    if sub:
        try:
            rows = acc.sub_accounts(user)
            mine = any(isinstance(x, dict) and str(x.get("subAccountUser") or "").lower() == account for x in rows)
            rep.add(mine, "субаккаунт в subAccounts мастера" if mine else "субаккаунта нет в subAccounts мастера")
        except Exception as e:  # noqa
            rep.add(None, f"subAccounts не прочитан — {_err(e)}")
    if not agent:
        rep.add(False, "wallets.sol_hl.hl_agent_address не задан — агент не проверен")
    else:
        try:
            r, who = _role(acc.user_role(agent))
            ok = r == "agent" and who is not None and who == user
            rep.add(ok, f"агент {_short(agent)}: userRole {r}" + (f" мастера {_short(who)}" if who else "")
                    + ("" if ok else " — не агент этого мастера: одобрите API-кошелёк в HL"))
        except Exception as e:  # noqa
            rep.add(None, f"userRole агента не прочитан — {_err(e)}")
        if user:
            try:
                st = acc.agent_status(user, agent)
                if not st["listed"]:
                    rep.add(INFO, "агента нет в extraAgents (безымянный агент там не виден — решает userRole)")
                else:
                    vu = st["valid_until"]
                    until = (datetime.fromtimestamp(vu / 1000, timezone.utc).strftime("%Y-%m-%d")
                             if isinstance(vu, int) else "без срока")
                    rep.add(False if st["expired"] else INFO,
                            f"агент в extraAgents «{st['name']}», до {until}" + (" — срок истёк" if st["expired"] else ""))
            except Exception as e:  # noqa
                rep.add(INFO, f"extraAgents не прочитан — {_err(e)}")
    try:
        mv = acc.margin(ref.collateral_token if ref else 0)
        lev, reserve = ctx.leverage(), ctx.limit("min_hl_margin_reserve_usdc")
        if mv.available is None:
            rep.add(None, f"маржа ({mv.mode}): неизвестна — {mv.reason}")
        else:
            txt = f"маржа {_n(mv.available, 2)} USDC ({mv.source})"
            if mv.available <= 0:
                rep.add(False, txt + f" — маржи нет: пополните USDC в {ctx.market.dex or 'основной'} ledger")
            elif lev and reserve is not None:
                need = clip / lev + reserve
                rep.add(mv.available >= need, txt + f" · нужно ≥ {_n(need, 2)} (клип {_n(clip)} / {_n(lev)}x + "
                                                    f"резерв {_n(reserve)})")
            else:
                rep.add(INFO, txt + " · потребность не посчитана: не задано плечо или резерв")
    except Exception as e:      # noqa
        rep.add(None, f"маржа не прочитана — {_err(e)}")
    try:
        sp = acc.spot_state()
        row = next((b for b in sp["balances"] if isinstance(b, dict) and b.get("coin") == "USDC"), None)
        rep.add(INFO, f"спот HL: USDC {_n(R.to_dec(row['total']))} (hold {_n(R.to_dec(row['hold']))})" if row
                else "спот HL: USDC нет")
    except Exception as e:      # noqa
        rep.add(INFO, f"спот HL не прочитан — {_err(e)}")
    pos = acc.position(critical=False)
    if pos is None:
        rep.add(None, f"позиция {ctx.fullcoin} не прочитана")
    else:
        rep.add(INFO, f"позиция {ctx.fullcoin}: {_n(pos)}" if pos else f"позиции {ctx.fullcoin} нет")
    try:
        oo = [o for o in acc.open_orders() if isinstance(o, dict) and o.get("coin") == ctx.fullcoin]
        if oo:
            rep.add(INFO, f"открытых заявок по {ctx.fullcoin}: {len(oo)}")
    except Exception as e:      # noqa
        rep.add(INFO, f"открытые заявки не прочитаны — {_err(e)}")
    try:
        a = acc.active_asset_data()
        want = ctx.leverage()
        rep.add(INFO, f"плечо на счёте: {a['leverage_type']} {a['leverage']}x"
                + (f" (перед входом исполнитель выставит isolated {_n(want)}x)" if want else ""))
    except Exception as e:      # noqa
        rep.add(INFO, f"плечо на счёте не прочитано — {_err(e)}")
    try:
        f = acc.user_fees()
        ctx._fees = f
        rate = R.taker_rate_estimate(f["cross"], ref) if ref else None
        scale = _safe(lambda: R.hip3_fee_scale(ref.deployer_fee_scale)) if ref and ref.dex else None
        rep.add(INFO, (f"тейкер ≈{_n(rate * 100, 4)}% (ставка счёта {_n(f['cross'] * 100, 4)}%"
                       + (f" × {_n(scale)} HIP-3)" if scale else ")")) if rate is not None
                else "ставка тейкера неизвестна (growth mode или нет меты)")
    except Exception as e:      # noqa
        rep.add(INFO, f"комиссии счёта не прочитаны — {_err(e)}")
    try:
        rl = acc.rate_limit()
        if isinstance(rl, dict):
            rep.add(INFO, f"запросы счёта: {rl.get('nRequestsUsed')} из {rl.get('nRequestsCap')}")
    except Exception as e:      # noqa
        rep.add(INFO, f"userRateLimit не прочитан — {_err(e)}")


def _check_wallet(ctx: Ctx, rep: Report, clip: D) -> None:
    rep.section("Кошелёк Solana")
    if not ctx.wallet:
        rep.add(False, "wallets.sol_hl.solana_address не задан — балансы не проверены")
        return
    rpc = ctx.rpc()
    if rpc is None:
        rep.add(None, "балансы не прочитаны: нет RPC с верным genesis")
        return
    reserve = ctx.limit("min_sol_reserve_lamports")
    try:
        lam, _ = rpc.balance(ctx.wallet)
        txt = f"{_short(ctx.wallet)}: SOL {_n(human(lam, 9), 4)}"
        if reserve is None:
            rep.add(INFO, txt + " · резерв SOL не задан")
        else:
            rep.add(lam >= reserve, txt + f" · резерв {_n(human(reserve, 9), 4)}"
                    + ("" if lam >= reserve else " — меньше резерва"))
    except (RpcUnavailable, RpcError, GenesisMismatch) as e:
        rep.add(None, f"SOL не прочитан — {_err(e)}")
    for sym, mint, prog, dec, need in (("USDC", ctx.quote_mint, ctx.quote_program, ctx.quote_decimals, clip),
                                       (ctx.symbol, ctx.mint, ctx.mint_program, ctx.mint_decimals, None)):
        addr = A.ata(ctx.wallet, mint, prog)
        try:
            ta = A.read_token_account(rpc, addr)
        except (A.AccountError, RpcUnavailable, RpcError, GenesisMismatch) as e:
            rep.add(None, f"{sym}: ATA {_short(addr)} не прочитан — {_err(e)}")
            continue
        if ta is None:
            if need is not None:
                rep.add(False, f"{sym}: ATA {_short(addr)} нет — покупать не на что")
            else:
                rent = _safe(lambda: A.ata_rent_lamports(rpc, prog, ctx.mint_exts))
                rep.add(INFO, f"{sym}: ATA {_short(addr)} нет — создастся первой покупкой"
                        + (f" (rent {_n(human(rent, 9))} SOL, возвратный)" if rent is not None else ""))
            continue
        amt = human(ta.amount, dec)
        bad = A.account_policy(ta, owner=ctx.wallet, mint=mint, program=prog)
        if need is not None and amt < need:
            bad.append(f"{sym} {_n(amt)} меньше клипа {_n(need)}")
        rep.check(f"{sym} {_n(amt)} (ATA {_short(addr)})", bad)


def _check_providers(ctx: Ctx, rep: Report, clip: D, quotes: bool) -> None:
    rep.section("Провайдеры маршрутов")
    c = ctx.creds
    unloaded = ctx.mode != "dry" and c is None          # ключи могут лежать в окружении — не загружены из-за ошибки выше
    jup = ("ключи связки не загружены — запросы без ключа" if unloaded else
           "ключ есть" if c and c.jupiter else "без ключа — keyless ≈0.5 запроса/с")
    rep.add(INFO, f"Jupiter: {jup} · Order только показ, исполнимый путь — Build")
    if ctx.mode == "dry":
        rep.add(INFO, "OKX: ключ в dry не читается")
    elif unloaded:
        rep.add(None, "OKX: ключи связки не загружены (см. «ключи и адреса связки») — полного сравнения нет")
    elif c.okx is None:
        rep.add(False, f"OKX: ключа нет ({', '.join(O.OKX_ENV.values())}) — полного сравнения нет")
    else:
        rep.add(INFO, "OKX: ключ есть · темп общий с коллектором и трейдером (runtime/okxdex.pace)")
    if not quotes:
        rep.add(None, "котировки не запрашивались (--no-quotes) — маршруты не проверены")
        return
    cmp = compare(ctx, "buy", usd_raw(clip, ctx.quote_decimals))
    if cmp.error:
        rep.add(None, f"котировки: {cmp.error}")
        return
    d = cmp.decision
    for x in d.ranked:
        cand = x.cand
        out = f"{_n(human(cand.expected_out_raw, cand.output_decimals), 2)} {ctx.symbol}" if cand.expected_out_raw else "—"
        txt = f"{cand.path}: {out} за {_n(clip)} USDC · ответ {cand.latency_ms} мс"
        if cand.hard_reasons:
            rep.add(INFO, txt + " · исключён: " + ", ".join(cand.hard_reasons))
        else:
            rep.add(True if x.eligible else INFO, txt)
    for u in d.unavailable:
        rep.add(INFO, f"{u.path}: недоступен — {u.reason}" + (f" ({K.redact(u.detail)[:80]})" if u.detail else ""))
    if d.winner is not None:
        rep.add(True, f"выбор {d.status}: {d.winner.path}")
    else:
        w = d.preview_winner
        live = [] if w is None else list(dict.fromkeys(_reason_text(r) for r in w.reasons if SR.live_only(r)))
        rep.add(False, "исполнимого маршрута нет: " + "; ".join(d.reasons or (d.status,))
                + (f" · лучший показ {w.path} — для live: {', '.join(live)}" if w is not None and live else ""))


def _check_settings(ctx: Ctx, rep: Report) -> None:
    rep.section("Настройки")
    for e in ctx.config_errors:
        rep.add(False, e)
    for b in ctx.readiness.blockers:
        head = "в owner.toml не задано: "
        if b.startswith(head):
            keys = b[len(head):].split(", ")
            rep.add(False, f"в owner.toml не задано ({len(keys)}): {_group_keys(keys)}")
        else:
            rep.add(False, b)
    ei = ctx.readiness.env_issues
    if ei is None:
        rep.add(INFO, "окружение не читалось (режим dry)")
    elif ei:
        rep.add(False, "в окружении нет: " + ", ".join(ei))
    else:
        rep.add(True, "переменные окружения на месте (значения не показываются)")


def doctor(ctx: Ctx, *, usd: D | None = None, quotes: bool = True) -> Report:
    clip = ctx.clip_usd(usd)
    rep = Report(_header(ctx, "doctor"))
    _check_rpc(ctx, rep)
    _check_instrument(ctx, rep)
    ref = _check_market(ctx, rep, clip)
    _check_account(ctx, rep, clip, ref)
    _check_wallet(ctx, rep, clip)
    _check_providers(ctx, rep, clip, quotes)
    _check_settings(ctx, rep)
    rep.finish()
    return rep


def hl_preflight(ctx: Ctx, *, usd: D | None = None) -> Report:
    clip = ctx.clip_usd(usd)
    rep = Report(_header(ctx, "hl-preflight"))
    ref = _check_market(ctx, rep, clip)
    _check_account(ctx, rep, clip, ref)
    rep.finish("hl-preflight")
    return rep


# --- серия замеров -----------------------------------------------------------------------------------
RECORD_SCHEMA = """
CREATE TABLE IF NOT EXISTS record_runs(id INTEGER PRIMARY KEY, started REAL NOT NULL, minutes INTEGER NOT NULL,
  profile TEXT, instrument TEXT, fullcoin TEXT, mode TEXT, clip_usd TEXT, hl_every INTEGER, routes_every INTEGER,
  rpc_every INTEGER, finished REAL);
CREATE TABLE IF NOT EXISTS hl_samples(id INTEGER PRIMARY KEY, run INTEGER, ts REAL NOT NULL, fullcoin TEXT,
  book_ms INTEGER, book_age_ms INTEGER, bid TEXT, ask TEXT, bid_sz TEXT, ask_sz TEXT, clip_usd TEXT, sell_qty TEXT,
  sell_vwap TEXT, sell_slip_bps TEXT, buy_qty TEXT, buy_vwap TEXT, buy_slip_bps TEXT, funding TEXT, mark TEXT,
  oracle TEXT, oi TEXT, latency_ms INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS route_samples(id INTEGER PRIMARY KEY, run INTEGER, ts REAL NOT NULL, cycle INTEGER,
  side TEXT, amount_in_raw TEXT, path TEXT, status TEXT NOT NULL, reason TEXT, expected_out_raw TEXT,
  min_out_raw TEXT, metric TEXT, conservative TEXT, fees_usdc TEXT, impact_bps TEXT, latency_ms INTEGER,
  age_ms INTEGER, basis_bps TEXT, edge_usdc TEXT, hedge_qty TEXT, preview_winner INTEGER, decision TEXT);
CREATE TABLE IF NOT EXISTS rpc_samples(id INTEGER PRIMARY KEY, run INTEGER, ts REAL NOT NULL, endpoint TEXT,
  ok INTEGER NOT NULL, slot INTEGER, block_height INTEGER, last_valid_height INTEGER, blockhash_margin INTEGER,
  health TEXT, behind_slots INTEGER, latency_ms REAL, error TEXT);
"""


def record_path(db: str | Path | None) -> Path:
    p = Path(db) if db else config.RUNTIME / RECORD_DB
    if p.name in FORBIDDEN_DB:
        raise ValueError(f"{p.name}: серия пишется в отдельный SQLite, не в базу трейдера или коллектора")
    return p


def open_record_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(record_path(path)), timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(RECORD_SCHEMA)
    return con


def _insert(con: sqlite3.Connection, table: str, row: dict) -> None:
    cols = list(row)
    con.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [row[c] for c in cols])
    con.commit()


def _join_err(row: dict, msg: str) -> None:
    row["error"] = f"{row['error']}; {msg}" if row.get("error") else msg


def sample_hl(ctx: Ctx, clip: D) -> dict:
    row: dict = {"fullcoin": ctx.fullcoin, "clip_usd": str(clip)}
    t0 = ctx.deps.clock()
    try:
        ref = ctx.ref()
        b = ctx.market.book(BOOK_LEVELS)
        row["latency_ms"] = int((ctx.deps.clock() - t0) * 1000)
        t = ctx.market.last_book_ms
        row["book_ms"] = t
        row["book_age_ms"] = int(ctx.deps.wall() * 1000) - t if t else None
        if b.bids:
            row["bid"], row["bid_sz"] = str(b.bids[0][0]), str(b.bids[0][1])
        if b.asks:
            row["ask"], row["ask_sz"] = str(b.asks[0][0]), str(b.asks[0][1])
        d = book_depth(b, clip, ref.sz_decimals)
        for side in ("sell", "buy"):
            if d[side] is None:
                _join_err(row, f"глубины на клип нет ({side})")
            else:
                q, vwap, slip = d[side]
                row.update({f"{side}_qty": str(q), f"{side}_vwap": str(vwap), f"{side}_slip_bps": _n(slip, 2)})
    except Exception as e:      # noqa — провал = причина, значения NULL (не 0)
        _join_err(row, "book " + _err(e))
    try:
        c = ctx.market.ctx()
        row.update(funding=str(c["funding"]), mark=str(c["mark"]), oracle=str(c["oracle"]), oi=str(c["open_interest"]))
    except Exception as e:      # noqa
        _join_err(row, "ctx " + _err(e))
    return row


def sample_rpc(ctx: Ctx) -> list[dict]:
    out = []
    for r in ctx.rpcs:
        row: dict = {"endpoint": r.label, "ok": 0}
        try:
            r.check_genesis()
            t0 = ctx.deps.clock()
            bh = r.latest_blockhash("confirmed")
            row["latency_ms"] = round((ctx.deps.clock() - t0) * 1000, 1)
            height = r.block_height("confirmed")
            row.update(ok=1, slot=bh.context_slot, block_height=height, last_valid_height=bh.last_valid_block_height,
                       blockhash_margin=bh.last_valid_block_height - height)
            try:
                healthy, behind = r.health()
                row.update(health="ok" if healthy else "unhealthy", behind_slots=behind)
            except (RpcUnavailable, RpcError) as e:
                row["health"] = "?"
                _join_err(row, "health " + _err(e))
        except Exception as e:  # noqa
            _join_err(row, _err(e))
        out.append(row)
    return out


def _route_rows(ctx: Ctx, cmp: Compare) -> list[dict]:
    base = {"side": cmp.side, "amount_in_raw": str(cmp.amount_raw)}
    if cmp.error:
        return [dict(base, path="*", status="error", reason=cmp.error)]
    d, now = cmp.decision, ctx.deps.clock()
    win = d.winner or d.preview_winner
    out = []
    for x in d.ranked:
        c, p = x.cand, x.pair
        out.append(dict(base, path=c.path, status="eligible" if x.eligible else "preview" if x.previewable else "excluded",
                        reason=",".join(c.reasons) or None, expected_out_raw=_s(c.expected_out_raw),
                        min_out_raw=_s(c.effective_min_out), metric=_s(x.metric), conservative=_s(x.conservative),
                        fees_usdc=_s(x.valuation.total if x.valuation else None), impact_bps=_s(c.price_impact_bps),
                        latency_ms=c.latency_ms, age_ms=int((now - c.received_mono) * 1000),
                        basis_bps=_s(p.basis_bps if p else None), edge_usdc=_s(p.edge if p else None),
                        hedge_qty=_s(p.qty if p else None), preview_winner=int(c is win), decision=d.status))
    for u in d.unavailable:
        out.append(dict(base, path=u.path, status="unavailable",
                        reason=K.redact(f"{u.reason}: {u.detail}" if u.detail else u.reason)[:200], decision=d.status))
    return out


def sample_routes(ctx: Ctx, clip: D) -> list[dict]:
    """Покупка на клип и продажа того объёма токенов, что дал бы лучший путь покупки (одинаковый объём по путям)."""
    buy = compare(ctx, "buy", usd_raw(clip, ctx.quote_decimals))
    rows = _route_rows(ctx, buy)
    w = (buy.decision.winner or buy.decision.preview_winner) if buy.decision else None
    if w is not None and w.expected_out_raw:
        rows += _route_rows(ctx, compare(ctx, "sell", w.expected_out_raw))
    else:
        rows.append({"side": "sell", "path": "*", "status": "skipped",
                     "reason": "нет объёма продажи: покупка не дала котировки"})
    return rows


def record(ctx: Ctx, *, minutes: int, db: str | Path | None = None, hl_every: int = HL_EVERY_S,
           routes_every: int = ROUTES_EVERY_S, rpc_every: int = RPC_EVERY_S, usd: D | None = None,
           stop: threading.Event | None = None) -> str:
    """Серия замеров в свой SQLite. Любой провал записывается причиной, значения — NULL."""
    if minutes <= 0 or min(hl_every, routes_every, rpc_every) <= 0:
        raise ValueError("минуты и интервалы — целые > 0")
    clip, path = ctx.clip_usd(usd), record_path(db)
    con = open_record_db(path)
    wall, sleep, stop = ctx.deps.wall, ctx.deps.sleep, stop or threading.Event()
    start = wall()
    end = start + minutes * 60
    cur = con.execute("INSERT INTO record_runs(started, minutes, profile, instrument, fullcoin, mode, clip_usd, hl_every,"
                      " routes_every, rpc_every) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (start, minutes, SOL_HL, ctx.instrument_id, ctx.fullcoin, ctx.mode, str(clip), hl_every,
                       routes_every, rpc_every))
    con.commit()
    run = cur.lastrowid
    every = {"rpc": rpc_every, "hl": hl_every, "routes": routes_every}
    nxt = dict.fromkeys(every, start)
    cycle = 0
    try:
        while not stop.is_set() and wall() < end:
            for kind in ("rpc", "hl", "routes"):
                if stop.is_set() or wall() < nxt[kind]:
                    continue
                ts = wall()
                if kind == "rpc":
                    rows = [("rpc_samples", r) for r in sample_rpc(ctx)]
                elif kind == "hl":
                    rows = [("hl_samples", sample_hl(ctx, clip))]
                else:
                    cycle += 1
                    rows = [("route_samples", dict(r, cycle=cycle)) for r in sample_routes(ctx, clip)]
                for table, r in rows:
                    _insert(con, table, dict(r, run=run, ts=ts))
                nxt[kind] = max(nxt[kind] + every[kind], wall())
            wait = min(min(nxt.values()), end) - wall()
            if wait > 0 and not stop.is_set():
                sleep(min(wait, 1.0))
    except KeyboardInterrupt:
        pass
    finally:
        con.execute("UPDATE record_runs SET finished=? WHERE id=?", (wall(), run))
        con.commit()
    text = summarize(con, run, path)
    con.close()
    return text


def summarize(con: sqlite3.Connection, run: int, path: Path) -> str:
    started, finished = con.execute("SELECT started, finished FROM record_runs WHERE id=?", (run,)).fetchone()
    lines = [f"sol-hl record: {path} · прогон {run} · {_n(D(str(round((finished or started) - started, 1))) / 60, 1)} мин"]
    n, bad = con.execute("SELECT COUNT(*), SUM(error IS NOT NULL) FROM hl_samples WHERE run=?", (run,)).fetchone()
    lines.append(f"стакан HL: {n} замеров, с ошибкой {bad or 0}")
    for ep, cnt, ok, mg, lat in con.execute(
            "SELECT endpoint, COUNT(*), SUM(ok), MIN(blockhash_margin), AVG(latency_ms) FROM rpc_samples WHERE run=? "
            "GROUP BY endpoint", (run,)):
        lines.append(f"RPC {ep}: {ok or 0}/{cnt} ответили · мин. запас высот blockhash {mg if mg is not None else '—'}"
                     f" · {'—' if lat is None else round(lat)} мс")
    for side, p, cnt, okc, lat in con.execute(
            "SELECT side, path, COUNT(*), SUM(status IN ('eligible','preview')), AVG(latency_ms) FROM route_samples "
            "WHERE run=? AND path != '*' GROUP BY side, path ORDER BY side, path", (run,)):
        lines.append(f"{side} {p}: котировка {okc or 0}/{cnt}" + (f" · ответ ≈{round(lat)} мс" if lat else ""))
    miss = con.execute("SELECT COUNT(*) FROM route_samples WHERE run=? AND path = '*'", (run,)).fetchone()[0]
    if miss:
        lines.append(f"сборов без котировок: {miss} (причины в route_samples.reason)")
    return K.redact("\n".join(lines))


# --- CLI ------------------------------------------------------------------------------------------
def _dec_arg(s: str) -> D:
    try:
        d = D(s)
    except Exception:
        raise argparse.ArgumentTypeError(f"не число: {s!r}") from None
    if not d.is_finite() or d <= 0:
        raise argparse.ArgumentTypeError("нужно число > 0")
    return d


def _pos_int(s: str) -> int:
    try:
        v = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"не целое: {s!r}") from None
    if v <= 0:
        raise argparse.ArgumentTypeError("нужно целое > 0")
    return v


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--owner", help="путь к owner.toml (по умолчанию runtime/owner.toml)")
    common.add_argument("--instrument", default=DEFAULT_INSTRUMENT, help="instrument_id из instruments.json")
    ap = argparse.ArgumentParser(prog="funding_bot sol-hl",
                                 description="связка SOL×HL, только чтение: ни подписей, ни отправок")
    sub = ap.add_subparsers(dest="sol_cmd", required=True)
    d = sub.add_parser("doctor", parents=[common], help="готовность связки: сеть, mint, HL, кошелёк, провайдеры")
    d.add_argument("--usd", type=_dec_arg, help="объём показа, USDC (по умолчанию max_clip_usdc, иначе 150)")
    d.add_argument("--no-quotes", action="store_true", help="без котировок (провайдеры не вызываются)")
    q = sub.add_parser("quote-compare", parents=[common], help="три пути на один объём: стоимость, победитель, причины")
    q.add_argument("--side", choices=("buy", "sell"), required=True, help="buy: USDC → токен; sell: токен → USDC")
    q.add_argument("--amount-raw", type=_pos_int, required=True, help="вход exactIn в raw (USDC 6 знаков, ANSEM 6)")
    q.add_argument("--slippage-bps", type=_pos_int, help="если limits.max_spot_slippage_bps пуст")
    h = sub.add_parser("hl-preflight", parents=[common], help="Hyperliquid: рынок и счёт по публичным адресам")
    h.add_argument("--usd", type=_dec_arg)
    r = sub.add_parser("record", parents=[common], help="серия замеров в отдельный SQLite (не trade.db)")
    r.add_argument("--minutes", type=_pos_int, required=True)
    r.add_argument("--db", help=f"файл серии (по умолчанию runtime/{RECORD_DB})")
    r.add_argument("--usd", type=_dec_arg)
    r.add_argument("--hl-every", type=_pos_int, default=HL_EVERY_S, help="стакан и фандинг HL, с")
    r.add_argument("--routes-every", type=_pos_int, default=ROUTES_EVERY_S, help="сравнение путей, с")
    r.add_argument("--rpc-every", type=_pos_int, default=RPC_EVERY_S, help="лаг RPC и запас высот blockhash, с")
    return ap


def main(argv: Sequence[str] | None = None, *, environ=None, deps: Deps | None = None,
         out: Callable[[str], None] = print) -> int:
    """Код: 0 — готово (doctor: live_ready=true; quote-compare: есть котировка); 1 — не готово; 2 — не запускается."""
    a = build_parser().parse_args(argv)
    K.install_log_redaction()
    env = os.environ if environ is None else environ
    try:
        cfg = O.load(a.owner)
    except O.OwnerConfigError as e:
        out(f"✗ owner.toml: {e}")
        return 2
    ctx = Ctx(cfg, env, deps or Deps(), a.instrument)
    if a.sol_cmd == "doctor":
        rep = doctor(ctx, usd=a.usd, quotes=not a.no_quotes)
        out(rep.render())
        return 0 if rep.ready else 1
    if a.sol_cmd == "hl-preflight":
        rep = hl_preflight(ctx, usd=a.usd)
        out(rep.render())
        return 0 if rep.ready else 1
    if a.sol_cmd == "quote-compare":
        cmp = compare(ctx, a.side, a.amount_raw, slippage_bps=a.slippage_bps)
        out(K.redact("\n".join([_header(ctx, "quote-compare"), *render_compare(ctx, cmp)])))
        if cmp.error:
            return 2
        return 0 if (cmp.decision.winner or cmp.decision.preview_winner) is not None else 1
    stop = threading.Event()
    try:
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    except ValueError:          # не главный поток (тесты)
        pass
    try:
        text = record(ctx, minutes=a.minutes, db=a.db, hl_every=a.hl_every, routes_every=a.routes_every,
                      rpc_every=a.rpc_every, usd=a.usd, stop=stop)
    except ValueError as e:
        out(f"✗ {e}")
        return 2
    out(text)
    return 0
