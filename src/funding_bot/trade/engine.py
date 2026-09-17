"""Исполнитель фазы 2 (trade_spec §6): ОДИН поток, который подписывает и отправляет; машины состояний сделки,
намерения и клипа; guard() перед каждым внешним действием; вход и выход клипами; перенос остатка, инвариант ног,
HEDGE_DEFICIT, «стоп», «продолжить», «дохедж», «откат». Плюс Desk — предпроверки и план с кнопками (поток заданий,
только чтение) и сборка ног по режиму (build_runtime).

Порядок в клипе — неопределённая нога первой (lphedge: шорт первым дважды оставил голый шорт на LIT):
  вход:  своп DEX (стейбл → токен) → по ЧЕКУ (логи Transfer) → перп SELL на пришедшее количество + перенос;
  выход: своп DEX (токен → стейбл) → перп BUY reduceOnly на проданное; последний клип закрывает весь шорт.
Инвариант сделки: 0 ≤ токены DEX − |шорт| < шаг перпа, по журналу и (live) по positionRisk. Нарушен — HEDGE_DEFICIT
и пауза; дальше только команда владельца («дохедж» / «откат»), авто-откат — лишь если владелец задал срок.

Запись-до на каждое действие (store): строка → COMMIT → внешний вызов → итог → COMMIT. Клип DEX_SENT пишется ДО
свопа; подписанная транзакция — кошельком до отправки; client_id заявки — до отправки, nonce подписи — в on_signed.
UNKNOWN у заявки не повторяется никогда: только settle_unknown(); «не выставлена» (NOT_FOUND = три -2013, позиция и
сделки неизменны) — единственный повод послать снова, и то под новым номером попытки.

«стоп»: флаг paused в БД + Event в памяти. Ворота нижнего уровня (AsterTrade.call, EvmWallet) на паузе отправку не
пропускают, КРОМЕ хеджа уже исполненной DEX-ноги (hedge=True, Q7): начатая пара ног доводится, новое не начинается.
SIGTERM — то же в памяти (term), без записи в БД.

Режимы: сделка несёт sim. sim=1 — ноги симуляции (sim.py) на живых публичных данных; sim=0 — боевые ноги, и только
если процесс загрузил ключи live. dry никогда не вызывает keys.load (ключей в памяти нет вовсе).
"""
from __future__ import annotations
import json, logging, math, queue, threading, time
from dataclasses import dataclass, field, replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, InvalidOperation
from typing import Any, Callable, Mapping
from .. import config
from ..symbols import norm_symbol_factor
from . import formatters, owner as owner_mod, planner, report, store, tconfig
from .keys import effective_mode, redact
from .owner import OwnerCfg, OwnerConfigError, OwnerMissing
from .ledger_flows import spot_quote_flows, perp_quote_flows
from .exposure import Exposure
from .operations import ClipLifecycle, OperationController, SpotSettlement
from .coordinator import HedgeAction, HedgeProgram, LifecycleCoordinator
from .planner import PlanRefused
from .runtime import is_sol_deal
from .store import ClipState, DealState, IntentStatus, PerpOrderState
from .types import ClipPlan, InstrumentSpec, Plan, PerpFill

log = logging.getLogger(__name__)
D = Decimal
ZERO = D(0)
WEI = D(10) ** 18

CHAIN = "bsc"                     # спот-сеть фазы 2 (Solana/Robinhood — позже тем же SpotLeg)
VENUE = "aster"                   # перп фазы 2
KIND_LETTER = {"entry": "e", "exit": "x", "rehedge": "h", "undo": "u"}   # буква в client_id заявки
MAIN_KINDS = ("entry", "exit")
PERP_ATTEMPTS_MAX = 3             # повтор дочерней заявки — только после доказанного NOT_FOUND и не больше
NATIVE_EVM = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"   # нативная монета EVM в API OKX DEX
NATIVE_PX_TTL_S = 60.0
SIGMA_KLINES = 61                 # σ марка: 60 минутных свечей → σ₁ₛ = σ₁ₘ/√60 [A: /fapi/v1/klines Aster = Binance]


class Pause(Exception):
    """guard() или шаг клипа: сделка на паузу с причиной reason (короткий код) и текстом владельцу."""

    def __init__(self, reason: str, text: str):
        super().__init__(text)
        self.reason = reason
        self.text = text


class Refused(Exception):
    """Команда владельца отклонена до плана. Несёт факты (topic, facts), не HTML — рендерит только interface
    (interface.presenter.render_execution_notice), тем же путём, что и уведомления исполнителя (AC-07).
    Обычный случай — просто причина: Refused("текст") → topic='refused', facts={'reason': "текст"}."""

    def __init__(self, reason: str | None = None, *, topic: str = 'refused', facts: dict | None = None):
        self.topic = topic
        self.facts = facts if facts is not None else {'reason': reason}
        super().__init__(self.facts.get('reason') or reason or topic)


def dget(x: Any) -> D | None:
    """TEXT/число/None из plan_json и БД → Decimal; пусто → None."""
    if x is None or (isinstance(x, str) and not x.strip()):
        return None
    if isinstance(x, D):
        return x
    if isinstance(x, float):
        return D(repr(x))
    try:
        return D(str(x))
    except InvalidOperation:
        return None


def floor_step(x: D, step: D) -> D:
    return (x / step).to_integral_value(ROUND_FLOOR) * step


def ceil_step(x: D, step: D) -> D:
    return (x / step).to_integral_value(ROUND_CEILING) * step


# --- соединения: одно на поток (store: транзакции одного соединения из разных потоков перемешались бы) ------
class Conns:
    def __init__(self, path=None):
        self.path = path
        self._tl = threading.local()

    def get(self):
        c = getattr(self._tl, "con", None)
        if c is None:
            c = store.connect(self.path)
            self._tl.con = c
        return c


class CfgHolder:
    """Параметры владельца для ног: во время исполнения — замороженная копия намерения (тот план, что одобрен), вне
    его — свежий owner.toml. Своя подмена у каждого потока: план в потоке заданий не видит копию исполнителя."""

    def __init__(self, loader: Callable[[], OwnerCfg] = owner_mod.load):
        self.loader = loader
        self._tl = threading.local()

    def __call__(self) -> OwnerCfg:
        c = getattr(self._tl, "cfg", None)
        return c if c is not None else self.loader()

    def set(self, cfg: OwnerCfg | None) -> None:
        self._tl.cfg = cfg


@dataclass
class Legs:
    """Пара ног одной «правды»: sim=True — симуляция, False — боевые (или боевые только для чтения в readonly)."""
    spot: Any
    perp: Any
    sim: bool
    native_px: Callable[[], D | None] = lambda: None
    can_send: bool = False            # боевые ноги с загруженными ключами live

    @property
    def fill_venue(self) -> str:
        """Площадка в perp_orders/perp_fills: сделки симуляции не смешиваются с биржевыми (PRIMARY KEY venue+id)."""
        return ("sim:" if self.sim else "") + self.perp.venue


# --- книга сделки по журналу ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DealBook:
    tokens_raw: int | None            # токены сделки по клипам (сырые); None — есть клип с неизвестным исходом DEX
    short: D | None                   # шорт по заявкам (модуль); None — есть заявка с неизвестным исходом
    why: str | None = None
    # инструмент сделки (ревью 13.09, фаза 1): m — токенов в контракте; inst_ok=False — m не подтверждён (сделка до
    # фазы 1 без сверки по журналу): наращивать нельзя, закрывать можно. known от инструмента не зависит.
    # m_known=False — m не известен вовсе (M3: inst_json не читается, запасной путь нашёл множитель): m = 1 здесь только
    # заглушка, дельта ног в токенах — None; сделку закрывает только полный выход (или «выход перп»)
    m: D = D(1)
    inst_ok: bool = True
    inst_why: str | None = None
    m_known: bool = True

    @property
    def known(self) -> bool:
        return self.tokens_raw is not None and self.short is not None

    @property
    def m_view(self) -> D:
        """m для текстов: 0 — m не известен («N контр.» без пересчёта в токены, views.contracts)."""
        return self.m if self.m_known else ZERO

    def tokens(self, dec: int) -> D | None:
        return None if self.tokens_raw is None else D(self.tokens_raw) / D(10) ** dec

    def tstep(self, step: D) -> D:
        """Шаг перпа в токенах: шаг (контракты) · m."""
        return step * self.m

    def delta(self, dec: int) -> D | None:
        """Токены − |шорт|·m, в ТОКЕНАХ: + голый лонг, − голый шорт. В норме [0, шаг·m). m не известен — None."""
        t = self.tokens(dec)
        if t is None or self.short is None or not self.m_known:
            return None
        return t - self.short * self.m

    def hedged(self, dec: int, step: D) -> bool | None:
        """Ноги ровно: 0 ≤ дельта < шаг·m (меньше одного шага контрактов не хеджируется). None — книга неизвестна."""
        d = self.delta(dec)
        return None if d is None else ZERO <= d < self.tstep(step)


_OPEN_PERP = (PerpOrderState.INTENT, PerpOrderState.SENT, PerpOrderState.UNKNOWN)


def _dn(x: D) -> str:
    """Decimal в тексте отказа без экспоненты: 1E+3 → «1000»."""
    return format(D(x).normalize(), "f")


def _symbol_parts(symbol: str) -> tuple[str, str | None]:
    """«1000BONKUSDT» → («1000BONK», «USDT»); Gate «FATCOIN_USDT» → («FATCOIN», «USDT»); квоты нет — (символ, None)."""
    s = str(symbol).upper()
    for q in ("USDT", "USDC", "USD"):
        if s.endswith(q) and len(s) > len(q):
            base = s[:-len(q)]
            if base.endswith("_") and len(base) > 1:    # Gate пишет базу и квоту через «_»; у Aster «_» перед квотой нет
                base = base[:-1]
            return base, q
    return s, None


def _name_units(coin: str, symbol: str) -> tuple[str, D]:
    """База и множитель по ИМЕНИ символа: 1000BONKUSDT → (BONK, 1000). kPEPE, записанный заглавными (KPEPEUSDT при
    монете PEPE), — тоже тысяча PEPE (как norm_symbol_factor для «k»), а не другой актив KPEPE."""
    norm, fac = norm_symbol_factor(_symbol_parts(symbol)[0])
    c = coin.upper()
    if norm != c and norm == "K" + c:
        norm, fac = c, 1000.0
    return norm, D(int(fac))


def unit_refusal(coin: str, symbol: str, *, m: D | None = None, allow_multiplier: bool = False,
                 venue: str = VENUE) -> D:
    """Ревью 13.09, С1: единицы контракта. Возвращает m — токенов в одном контракте (по бирже, если m передан, иначе по
    имени символа). База символа — другой актив — отказ всегда (разрешение владельца его не снимает); имя символа и
    биржа расходятся — отказ; m ≠ 1 без разрешения владельца (owner.toml [perp.aster] allow_contract_multiplier =
    true, _allow_multiplier) — отказ до любого действия, текст называет ключ."""
    v = formatters
    norm, fac = _name_units(coin, symbol)
    if norm != coin.upper():
        raise Refused(f"контракт {symbol} — это {norm}, а не {coin}: другой актив, не торгую")
    # Gate: имя множителя не несёт (FATCOIN_USDT), m — quanto_multiplier биржи (GateTrade.instrument, авторитетно);
    # множитель, записанный в имени Gate и не равный бирже, — по-прежнему отказ (единицы неоднозначны)
    if m is not None and fac != m and not (venue == "gate" and fac == 1):
        raise Refused(f"контракт {symbol}: имя символа (×{_dn(fac)}) и биржа (×{_dn(m)} {norm} в контракте) "
                                "расходятся — не торгую")
    m_eff = fac if m is None else D(m)
    if m_eff != 1 and not allow_multiplier:
        raise Refused(f"контракт {symbol} — {_dn(m_eff)} {norm} в одном контракте, а без разрешения "
                                f"владельца исполнитель торгует только 1 контракт = 1 токен {coin}: не торгую. "
                                f"Разрешение — owner.toml [perp.{venue}] {MULT_KEY} = true")
    return m_eff


MULT_KEY = "allow_contract_multiplier"

# источники спецификации, у которых m НЕ известен (ревью 13.09, M3): inst_json записан, но не читается; сделка до фазы 1,
# где запасной путь сам нашёл чужую базу, множитель в имени символа или расхождение цены контракта и токена. Подставить
# m = 1 нельзя: дельта по m = 1 вела бы к «откату», который продаёт захеджированные токены (голый шорт)
M_UNKNOWN_SOURCES = ("unreadable", "legacy:m_unknown")


def m_known(inst: InstrumentSpec) -> bool:
    return inst.source not in M_UNKNOWN_SOURCES


def _allow_multiplier(cfg, venue: str = VENUE) -> bool:
    """Разрешение владельца на контракты с множителем (m ≠ 1): только явное true в [perp.<площадка сделки>]; пусто
    или false — нельзя."""
    return cfg.get(f"perp.{venue}.{MULT_KEY}") is True


# --- EVM-связки прежнего движка (спот OKX DEX × перп): сеть и площадка — пары/сделки, а не константы модуля --------
# (FATCOIN 13.09: okx·robinhood × gate рядом с okx·bsc × aster; путь BSC × Aster — ровно прежний)


def _chain_tag(chain: str) -> str:
    """Короткое имя сети в подписях и строках таблицы: robinhood → rh (dexleg.TAG), прочие — как есть."""
    return {"robinhood": "rh", "solana": "sol"}.get(chain, chain)


def _cv(chain: str | None, venue: str | None) -> tuple[str, str]:
    """(сеть, площадка) канонически; не заданы — прежняя связка BSC × Aster (CHAIN, VENUE)."""
    c = tconfig.canonical_chain(chain) if chain else CHAIN
    return c, (str(venue).strip().lower() if venue else VENUE)


def _deal_cv(deal: Mapping) -> tuple[str, str]:
    """(сеть, площадка) EVM-сделки. Пара не из EVM_PROFILES (колонку сделки подменили, сеть неизвестна) — прежняя
    BSC × Aster, как до связок: такую сделку остановит сверка с замороженным инструментом («расходятся»), а не
    чужие ключи владельца; ноги другой связки ей не достаются."""
    d = dict(deal)
    try:
        cv = _cv(d.get("chain"), d.get("perp_venue"))
    except KeyError:
        return CHAIN, VENUE
    return cv if cv in owner_mod.EVM_PROFILE_OF else (CHAIN, VENUE)


def _cmd_cv(spot_s: str, perp_s: str) -> tuple[str, str]:
    """(сеть, площадка) команды входа до выбора строки: сеть из «okx·<сеть>»; без сети — сеть связок этого перпа
    (aster → bsc, gate → robinhood); неизвестная — прежняя BSC (find_pair откажет честно)."""
    venue = str(perp_s or "").strip().lower() or VENUE
    chain = str(spot_s or "").partition("·")[2]
    try:
        chain = tconfig.canonical_chain(chain) if chain else ""
    except KeyError:
        chain = ""
    if not chain:
        chains = sorted(c for (c, vn) in owner_mod.EVM_PROFILE_OF if vn == venue)
        chain = chains[0] if chains else CHAIN
    return chain, venue


def _lim(cfg, deal: Mapping):
    """Лимиты владельца для площадки и сети сделки (planner.limits_from_owner)."""
    chain, venue = _deal_cv(deal)
    return planner.limits_from_owner(cfg, venue, chain)


def _live_miss(cfg, deal: Mapping) -> list:
    chain, venue = _deal_cv(deal)
    return list(cfg.live_missing(venue, chain))


def _profile_legs(legs_fn, profile: str, sim: bool) -> tuple[Any, str | None]:
    """Ноги НЕ старой EVM-связки — только из реестра (RuntimeRegistry.for_profile). Нет реестра, связка не собрана или
    в этом режиме боевых ног нет — (None, причина): такой сделке ноги BSC/Aster не достаются никогда."""
    from .runtime import ProfileDown
    fn = getattr(legs_fn, "for_profile", None)
    if fn is None:
        return None, f"связка {profile} не подключена в этом процессе"
    try:
        lg = fn(profile, bool(sim))
    except ProfileDown as e:
        return None, f"связка {profile} не собрана: {e.reason}"
    if lg is None:
        return None, "ключи live не загружены (mode в owner.toml и перезапуск службы)"
    return lg, None


def _period_of(con, deal_id: str) -> D:
    """Период фандинга сделки, ч — из спецификации первого входа; нет или не читается — 1 ч."""
    try:
        spec = json.loads(con.execute("SELECT spec_json FROM intents WHERE deal_id=? AND kind='entry' ORDER BY created "
                                      "LIMIT 1", (deal_id,)).fetchone()[0])
        return D(str(spec.get("period_h") or 1))
    except (TypeError, ValueError, KeyError, AttributeError, InvalidOperation):
        return D(1)


def legacy_instrument(con, deal: Mapping, now: float | None = None) -> InstrumentSpec:
    """Вердикт миграции для сделки без inst_json (открыта до фазы 1): прежний код вёл журнал «1 контракт = 1 токен».
    Подтверждено, если база символа = монета, множителя в имени нет, а (средняя цена SELL перпа сделки) / (средняя
    цена входа на DEX) ∈ [1/UNIT_PX_RATIO_MAX, UNIT_PX_RATIO_MAX] → m = 1, source migration:verified_ratio. Иначе
    m = 1 (единицы журнала прежнего кода), verified=False с причиной: наращивать нельзя, закрывать можно. Сам нашёл
    чужую базу, множитель в имени или расхождение цены (ревью 13.09, M3) — m НЕ известен (source legacy:m_unknown,
    m = 1 лишь заглушка): только полный выход. Только БД, без сети (DQA9Q 13.09: 0.04093 / 0.0407984 = 1.0032)."""
    coin, symbol, dec = str(deal["coin"]), str(deal["symbol"]), int(deal["token_dec"])
    base_raw, quote = _symbol_parts(symbol)
    norm, fac = _name_units(coin, symbol)
    why, ratio, m_bad = None, None, True
    if norm != coin.upper():
        why = f"база символа {symbol} — другой актив ({norm}, а не {coin})"
    elif fac != 1:
        why = f"множитель в имени символа {symbol} (×{_dn(fac)})"
    else:
        prefix = f"fb-{deal['id']}-"
        q = qq = ZERO
        for o in con.execute("SELECT executed_qty, cum_quote FROM perp_orders WHERE substr(client_id, 1, ?)=? AND "
                             "side='SELL' AND state IN ('FILLED','PARTIALLY_FILLED')", (len(prefix), prefix)):
            q += dget(o["executed_qty"]) or ZERO
            qq += dget(o["cum_quote"]) or ZERO
        din = dout = 0
        for c in con.execute("SELECT c.dex_in, c.dex_out FROM clips c JOIN intents i ON c.intent_id = i.id WHERE "
                             "i.deal_id=? AND i.kind='entry' AND c.state IN ('DEX_OK','PERP_SENT','BALANCED',"
                             "'HEDGE_DEFICIT')", (deal["id"],)):
            din += int(c["dex_in"] or 0)
            dout += int(c["dex_out"] or 0)
        try:
            sdec = config.OKX_DEX_STABLES[tconfig.chain_index(deal["chain"])][1]
        except KeyError:                       # незнакомая сеть: цену входа не посчитать — не подтверждено
            sdec = None
        if q <= 0 or qq <= 0 or din <= 0 or dout <= 0 or sdec is None:
            # сверить нечем, но и противоречия нет: журнал прежнего кода — в его единицах (1 контракт = 1 токен)
            why, m_bad = "нет исполненного шорта или чеков входа — множитель не сверить", False
        else:
            ratio = (qq / q) / ((D(din) / D(10) ** int(sdec)) / (D(dout) / D(10) ** dec))
            if not (D(1) / tconfig.UNIT_PX_RATIO_MAX <= ratio <= tconfig.UNIT_PX_RATIO_MAX):
                why = f"цена контракта / цена токена = {formatters.num(ratio, 4)} — единицы не сходятся"
    ok = why is None
    source = "migration:verified_ratio" if ok else ("legacy:m_unknown" if m_bad else "legacy:unverified")
    return InstrumentSpec(chain=str(deal["chain"]), token=str(deal["token"]).lower(), token_dec=dec,
                          perp_venue=str(deal["perp_venue"]), perp_symbol=symbol, units_per_contract=D(1),
                          perp_base_asset=base_raw, quote_asset=quote, period_h=_period_of(con, deal["id"]),
                          source=source, verified=ok,
                          verified_ts=(time.time() if now is None else now) if ok else None, px_ratio=ratio, why=why)


def deal_instrument(con, deal: Mapping) -> InstrumentSpec:
    """Спецификация сделки: inst_json, если записан и читается, иначе вердикт миграции на лету (legacy_instrument, без
    записи: пишет только старт трейдера — backfill_instruments). inst_json записан, но не читается (порча, ручная правка,
    схема будущей версии после отката) — m НЕ известен (source unreadable, ревью 13.09 M3): не «legacy без inst_json»,
    m = 1 подставлять нельзя — сделка могла быть открыта с множителем."""
    raw = dict(deal).get("inst_json")
    if raw:
        try:
            return InstrumentSpec.from_json(raw)
        except ValueError as e:
            return replace(legacy_instrument(con, deal), source="unreadable", verified=False, verified_ts=None,
                           px_ratio=None, why=f"спецификация инструмента не читается ({e})")
    return legacy_instrument(con, deal)


def backfill_instruments(con, *, now: float) -> list[tuple[str, InstrumentSpec]]:
    """Старт трейдера: активным сделкам без inst_json записать ПОДТВЕРЖДЁННЫЙ вердикт миграции (set_deal_inst) +
    событие inst_backfill (m, source, px_ratio). Неподтверждённый не пишется — пересчитывается на лету (добор
    fills может его подтвердить позже); повторный старт ничего не пишет."""
    out = []
    for d in store.active_deals(con):
        if d.get("inst_json"):
            continue
        inst = legacy_instrument(con, d, now=now)
        if not inst.verified:
            log.warning("инструмент сделки %s не подтверждён: %s", d["id"], inst.why)
            continue
        with store.tx(con):
            if store.set_deal_inst(con, d["id"], inst.to_json()):
                store.event(con, "inst_backfill", deal_id=d["id"], m=inst.m, source=inst.source,
                            px_ratio=inst.px_ratio, now=now)
                out.append((d["id"], inst))
    return out


_IDENT_NAMES = ("сеть", "токен", "decimals", "площадка", "символ")


def _inst_vs_deal(deal: Mapping, inst: InstrumentSpec) -> str | None:
    """Колонки сделки против её замороженного инструмента (ревью 13.09, Н2): расходятся — сделку правили руками или
    запись битая; такой сделке исполнитель не отправит ничего (своп и хедж идут по колонкам)."""
    have = (str(deal["chain"]), str(deal["token"]).lower(), int(deal["token_dec"]), str(deal["perp_venue"]),
            str(deal["symbol"]))
    frozen = (inst.chain, inst.token.lower(), int(inst.token_dec), inst.perp_venue, inst.perp_symbol)
    diff = [f"{n} {a} ≠ {b}" for n, a, b in zip(_IDENT_NAMES, have, frozen) if a != b]
    return f"сделка {deal['id']} и её инструмент расходятся ({'; '.join(diff)})" if diff else None


def _same_identity(coin: str, was: tuple, now: tuple) -> None:
    """Ревью 13.09, Н2: (сеть, токен, …, символ) замороженного инструмента против строки таблицы или плана. Другой
    токен/символ/decimals — это другой актив: одобренный план к нему не относится, нужен новый."""
    if was != now:
        raise Refused(f"инструмент {coin} в таблице сменился ({was[1]}/{was[-1]} → "
                                       f"{now[1]}/{now[-1]}) — это другой актив, нужен новый план")


def deal_book(con, deal_id: str) -> DealBook:
    """Позиция сделки по журналу: токены — из чеков клипов (вход +dex_out, выход/откат −dex_in), шорт — из итогов
    заявок сделки (SELL +, BUY −; сделка — по префиксу client_id fb-<сделка>-). Неизвестное не превращается в 0.
    m и подтверждённость — из спецификации инструмента сделки (deal_instrument)."""
    why = []
    tok: int | None = 0
    for r in con.execute("SELECT c.id, c.state, c.dex_in, c.dex_out, i.kind FROM clips c JOIN intents i "
                         "ON c.intent_id = i.id WHERE i.deal_id=? ORDER BY c.id", (deal_id,)):
        st = r["state"]
        if st in (ClipState.DEX_SENT, ClipState.DEX_UNKNOWN):
            why.append(f"клип {r['id']}: исход DEX неизвестен")
            tok = None
            continue
        if st in (ClipState.PLANNED, ClipState.DEX_REVERTED) or tok is None:
            continue
        if r["kind"] == "entry":
            tok += int(r["dex_out"] or 0)
        elif r["kind"] in ("exit", "undo"):
            tok -= int(r["dex_in"] or 0)
    short: D | None = ZERO
    prefix = f"fb-{deal_id}-"
    for o in con.execute("SELECT client_id, side, state, executed_qty FROM perp_orders WHERE substr(client_id, 1, ?)=?",
                         (len(prefix), prefix)):
        if o["state"] in _OPEN_PERP:
            why.append(f"заявка {o['client_id']}: исход неизвестен")
            short = None
            continue
        if short is None:
            continue
        q = dget(o["executed_qty"]) or ZERO
        short += q if o["side"] == "SELL" else -q
    m, inst_ok, inst_why, mk = D(1), True, None, True
    row = store.get_deal(con, deal_id)
    if row is not None:
        inst = deal_instrument(con, row)
        m, inst_ok, inst_why, mk = inst.m, inst.verified, inst.why, m_known(inst)
    return DealBook(tokens_raw=tok, short=short, why="; ".join(why) or None, m=m, inst_ok=inst_ok, inst_why=inst_why,
                    m_known=mk)


def _partial_closes_short(bk: DealBook, units: int, dec: int, step: D) -> bool:
    """Ревью 13.09, M2 (m ≠ 1): частичный выход units сырых токенов откупил бы весь шорт (исполнитель берёт
    ceil((продано − δ)/m) к шагу контрактов) или оставил бы токенов меньше шага·m — сделка закрылась бы с остатком вне
    учёта и хеджа. Такой выход — полный. Продаёт все токены сделки — остатка нет, это не он. Только единицы контракта,
    без порогов в $."""
    if units >= bk.tokens_raw:
        return False
    sold, delta = D(units) / D(10) ** dec, bk.delta(dec)
    q = ceil_step((sold - delta) / bk.m, step) if sold > delta else ZERO
    return q >= bk.short or D(bk.tokens_raw - units) / D(10) ** dec < bk.tstep(step)


def deal_fills(con, deal_id: str, intent_id: str | None = None) -> list[dict]:
    """userTrades сделки (или одного намерения): perp_fills ↔ perp_orders по (venue, order_id)."""
    from . import scoped_accounting
    scoped = scoped_accounting.deal_fills(con, deal_id, intent_id)
    if scoped is not None:
        return scoped
    prefix = f"fb-{deal_id}-"
    sql = ("SELECT f.* FROM perp_fills f JOIN perp_orders o ON f.venue = o.venue AND f.order_id = o.order_id "
           "WHERE substr(o.client_id, 1, ?)=?")
    args: list = [len(prefix), prefix]
    if intent_id is not None:
        sql += " AND o.clip_id IN (SELECT id FROM clips WHERE intent_id=?)"
        args.append(intent_id)
    return [dict(r) for r in con.execute(sql + " ORDER BY f.trade_id", args)]


def orders_as_fills(con, intent_id: str, fee: D) -> list[dict]:
    """Запасной источник для отчёта, если userTrades не добраны: итоги заявок; комиссия — оценка по тарифу."""
    out = []
    for o in con.execute("SELECT o.* FROM perp_orders o JOIN clips c ON o.clip_id = c.id WHERE c.intent_id=? "
                         "AND o.state IN ('FILLED','PARTIALLY_FILLED')", (intent_id,)):
        q, cq = dget(o["executed_qty"]) or ZERO, dget(o["cum_quote"]) or ZERO
        out.append({"qty": q, "quote_qty": cq, "price": dget(o["avg_price"]), "commission_abs": cq * fee,
                    "commission_asset": "USDT", "maker": 0, "realized_pnl": None})
    return out


def intent_txs(con, intent_id: str) -> list[dict]:
    """Транзакции намерения: свопы клипов и approve (он до первого клипа, clip_id пуст — связь через событие
    «approve» журнала со всеми хэшами группы nonce)."""
    return [dict(r) for r in con.execute(
        "SELECT t.* FROM dex_txs t JOIN clips c ON t.clip_id = c.id WHERE c.intent_id=? "
        "UNION SELECT t.* FROM dex_txs t WHERE t.kind='approve' AND t.tx_hash IN "
        "(SELECT j.value FROM exec_events e, json_each(e.json, '$.hashes') j WHERE e.intent_id=? AND e.kind='approve') "
        "ORDER BY id", (intent_id, intent_id))]


# --- рыночные вспомогательные ------------------------------------------------------------------------
def sigma_1s(perp, symbol: str) -> D | None:
    """σ доходности марка за 1 с по минутным свечам (σ₁ₘ/√60). Нет данных — None: план тогда не оценит риск голой
    ноги и в live откажет (unhedged_usd_max обязателен), в dry — строит без этого ограничения."""
    own = getattr(perp, "sigma_1s", None) or getattr(getattr(perp, "inner", None), "sigma_1s", None)
    if callable(own):                          # своя модель σ площадки (Gate: /futures/usdt/candlesticks)
        try:
            return own(symbol)
        except Exception as e:                 # noqa
            log.warning("σ %s не получена: %s", symbol, redact(e))
            return None
    if getattr(perp, "venue", None) not in (None, "aster"):
        return None                            # свечи /fapi/v1/klines — только у Aster: у другой площадки модель σ не
        #                                        подключена (SOL×HL §3.2 п.4) — честное «неизвестно», а не 0
    http = getattr(perp, "http", None)
    if http is None:
        return None
    try:
        rows = http.get("/fapi/v1/klines", {"symbol": symbol, "interval": "1m", "limit": SIGMA_KLINES}, retries=1)
        closes = [D(str(r[4])) for r in rows if isinstance(r, (list, tuple)) and len(r) > 4]
    except Exception as e:                     # noqa
        log.warning("σ %s не получена: %s", symbol, redact(e))
        return None
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 10:
        return None
    m = sum(rets, ZERO) / len(rets)
    var = sum(((x - m) ** 2 for x in rets), ZERO) / (len(rets) - 1)
    return planner.dsqrt(var) / planner.dsqrt(D(60))


class NativePrice:
    """Цена газовой монеты в $ (OKX market price, кэш 60 с). Не получена — None (газ в $ — «—», не 0)."""

    def __init__(self, okx, chain_index: str = "56", clock: Callable[[], float] = time.time):
        self.okx, self.ci, self._clock = okx, chain_index, clock
        self._v: tuple[float, D | None] = (0.0, None)
        self._lock = threading.Lock()

    def __call__(self) -> D | None:
        now = self._clock()
        with self._lock:
            if now - self._v[0] < NATIVE_PX_TTL_S and self._v[1] is not None:
                return self._v[1]
        try:
            got = self.okx.market_prices([(self.ci, NATIVE_EVM)])
            px = next(iter(got.values()))[0] if got else None
            v = D(str(px)) if px else None
        except Exception as e:                 # noqa
            log.warning("цена BNB не получена: %s", redact(e))
            v = None
        with self._lock:
            self._v = (now, v)
        return v


# --- сборка ног по режиму ---------------------------------------------------------------------------------
@dataclass
class Runtime:
    mode: str                         # режим загрузки (dry | readonly | live) — повышение только перезапуском
    keys: Any                         # keys.Keys | None (в dry None: ключи не читались)
    sim: Legs
    live: Legs | None
    cfg: CfgHolder
    native_px: Callable[[], D | None]


def build_runtime(cfg: OwnerCfg, conns: Conns, *, holder: CfgHolder | None = None, mode: str | None = None,
                  environ=None, okx=None, rpc=None, aster=None, credentials=None) -> Runtime:
    """Ноги по режиму owner.toml (mode — только понизить). dry: keys.load НЕ вызывается вовсе — ни один ключ не
    читается из окружения; readonly: подписанные чтения, симуляция входов с настоящими балансами; live: всё."""
    from ..okxdex import OkxDex
    from .aster_trade import AsterTrade
    from .evm import EvmRpc, EvmWallet
    from .evm_swap import OkxEvmSpot
    from .sim import SimPerp, SimSpot
    holder = holder or CfgHolder()
    m = effective_mode(cfg.mode, mode)
    okx = okx if okx is not None else OkxDex()
    rpc = rpc if rpc is not None else EvmRpc(tconfig.bsc_rpc_urls(environ))
    native_px = NativePrice(okx)

    def mode_state():
        return holder.loader().mode, store.execution_paused(conns.get())

    wallet = cfg.get(f"wallets.{CHAIN}")
    placeholder = "0x" + "0" * 40              # кошелёк не задан (dry): только котировки, балансы неизвестны
    k = None
    if m == "dry":
        perp_pub = aster if aster is not None else AsterTrade(mode_state=mode_state)
        spot_ro = OkxEvmSpot(okx, rpc, holder, chain=CHAIN, wallet=wallet or placeholder, native_usd=native_px)
        sim = Legs(SimSpot(spot_ro, native_px=native_px, wallet_known=bool(wallet)), SimPerp(perp_pub), True, native_px)
        return Runtime(m, None, sim, None, holder, native_px)
    from . import keys as keys_mod
    k = credentials.legacy(cfg, m) if credentials is not None else keys_mod.load(cfg, m, environ=environ)
    perp = aster if aster is not None else AsterTrade.from_keys(k, mode_state)
    sender = None
    if k.evm is not None:
        tl = conns

        def gate(in_flight: bool) -> None:
            k.gate(holder.loader().mode, "send", paused=store.execution_paused(tl.get()), hedge=in_flight)

        sender = EvmWallet(rpc, tconfig.CHAIN_IDS[CHAIN], k.evm, lambda row: store.dex_tx_signed(tl.get(), **row),
                           gate=gate, on_sent=lambda h: store.dex_tx_sent(tl.get(), h),
                           on_resolved=lambda h, st, info: store.dex_tx_resolve(tl.get(), h, st, **info), chain=CHAIN)
    spot = OkxEvmSpot(okx, rpc, holder, chain=CHAIN, wallet=wallet, sender=sender, native_usd=native_px)
    spot_ro = OkxEvmSpot(okx, rpc, holder, chain=CHAIN, wallet=wallet, native_usd=native_px)
    sim = Legs(SimSpot(spot_ro, native_px=native_px), SimPerp(perp), True, native_px)
    live = Legs(spot, perp, False, native_px, can_send=k.mode == "live")
    return Runtime(m, k, sim, live, holder, native_px)


# --- Desk: предпроверки и план с кнопками (поток заданий; только чтение сети) ------------------------------
@dataclass
class Proposal:
    """Предложение владельцу: намерение proposed (истекает через PLAN_TTL_S) и факты плана для кнопок — HTML
    строит только interface (interface.presenter.render_proposal_view), не Desk (AC-07). view_topic называет,
    каким рендерером в tg/sol_views это станет текстом ('plan' | 'sol_plan' | 'fix_plan'), view_facts — сырые
    аргументы этого рендерера (Decimal/str/bool/кортежи скаляров — те же поля, без HTML)."""
    intent_id: str
    nonce: str
    deal_id: str
    kind: str
    view_topic: str
    view_facts: dict
    plan: Plan | None = None
    superseded: tuple = ()                      # прежние предложения той же сделки, снятые этим (бот снимет кнопки)


@dataclass
class Notice:
    """Итог команды без плана и кнопок (например «продолжить» — сделка уже сверена и на паузе): тоже факты, не
    HTML. Тот же (topic, facts), что и Refused/Hooks.notice — рендерит только interface."""
    topic: str
    facts: dict


@dataclass
class PairInfo:
    coin: str
    chain: str
    token: str
    token_dec: int
    symbol: str
    period_h: D
    spot_label: str
    ident_ev: str | None = None                 # доказательство identity строки таблицы (в спецификацию инструмента)
    spec: InstrumentSpec | None = None          # спецификация инструмента входа (Desk._instrument, ревью 13.09)
    venue: str = VENUE                          # площадка перпа пары (EVM-связки: aster | gate)


def _calib_from(d: dict) -> planner.Calib:
    pts = tuple((D(str(a)), D(str(b))) for a, b in (d.get("points") or ()))
    return planner.Calib(kind=d["kind"], k=D(str(d["k"])), c0=D(str(d["c0"])), g=D(str(d["g"])),
                         p_ref=D(str(d["p_ref"])), points=pts, k_raw=D(str(d["k_raw"])),
                         resid_bps=D(str(d["resid_bps"])), k_v3=dget(d.get("k_v3")), k_ratio=dget(d.get("k_ratio")))


def plan_from_json(s: str) -> Plan:
    """plan_json (store.jdump(Plan)) → Plan; Decimal из строк — там, где исполнитель считает."""
    d = json.loads(s)
    clips = [ClipPlan(seq=int(c["seq"]), dex_in_units=int(c["dex_in_units"]),
                      children=[(D(str(q)), D(str(p))) for q, p in c.get("children") or ()]) for c in d["clips"]]
    return Plan(deal_id=d["deal_id"], kind=d["kind"], coin=d["coin"], spot=d["spot"], perp=d["perp"],
                symbol=d["symbol"], leg_usd=D(str(d["leg_usd"])), clips=clips, est=d.get("est") or {},
                inputs=d.get("inputs") or {}, missing_owner_keys=list(d.get("missing_owner_keys") or ()),
                expires=float(d.get("expires") or 0))


def _allowance_ok(spot, token: str, need: int) -> bool | None:
    """Хватит ли allowance (для строки «газ approve» в плане). Не знаем — None (план закладывает approve)."""
    base = getattr(spot, "inner", spot)
    fn = getattr(base, "_allowance_any", None)
    if fn is None:
        return None
    try:
        return fn(token) >= need
    except Exception:                          # noqa
        return None


class Desk:
    """Предпроверки §6 п.3 и план §7 для кнопок. Ничего не подписывает и не отправляет: деньги двигает только
    Engine после атомарного одобрения. legs(sim) → Legs | None. В симуляции (dry/readonly) предпроверки, которые
    отказали бы live (балансы, маржа), становятся заметками в плане, а не отказом — «весь поток идёт» (§8)."""

    def __init__(self, conns: Conns, legs: Callable[[bool], Legs | None], *,
                 owner_loader: Callable[[], OwnerCfg] = owner_mod.load, table_loader: Callable[[], dict] | None = None,
                 keys_mode: str | None = None, busy: Callable[[], bool] = lambda: False,
                 clock: Callable[[], float] = time.time, registry_loader: Callable[[OwnerCfg], Any] | None = None):
        self.conns, self.legs = conns, legs
        self.owner_loader = owner_loader
        if table_loader is None:
            from ..serve import load_table
            table_loader = load_table
        self.table_loader = table_loader
        self.keys_mode = keys_mode
        self.busy = busy
        self.clock = clock
        # связка SOL × HL (trade/sol_flow.py): реестр инструментов (runtime/instruments.json) и её предложения. legs —
        # runtime.RuntimeRegistry (или прежний legs(sim): тогда связка «не подключена» — честный отказ)
        self.registry_loader = registry_loader
        self._sol_desks = {}

    def sol(self, profile=None):
        profile = profile or owner_mod.SOL_HL
        if profile not in self._sol_desks:
            from .sol_flow import SolDesk
            self._sol_desks[profile] = SolDesk(self, profile)
        return self._sol_desks[profile]

    def propose_profile_entry(self, cmd, chat: int | None) -> Proposal:
        """Вход связки (tg.parse.ProfileEntry): инструмент — из реестра, не из таблицы коллектора."""
        return self.sol(cmd.profile).propose_entry(cmd, chat)

    def propose_profile_exit(self, cmd, chat: int | None) -> Proposal:
        """Выход с именем связки или количеством (tg.parse.ProfileExit). Сделка связки — её план выхода (частичный в
        пилоте — честный отказ); сделка BSC — прежний выход только без количества и с именем «bsc», иначе отказ
        с форматом (сумма в токенах у BSC не разбирается)."""
        from ..operator_commands import EXIT_FMT
        from .runtime import profile_of_deal
        deal = self.resolve_deal(cmd.target)
        v = formatters
        if not is_sol_deal(deal):
            if cmd.profile not in (None, owner_mod.LEGACY_PROFILE) or cmd.tokens is not None or cmd.usdc is not None:
                raise Refused(f"сделка {deal['id']} — связки BSC/Aster, формат: {EXIT_FMT}")
            return self.propose_exit(deal["id"], None, False, chat)
        profile = profile_of_deal(deal)
        if cmd.profile not in (None, profile):
            raise Refused(f"сделка {deal['id']} — профиля {profile}, а не {cmd.profile}")
        if cmd.perp_dex and str(deal["symbol"]).split(":", 1)[0] != cmd.perp_dex:
            raise Refused(f"сделка {deal['id']} на {deal['symbol']}, а не на dex {cmd.perp_dex}")
        if cmd.tokens is not None or cmd.usdc is not None:
            raise Refused(f"частичный выход в пилоте выключен — только «выход {deal['id']}» целиком")
        return self.sol(profile).propose_exit(deal, None, False, chat)

    # --- общее ---
    def cfg(self) -> OwnerCfg:
        try:
            return self.owner_loader()
        except OwnerConfigError as e:
            raise Refused(topic='owner_config_error', facts={'reason': str(e)}) from None

    def mode(self, cfg: OwnerCfg) -> str:
        """Меньший из режима файла и режима загрузки ключей (повышение — только перезапуском)."""
        return effective_mode(cfg.mode, self.keys_mode) if self.keys_mode else "dry"

    def _pair_mode(self, cfg: OwnerCfg, chain: str, venue: str) -> str:
        """Режим связки пары: BSC × Aster — ровно mode(cfg); иная EVM-связка — меньший из её profile_mode (выключена —
        не выше readonly) и режима загрузки ключей: общий mode = live не делает живой связку, которую владелец не
        перевёл в live."""
        prof = owner_mod.EVM_PROFILE_OF.get((chain, venue), owner_mod.LEGACY_PROFILE)
        if prof == owner_mod.LEGACY_PROFILE:
            return self.mode(cfg)
        return effective_mode(cfg.profile_mode(prof), self.keys_mode) if self.keys_mode else "dry"

    def _legs(self, sim: bool) -> Legs:
        lg = self.legs(sim)
        if lg is None:
            raise Refused("живую сделку в этом режиме не трогаю: ключи live не загружены "
                                           "(mode в owner.toml и перезапуск службы)")
        return lg

    def _legs_for(self, chain: str, venue: str, sim: bool) -> Legs:
        """Ноги EVM-связки пары: BSC × Aster — ровно прежние legs(sim); иные — ноги своей связки из реестра."""
        prof = owner_mod.EVM_PROFILE_OF.get((chain, venue), owner_mod.LEGACY_PROFILE)
        if prof == owner_mod.LEGACY_PROFILE:
            return self._legs(sim)
        lg, why = _profile_legs(self.legs, prof, sim)
        if lg is None:
            raise Refused(f"живую сделку в этом режиме не трогаю: {why}")
        return lg

    def _deal_legs(self, deal: Mapping) -> Legs:
        chain, venue = _deal_cv(deal)
        return self._legs_for(chain, venue, bool(dict(deal)["sim"]))

    def _common_checks(self, cfg: OwnerCfg, sim: bool, action: str, chain: str = CHAIN, venue: str = VENUE) -> None:
        con = self.conns.get()
        if store.is_paused(con):
            raise Refused("пауза («стоп»): новое не начинаю. Снять — «продолжить»")
        if self.busy() or store.busy_intents(con):
            row = con.execute("SELECT id FROM intents WHERE status IN ('approved','running') LIMIT 1").fetchone()
            raise Refused(topic='busy', facts={'intent_id': row[0] if row else None})
        if not sim:
            try:
                cfg.require_live(venue, chain)
            except OwnerMissing as e:
                raise Refused(topic='owner_missing', facts={'keys': list(e.keys), 'action': action}) from None

    def find_pair(self, coin: str, spot: str, perp: str, *, allow_multiplier: bool = False) -> PairInfo:
        v = formatters
        dex, _, chain = spot.partition("·")
        venue = str(perp).strip().lower()
        chains = sorted(c for (c, vn) in owner_mod.EVM_PROFILE_OF if vn == venue)   # сети спота связок этого перпа
        if dex == "okx" and not chain:          # «okx dex» без сети (владелец 12.09): сеть — из таблицы, план её назовёт
            names = {ix: name for name, ix in config.OKX_DEX_CHAINS.items()}
            have = sorted({names.get(str(r.get("spot") or "").split(":", 1)[0], "?")
                           for r in (self.table_loader() or {}).get("sf_rows") or []
                           if str(r.get("base") or "").upper() == coin and r.get("spot_ex") == "okxdex"
                           and r.get("perp_ex") == perp})
            ok = [c for c in have if c in chains]
            if have and not ok:
                raise Refused(f"{coin} на OKX DEX есть только в сетях: {', '.join(have)} — в фазе 2 "
                                        f"торгую только okx·{_chain_tag(chains[0]) if chains else CHAIN}")
            chain = ok[0] if ok else (chains[0] if chains else CHAIN)   # строк нет — ниже честный «пары нет»
            spot = f"okx·{_chain_tag(chain)}"
        try:
            chain = tconfig.canonical_chain(chain) if chain else chain
        except KeyError:
            pass                                # неизвестная сеть — отказ ниже
        if not chains:
            raise Refused(f"перп {perp}: в фазе 2 пока только "
                                    f"{', '.join(sorted({vn for _c, vn in owner_mod.EVM_PROFILE_OF}))}")
        if dex != "okx" or chain not in chains:
            raise Refused(f"спот {spot}: в фазе 2 пока только "
                                    f"{', '.join('okx·' + _chain_tag(c) for c in chains)}")
        ci = tconfig.chain_index(chain)
        tbl = self.table_loader() or {}
        rows = [r for r in tbl.get("sf_rows") or [] if str(r.get("base") or "").upper() == coin
                and r.get("spot_ex") == "okxdex" and r.get("perp_ex") == perp
                and str(r.get("spot") or "").startswith(ci + ":")]
        if not rows:
            raise Refused(f"пары {coin} okx·{_chain_tag(chain)} / {perp} нет в таблице коллектора (table.json)")
        # торгуем только токен, доказанный контрактом для САМОГО перпа: строка, связанная через другую площадку
        # (ident_ev «dex_link:…», 12.09), остаётся на дашборде для просмотра, но не для денег
        proven = [r for r in rows if not r.get("mismatch") and r.get("ident") == "same"
                  and not str(r.get("ident_ev") or "").startswith("dex_link:")]
        if not proven and any(str(r.get("ident_ev") or "").startswith("dex_link:") for r in rows):
            raise Refused(f"токен {coin} на {chain} связан с перпом {perp} только через другую площадку — "
                                    "для торговли нужно прямое доказательство (контракт в индексе самого перпа)")
        if not proven:
            raise Refused(f"токен {coin} на {chain} не доказан identity (состав индекса и контракты) — "
                                    "не торгую")
        plain = [r for r in proven if r.get("spot_label") == f"okx·{_chain_tag(chain)}"] or proven
        if len({r["spot"] for r in plain}) > 1:
            raise Refused(f"у {coin} несколько токенов на {chain} — какой торговать, решает владелец")
        r = plain[0]
        token = str(r["spot"]).split(":", 1)[1].lower()
        unit_refusal(coin, str(r["perp"]), allow_multiplier=allow_multiplier, venue=venue)  # ранний отказ; m с биржи —
        #                                                                         в _instrument
        return PairInfo(coin=coin, chain=chain, token=token, token_dec=-1, symbol=str(r["perp"]),
                        period_h=D(str(r.get("period") or 1)), spot_label=str(r.get("spot_label") or spot),
                        ident_ev=str(r.get("ident_ev") or "") or None, venue=venue)

    def pair_from(self, inst: InstrumentSpec, deal: Mapping, *, verify_table: bool, spot_s: str | None = None,
                  perp_s: str | None = None, cfg: OwnerCfg | None = None) -> PairInfo:
        """PairInfo из ЗАМОРОЖЕННОЙ спецификации сделки — не выбор строки таблицы по монете (ревью 13.09, Н2).
        verify_table (перекотировка входа, добор): строка той же монеты обязана быть доказанной (find_pair: ident same,
        не mismatch, не dex_link, одна) и указывать на тот же токен и символ — иначе «сменился или отозван». Выход,
        откат и дохедж — verify_table=False: таблицу не читают вовсе. Колонки сделки ≠ её инструменту — отказ всегда."""
        bad = _inst_vs_deal(deal, inst)
        if bad:
            raise Refused(f"{bad} — не торгую")
        period = inst.period_h or self._period(dict(deal))
        if verify_table:
            tp = self.find_pair(str(deal["coin"]), str(spot_s), str(perp_s),
                                allow_multiplier=cfg is not None and _allow_multiplier(cfg, inst.perp_venue or VENUE))
            _same_identity(str(deal["coin"]), (inst.chain, inst.token.lower(), inst.perp_symbol),
                           (tp.chain, tp.token.lower(), tp.symbol))
            period = tp.period_h              # период фандинга — справка (не идентичность): свежий, как до фазы 1
        return PairInfo(coin=str(deal["coin"]), chain=inst.chain, token=inst.token, token_dec=int(inst.token_dec),
                        symbol=inst.perp_symbol, period_h=period, spot_label=f"okx·{inst.chain}", ident_ev=inst.ident_ev,
                        spec=inst, venue=inst.perp_venue or VENUE)

    def _token_dec(self, spot, token: str, quotes=()) -> int:
        fn = getattr(spot, "decimals", None)
        if fn is not None:
            try:
                return int(fn(token))
            except Exception as e:             # noqa
                log.warning("decimals %s: %s", token, redact(e))
        for q in quotes:
            if q.token_out.lower() == token:
                return int(q.dec_out)
            if q.token_in.lower() == token:
                return int(q.dec_in)
        raise Refused(f"decimals токена {token} не прочитаны")

    def _market(self, legs: Legs, pair: PairInfo, token_units_bal: dict, calib, approve_usd: D) -> planner.Market:
        perp = legs.perp
        f = perp.filters(pair.symbol)
        book = perp.book(pair.symbol, tconfig.ASTER_DEPTH_LIMIT)
        _mark, rate, _nxt = perp.funding(pair.symbol)
        npx = legs.native_px()
        nat = token_units_bal.get("native")
        native_usd = (D(nat) / WEI * npx) if (nat is not None and npx is not None) else None
        return planner.Market(book=book, filters=f, fee_taker=D(str(config.FEES_TAKER[perp.venue])),
                              sigma_1s=sigma_1s(perp, pair.symbol), funding_h=rate / pair.period_h,
                              native_usd=native_usd, native_px=npx, approve_usd=approve_usd, chain=pair.chain)

    def _quotes(self, spot, t_in: str, t_out: str, total: int) -> list:
        try:
            return [spot.quote(t_in, t_out, a) for a in planner.calib_amounts(total)]
        except PlanRefused:
            raise
        except Exception as e:                 # NoLiquidity, Unsupported, сеть OKX
            raise Refused(f"котировки OKX DEX не получены: {redact(e)}") from None

    # --- вход ---
    def plan_entry(self, coin: str, spot_s: str, perp_s: str, usd: D, *, cfg: OwnerCfg | None = None,
                   sim: bool | None = None, write_checks: bool = True,
                   pair: PairInfo | None = None, carry0: D = ZERO) -> tuple[Plan, dict]:
        """План входа без записи в БД (CLI `plan` и перекотировка у кнопки). ctx — всё для PlanView и spec.
        pair — замороженный инструмент сделки (pair_from: перекотировка входа и добор, ревью 13.09, Н2): строку таблицы
        по монете заново не выбираем; decimals с цепи и контракт с биржи обязаны совпасть с замороженными.
        carry0 — дельта сделки в токенах до плана («продолжить», перекотировка добора): при m ≠ 1 контракты клипов
        считаются с переносом, как у исполнителя (ревью 13.09, M1)."""
        v = formatters
        cfg = cfg or self.cfg()
        sim = (self._pair_mode(cfg, *_cmd_cv(spot_s, perp_s)) != "live") if sim is None else sim
        if write_checks:
            self._common_checks(cfg, sim, "вход", *_cmd_cv(spot_s, perp_s))
            con = self.conns.get()
            mx = cfg.get("limits.max_open_deals")
            n_open = len(store.active_deals(con))
            if mx is not None and n_open >= mx:
                raise Refused(f"открытых сделок {n_open} из {mx} (max_open_deals) — вход запрещён")
            self._daily_stop_check(cfg, sim)
        cap = cfg.get("limits.deal_max_usd_per_leg")
        if cap is not None and usd > cap:
            raise Refused(f"{v.leg(usd)} больше лимита сделки на ногу {v.leg(cap)} (deal_max_usd_per_leg)")
        # множитель контракта — только с разрешения владельца (cfg: свежий у предложения, замороженный у перекотировки;
        # исполнитель ещё раз читает свежий в _entry_limits)
        allow = _allow_multiplier(cfg, pair.venue if pair is not None else _cmd_cv(spot_s, perp_s)[1])
        frozen = pair.spec if pair is not None else None
        pair = replace(pair) if pair is not None else self.find_pair(coin, spot_s, perp_s, allow_multiplier=allow)
        if write_checks:
            for d in store.active_deals(self.conns.get()):
                if d["token"] == pair.token or (d["perp_venue"] == pair.venue and d["symbol"] == pair.symbol):
                    st = v.DEAL_STATE_LABEL.get(d["state"], d["state"])
                    raise Refused(f"по {coin} уже есть сделка {d['id']} ({st}) — вход запрещён")
        legs = self._legs_for(pair.chain, pair.venue, sim)
        stable, sdec = config.OKX_DEX_STABLES[tconfig.chain_index(pair.chain)]
        total = int((usd * D(10) ** sdec).to_integral_value(ROUND_FLOOR))
        quotes = self._quotes(legs.spot, stable, pair.token, total)
        pair.token_dec = self._token_dec(legs.spot, pair.token, quotes)
        if frozen is not None and pair.token_dec != frozen.token_dec:
            raise Refused(f"decimals токена {coin} на цепи ({pair.token_dec}) ≠ сделке ({frozen.token_dec}) — "
                                    "это другой актив, нужен новый план")
        try:
            calib = planner.calibrate(quotes, "entry", p_ref=legs.spot.pool_price(pair.token))
            bal = legs.spot.balances(pair.token)
            approve_usd = ZERO if _allowance_ok(legs.spot, stable, total) else calib.g
            mkt = self._market(legs, pair, bal, calib, approve_usd)
            # инструмент (ревью 13.09, С1) — ДО плана: m с биржи, база = монета, цена контракта / m — цена ОДНОГО
            # токена на DEX; m ≠ 1 — только с разрешения владельца. План считает контракты = токены / m
            bid = mkt.book.bids[0][0] if mkt.book.bids else None
            pair.spec = self._instrument(pair, legs, bid, calib.p_ref, allow=allow)
            if frozen is not None and pair.spec.inst_hash() != frozen.inst_hash():
                was, now = frozen.m, pair.spec.m     # биржа сменила контракт (m) после входа — старый план не про него
                ch = f" (m {_dn(was)} → {_dn(now)})" if was != now else ""
                raise Refused(f"контракт {pair.symbol} на бирже изменился{ch} — нужен новый план")
            lim = planner.limits_from_owner(cfg, pair.venue, pair.chain)
            plan = planner.plan(deal_id="", kind="entry", coin=coin, spot=spot_s, perp=perp_s, symbol=pair.symbol,
                                leg_usd=usd, total_in_units=total, dec_in=sdec, calib=calib, mkt=mkt, lim=lim,
                                now=self.clock(), units_per_contract=pair.spec.m, carry0=carry0)
        except PlanRefused as e:
            raise Refused(str(e)) from None
        plan.inputs["inst_hash"] = pair.spec.inst_hash()
        notes = self._entry_live_notes(cfg, legs, pair, bal, total, usd, sdec, plan)
        if notes and not sim:
            raise Refused("; ".join(notes))
        ctx = {"pair": pair, "sim": sim, "cfg": cfg, "stable": stable, "sdec": sdec, "total": total, "bal": bal,
               "mkt": mkt, "notes": notes, "legs": legs, "inst": pair.spec, "m": pair.spec.m}
        return plan, ctx

    def _instrument(self, pair: PairInfo, legs: Legs, bid: D | None, p_ref: D | None, *,
                    allow: bool) -> InstrumentSpec:
        """Спецификация инструмента входа (ревью 13.09, С1): m — с биржи (baseAsset exchangeInfo), база контракта —
        та же монета, множитель — только с разрешения владельца, а цена контракта / m ≈ цене токена на DEX
        (UNIT_PX_RATIO_MAX — единицы, не видные ни в имени, ни в baseAsset). Любое «не знаю» — отказ до свопа."""
        v = formatters
        venue = v.VENUE_LABEL.get(pair.venue, pair.venue)
        fn = getattr(legs.perp, "instrument", None)
        pi, err = None, "нога не отдаёт контракт"
        if fn is not None:
            try:
                pi = fn(pair.symbol)
                err = "exchangeInfo без baseAsset"
            except Exception as e:             # noqa — exchangeInfo не прочитан: m не известен, вход не начинаем
                err = f"exchangeInfo не прочитан: {redact(e)}"
        if pi is None or pi.m is None or pi.base is None:
            raise Refused(f"множитель контракта {pair.symbol} на {venue} не известен ({err}) — не торгую")
        if str(pi.base).upper() != pair.coin.upper():
            raise Refused(f"контракт {pair.symbol} — это {pi.base}, а не {pair.coin}: другой актив, не торгую")
        m = unit_refusal(pair.coin, pair.symbol, m=pi.m, allow_multiplier=allow, venue=pair.venue)
        if not bid or not p_ref:
            raise Refused(f"цена для сверки единиц {pair.symbol} не получена — не торгую")
        ratio = (bid / m) / p_ref
        if not (D(1) / tconfig.UNIT_PX_RATIO_MAX <= ratio <= tconfig.UNIT_PX_RATIO_MAX):
            raise Refused(f"цена {pair.symbol} {v.num(bid / m, 6)} против {v.num(p_ref, 6)} за токен на DEX — "
                                    f"единицы не сходятся (множитель контракта или не тот токен): не торгую")
        return InstrumentSpec(chain=pair.chain, token=pair.token.lower(), token_dec=int(pair.token_dec),
                              perp_venue=pair.venue, perp_symbol=pair.symbol, units_per_contract=m,
                              perp_base_asset=pi.base_asset, quote_asset=pi.quote_asset, contract_type=pi.contract_type,
                              period_h=pair.period_h, ident_ev=pair.ident_ev, source="exchangeInfo:baseAsset",
                              verified=True, verified_ts=self.clock(), px_ratio=ratio)

    def _entry_live_notes(self, cfg, legs: Legs, pair: PairInfo, bal: dict, total: int, usd: D, sdec: int,
                          plan: Plan | None = None) -> list[str]:
        """Предпроверки, которые в live — отказ, а в симуляции — заметка в плане."""
        v = formatters
        notes = []
        vl = v.VENUE_LABEL.get(pair.venue, pair.venue)
        stn, nat = tconfig.STABLE_SYMBOL.get(pair.chain, "USDT"), tconfig.NATIVE_SYMBOL.get(pair.chain, "BNB")
        st = bal.get("stable")
        if st is None:
            notes.append(f"баланс {stn} не прочитан")
        elif st < total:
            notes.append(f"{stn} в кошельке {v.num(D(st) / D(10) ** sdec)} — меньше {v.leg(usd)} на ногу")
        if bal.get("native") is None:
            notes.append(f"баланс {nat} не прочитан — газ не проверен")
        lev = cfg.get(f"perp.{pair.venue}.leverage")
        mg = legs.perp.available_margin()
        # маржа — на заявки плана (Σ кол-во · кэп дочерних: контракты · цена контракта, $), но не меньше суммы на ногу
        # (прежняя мера при m = 1): план с перепутанными единицами (ревью 13.09, С1: ×1000) не пройдёт и здесь
        need = max([usd] + ([sum((q * cap for cp in plan.clips for q, cap in cp.children), ZERO)] if plan else []))
        m = dget((plan.est or {}).get("units_per_contract")) if plan else None
        caps = [cap for cp in plan.clips for _q, cap in cp.children] if plan else []
        step, toks = (dget(((plan.inputs or {}).get("filters") or {}).get("step")), dget((plan.est or {}).get("tokens"))) \
            if plan else (None, None)
        if m is not None and m != 1 and caps and step and toks is not None:
            # страховка (ревью 13.09, M1): все токены плана одним счётом — floor(Σ/m) к шагу по кэпу лучшей дочерней
            # (технический инвариант в единицах контракта; исполнитель берёт контракты по стакану на факт прихода)
            need = max(need, floor_step(toks / m, step) * max(caps))
        if mg is None:
            notes.append(f"маржа {vl} не прочитана")
        elif lev is not None and mg < need / D(lev):
            notes.append(f"маржа {vl} {v.num(mg)} — меньше {v.num(need / D(lev))} USDT (сумма / плечо {lev}x)")
        br = getattr(legs.perp, "leverage_bracket", None)
        if not legs.sim and br is not None and lev is not None:
            try:
                rows = br(pair.symbol)
                rows = rows if isinstance(rows, list) else [rows]
                caps = [int(b.get("initialLeverage")) for r in rows if isinstance(r, dict)
                        for b in (r.get("brackets") or []) if b.get("initialLeverage") is not None]
                if caps and max(caps) < lev:
                    notes.append(f"плечо {lev}x больше допустимого {max(caps)}x (leverageBracket)")
            except Exception as e:             # noqa
                notes.append(f"leverageBracket не прочитан: {redact(e)}")
        if self.keys_mode and cfg.mode == "live" and self.keys_mode != "live":
            notes.append(f"в owner.toml live, но служба запущена в {self.keys_mode} — нужен перезапуск")
        guard_notes = getattr(legs.spot, "guard_notes", None)
        if guard_notes:
            notes.append(guard_notes[-1])
        return notes

    def _daily_stop_check(self, cfg: OwnerCfg, sim: bool) -> None:
        stop = cfg.get("limits.daily_loss_stop_usd")
        if not isinstance(stop, D) or sim:
            return
        basis = cfg.get("limits.daily_loss_basis")
        if basis != "realized_costs":
            raise Refused(f"дневной стоп по «{basis}» пока не считается — вход запрещён")
        day0 = math.floor(self.clock() / 86400) * 86400
        used = ZERO
        from . import accounting
        con = self.conns.get()
        for r in con.execute("SELECT json,deal_id,intent_id FROM exec_events WHERE kind='final' AND ts>=?", (day0,)):
            if accounting.is_bound(con, r[1]):
                try:
                    cost = accounting.event_cost(con, r[1], r[2], json.loads(r[0]))
                except Exception:
                    cost = None
                if cost is None:
                    raise Refused('дневные издержки не подтверждены для счёта — вход запрещён')
                used += cost
                continue
            try:
                used += D(str(json.loads(r[0]).get("cost_usd") or 0))
            except (ValueError, InvalidOperation, AttributeError):
                continue
        if used >= stop:
            v = formatters
            raise Refused(f"дневной стоп: издержки сегодня {v.money(used, html=False)} ≥ {v.leg(stop)} — "
                                    "вход запрещён")

    def _root_proposal(self, deal, kind, spec, plan, chat, operation_id=None):
        from .operation_roots import propose
        profile = owner_mod.EVM_PROFILE_OF.get(_deal_cv(deal), owner_mod.LEGACY_PROFILE)
        spec = dict(spec)
        spec["approval"] = {
            "side": kind,
            "target_raw": sum(c.dex_in_units for c in plan.clips) if kind == "entry" else int(spec["units"]),
            "approved_total_pct": plan.est.get("total_pct"),
            "plan_cost_drift_pct": OwnerCfg.from_frozen(spec["owner"]).get("exec.plan_cost_drift_pct"),
        }
        if kind == "entry":
            spec["approval"]["funding_h"] = spec.get("funding_h")
        return propose(self.conns.get(), deal=deal, kind=kind, spec=spec, plan=plan,
                       profile_id=profile, chat=chat, operation_id=operation_id)

    def propose_entry(self, coin: str, spot_s: str, perp_s: str, usd: D, chat: int | None,
                      deal_id: str | None = None) -> Proposal:
        plan, ctx = self.plan_entry(coin, spot_s, perp_s, usd)
        con = self.conns.get()
        pair, cfg, sim, inst = ctx["pair"], ctx["cfg"], ctx["sim"], ctx["inst"]
        if deal_id is None:
            deal_id = store.create_deal(con, coin=coin, chain=pair.chain, token=pair.token, token_dec=pair.token_dec,
                                        perp_venue=perp_s, symbol=pair.symbol, leg_usd=usd,
                                        owner_json=cfg.frozen_json(), sim=sim, inst=inst)
            from .adapters.execution_scope import bind_draft
            bind_draft(con, store.get_deal(con, deal_id), ctx['legs'].perp)
        else:                                  # перекотировка черновика (bot.requote): тот же инструмент — или новый вход
            raw = (store.get_deal(con, deal_id) or {}).get("inst_json")
            if not raw:
                store.set_deal_inst(con, deal_id, inst.to_json())
            else:
                try:
                    was = InstrumentSpec.from_json(raw).inst_hash()
                except ValueError:
                    was = None
                if was != inst.inst_hash():
                    raise Refused(f"инструмент {coin} у кнопки другой ({pair.token}/{pair.symbol}) — "
                                                   "это новый вход, пришлите команду заново")
        plan.deal_id = deal_id
        spec = {"kind": "entry", "coin": coin, "spot": spot_s, "perp": perp_s, "usd": usd, "token": pair.token,
                "token_dec": pair.token_dec, "symbol": pair.symbol, "period_h": pair.period_h, "sim": sim,
                "owner": cfg.frozen_json(), "funding_h": ctx["mkt"].funding_h,
                "instrument": inst.as_dict(), "inst_hash": inst.inst_hash()}
        iid, nonce = self._root_proposal(store.get_deal(con, deal_id), "entry", spec, plan, chat)
        facts = self.plan_view(iid, plan, ctx)
        store.event(con, "proposed", deal_id=deal_id, intent_id=iid, total_usd=plan.est.get("total_usd"),
                    n=plan.est.get("n"), sim=sim)
        return Proposal(iid, nonce, deal_id, "entry", 'plan', facts, plan)

    # --- выход ---
    def resolve_deal(self, target: str) -> dict:
        """id сделки (или намерения) либо монета → активная сделка. Монета той же формы, что id, — сверка с БД."""
        con = self.conns.get()
        t = (target or "").upper()
        d = store.get_deal(con, t)
        if d is None:
            it = store.get_intent(con, t)
            if it is not None:
                d = store.get_deal(con, it["deal_id"])
        if d is None:
            act = [x for x in store.active_deals(con) if x["coin"].upper() == t]
            if len(act) > 1:
                raise Refused(f"по {t} несколько сделок — укажите id: "
                                               + ", ".join(x["id"] for x in act))
            d = act[0] if act else None
        if d is None:
            raise Refused(f"сделки «{t}» нет")
        return d

    def plan_exit(self, deal: dict, usd: D | None, perp_only: bool, *, units: int | None = None,
                  cfg: OwnerCfg | None = None, write_checks: bool = True, to_full: bool = True) -> tuple[Plan, dict]:
        """План выхода. units — цель частичного выхода в сырых токенах, замороженная в намерении (возобновление,
        перекотировка у кнопки): $ в токены заново не переводятся (ревью 13.09, Н1). Иначе usd → токены по цене пула
        (новая команда владельца), usd None — весь спот сделки.
        m ≠ 1 (ревью 13.09, M2): частичный выход, который откупил бы весь шорт или оставил меньше шага·m токенов, —
        полный (план и шапка так и называют); to_full=False (перекотировка одобренного частичного у кнопки) — отказ,
        одобрено было другое. m не известен (M3) — только полный выход без сверки дельты (_plan_blind_exit)."""
        v = formatters
        cfg = cfg or self.cfg()
        sim = bool(deal["sim"])
        if write_checks:
            self._common_checks(cfg, sim, "выход", *_deal_cv(deal))
        if deal["state"] not in (DealState.OPEN, DealState.PAUSED):
            st = v.DEAL_STATE_LABEL.get(deal["state"], deal["state"])
            raise Refused(f"сделка {deal['id']} {st} — выход не начинаю")
        legs = self._deal_legs(deal)
        con = self.conns.get()
        dec = int(deal["token_dec"])
        bk = deal_book(con, deal["id"])
        if not bk.known:
            raise Refused(f"книга сделки неизвестна ({bk.why}) — сначала «позиции»")
        inst = deal_instrument(con, deal)
        pair = self.pair_from(inst, deal, verify_table=False)     # инструмент сделки, таблица не нужна (Н2)
        if not bk.inst_ok and (usd is not None or units is not None) and not perp_only:
            # m не подтверждён (ревью 13.09, R6): частичный выход с неверным m снял бы шорт больше хеджа; целиком —
            # весь шорт при любом m
            raise Refused(f"инструмент сделки {deal['id']} не подтверждён ({bk.inst_why}) — частичный выход "
                                    f"не посчитать, только «выход {deal['id']}» целиком")
        f = legs.perp.filters(deal["symbol"])
        delta, ts = bk.delta(dec), bk.tstep(f.step)            # дельта и шаг — в токенах (m токенов в контракте)
        if bk.m_known and not (ZERO <= delta < ts) and not perp_only:
            raise Refused(f"ноги не ровно: без хеджа {v.tok(delta, True, ts)} {deal['coin']} — сначала "
                                    f"«дохедж {deal['id']}» или «откат {deal['id']}»")
        stable, sdec = config.OKX_DEX_STABLES[tconfig.chain_index(deal["chain"])]
        wallet_units = bk.tokens_raw
        if not sim:
            wal = legs.spot.balances(deal["token"]).get("token")
            if wal is None:
                raise Refused("баланс токена в кошельке не прочитан — выход не начинаю")
            wallet_units = min(wallet_units, int(wal))
        ctx = {"pair": pair, "sim": sim, "cfg": cfg, "stable": stable, "sdec": sdec, "deal": deal, "book": bk,
               "legs": legs, "notes": [], "perp_only": perp_only, "usd": usd, "inst": inst, "m": bk.m_view}
        if perp_only:
            plan = self._plan_perp_only(deal, bk, f, legs, cfg, ctx)
            plan.inputs["inst_hash"] = inst.inst_hash()
            return plan, ctx
        if not bk.m_known:                     # частичный уже отказан выше (inst_ok): остаётся только полный
            plan = self._plan_blind_exit(deal, bk, f, legs, cfg, ctx, wallet_units)
            plan.inputs["inst_hash"] = inst.inst_hash()
            return plan, ctx
        target, units = units, wallet_units
        full = usd is None and target is None
        if target is not None:                 # цель в токенах уже заморожена — цена пула её не меняет
            units = min(units, int(target))
            full = units >= bk.tokens_raw
        elif not full:
            p = legs.spot.pool_price(deal["token"])
            if p is None or p <= 0:
                raise Refused("цена токена на DEX не получена — сумму в токены не перевести")
            units = min(units, int((usd / p * D(10) ** dec).to_integral_value(ROUND_FLOOR)))
            full = units >= bk.tokens_raw
        if not full and bk.m != 1 and units > 0 and _partial_closes_short(bk, units, dec, f.step):
            # ревью 13.09, M2: исполнитель откупил бы весь шорт, а меньше шага·m токенов осталось бы вне учёта (CLOSED с
            # ложным итогом) — это полный выход; одобренный частичный у кнопки в полный не превращается
            left = D(bk.tokens_raw - units) / D(10) ** dec
            if not to_full:
                raise Refused(f"частичный выход теперь откупил бы весь шорт (осталось бы {v.tok(left, step=ts)} "
                                        f"{deal['coin']}) — нужен новый план: «выход {deal['id']}»")
            full, units = True, wallet_units
            ctx["m2_full"] = True              # «продолжить» берёт отсюда all=True (полный выход, а не частичный)
            ctx["notes"].append(f"остаток {v.tok(left, step=ts)} {deal['coin']} меньше "
                                f"{v.contracts(f.step, bk.m, step=f.step)} — выход всей сделки")
        if units <= 0:
            raise Refused("продавать нечего: токенов сделки в кошельке 0")
        quotes = self._quotes(legs.spot, deal["token"], stable, units)
        try:
            calib = planner.calibrate(quotes, "exit", p_ref=legs.spot.pool_price(deal["token"]))
            bal = legs.spot.balances(deal["token"])
            approve_usd = ZERO if (sim or _allowance_ok(legs.spot, deal["token"], units)) else calib.g
            mkt = self._market(legs, pair, bal, calib, approve_usd)
            lim = _lim(cfg, deal)
            plan = planner.plan(deal_id=deal["id"], kind="exit", coin=deal["coin"], spot=f"okx·{deal['chain']}",
                                perp=deal["perp_venue"], symbol=deal["symbol"], leg_usd=D(units) / D(10) ** dec *
                                calib.p_ref, total_in_units=units, dec_in=dec, calib=calib, mkt=mkt, lim=lim,
                                now=self.clock(), units_per_contract=bk.m, carry0=delta)
        except PlanRefused as e:
            raise Refused(str(e)) from None
        plan.inputs["inst_hash"] = inst.inst_hash()           # перекотировка у кнопки сверит с намерением
        ctx.update(units=units, full=full, bal=bal, mkt=mkt)
        return plan, ctx

    def _period(self, deal: dict) -> D:
        return _period_of(self.conns.get(), deal["id"])

    def _plan_perp_only(self, deal, bk: DealBook, f, legs: Legs, cfg, ctx) -> Plan:
        """«выход <id> перп»: откупить весь шорт reduceOnly, спот не трогать (явная команда владельца)."""
        book = legs.perp.book(deal["symbol"], tconfig.ASTER_DEPTH_LIMIT)
        lim = _lim(cfg, deal)
        fee = D(str(config.FEES_TAKER[legs.perp.venue]))
        try:
            pk = planner.pick_perp(book, "BUY", bk.short, f, fee, lim, reduce_only=True)
        except PlanRefused as e:
            raise Refused(str(e)) from None
        ch, pc = pk.children, pk.cost
        mid = planner.mid(book)
        est = {"n": 1, "clip_usd": pc.notional, "m": len(ch), "children": len(ch),
               "tokens": bk.short * bk.m if bk.m_known else None, "contracts": bk.short,
               "units_per_contract": bk.m if bk.m_known else None,
               "perp_spread_usd": pc.spread, "perp_fee_usd": pc.fee, "total_usd": pc.total,
               "total_pct": (pc.total / pc.notional * 100) if pc.notional else None, "perp_only": True,
               **pk.est()}                   # α/β (подобранные «auto» — тоже) и предел времени — с планом
        ctx.update(units=0, full=False, bal=legs.spot.balances(deal["token"]))
        return Plan(deal_id=deal["id"], kind="exit", coin=deal["coin"], spot=f"okx·{deal['chain']}",
                    perp=deal["perp_venue"], symbol=deal["symbol"], leg_usd=pc.notional,
                    clips=[ClipPlan(seq=1, dex_in_units=0, children=ch)], est=est,
                    inputs={"book_top": {"mid": mid}, "fee_taker": fee, "size_usd": pc.notional},
                    missing_owner_keys=_live_miss(cfg, deal), expires=self.clock() + tconfig.PLAN_TTL_S)

    def _plan_blind_exit(self, deal, bk: DealBook, f, legs: Legs, cfg, ctx, units: int) -> Plan:
        """Полный выход сделки, у которой m не известен (ревью 13.09, M3): дельту ног в токенах не посчитать — план её
        не сверяет и выход на клипы не делит. Весь спот сделки (min журнала и кошелька) — одним свопом, затем весь шорт
        журнала (контракты) — BUY reduceOnly. Ни то, ни другое от m не зависит (как «выход перп» и откат)."""
        v = formatters
        dec, stable = int(deal["token_dec"]), ctx["stable"]
        lim = _lim(cfg, deal)
        fee = D(str(config.FEES_TAKER[legs.perp.venue]))
        book = legs.perp.book(deal["symbol"], tconfig.ASTER_DEPTH_LIMIT)
        ch, pc = [], planner.PerpCost(spread=ZERO, fee=ZERO, notional=ZERO)
        ab: dict = {"alpha": None, "beta_bps": None, "ab_band": False}  # шорта нет — заявок нет, «auto» не нужно
        try:
            if bk.short > 0:
                pk = planner.pick_perp(book, "BUY", bk.short, f, fee, lim, reduce_only=True)
                ch, pc, ab = pk.children, pk.cost, pk.est()
            quotes = self._quotes(legs.spot, deal["token"], stable, units) if units > 0 else []
            calib = planner.calibrate(quotes, "exit", p_ref=legs.spot.pool_price(deal["token"])) if quotes else None
        except PlanRefused as e:
            raise Refused(str(e)) from None
        toks = D(units) / D(10) ** dec
        S = toks * calib.p_ref if calib is not None else ZERO
        dex = planner.dex_cost(1, S, calib.k, calib.g, tconfig.R_PRIOR, calib.c0) if calib is not None else None
        approve_usd = ZERO if (calib is None or ctx["sim"] or _allowance_ok(legs.spot, deal["token"], units)) \
            else calib.g
        total = (dex.total if dex is not None else ZERO) + pc.total + approve_usd
        notional = S + pc.notional
        est = {"n": 1, "clip_usd": S, "m": len(ch), "children": len(ch), "tokens": toks,
               "dex_px": calib.p_ref if calib is not None else None, "contracts": bk.short, "units_per_contract": None,
               "dex_fee_usd": dex.fee if dex else ZERO, "dex_impact_usd": dex.impact if dex else ZERO,
               "gas_usd": dex.gas if dex else ZERO, "gas_per_swap_usd": calib.g if calib is not None else ZERO,
               "approve_usd": approve_usd, "perp_spread_usd": pc.spread, "perp_fee_usd": pc.fee,
               "perp_no_refill_usd": planner.perp_cost(ch, book, "BUY", fee, refill=False).total if ch else ZERO,
               "pace_usd": ZERO, "total_usd": total, "notional_usd": notional,
               "total_pct": (total / notional * 100) if notional else None, "basis_bps": None,
               "unhedged_risk_usd": None, "blind": True, **ab}
        ctx.update(units=units, full=True, bal=legs.spot.balances(deal["token"]))
        ctx["notes"].append("множитель контракта не известен — продаю весь спот и откупаю весь шорт")
        return Plan(deal_id=deal["id"], kind="exit", coin=deal["coin"], spot=f"okx·{deal['chain']}",
                    perp=deal["perp_venue"], symbol=deal["symbol"], leg_usd=S,
                    clips=[ClipPlan(seq=1, dex_in_units=units, children=ch)], est=est,
                    inputs={"calib": calib.as_dict() if calib is not None else {}, "book_top": {"mid": planner.mid(book)},
                            "filters": {"step": f.step, "tick": f.tick}, "fee_taker": fee, "size_usd": S,
                            "r": tconfig.R_PRIOR, "side": "BUY", "reduce_only": True},
                    missing_owner_keys=_live_miss(cfg, deal), expires=self.clock() + tconfig.PLAN_TTL_S)

    def propose_exit(self, target: str, usd: D | None, perp_only: bool, chat: int | None) -> Proposal:
        deal = self.resolve_deal(target)
        if is_sol_deal(deal):                  # связка SOL × HL: свои ноги и правила (sol_flow)
            from .runtime import profile_of_deal
            return self.sol(profile_of_deal(deal)).propose_exit(deal, usd, perp_only, chat)
        plan, ctx = self.plan_exit(deal, usd, perp_only)
        cfg = ctx["cfg"]
        inst = ctx["inst"]
        spec = {"kind": "exit", "coin": deal["coin"], "usd": usd, "units": ctx.get("units", 0),
                "all": bool(ctx.get("full")), "perp_only": perp_only, "token": deal["token"],
                "token_dec": int(deal["token_dec"]), "symbol": deal["symbol"], "period_h": ctx["pair"].period_h,
                "sim": bool(deal["sim"]), "owner": cfg.frozen_json(), "instrument": inst.as_dict(),
                "inst_hash": inst.inst_hash(),
                "root": None, "root_units": ctx.get("units", 0)}      # корень цепочки «продолжить» — само намерение
        con = self.conns.get()
        if perp_only:
            iid, nonce = store.create_intent(con, deal_id=deal["id"], kind="exit", spec=spec, plan=plan, chat=chat)
        else:
            iid, nonce = self._root_proposal(deal, "exit", spec, plan, chat)
        facts = self.plan_view(iid, plan, ctx)
        store.event(con, "proposed", deal_id=deal["id"], intent_id=iid, total_usd=plan.est.get("total_usd"),
                    sim=bool(deal["sim"]))
        return Proposal(iid, nonce, deal["id"], "exit", 'plan', facts, plan)

    # --- дохедж / откат / продолжить ---
    def _deficit(self, deal: dict) -> tuple[Legs, DealBook, Any, D]:
        v = formatters
        legs = self._deal_legs(deal)
        bk = deal_book(self.conns.get(), deal["id"])
        if not bk.known:
            raise Refused(f"книга сделки неизвестна ({bk.why}) — сначала «позиции»")
        f = legs.perp.filters(deal["symbol"])
        return legs, bk, f, bk.delta(int(deal["token_dec"]))

    def propose_fix(self, kind: str, target: str, chat: int | None) -> Proposal:
        """«дохедж <id>» (перп на голую часть) и «откат <id>» (продать голый лонг на DEX) — тоже только кнопкой."""
        v = formatters
        cfg = self.cfg()
        deal = self.resolve_deal(target)
        if is_sol_deal(deal):
            from .runtime import profile_of_deal
            return self.sol(profile_of_deal(deal)).propose_fix(kind, deal, chat)
        self._common_checks(cfg, bool(deal["sim"]), "дохедж" if kind == "rehedge" else "откат", *_deal_cv(deal))
        if deal["state"] not in (DealState.PAUSED, DealState.OPEN):
            raise Refused(f"сделка {deal['id']} {v.DEAL_STATE_LABEL.get(deal['state'], deal['state'])}")
        legs, bk, f, delta = self._deficit(deal)
        if not bk.m_known:                     # ревью 13.09, M3: дельта по m = 1 продала бы захеджированные токены
            raise Refused(f"{v.m_unknown_text(deal['id'])} ({bk.inst_why}): дохедж и откат не посчитать")
        dec = int(deal["token_dec"])
        inst = deal_instrument(self.conns.get(), deal)
        spec = {"kind": kind, "coin": deal["coin"], "token": deal["token"], "token_dec": dec, "symbol": deal["symbol"],
                "sim": bool(deal["sim"]), "owner": cfg.frozen_json(), "instrument": inst.as_dict(),
                "inst_hash": inst.inst_hash()}
        px = planner.mid(legs.perp.book(deal["symbol"], 5))
        ab: dict = {}
        side, qty = None, None
        ts = bk.tstep(f.step)                  # дельта — в токенах; qty дохеджа — контракты (m токенов в контракте)
        if kind == "rehedge":
            if delta >= ts:
                side, qty = "SELL", floor_step(delta / bk.m, f.step)
            elif delta < 0:
                side, qty = "BUY", min(ceil_step(-delta / bk.m, f.step), bk.short)
            else:
                raise Refused(f"ноги ровно (дельта {v.tok(delta, True, ts)} меньше шага) — дохеджировать "
                                        "нечего")
            if side == "SELL" and not bk.inst_ok:      # наращивать шорт при неизвестном m нельзя (ревью 13.09, R6)
                raise Refused(f"инструмент сделки {deal['id']} не подтверждён ({bk.inst_why}) — дохедж "
                                        f"продажей запрещён; «откат {deal['id']}» или «выход {deal['id']}»")
            if side == "SELL" and bk.m != 1:           # продажа контрактов с множителем — только с разрешения (R8)
                vn = _deal_cv(deal)[1]
                unit_refusal(deal["coin"], deal["symbol"], m=bk.m, venue=vn, allow_multiplier=_allow_multiplier(cfg, vn))
            spec.update(side=side, qty=qty)
            ab = self._fix_ab(legs, deal, f, side, qty, cfg)
        else:
            if delta < ts:
                raise Refused("откат — только для голого лонга (дельта ≥ шага); голый шорт — «дохедж»")
            qty = delta - (delta % ts)                 # токены: кратно шагу·m — остаток остаётся захеджированным
            units = int((qty * D(10) ** dec).to_integral_value(ROUND_FLOOR))
            spec.update(units=units)
        plan = Plan(deal_id=deal["id"], kind=kind, coin=deal["coin"], spot=f"okx·{deal['chain']}",
                    perp=deal["perp_venue"], symbol=deal["symbol"], leg_usd=(abs(delta) * px / bk.m) if px else ZERO,
                    clips=[], est={"delta": delta, **ab}, inputs={"inst_hash": inst.inst_hash()},
                    missing_owner_keys=_live_miss(cfg, deal), expires=self.clock() + tconfig.PLAN_TTL_S)
        con = self.conns.get()
        iid, nonce = store.create_intent(con, deal_id=deal["id"], kind=kind, spec=spec, plan=plan, chat=chat)
        facts = dict(intent_id=iid, kind=kind, coin=deal["coin"], deal_id=deal["id"], delta=delta,
                    qty=qty, side=side, usd=plan.leg_usd, perp_venue=deal["perp_venue"],
                    step=f.step, ttl_s=tconfig.PLAN_TTL_S, sim=bool(deal["sim"]), m=bk.m)
        return Proposal(iid, nonce, deal["id"], kind, 'fix_plan', facts, plan)

    def _fix_ab(self, legs: Legs, deal: dict, f, side: str, qty: D, cfg: OwnerCfg) -> dict:
        """α/β дохеджа — в план: исполнитель берёт их оттуда. Числа владельца — как есть (стакан проверит исполнитель,
        как раньше); «auto» — подбор по стакану сейчас, заявки пойдут ровно с ним."""
        lim = _lim(cfg, deal)
        if lim.alpha != planner.AUTO and lim.beta_bps != planner.AUTO:
            return {"alpha": lim.alpha, "beta_bps": lim.beta_bps, "ab_band": False, "alpha_auto": False,
                    "beta_auto": False}
        book = legs.perp.book(deal["symbol"], tconfig.ASTER_DEPTH_LIMIT)
        fee = D(str(config.FEES_TAKER[legs.perp.venue]))
        try:
            return planner.pick_perp(book, side, qty, f, fee, lim, reduce_only=side == "BUY").est()
        except PlanRefused as e:
            raise Refused(str(e)) from None

    def propose_resume(self, target: str, chat: int | None) -> Proposal | Notice:
        """«продолжить <id>»: HALTED_MISMATCH — сверка и (если сошлось) PAUSED; иначе свежий план на остаток
        прерванного входа или выхода. Сам исполнитель ничего не продолжает."""
        v = formatters
        deal = self.resolve_deal(target)
        if is_sol_deal(deal):
            return self.sol().propose_resume(deal, chat)
        con = self.conns.get()
        last = con.execute("SELECT * FROM intents WHERE deal_id=? AND kind IN ('entry','exit') "
                           "AND status NOT IN ('proposed','rejected','expired') ORDER BY created DESC "
                           "LIMIT 1", (deal["id"],)).fetchone()
        from .adapters.obligations import unresolved
        if unresolved(con, deal):
            intent = f" ({last['id']})" if last is not None else ""
            raise Refused(f'исход прошлой отправки{intent} неизвестен — сначала «позиции»')
        if deal["state"] == DealState.HALTED_MISMATCH:
            from . import reconcile
            legs = self._deal_legs(deal)
            chk = reconcile.check_deal(con, deal, legs)
            if chk.matched:
                store.set_deal_state(con, deal["id"], DealState.PAUSED, reason="сверено владельцем")
                return Notice('resume_checked', {'deal_id': deal["id"], 'sim': bool(deal["sim"])})
            detail = chk.detail if chk.detail is None or type(chk.detail) is str else str(chk.detail)
            return Notice('resume_mismatch', {'deal_id': deal["id"], 'detail': detail, 'sim': bool(deal["sim"])})
        if last is None:
            raise Refused("у сделки нет входа — продолжать нечего")
        spec = json.loads(last["spec_json"])
        op = None
        if not spec.get('perp_only'):
            from .operation_roots import adopt_legacy
            try:
                oid = adopt_legacy(con, deal)
                op = store.get_operation(con, oid) if oid else None
            except (store.StoreError, ValueError) as e:
                raise Refused(f'цель операции не подтверждена: {e}') from None
            if op and int(op['reserved_raw']):
                raise Refused('исход прошлой отправки неизвестен — сначала «позиции»')
            if op and op['state'] == store.OpState.PAUSED_UNKNOWN:
                store.set_operation_state(con, op['id'], store.OpState.STOPPED, reason='reservation resolved')
        if last["kind"] == "entry" and last["status"] in (IntentStatus.PARTIAL, IntentStatus.INTERRUPTED,
                                                          IntentStatus.FAILED):
            if not op:
                raise Refused('нет подтверждённой корневой цели входа')
            stable, sdec = config.OKX_DEX_STABLES[tconfig.chain_index(deal["chain"])]
            rest = D(store.operation_remaining(op)) / D(10) ** sdec
            if rest <= 0:
                raise Refused("вход уже набран полностью")
            if deal["state"] not in (DealState.PAUSED, DealState.OPEN):
                raise Refused(f"сделка {deal['id']} {v.DEAL_STATE_LABEL.get(deal['state'], deal['state'])}")
            return self._propose_entry_more(deal, rest, spec, chat, operation_id=op["id"])
        if last["kind"] == "exit" and last["status"] in (IntentStatus.PARTIAL, IntentStatus.INTERRUPTED,
                                                         IntentStatus.FAILED):
            if not spec.get("perp_only") and op:
                # ревью 13.09, Н1: повтор исходной суммы продал бы её ещё раз (150-200 % заказанного) — остаток в
                # токенах от замороженной цели прерванного намерения
                return self._propose_exit_more(deal, dict(last), spec, chat, operation_id=op["id"])
            return self.propose_exit(deal["id"], None if spec.get("all") else dget(spec.get("usd")),
                                     bool(spec.get("perp_only")), chat)          # весь спот / весь шорт — повтор верен
        raise Refused(f"последнее намерение {last['id']} — {last['status']}: продолжать нечего")

    @staticmethod
    def _sold_units(clips) -> int:
        """Сырые токены, проданные клипами выхода: своп ушёл и не откатился (PLANNED — своп не отправлен)."""
        return sum((int(c["dex_in"] or 0) for c in clips
                    if c["state"] not in (ClipState.PLANNED, ClipState.DEX_REVERTED)), 0)

    def _propose_exit_more(self, deal: dict, last: dict, spec0: dict, chat: int | None, *, operation_id=None) -> Proposal:
        """«продолжить» прерванного частичного выхода (ревью 13.09, Н1): остаток = цель прерванного намерения в токенах −
        продано им. Каждое звено хранит свою цель, у кнопки её не пересчитывают — по цепочке это цель корня − Σ продаж.
        Частичный остаётся частичным (all=False); полный выход идёт веткой spec.all и остаётся полным."""
        v = formatters
        cfg = self.cfg()
        sim = bool(deal["sim"])
        self._common_checks(cfg, sim, "выход", *_deal_cv(deal))
        con = self.conns.get()
        dec = int(deal["token_dec"])
        clips = store.clips_of(con, last["id"])
        for c in clips:
            if c["state"] in (ClipState.DEX_SENT, ClipState.DEX_UNKNOWN):
                raise Refused(f"исход клипа {c['seq']} выхода {last['id']} неизвестен — сначала «позиции»")
        sold = self._sold_units(clips)
        try:
            target = int(spec0["units"])
        except (KeyError, TypeError, ValueError):
            target = 0
        if target <= 0:                        # намерение без цели в токенах (план прежнего кода)
            raise Refused(f"частичный выход {last['id']} прерван: продано {v.num(D(sold) / D(10) ** dec)} "
                                    f"{deal['coin']} — остаток не вычислить; «выход {deal['id']} <остаток $>»")
        root = spec0.get("root") or last["id"]
        root_units = int(spec0.get("root_units") or target)
        op = store.get_operation(con, operation_id) if operation_id else None
        if op and int(op['reserved_raw']):
            raise Refused('резерв операции ещё не разрешён')
        rest = store.operation_remaining(op) if op else target - sold
        if op:
            root_units = int(op['target_raw'])
        if rest <= 0:
            ts = D(10) ** dec
            raise Refused(f"выход {root} выполнен: продано {v.tok(D(root_units - rest) / ts)} из "
                                    f"{v.tok(D(root_units) / ts)} {deal['coin']}")
        plan, ctx = self.plan_exit(deal, None, False, units=rest, cfg=cfg, write_checks=False)
        inst = ctx["inst"]
        full = bool(spec0.get("all")) or bool(ctx.get("m2_full"))        # m ≠ 1: остаток меньше шага·m — выход всей сделки (ревью 13.09, M2)
        usd = plan.leg_usd.quantize(D("0.01"), ROUND_HALF_EVEN)  # ≈ $ остатка — только шапка и кнопка («50 из 200 $»)
        spec = {"kind": "exit", "coin": deal["coin"], "usd": usd, "units": int(ctx["units"]), "all": full,
                "perp_only": False, "token": deal["token"], "token_dec": dec, "symbol": deal["symbol"],
                "period_h": ctx["pair"].period_h, "sim": sim, "owner": cfg.frozen_json(), "resume": True,
                "root": root, "root_units": root_units, "instrument": inst.as_dict(), "inst_hash": inst.inst_hash()}
        iid, nonce = self._root_proposal(deal, "exit", spec, plan, chat, operation_id=operation_id)
        store.event(con, "proposed", deal_id=deal["id"], intent_id=iid, total_usd=plan.est.get("total_usd"), sim=sim)
        if not full:
            ctx.update(resume=True, exit_root_units=root_units, usd=usd, full=False)  # «Остаток выхода: 1 222 из 2 445»
        return Proposal(iid, nonce, deal["id"], "exit", 'plan', self.plan_view(iid, plan, ctx), plan)

    def _propose_entry_more(self, deal: dict, usd: D, spec0: dict, chat: int | None, *, operation_id=None) -> Proposal:
        inst = deal_instrument(self.conns.get(), deal)
        if not inst.verified:                  # m не подтверждён (ревью 13.09, R6): наращивать нельзя, закрывать можно
            raise Refused(f"инструмент сделки {deal['id']} не подтверждён ({inst.why}) — добор "
                                           f"запрещён; закрыть: «выход {deal['id']}»")
        cfg = self.cfg()
        self._common_checks(cfg, bool(deal["sim"]), "вход", *_deal_cv(deal))
        # инструмент — замороженный сделки (Н2): таблица только подтверждает, что он не сменился и не отозван
        pair = self.pair_from(inst, deal, verify_table=True, spot_s=spec0["spot"], perp_s=spec0["perp"], cfg=cfg)
        plan, ctx = self.plan_entry(deal["coin"], spec0["spot"], spec0["perp"], usd, cfg=cfg, sim=bool(deal["sim"]),
                                    write_checks=False, pair=pair, carry0=self._carry(deal))
        plan.deal_id = deal["id"]
        self._same_instrument(deal, ctx["pair"])                # последняя сверка: decimals — с цепи
        spec = dict(spec0, usd=usd, owner=cfg.frozen_json(), funding_h=ctx["mkt"].funding_h, resume=True)
        spec.update(token=inst.token, token_dec=inst.token_dec, symbol=inst.perp_symbol, instrument=inst.as_dict(),
                    inst_hash=inst.inst_hash())                 # всё — от инструмента СДЕЛКИ, не свежей строки
        con = self.conns.get()
        iid, nonce = self._root_proposal(deal, "entry", spec, plan, chat, operation_id=operation_id)
        ctx.update(resume=True, deal_leg_usd=dget(deal["leg_usd"]))      # «Остаток входа: 250 из 500 $»
        return Proposal(iid, nonce, deal["id"], "entry", 'plan', self.plan_view(iid, plan, ctx), plan)

    def _carry(self, deal: dict) -> D:
        """Дельта сделки в токенах сейчас — перенос для плана добора (ревью 13.09, M1); неизвестна — 0."""
        return deal_book(self.conns.get(), deal["id"]).delta(int(deal["token_dec"])) or ZERO

    def _same_instrument(self, deal: dict, pair: PairInfo) -> None:
        """Ревью 13.09, Н2: план по свежей строке таблицы, а своп и хедж — по токену и символу сделки. Строка
        сменилась (другой токен/символ/decimals) — это другой актив, одобренный план к нему не относится."""
        _same_identity(deal["coin"], (deal["chain"], str(deal["token"]).lower(), int(deal["token_dec"]), deal["symbol"]),
                       (pair.chain, pair.token.lower(), int(pair.token_dec), pair.symbol))

    def replan(self, it: dict, deal: dict) -> Plan:
        """Перекотировка у кнопки: тот же вход/выход по свежим котировкам и стакану (без записи в БД). Инструмент —
        замороженный сделки (pair_from): меняются только цены; сменилась идентичность — отказ, это новый план."""
        spec = json.loads(it["spec_json"])
        cfg = OwnerCfg.from_frozen(spec["owner"])
        if it["kind"] == "entry":
            inst = deal_instrument(self.conns.get(), deal)
            pair = self.pair_from(inst, deal, verify_table=True, spot_s=spec["spot"], perp_s=spec["perp"], cfg=cfg)
            plan, ctx = self.plan_entry(deal["coin"], spec["spot"], spec["perp"], D(str(spec["usd"])), cfg=cfg,
                                        sim=bool(deal["sim"]), write_checks=False, pair=pair, carry0=self._carry(deal))
            self._same_instrument(deal, ctx["pair"])
            return plan
        if not spec.get("all") and not spec.get("perp_only") and spec.get("units"):
            # частичный выход: цель — токены, одобренные владельцем; $ по цене пула у кнопки не переводятся (Н1);
            # в полный у кнопки не превращается (M2): одобрен был частичный
            return self.plan_exit(deal, None, False, units=int(spec["units"]), cfg=cfg, write_checks=False,
                                  to_full=False)[0]
        return self.plan_exit(deal, None if spec.get("all") else dget(spec.get("usd")), bool(spec.get("perp_only")),
                              cfg=cfg, write_checks=False)[0]

    # --- вид плана ---
    def plan_view(self, iid: str, plan: Plan, ctx: dict) -> dict:
        """Факты сообщения плана (вариант C): видимый каркас + всё, по чему views решает, выносить ли строку ⚠️.
        Техника исполнения (α/β, риск голой ноги, предел времени) заморожена в плане, но в сообщение не идёт.
        Форма — как у tg.views.PlanView (те же имена полей), но словарь: Desk не импортирует tg (AC-07), рендерит
        только interface.presenter.render_proposal_view (topic='plan') из Proposal.view_facts."""
        e, inp = plan.est, plan.inputs
        cfg, pair, sim = ctx["cfg"], ctx["pair"], ctx["sim"]
        entry = plan.kind == "entry"
        perp_only = bool(ctx.get("perp_only"))
        dex_cost = (dget(e.get("dex_fee_usd")) or ZERO) + (dget(e.get("dex_impact_usd")) or ZERO)
        mkt = ctx.get("mkt")
        fh = mkt.funding_h if mkt is not None else None
        sdec = ctx["sdec"]
        dec = pair.token_dec
        if entry:
            clips_usd = tuple(D(c.dex_in_units) / D(10) ** sdec for c in plan.clips)
        else:
            pref = dget((inp.get("calib") or {}).get("p_ref")) or ZERO
            clips_usd = tuple((D(c.dex_in_units) / D(10) ** dec * pref) for c in plan.clips if c.dex_in_units)
        pn = report.plan_numbers(plan) if "dex_fee_usd" in e else {}
        bal = ctx.get("bal") or {}
        tok_h = lambda u, d: None if u is None else D(u) / D(10) ** d
        bk = ctx.get("book")
        if entry:
            token_qty = None
        elif perp_only:                        # спот, что останется без хеджа
            token_qty = bk.tokens(dec) if bk is not None else None
        else:
            token_qty = tok_h(ctx.get("units"), dec)
        deal = ctx.get("deal")
        notes = tuple(ctx.get("notes") or ())
        if bk is not None and not bk.inst_ok:  # подтверждённых сделок текст не меняет
            notes += (f"инструмент сделки не подтверждён: {bk.inst_why}",)
        m = bk.m_view if bk is not None else ctx.get("m")
        if entry:
            perp_qty = None
        elif bk is not None and (ctx.get("full") or perp_only):
            perp_qty = bk.short                # весь шорт, контракты
        elif m is None or m == 1:              # частичный выход, 1 контракт = 1 токен: токены продажи (текст прежний)
            perp_qty = dget(e.get("tokens"))
        else:                                  # m ≠ 1: контракты откупа по плану (дочерние, вниз к шагу), не токены / m
            perp_qty = dget(e.get("contracts"))
        return dict(
            intent_id=iid, kind=plan.kind, coin=plan.coin, chain=pair.chain, perp_venue=plan.perp, symbol=plan.symbol,
            leg_usd=plan.leg_usd, clips_usd=clips_usd, token_qty=token_qty, m=m, perp_qty=perp_qty,
            deal_id=plan.deal_id, perp_only=perp_only, ttl_s=tconfig.PLAN_TTL_S, sim=sim,
            exit_all=entry or bool(ctx.get("full")),
            req_usd=ctx.get("usd"), deal_leg_usd=dget(deal["leg_usd"]) if deal is not None else ctx.get("deal_leg_usd"),
            resume=bool(ctx.get("resume")), step=dget((inp.get("filters") or {}).get("step")),
            leverage=cfg.get(f"perp.{pair.venue}.leverage"), margin_type=cfg.get(f"perp.{pair.venue}.margin_type"),
            impact_usd=dex_cost, gas_usd_clip=dget(e.get("gas_per_swap_usd")), gas_usd_total=dget(e.get("gas_usd")),
            approve_gas_usd=(dget(e.get("approve_usd")) or None) if e.get("approve_usd") else None,
            native_px=mkt.native_px if mkt is not None else None, total_usd=dget(e.get("total_usd")),
            exit_cost_usd=pn.get("exit_est_usd", dget(e.get("total_usd"))) if entry else None,
            breakeven_h=pn.get("breakeven_h"), funding_pct_h=(fh * 100) if fh is not None else None,
            usd_per_h=pn.get("usd_per_h"),
            basis_pct=(dget(e.get("basis_bps")) / 100) if e.get("basis_bps") is not None else None,
            min_funding_pct_h=cfg.get("limits.min_entry_funding_pct_h") if entry else None,
            min_basis_bps=cfg.get("limits.min_entry_basis_bps") if entry else None,
            wallet_stable=tok_h(bal.get("stable"), sdec), wallet_native=tok_h(bal.get("native"), 18),
            margin_avail=ctx["legs"].perp.available_margin(),
            open_deals=len(store.active_deals(self.conns.get())), max_open_deals=cfg.get("limits.max_open_deals"),
            missing_owner_keys=tuple(plan.missing_owner_keys) if sim else (), notes=notes,
            exit_root_qty=tok_h(ctx.get("exit_root_units"), dec))


# --- исполнитель -----------------------------------------------------------------------------------------
class Hooks:
    """Куда исполнитель пишет владельцу (реализует tg/bot.py; здесь — ничего не делающая основа для тестов).
    Исполнение не ждёт Telegram: методы только кладут сообщение в очередь отправителя."""

    def progress(self, iid: str, html: str) -> None: ...
    def report(self, html: str) -> None: ...
    def notice(self, topic: str, facts: dict) -> None: ...
    def final_report(self, snapshot) -> None: ...
    def requote(self, iid: str, reason: str) -> None: ...


@dataclass
class Run:
    it: dict
    deal: dict
    kind: str
    spec: dict
    plan: Plan
    legs: Legs
    cfg: OwnerCfg                     # замороженная копия намерения (то, что владелец одобрил)
    token: str
    dec: int
    symbol: str
    stable: str
    sdec: int
    f: Any
    started: float
    seq: int = 0
    n_total: int = 0
    sim_txs: list = field(default_factory=list)
    op_id: str | None = None
    m: D = D(1)                       # токенов в контракте — из инструмента сделки (заполняет _execute)
    inst: InstrumentSpec | None = None
    m_known: bool = True              # False — m не известен (ревью 13.09, M3): m = 1 лишь заглушка

    @property
    def mv(self) -> D:
        """m для текстов: 0 — m не известен («N контр.» без пересчёта в токены)."""
        return self.m if self.m_known else ZERO

    @property
    def iid(self) -> str:
        return self.it["id"]

    @property
    def did(self) -> str:
        return self.deal["id"]

    @property
    def letter(self) -> str:
        return KIND_LETTER[self.kind]


@dataclass
class HedgeResult:
    filled: D
    quote: D
    status: str                       # ok | deficit | unknown | reduce_only_reject
    text: str | None = None


_FINAL_PERP = ("FILLED", "PARTIALLY_FILLED", "EXPIRED", "REJECTED")


class Engine:
    """Единственный поток, который подписывает и отправляет. submit(iid) — только после атомарного одобрения
    (auth.press); повтор submit того же намерения безвреден: исполняется лишь статус approved."""

    def __init__(self, conns: Conns, legs: Callable[[bool], Legs | None], desk: Desk, hooks: Hooks | None = None, *,
                 owner_loader: Callable[[], OwnerCfg] = owner_mod.load, keys_mode: str | None = None,
                 holder: CfgHolder | None = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Any] = time.sleep, clip_gap_s: float | D = planner.CLIP_GAP_S,
                 busy_path=None, generic_context_factory: Callable[[Any, Mapping, Mapping, Mapping], Any] | None = None,
                 generic_registry=None):
        self.conns, self.legs, self.desk = conns, legs, desk
        self.busy_path = busy_path            # tconfig.TRADING_BUSY в боевом процессе; None — без файла (тесты)
        self.hooks = hooks or Hooks()
        self.owner_loader = owner_loader
        self.keys_mode = keys_mode
        self.holder = holder or CfgHolder(owner_loader)
        self.clock, self.sleep = clock, sleep
        self.clip_gap_s = float(clip_gap_s)
        self.q: "queue.Queue[str]" = queue.Queue()
        self._running = threading.Event()
        self.drain_evt = threading.Event()    # deployment fence; independent of owner pause
        self.pause_evt = threading.Event()    # «стоп» в памяти (флаг в БД ставит бот)
        self.term = threading.Event()         # SIGTERM: новое не начинать, текущую пару ног довести
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.current: tuple[str, int, int] | None = None      # (намерение, клип, клипов) — для ответа на «стоп»
        self._unwind_due: dict[str, float] = {}
        self._sol_engine = None
        # Generic plans are opt-in and receive a freshly scoped context after
        # the immutable intent is loaded.  There is deliberately no default
        # constructed from live credentials: a missing factory is a refusal.
        self.generic_context_factory = generic_context_factory
        self.generic_registry = generic_registry

    def _sol(self):
        """Шаги связки SOL × HL (trade/sol_flow.py): тот же поток, те же ворота «стоп»/SIGTERM, свои ноги."""
        if self._sol_engine is None:
            from .sol_flow import SolEngine
            self._sol_engine = SolEngine(self)
        return self._sol_engine

    def recover_sol(self, deal: Mapping) -> list[str]:
        """Исход прошлых отправок сделки связки (старт, «позиции»): только чтения и те же подписанные байты."""
        from .runtime import legs_of
        from .sol_flow import recover_deal
        return recover_deal(self.conns.get(), deal, legs_of(self.legs, deal))

    # --- поток ---
    def submit(self, iid: str) -> None:
        self.q.put(iid)

    def busy(self) -> bool:
        return self._running.is_set() or not self.q.empty()

    def start(self) -> "Engine":
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self.run, name="executor", daemon=True)
            self._thread.start()
        return self

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                iid = self.q.get(timeout=0.5)
            except queue.Empty:
                self._check_unwinds()
                continue
            self.execute(iid)

    def stop(self) -> None:
        self._stop.set()

    def wait_idle(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while self.busy() and time.monotonic() < end:
            time.sleep(0.2)
        return not self.busy()

    def _busy_file(self, on: bool) -> None:
        """runtime/trading.busy: пока есть — коллектор пропускает задание DEX (общий 1 запрос/с ключа OKX), а
        человек на сервере видит, что идёт исполнение. Сбой файла исполнению не мешает."""
        if self.busy_path is None:
            return
        try:
            if on:
                self.busy_path.parent.mkdir(parents=True, exist_ok=True)
                self.busy_path.write_text(f"{self.clock():.0f}\n")
            else:
                self.busy_path.unlink(missing_ok=True)
        except OSError as e:
            log.warning("trading.busy: %s", e)

    def execute(self, iid: str) -> None:
        self._running.set()
        self._busy_file(True)
        try:
            self._execute(iid)
        except Exception as e:                 # noqa — исполнитель не умирает; сделка уже на паузе или не начата
            log.exception("исполнитель: %s", iid)
            try:
                self.hooks.notice('executor_crash', {'intent_id': iid, 'error': f"{type(e).__name__}: {redact(e)}"})
            except Exception:                  # noqa
                pass
        finally:
            self.holder.set(None)
            self.current = None
            self._busy_file(False)
            self._running.clear()

    def _fail(self, iid: str, text: str, expect=IntentStatus.APPROVED) -> None:
        con = self.conns.get()
        from .adapters.obligations import unresolved
        with store.tx(con):
            it = store.get_intent(con, iid)
            if it is None or it['status'] != expect:
                return
            deal = store.get_deal(con, it['deal_id'])
            pending = unresolved(con, deal)
            op = store.operation_of_intent(con, iid)
            if op and op['state'] == store.OpState.APPROVED:
                if pending:
                    store.set_operation_state(con, op['id'], store.OpState.PAUSED_UNKNOWN, reason=text)
                else:
                    store.set_operation_state(con, op['id'], store.OpState.STOPPED, reason=text)
                    if not int(op['confirmed_raw']):
                        store.set_operation_state(con, op['id'], store.OpState.ABANDONED, reason=text)
            if pending and deal['state'] == DealState.DRAFT:
                store.set_deal_state(con, deal['id'], DealState.ENTERING, expect=DealState.DRAFT)
                store.set_deal_state(con, deal['id'], DealState.PAUSED, reason='unresolved prerequisite')
            if not store.set_intent_status(con, iid, IntentStatus.FAILED, expect=expect, err=text):
                raise store.StoreError('refusal CAS failed')
        self.hooks.notice('refused', {'reason': text})

    def _execute(self, iid: str) -> None:
        con = self.conns.get()
        it = store.get_intent(con, iid)
        if it is None or it["status"] != IntentStatus.APPROVED:
            log.info("исполнитель: %s не в approved (%s) — пропуск", iid, it and it["status"])
            return
        if self.term.is_set() or self.drain_evt.is_set():
            return self._fail(iid, "служба останавливается — план не начат, пришлите команду после рестарта")
        deal = store.get_deal(con, it["deal_id"])
        spec = json.loads(it["spec_json"])
        if spec.get("generic_operation_v1") is True:
            return self._execute_generic(con, it, deal, spec)
        if is_sol_deal(deal):
            policy = self._sol()
            run = policy.prepare_run(it, deal, spec)
        else:
            policy = self
            run = self._prepare_run(it, deal, spec)
        if run is None:
            return
        OperationController(con).run_operation(
            run, lambda: policy.execute_program(run), paused=lambda stop: policy._paused(run, stop),
            refused=lambda text: self._fail(iid, text))

    def _execute_generic(self, con, intent, deal, spec) -> None:
        """Dispatch a frozen generic plan through the same Engine queue only.

        GenericOperationCoordinator performs its own OperationController.admit;
        this method must not route the intent through legacy EVM/SOL preparation
        or fabricate a context from any live global adapter.
        """
        factory = self.generic_context_factory
        registry = self.generic_registry or getattr(self.legs, "adapters", None)
        if factory is None or registry is None:
            self._fail(intent["id"], "generic plan has no scoped adapter context — nothing sent")
            return
        try:
            context = factory(con, intent, deal, spec)
            if context is None:
                raise ValueError("factory returned no scoped adapter context")
            from .generic_operations import GenericOperationCoordinator
            GenericOperationCoordinator(con, registry, context).execute(intent["id"])
        except Exception as e:
            # The generic coordinator persists UNKNOWN itself.  A failure before
            # admission is a normal refusal; after admission it owns the durable
            # state and Engine only reports the crash at its outer boundary.
            current = store.get_intent(con, intent["id"])
            if current is not None and current["status"] == IntentStatus.APPROVED:
                self._fail(intent["id"], "generic operation not started: " + redact(e))
                return
            raise

    def execute_program(self, run):
        if run.kind != "undo":
            self._perp_ab(run)
        {"entry": self._entry, "exit": self._exit, "rehedge": self._rehedge, "undo": self._undo}[run.kind](run)

    def _prepare_run(self, it, deal, spec):
        con, iid = self.conns.get(), it['id']
        # инструмент намерения = инструмент сделки (ревью 13.09, Н2) — до ног, записи RUNNING и любых чтений сети
        inst = deal_instrument(con, deal)
        bad = self._inst_mismatch(deal, spec, inst)
        if bad:
            store.event(con, "inst_mismatch", deal_id=deal["id"], intent_id=iid, why=bad)
            self._fail(iid, bad)
            if deal["state"] == DealState.DRAFT:
                store.set_deal_state(con, deal["id"], DealState.ABORTED, expect=DealState.DRAFT, reason="не начата")
            return
        prof = owner_mod.EVM_PROFILE_OF.get(_deal_cv(deal), owner_mod.LEGACY_PROFILE)
        if prof == owner_mod.LEGACY_PROFILE:
            legs = self.legs(bool(deal["sim"]))
        else:                                  # EVM-связка не BSC/Aster: ноги своей связки, иначе — не начинаем
            legs, why = _profile_legs(self.legs, prof, bool(deal["sim"]))
            if legs is None:
                return self._fail(iid, f"живую сделку в этом режиме не двигаю: {why}")
        if legs is None or (not legs.sim and not legs.can_send):
            return self._fail(iid, "живую сделку в этом режиме не двигаю: ключи live не загружены")
        try:
            from .adapters.execution_scope import account_for
            account_for(con, deal, legs.perp)
        except Exception as e:
            return self._fail(iid, f"счёт исполнения не подтверждён: {redact(e)}")
        cfg = OwnerCfg.from_frozen(spec["owner"])
        self.holder.set(cfg)
        stable, sdec = config.OKX_DEX_STABLES[tconfig.chain_index(deal["chain"])]
        try:
            f = legs.perp.filters(deal["symbol"])
            from .adapters.mapping import map_perpetual
            from .adapters.execution_scope import continuation_identity
            continuity = continuation_identity(con, deal, it['kind'])
            map_perpetual(deal, account=account_for(con, deal, legs.perp), filters=f,
                          metadata_revision='frozen:' + deal['id'], continuation_evidence=continuity)
            from .adapters.spot_execution import evm_spec
            evm_spec(deal, legs.spot, stable, int(sdec), continuation_evidence=continuity)
        except Exception as e:                 # noqa — ничего не отправлено
            self._fail(iid, f"фильтры {deal['symbol']} не прочитаны: {redact(e)}",
                       expect=IntentStatus.APPROVED)
            return
        run = Run(it=it, deal=deal, kind=it["kind"], spec=spec, plan=plan_from_json(it["plan_json"]), legs=legs,
                  cfg=cfg, token=deal["token"], dec=int(deal["token_dec"]), symbol=deal["symbol"], stable=stable,
                  sdec=int(sdec), f=f, started=self.clock())
        op = store.operation_of_intent(con, iid)
        run.op_id = op['id'] if op else None
        run.inst = inst                        # единицы сделки (ревью 13.09, С1): токены ↔ контракты через m
        run.m, run.m_known = inst.m, m_known(inst)
        if run.kind in MAIN_KINDS and not spec.get("perp_only"):
            fresh = self._requote(run)
            if fresh is None:
                return
            run.plan = fresh
        return run

    @staticmethod
    def _inst_mismatch(deal: dict, spec: dict, inst: InstrumentSpec) -> str | None:
        """Ревью 13.09, Н2: намерение исполняется только по тому инструменту, по которому его одобрили. Отпечатка нет
        (план старого кода), отпечаток ≠ инструменту сделки, колонки сделки ≠ её инструменту, токен/символ/decimals
        spec ≠ сделке — текст отказа; иначе None. Для всех видов намерений, до любого действия."""
        did = deal["id"]
        h = spec.get("inst_hash")
        if not h:
            return "план построен без отпечатка инструмента (до обновления) — пришлите команду заново"
        if h != inst.inst_hash():
            return f"инструмент намерения ≠ инструменту сделки {did} — ничего не отправлено"
        bad = _inst_vs_deal(deal, inst)
        if bad:
            return f"{bad} — ничего не отправлено"
        have = {"token": str(deal["token"]).lower(), "symbol": str(deal["symbol"]), "token_dec": int(deal["token_dec"])}
        for k, want in have.items():
            if spec.get(k) is None:
                continue
            try:
                got = int(spec[k]) if k == "token_dec" else (str(spec[k]).lower() if k == "token" else str(spec[k]))
            except (TypeError, ValueError):
                got = spec[k]
            if got != want:
                return f"намерение: {k} {spec[k]} ≠ сделке {did} ({want}) — ничего не отправлено"
        return None

    # --- перекотировка у кнопки (§6 п.7) ---
    def _requote(self, run: Run) -> Plan | None:
        con = self.conns.get()
        try:
            fresh = self.desk.replan(run.it, store.get_deal(con, run.did))
        except Refused as e:
            store.set_intent_status(con, run.iid, IntentStatus.FAILED, err="перекотировка не удалась")
            self._abort_draft(run)
            self.hooks.notice(e.topic, e.facts)
            return None
        if fresh.inputs.get("inst_hash") != run.spec.get("inst_hash"):
            # второй слой (Н2): свежий план — по другому инструменту. Новый план по нему бот сам не предлагает
            why = "перекотировка: инструмент сменился"
            store.set_intent_status(con, run.iid, IntentStatus.FAILED, err=why)
            store.event(con, "inst_mismatch", deal_id=run.did, intent_id=run.iid, why=why)
            self._abort_draft(run)
            self.hooks.notice('refused', {'reason': f"перекотировка: инструмент {run.deal['coin']} сменился — "
                                          "ничего не отправлено, нужен новый план"})
            return None
        v = formatters
        reasons = []
        drift = run.cfg.get("exec.plan_cost_drift_pct")
        old, new = dget(run.plan.est.get("total_pct")), dget(fresh.est.get("total_pct"))
        if drift is not None and old is not None and new is not None and new - old > drift:
            reasons.append(f"издержки {v.pct(old)} → {v.pct(new)} (допуск {v.num(drift, 1)} п.п.)")
        if run.kind == "entry":
            f0, f1 = dget(run.spec.get("funding_h")), dget(fresh.inputs.get("funding_h"))
            if f0 is not None and f0 > 0 and f1 is not None and f1 <= 0:
                reasons.append(f"фандинг сменил знак: {v.pct(f0 * 100, 3, sign=True)}/ч → "
                               f"{v.pct(f1 * 100, 3, sign=True)}/ч")
        if reasons:
            why = "; ".join(reasons)
            store.set_intent_status(con, run.iid, IntentStatus.FAILED, err=f"перекотировка: {why}")
            self._release_unstarted_root(run)
            store.event(con, "requote", deal_id=run.did, intent_id=run.iid, why=why)
            self.hooks.requote(run.iid, why)
            return None
        store.event(con, "requote_ok", deal_id=run.did, intent_id=run.iid, old_pct=old, new_pct=new,
                    alpha=fresh.est.get("alpha"), beta_bps=fresh.est.get("beta_bps"),
                    exec_time_max_s=fresh.est.get("exec_time_max_s"))    # числа, с которыми пойдут заявки
        return fresh

    def _release_unstarted_root(self, run: Run) -> None:
        con = self.conns.get()
        from .adapters.obligations import unresolved
        with store.tx(con):
            op = store.operation_of_intent(con, run.iid)
            deal = store.get_deal(con, run.did)
            if op and op['state'] == store.OpState.APPROVED:
                if unresolved(con, deal):
                    store.set_operation_state(con, op['id'], store.OpState.PAUSED_UNKNOWN,
                                              reason='unresolved prerequisite')
                    if deal['state'] == DealState.DRAFT:
                        store.set_deal_state(con, run.did, DealState.ENTERING)
                        store.set_deal_state(con, run.did, DealState.PAUSED, reason='unresolved prerequisite')
                else:
                    store.set_operation_state(con, op['id'], store.OpState.STOPPED, reason='plan not started')
                    if not int(op['confirmed_raw']):
                        store.set_operation_state(con, op['id'], store.OpState.ABANDONED, reason='plan not started')

    def _abort_draft(self, run: Run) -> None:
        self._release_unstarted_root(run)
        con = self.conns.get()
        d = store.get_deal(con, run.did)
        if d and d["state"] == DealState.DRAFT:
            store.set_deal_state(con, run.did, DealState.ABORTED, expect=DealState.DRAFT, reason="не начата")

    # --- guard() перед каждым клипом (§6 п.9) ---
    def guard(self, run: Run, *, invariant: bool = True) -> None:
        con = self.conns.get()
        if store.is_paused(con) or self.pause_evt.is_set():
            raise Pause("stop", "стоп владельца: новое не начинаю")
        if self.drain_evt.is_set():
            raise Pause("drain", "переключение версии: новое не начинаю")
        if self.term.is_set():
            raise Pause("terminate", "служба останавливается: новое не начинаю")
        try:
            cfg = self.owner_loader()
        except OwnerConfigError as e:
            raise Pause("owner", f"owner.toml не прочитан: {e}") from None
        if not run.legs.sim:
            ch, vn = _deal_cv(run.deal)
            prof = owner_mod.EVM_PROFILE_OF.get((ch, vn), owner_mod.LEGACY_PROFILE)
            # режим связки сделки: у старой — общий mode (как было), у другой — её profile_mode (не выше общего)
            m = effective_mode(cfg.mode if prof == owner_mod.LEGACY_PROFILE else cfg.profile_mode(prof),
                               self.keys_mode or "dry")
            if m != "live":
                raise Pause("mode", f"режим {m}: отправки запрещены")
            try:
                cfg.require_live(vn, ch)
            except OwnerMissing as e:
                raise Pause("owner_missing", str(e)) from None
            reserve = cfg.get("dex.native_reserve")
            if reserve is not None:
                nat = run.legs.spot.balances(run.token).get("native")
                if nat is None or D(nat) < reserve * WEI:
                    have = "не прочитан" if nat is None else formatters.num(D(nat) / WEI, 4)
                    raise Pause("native", f"{tconfig.NATIVE_SYMBOL.get(ch, 'BNB')} {have} — меньше резерва "
                                          f"{formatters.num(reserve, 4)}")
        if run.kind == "entry":
            cap = cfg.get("limits.deal_max_usd_per_leg")
            if cap is not None and D(str(run.deal["leg_usd"])) > cap:
                v = formatters
                raise Pause("limit", f"сумма сделки {v.leg(run.deal['leg_usd'])} больше нового лимита {v.leg(cap)}")
            f0 = dget(run.spec.get("funding_h"))
            if f0 is not None and f0 > 0:
                try:
                    _m, rate, _n = run.legs.perp.funding(run.symbol)
                except Exception as e:         # noqa
                    raise Pause("funding", f"фандинг не прочитан: {redact(e)}") from None
                if rate <= 0:
                    raise Pause("funding_sign", f"фандинг сменил знак: {formatters.pct(rate * 100, 3, sign=True)} за "
                                                "интервал")
        tmax, t_auto = self._exec_time_max(run)
        if tmax is not None and self.clock() - run.started > float(tmax):
            took = formatters.dur(float(tmax))
            raise Pause("exec_time", f"исполнение дольше {took} (exec_time_max_s auto — число плана)" if t_auto
                        else f"исполнение дольше {took} (exec_time_max_s)")
        if invariant:
            self._invariant(run)

    def _exec_time_max(self, run: Run) -> tuple[D | None, bool]:
        """Предел времени исполнения: число владельца — из замороженной копии; «auto» — число, замороженное в плане
        (3 × ожидаемая длительность + 60 с), а не слово из owner.toml. В плане числа нет (откат) — без предела."""
        v = run.cfg.get("exec.exec_time_max_s")
        if v == planner.AUTO:
            return dget((run.plan.est or {}).get("exec_time_max_s")), True
        return v, False

    def _perp_ab(self, run: Run) -> tuple[D | None, D | None, bool]:
        """α/β дочерних IOC — из исполняемого плана (подобранные «auto» заморожены в нём; перекотировка у кнопки
        приносит свой план). Старый план без них — числа из замороженной копии owner.toml. «auto» без чисел плана —
        пауза: подбирать α/β заново посреди исполнения нельзя."""
        e = run.plan.est or {}
        if "alpha" in e or "beta_bps" in e:
            return dget(e.get("alpha")), dget(e.get("beta_bps")), bool(e.get("ab_band"))
        lim = _lim(run.cfg, run.deal)
        if isinstance(lim.alpha, str) or isinstance(lim.beta_bps, str):
            raise Pause("plan_ab", "α/β «auto» не заморожены в плане — исполнение не начинаю, нужна свежая команда")
        return lim.alpha, lim.beta_bps, False

    def _position(self, run: Run, expect: D | None = None) -> D | None:
        """Позиция перпа: до 3 чтений, пока не прочитана — и, если задан expect (шорт журнала), пока не совпала:
        короткий «позиции нет» сразу после первого филла нового контракта (Gate POSITION_NOT_FOUND → 0) — повод
        перечитать, а не остановить сделку (проверка Fable 14.09). Совпало с первого раза — одно чтение, как раньше."""
        pos = None
        for i in range(3):
            pos = run.legs.perp.position(run.symbol)
            if pos is not None and (expect is None or pos == expect):
                return pos
            if i < 2:
                self.sleep(1.0)
        return pos

    def _invariant(self, run: Run, position: bool = True) -> DealBook:
        """0 ≤ токены − |шорт|·m < шаг·m по журналу (в токенах); в live — и шорт по positionRisk (контракты). Иначе
        пауза. m не известен (ревью 13.09, M3) — дельту не сверить: идёт только полный выход или «выход перп» (позиция
        биржи = журнал сверяется), всё остальное — пауза."""
        bk = deal_book(self.conns.get(), run.did)
        if not bk.known:
            raise Pause("book_unknown", f"книга сделки неизвестна: {bk.why}")
        v = formatters
        if bk.m_known:
            delta, ts = bk.delta(run.dec), bk.tstep(run.f.step)
            if not (ZERO <= delta < ts):
                raise Pause("hedge_deficit", f"дельта ног {v.tok(delta, True, ts)} — вне [0, шаг {v.tok(ts, step=ts)})")
        else:
            spec = getattr(run, "spec", None) or {}
            if not (getattr(run, "kind", None) == "exit" and (spec.get("all") or spec.get("perp_only"))):
                raise Pause("inst_unverified", f"{v.m_unknown_text(run.did)} ({bk.inst_why})")
        if position and not run.legs.sim:
            pos = self._position(run, expect=-bk.short)
            vl = v.VENUE_LABEL.get(run.legs.perp.venue, run.legs.perp.venue)
            if pos is None:
                raise Pause("position_unknown", f"позиция {vl} не прочитана — ноги не сверить")
            if pos != -bk.short:
                mv = bk.m_view
                raise Pause("position_mismatch", f"позиция {vl} {v.contracts(pos, mv, True, run.f.step)} ≠ журнал "
                                                 f"сделки {v.contracts(-bk.short, mv, True, run.f.step)}")
        return bk

    # --- пауза / остановка ---
    def _progressed(self, iid: str) -> bool:
        con = self.conns.get()
        n = con.execute("SELECT count(*) FROM clips WHERE intent_id=? AND state NOT IN ('PLANNED','DEX_REVERTED')",
                        (iid,)).fetchone()[0]
        m = con.execute("SELECT count(*) FROM perp_orders o JOIN clips c ON o.clip_id=c.id WHERE c.intent_id=? AND "
                        "o.state NOT IN ('NOT_PLACED','EXPIRED','REJECTED')", (iid,)).fetchone()[0]
        return bool(n or m)

    def _set_deal(self, did: str, new: str, reason: str | None = None, **fields) -> None:
        con = self.conns.get()
        try:
            store.set_deal_state(con, did, new, reason=reason, **fields)
        except store.BadTransition as e:
            log.error("сделка %s: %s", did, e)

    def _paused(self, run: Run, p: Pause) -> None:
        con = self.conns.get()
        bk = deal_book(con, run.did)
        progressed = self._progressed(run.iid)
        target = OperationController(con).pause(run, p, progressed=progressed,
                                                empty=bk.tokens_raw == 0 and bk.short == 0)
        store.event(con, "paused", deal_id=run.did, intent_id=run.iid, reason=p.reason, text=p.text,
                    state=str(target))
        delta = bk.delta(run.dec) if bk.known else None
        ts = run.f.step * bk.m                 # шаг в токенах: меньше одного шага контрактов — не голая нога
        unhedged = None if delta is None else (ZERO if ZERO <= delta < ts else delta)
        px = None
        try:
            px = planner.mid(run.legs.perp.book(run.symbol, 5))
        except Exception:                      # noqa
            pass
        px_tok = (px / bk.m) if px else None   # мид контракта / m — цена токена (голая нога — в токенах)
        auto = run.cfg.get("exec.auto_unwind_naked_after_s")
        if auto is not None and unhedged is not None and unhedged >= ts and target == DealState.PAUSED:
            self._unwind_due[run.did] = self.clock() + float(auto)
        done = con.execute("SELECT count(*) FROM clips WHERE intent_id=? AND state=?",
                           (run.iid, str(ClipState.BALANCED))).fetchone()[0]   # «после клипа 1/2», а не «на 2/2»
        self.hooks.notice('halt', dict(
            intent_id=run.iid, kind="entry" if run.kind == "entry" else "exit", coin=run.deal["coin"], reason=p.text,
            perp_venue=run.deal["perp_venue"], deal_id=run.did, clip=run.seq or None, clips=run.n_total or None,
            perp_pos=self._safe_pos(run), wallet_tokens=self._wallet_tokens(run, bk),
            unhedged_qty=unhedged, unhedged_usd=(abs(unhedged) * px_tok) if (unhedged is not None and px_tok) else None,
            auto_unwind_s=auto if (unhedged and unhedged >= ts and target == DealState.PAUSED) else None,
            sim=run.legs.sim, ts=self.clock(), clips_done=int(done), state=str(target), step=run.f.step,
            m=getattr(bk, "m_view", bk.m)))

    def _safe_pos(self, run: Run) -> D | None:
        try:
            return run.legs.perp.position(run.symbol)
        except Exception:                      # noqa
            return None

    def _wallet_tokens(self, run: Run, bk: DealBook) -> D | None:
        if run.legs.sim:
            return bk.tokens(run.dec)
        try:
            u = run.legs.spot.balances(run.token).get("token")
        except Exception:                      # noqa
            u = None
        return None if u is None else D(u) / D(10) ** run.dec

    def _check_unwinds(self) -> None:
        """Авто-откат голой ноги — только если владелец задал auto_unwind_naked_after_s (пусто = никогда)."""
        if self.drain_evt.is_set() or self.term.is_set():
            return
        now = self.clock()
        for did, due in list(self._unwind_due.items()):
            if now < due:
                continue
            del self._unwind_due[did]
            con = self.conns.get()
            deal = store.get_deal(con, did)
            if deal is None or deal["state"] != DealState.PAUSED:
                continue
            try:
                prop = self.desk.propose_fix("undo", did, chat=None)
                if store.approve_intent(con, prop.intent_id, prop.nonce):
                    store.event(con, "auto_unwind", deal_id=did, intent_id=prop.intent_id)
                    self.hooks.notice('auto_unwind', dict(coin=deal['coin'], qty=dget((prop.plan.est or {}).get('delta')),
                                      usd=prop.plan.leg_usd, sim=bool(deal['sim'])))
                    self.execute(prop.intent_id)
            except (Refused, store.StoreError) as e:
                log.warning("авто-откат %s не начат: %s", did, e)

    # --- шаги клипа ---
    def _setup(self, run: Run) -> None:
        vn = _deal_cv(run.deal)[1]
        lev, mt = run.cfg.get(f"perp.{vn}.leverage"), run.cfg.get(f"perp.{vn}.margin_type")
        if lev is None or mt is None:
            if run.legs.sim:
                return
            raise Pause("owner_missing", "плечо/тип маржи не заданы")
        try:
            run.legs.perp.setup(run.symbol, int(lev), mt)
        except Exception as e:                 # noqa — настройка идемпотентна; сделок ещё нет
            venue = formatters.VENUE_LABEL.get(run.legs.perp.venue, run.legs.perp.venue)
            raise Pause("setup", f"настройка {run.symbol} на {venue}: {redact(e)}") from None

    def _approve(self, run: Run, token: str, need: int) -> None:
        if run.legs.sim or need <= 0:
            return
        from .evm import SentUnknown
        try:
            from .adapters.spot_execution import ensure_evm_allowance
            res = ensure_evm_allowance(run.legs.spot, token, need)
        except SentUnknown as e:
            raise Pause("approve_unknown", f"approve: исход неизвестен ({e})") from None
        except Exception as e:                 # noqa — до отправки (гард, режим, узел отверг)
            raise Pause("approve_refused", f"approve не отправлен: {redact(e)}") from None
        if res is not None:                    # approve идёт до первого клипа: к намерению его привязывает журнал
            store.event(self.conns.get(), "approve", deal_id=run.did, intent_id=run.iid, status=res.status,
                        hashes=list(res.hashes or (res.tx_hash,)))
        if res is not None and res.status != "ok":
            raise Pause("approve_failed", f"approve {res.status}: {getattr(res, 'note', '')}")

    def _dex_touched(self, clip_id: int) -> bool:
        return bool(self.conns.get().execute("SELECT count(*) FROM dex_txs WHERE clip_id=? AND state != 'DROPPED'",
                                             (clip_id,)).fetchone()[0])

    def _compose_context(self, run, clip_id, *, account, authorize):
        """Keep the exact clip bindings alive through spot and every hedge child."""
        from .adapters.execution import build_perp_binding
        from .adapters.registry import production_registry
        contexts = getattr(run, '_clip_contexts', None)
        if contexts is None:
            contexts = run._clip_contexts = {}
        if clip_id not in contexts:
            spec, bindings, _ = build_perp_binding(
                self.conns.get(), deal=run.deal, clip_id=clip_id, native=run.legs.perp,
                account=account, fill_venue=run.legs.fill_venue, clock=self.clock, authorize=authorize)
            registry = self.legs if callable(getattr(self.legs, 'compose', None)) else (
                getattr(self.legs, 'adapters', None) or production_registry())
            contexts[clip_id] = dict(registry=registry, perp_spec=spec, perp_bindings=bindings)
        return contexts[clip_id]

    def _dex(self, run: Run, clip_id: int, t_in: str, t_out: str, units: int, retry: bool = True):
        """Своп клипа с записью-до. Не отправлено — DEX_REVERTED (ничего не двигалось) и пауза; исход неизвестен —
        DEX_UNKNOWN и пауза; откат в сети — один повтор по свежей котировке, второй — пауза."""
        con = self.conns.get()
        from .evm import SentUnknown
        OperationController(con).begin_spot(clip_id, operation_id=run.op_id,
                                              reserve_raw=int(units) if run.op_id else None, retry_reverted=True)
        try:
            from .adapters.spot_execution import submit_evm
            from .adapters.execution_scope import account_for
            context = self._compose_context(run, clip_id,
                account=account_for(con, run.deal, run.legs.perp),
                authorize=lambda *_: self._authorize_child(run))
            res = submit_evm(con, deal=run.deal, clip_id=clip_id, native=run.legs.spot,
                             stable=run.stable, stable_dec=run.sdec, token_in=t_in, token_out=t_out,
                             amount_raw=int(units), clock=self.clock, authorize=lambda *_: self._authorize_child(run),
                             compose_context=context, registry=getattr(self.legs, 'adapters', None))
        except Exception as e:                 # noqa
            if isinstance(e, SentUnknown) or (not run.legs.sim and self._dex_touched(clip_id)):
                store.set_clip_state(con, clip_id, ClipState.DEX_UNKNOWN)
                raise Pause("dex_unknown", f"исход свопа неизвестен: {redact(e)}") from None
            OperationController(con).settle_spot(clip_id, SpotSettlement(False), operation_id=run.op_id,
                                                  reserve_raw=int(units) if run.op_id else None)
            store.event(con, "dex_not_sent", deal_id=run.did, intent_id=run.iid, clip_id=clip_id, err=redact(e))
            raise Pause("dex_refused", f"своп не отправлен: {redact(e)}") from None
        if run.legs.sim:
            run.sim_txs.append({"kind": "swap", "gas_used": res.gas_wei, "eff_gas_price": 1, "status": 1,
                                "tx_hash": res.tx_hash})
        store.event(con, "dex", deal_id=run.did, intent_id=run.iid, clip_id=clip_id, status=res.status,
                    tx=res.tx_hash, a_in=res.amount_in, a_out=res.amount_out, gas_usd=res.gas_usd)
        if res.status == "ok":
            OperationController(con).settle_spot(
                clip_id, SpotSettlement(True, int(res.amount_in), int(res.amount_out)),
                operation_id=run.op_id, reserve_raw=int(units) if run.op_id else None)
            return res
        if res.status == "reverted":
            OperationController(con).settle_spot(clip_id, SpotSettlement(False), operation_id=run.op_id,
                                                  reserve_raw=int(units) if run.op_id else None)
            if retry:
                log.warning("своп клипа %s откатился — один повтор по свежей котировке", clip_id)
                return self._dex(run, clip_id, t_in, t_out, units, retry=False)
            raise Pause("dex_revert", "своп откатился дважды (minReceive/ликвидность) — газ потерян, токены не двигались")
        store.set_clip_state(con, clip_id, ClipState.DEX_UNKNOWN, dex_in=int(res.amount_in), dex_out=int(res.amount_out))
        raise Pause("dex_unknown", f"своп замайнен, но итог не сходится: {getattr(res, 'note', '') or res.status}")

    def _wait_refill(self, run: Run, side: str, cap: D | None, wait_s: D | None) -> bool:
        """Лучший уровень съеден — ждать его восстановления до refill_wait_max_s (пусто = не ждать)."""
        if not wait_s:
            return False
        end = self.clock() + float(wait_s)
        while self.clock() < end:
            self.sleep(1.0)
            try:
                b = run.legs.perp.book(run.symbol, 5)
            except Exception:                  # noqa
                continue
            lv = b.bids if side == "SELL" else b.asks
            if lv and lv[0][1] > 0 and (cap is None or (lv[0][0] >= cap if side == "SELL" else lv[0][0] <= cap)):
                return True
        return False

    def _hedge(self, run: Run, clip_id: int, side: str, qty: D, reduce_only: bool) -> HedgeResult:
        """Перп-нога клипа дочерними LIMIT IOC (hedge=True: ставится и на паузе — Q7). Недобор — повтор по свежему
        стакану не хуже первого кэпа, до ASTER_IOC_PARTIAL_RETRIES раз; дальше — HEDGE_DEFICIT."""
        con = self.conns.get()
        perp, f = run.legs.perp, run.f
        store.set_clip_state(con, clip_id, ClipState.PERP_SENT)
        short0 = deal_book(con, run.did).short or ZERO
        alpha, beta, band = self._perp_ab(run)          # числа плана, не owner.toml: «auto» сюда не доходит
        refill = run.cfg.get("exec.refill_wait_max_s")
        remaining, filled, quote = qty, ZERO, ZERO
        child, partial, cap0 = 0, 0, None
        known: list[int] = []
        while remaining > 0:
            try:
                book = perp.book(run.symbol, tconfig.ASTER_DEPTH_LIMIT)
                children = planner.perp_children(book, side, remaining, f, alpha, beta, reduce_only, band=band)
            except PlanRefused as e:
                if self._wait_refill(run, side, cap0, refill):
                    continue
                return HedgeResult(filled, quote, "deficit", str(e))
            except Exception as e:             # noqa — стакан не прочитан
                if self._wait_refill(run, side, cap0, refill):
                    continue
                return HedgeResult(filled, quote, "deficit", f"стакан не прочитан: {redact(e)}")
            if not children:
                break
            if cap0 is None:
                cap0 = children[0][1]
            under = False
            for q, cap in children:
                cap = max(cap, cap0) if side == "SELL" else min(cap, cap0)
                child += 1
                pos_before = -(short0 + (filled if side == "SELL" else -filled))
                try:
                    fill = self._child(run, clip_id, side, q, cap, reduce_only, child, pos_before, known)
                except Pause as p:
                    st = "unknown" if p.reason == "perp_unknown" else (
                        "reduce_only_reject" if p.reason == "reduce_only_reject" else "deficit")
                    return HedgeResult(filled, quote, st, p.text)
                filled += fill.qty
                quote += fill.quote
                remaining -= fill.qty
                if fill.order_id is not None:
                    known.append(int(fill.order_id))
                if fill.qty < q:
                    under = True
                    break
            if under:
                partial += 1
                if partial > tconfig.ASTER_IOC_PARTIAL_RETRIES:
                    v = formatters
                    return HedgeResult(filled, quote, "deficit",
                                       f"IOC исполнилась не полностью {partial} "
                                       f"{v.plural(partial, 'раз', 'раза', 'раз')} (кэп {v.px(cap0)}) — не хватает "
                                       f"{v.contracts(remaining, run.mv, step=f.step, coin=run.deal['coin'])}")
                self._wait_refill(run, side, cap0, refill)
        return HedgeResult(filled, quote, "ok")

    def _child(self, run: Run, clip_id: int, side: str, q: D, cap: D, ro: bool, child: int, pos_before: D,
               known: list[int]) -> PerpFill:
        """Одна дочерняя заявка: client_id в БД → отправка → итог. UNKNOWN — settle_unknown(), без повторной
        отправки; снова — только после доказанного NOT_FOUND и под новым номером попытки."""
        con = self.conns.get()
        perp = run.legs.perp
        attempt, rerounded = 1, False
        while True:
            cid = store.client_order_id(run.did, run.letter, clip_id, child, attempt)
            since_ms = int(self.clock() * 1000)
            try:
                from .adapters.execution import submit_ioc
                from .adapters.execution_scope import account_for
                account = account_for(con, run.deal, perp)
                fill = submit_ioc(con, deal=run.deal, clip_id=clip_id, native=perp,
                                  account=account, fill_venue=run.legs.fill_venue, client_id=cid,
                                  side=side, quantity=q, price=cap, reduce_only=ro, clock=self.clock,
                                  authorize=lambda *_: self._authorize_child(run),
                                  pair=getattr(run, '_clip_contexts', {}).get(clip_id, {}).get('pair'),
                                  registry=getattr(self.legs, 'adapters', None))
            except Exception as e:             # noqa — до отправки: ворота, бюджет, параметры, сбой записи-до
                row = store.get_perp_order(con, cid)
                if row is None or row['state'] in (PerpOrderState.INTENT, PerpOrderState.NOT_PLACED):
                    if row is not None and row['state'] == PerpOrderState.INTENT:
                        store.perp_order_result(con, cid, PerpOrderState.NOT_PLACED, err=f"{type(e).__name__}: {e}")
                    raise Pause("perp_refused", f"заявка {cid} не отправлена: {type(e).__name__}: {redact(e)}") from None
                fill = PerpFill(cid, None, "UNKNOWN", ZERO, ZERO, ZERO, 0)
            if fill.status == "UNKNOWN":
                store.perp_order_result(con, cid, PerpOrderState.UNKNOWN, err=getattr(perp, "last_error", None))
                store.event(con, "perp_unknown", deal_id=run.did, intent_id=run.iid, clip_id=clip_id, cid=cid)
                from .adapters.execution import settle_ioc
                s = settle_ioc(con, deal=run.deal, native=perp, account=account, client_id=cid,
                               pos_before=pos_before, since_ms=since_ms, known_order_ids=frozenset(known))
                if s.status == "NOT_FOUND":
                    store.perp_order_result(con, cid, PerpOrderState.NOT_PLACED,
                                            err="адаптер доказал: не найдена, позиция и сделки неизменны — не выставлена")
                    attempt += 1
                    if attempt > PERP_ATTEMPTS_MAX:
                        raise Pause("perp_unknown", f"заявка не выставляется {PERP_ATTEMPTS_MAX} раза подряд")
                    continue
                if s.status not in _FINAL_PERP:
                    raise Pause("perp_unknown", f"исход заявки {cid} неизвестен — НЕ повторяю; сверка «позиции»")
                fill = s
            store.record_perp_fill(con, fill, err=getattr(perp, "last_error", None) if fill.status == "REJECTED"
                                   else None)
            if fill.status == "REJECTED":
                code = fill.err_code
                from .adapters.outcomes import rejection
                category = rejection(fill)
                if category == "precision" and not rerounded:
                    rerounded = True
                    f2 = perp.filters(run.symbol)
                    q = floor_step(q, f2.step)
                    cap = floor_step(cap, f2.tick) if side == "SELL" else ceil_step(cap, f2.tick)
                    if q <= 0:
                        raise Pause("perp_rejected", "после округления заявка нулевая")
                    attempt += 1
                    continue
                reason = "reduce_only_reject" if category == "reduce_only" else "perp_rejected"
                v = formatters                   # владельцу: «Aster −2022», а не «aster -2022» (минус — «−»)
                c = "?" if code is None else str(code).replace("-", v.MINUS)
                raise Pause(reason, f"{v.VENUE_LABEL.get(perp.venue, perp.venue)} {c}: "
                                    f"{getattr(perp, 'last_error', '') or 'отказ'}")
            return fill

    def _authorize_child(self, run):
        # A stop between the two legs must still permit the approved hedge.
        # Native mode/budget/signing guards remain mandatory after this check.
        intent = store.get_intent(self.conns.get(), run.iid)
        if (intent is None or intent['status'] != IntentStatus.RUNNING or
                intent['deal_id'] != run.did or (not run.legs.sim and not run.legs.can_send)):
            raise Pause('perp_refused', 'исполнение не имеет действующего одобренного намерения')

    # --- общие куски потоков ---
    def _stopping(self) -> bool:
        return store.is_paused(self.conns.get()) or self.pause_evt.is_set() or self.term.is_set() or self.drain_evt.is_set()

    def _deal_to(self, run: Run, new: str) -> None:
        con = self.conns.get()
        d = store.get_deal(con, run.did)
        if d["state"] == new:
            return
        try:
            if d["state"] == DealState.OPEN and new == DealState.ENTERING:
                store.set_deal_state(con, run.did, DealState.PAUSED, reason="продолжение входа")
            store.set_deal_state(con, run.did, new)
        except store.StoreBusy as e:
            raise Pause("busy", f"токен или символ уже заняты другой активной сделкой: {e}") from None
        except store.BadTransition as e:
            raise Pause("state", f"сделка {run.did} в состоянии {d['state']}: {e}") from None

    def _pool(self, run: Run) -> D | None:
        try:
            return run.legs.spot.pool_price(run.token)
        except Exception:                      # noqa
            return None

    def _mark(self, run: Run) -> D | None:
        try:
            return run.legs.perp.funding(run.symbol)[0]
        except Exception:                      # noqa
            return None

    def _gap(self, run: Run) -> None:
        """Пауза между клипами: пул должен успеть (или не успеть) восстановиться — это и меряем. Прерывается «стоп»."""
        end = self.clock() + self.clip_gap_s
        for _ in range(int(math.ceil(self.clip_gap_s)) + 1):
            left = end - self.clock()
            if left <= 0 or self._stopping():
                return
            self.sleep(min(1.0, left))

    def _set_carry(self, run: Run, delta: D | None) -> None:
        if delta is None:
            return
        con = self.conns.get()
        d = store.get_deal(con, run.did)
        store.set_deal_state(con, run.did, d["state"], carry=delta)

    def _after_hedge(self, run: Run, clip_id: int, hr: HedgeResult, carry_in: D | None) -> None:
        con = self.conns.get()
        after = deal_book(con, run.did)
        dout = after.delta(run.dec) if after.known else None
        fields = {"perp_qty": hr.filled, "perp_quote": hr.quote, "carry_in": carry_in, "carry_out": dout}
        if hr.status == "ok":
            store.set_clip_state(con, clip_id, ClipState.BALANCED, **fields)
            self._set_carry(run, dout)
            return
        if hr.status == "unknown":
            store.set_clip_state(con, clip_id, ClipState.PERP_SENT, **fields)
            raise Pause("perp_unknown", hr.text or "исход заявки неизвестен")
        store.set_clip_state(con, clip_id, ClipState.HEDGE_DEFICIT, **fields)
        self._set_carry(run, dout)
        raise Pause("reduce_only_reject" if hr.status == "reduce_only_reject" else "hedge_deficit",
                    f"перп не добран: {hr.text}")

    def _hedge_clip(self, run: Run, clip_id: int, carry_in: D | None, last_full: bool = False) -> None:
        """Перп-нога по ЧЕКУ: вход — SELL floor((пришло + перенос)/m/шаг) контрактов; выход — BUY reduceOnly так, чтобы
        дельта осталась в [0, шаг·m) токенов; последний клип полного выхода — весь шорт сделки, но только если токенов
        по журналу меньше шага·m. Роутер DEX вернул часть входа (ревью 13.09, С2) — общая ветка: остаток токенов
        остаётся захеджирован, сделка OPEN, повторный «выход» доводит до CLOSED. Остаток меньше одного шага контрактов
        (m токенов) не хеджируется — это перенос, а не голая нога."""
        con = self.conns.get()
        bk = deal_book(con, run.did)
        if not bk.known:
            raise Pause("book_unknown", f"книга сделки неизвестна: {bk.why}")
        delta, step, m = bk.delta(run.dec), run.f.step, bk.m
        decision = Exposure(D(1), m, step).decide(bk.tokens(run.dec), bk.short, run.kind,
                                                 last_full=last_full)
        side, qty, ro = decision.side, decision.quantity, decision.reduce_only
        if side is None or qty <= 0:
            store.set_clip_state(con, clip_id, ClipState.BALANCED, perp_qty=ZERO, perp_quote=ZERO, carry_in=carry_in,
                                 carry_out=delta)
            self._set_carry(run, delta)
            return
        self._after_hedge(run, clip_id, self._hedge(run, clip_id, side, qty, ro), carry_in)

    def _basis(self, run: Run, clip_id: int) -> None:
        con = self.conns.get()
        c = store.get_clip(con, clip_id)
        try:
            din, dout, pq, pqq = int(c["dex_in"]), int(c["dex_out"]), dget(c["perp_qty"]), dget(c["perp_quote"])
            if run.kind != "entry" or not din or not dout or not pq:
                return
            dex_px = (D(din) / D(10) ** run.sdec) / (D(dout) / D(10) ** run.dec)
            b = (pqq / pq / run.m / dex_px - 1) * 10_000             # цена контракта / m — цена токена
            store.set_clip_state(con, clip_id, c["state"], basis_bps=float(b))
        except (TypeError, ValueError, InvalidOperation, ZeroDivisionError):
            return

    def _replan_rest(self, run: Run, remaining: int, r: D, done: int, max_input: int) -> list[int]:
        """Перекотировать остаток, не укрупняя ни один ещё не исполненный клип.

        Одобрение фиксирует не только суммарный бюджет, но и размер клипов. Иначе
        оптимизатор после первого клипа может выбрать ``n=1`` и превратить остаток
        нескольких небольших swaps в один большой, с другим влиянием на пул и MEV-риском.
        """
        con = self.conns.get()
        perp = run.legs.perp
        calib = _calib_from(run.plan.inputs["calib"])
        entry = run.kind == "entry"
        dec_in = run.sdec if entry else run.dec
        to_usd = (lambda u: D(u) / D(10) ** run.sdec) if entry else (lambda u: D(u) / D(10) ** run.dec * calib.p_ref)
        alpha, beta, band = self._perp_ab(run)           # α/β плана: остаток не подбирает их заново
        try:
            book = perp.book(run.symbol, tconfig.ASTER_DEPTH_LIMIT)
            _m, rate, _n = perp.funding(run.symbol)
            mkt = planner.Market(book=book, filters=run.f, fee_taker=D(str(config.FEES_TAKER[perp.venue])),
                                 sigma_1s=sigma_1s(perp, run.symbol),
                                 funding_h=rate / D(str(run.spec.get("period_h") or 1)),
                                 native_px=run.legs.native_px(), chain=_deal_cv(run.deal)[0])
            base_lim = _lim(run.cfg, run.deal)
            cap = to_usd(max_input)
            if isinstance(base_lim.clip_max_usd, D):
                cap = min(cap, base_lim.clip_max_usd)
            lim = replace(base_lim, deal_max_usd=None, clip_max_usd=cap,
                          alpha=alpha, beta_bps=beta, ab_band=band)
            d0 = planner.dex_cost(max(run.seq, 1), to_usd(done), calib.k, calib.g, r, calib.c0).d_end \
                if done > 0 else ZERO
            carry = deal_book(con, run.did).delta(run.dec) or ZERO       # перенос после клипа (M1: как исполнитель)
            p = planner.plan(deal_id=run.did, kind=run.kind, coin=run.deal["coin"], spot=run.plan.spot,
                             perp=run.plan.perp, symbol=run.symbol, leg_usd=to_usd(remaining), total_in_units=remaining,
                             dec_in=dec_in, calib=calib, mkt=mkt, lim=lim, r=r, d0=d0, now=self.clock(),
                             units_per_contract=run.m, carry0=carry)
        except PlanRefused as e:
            raise Pause("replan", f"остаток не планируется: {e}") from None
        except Exception as e:                 # noqa — стакан/фандинг не прочитаны
            raise Pause("replan", f"остаток не планируется: {redact(e)}") from None
        store.event(con, "replan", deal_id=run.did, intent_id=run.iid, r=r, n=len(p.clips),
                    clip_cap_usd=cap)
        return [c.dex_in_units for c in p.clips if c.dex_in_units > 0]

    def _after_clip(self, run: Run, clip_id: int, queue_: list[int], px0, m0, r: D, done: int) -> tuple[list[int], D]:
        """Между клипами: пауза, замер восстановления r (за вычетом хода марка), перепланирование остатка."""
        if not queue_:
            return queue_, r
        con = self.conns.get()
        px1 = self._pool(run)
        self._gap(run)
        if self._stopping():
            return queue_, r                   # guard следующего клипа поставит паузу
        px2, m1 = self._pool(run), self._mark(run)
        if None not in (px0, px1, px2):
            rm = planner.recovery(px0, px1, px2, m0, m1)
            if rm is not None:
                r = rm
                c = store.get_clip(con, clip_id)
                store.set_clip_state(con, clip_id, c["state"], recovery=float(rm))
        return self._replan_rest(run, sum(queue_), r, done, max(queue_)), r

    def _progress(self, run: Run) -> None:
        if run.n_total < 2:                    # один клип — прогресс не пишем: сразу придёт итог (C: штатное не пишем)
            return
        try:
            con = self.conns.get()
            fee = D(str(config.FEES_TAKER[run.legs.perp.venue]))
            pn = report.progress_numbers(kind="entry" if run.kind == "entry" else "exit",
                                         clips=store.clips_of(con, run.iid), dec_token=run.dec, dec_stable=run.sdec,
                                         n_planned=run.n_total,
                                         txs=run.sim_txs if run.legs.sim else intent_txs(con, run.iid),
                                         native_px=run.legs.native_px(), fee_taker=fee, m=run.m)
            self.hooks.notice('progress', dict(
                intent_id=run.iid, kind="entry" if run.kind == "entry" else "exit", coin=run.deal["coin"],
                clip=run.seq, clips=run.n_total, spot_usd=pn["dex_usd"], spot_qty=pn["dex_tokens"],
                spot_avg=pn["dex_avg_px"], perp_qty=pn["perp_qty"], perp_usd=pn["perp_quote"],
                perp_avg=pn["perp_avg_px"], imbalance_qty=pn["imbalance"], imbalance_usd=pn["imbalance_usd"],
                gas_usd=pn["gas_usd"], fees_usd=pn["commission_usd"], sim=run.legs.sim, total_usd=run.plan.leg_usd,
                note=None, step=run.f.step, m=run.mv))
        except Exception as e:                 # noqa — отчёт не ломает исполнение
            log.warning("прогресс %s: %s", run.iid, redact(e))

    # --- вход ---
    def _entry_limits(self, run: Run) -> None:
        """Лимиты владельца ещё раз — до первого действия входа: план мог быть построен, пока другая сделка ещё не
        открылась (оба плана видели 0 открытых). Свежий owner.toml; открытые — все, кроме этой (она ещё DRAFT)."""
        try:
            cfg = self.owner_loader()
        except OwnerConfigError as e:
            raise Pause("owner", f"owner.toml не прочитан: {e}") from None
        mx = cfg.get("limits.max_open_deals")
        n_open = sum(1 for d in store.active_deals(self.conns.get()) if d["id"] != run.did)
        if mx is not None and n_open >= mx:
            raise Pause("limit", f"открытых сделок {n_open} из {mx} (max_open_deals) — вход не начинаю")
        cap = cfg.get("limits.deal_max_usd_per_leg")
        if cap is not None and D(str(run.deal["leg_usd"])) > cap:
            v = formatters
            raise Pause("limit", f"сумма сделки {v.leg(run.deal['leg_usd'])} больше нового лимита {v.leg(cap)}")
        inst = deal_instrument(self.conns.get(), run.deal)
        if not inst.verified:                  # одобрено раньше, а m сделки не подтверждён (ревью 13.09, R6)
            raise Pause("limit", f"инструмент сделки {run.did} не подтверждён ({inst.why}) — вход не начинаю")
        allow = _allow_multiplier(cfg, _deal_cv(run.deal)[1])   # свежий owner.toml: разрешение могли снять (R8)
        try:                                   # и намерение, одобренное до выката отказа по множителю (ревью 13.09, С1)
            unit_refusal(run.deal["coin"], run.deal["symbol"], m=inst.m, allow_multiplier=allow,
                         venue=_deal_cv(run.deal)[1])
        except Refused:
            why = f": нет разрешения владельца ({MULT_KEY})" if (inst.m != 1 and not allow) else ""
            raise Pause("limit", f"контракт {run.deal['symbol']} с множителем — вход не начинаю{why}") from None

    def _entry(self, run: Run) -> None:
        con = self.conns.get()
        self._entry_limits(run)
        self._deal_to(run, DealState.ENTERING)
        queue_ = [c.dex_in_units for c in run.plan.clips if c.dex_in_units > 0]
        run.n_total = len(queue_)
        self._setup(run)
        self._approve(run, run.stable, sum(queue_))
        self._run_spot_clips(run, queue_, entry=True)

    def _run_spot_clips(self, run: Run, amounts: list[int], *, entry: bool, full: bool = False) -> None:
        r = dget(run.plan.inputs.get("r")) or ZERO

        def progress(seq, total):
            run.seq, run.n_total = seq, total
            self.current = (run.iid, seq, total)

        def prepare(amount, last):
            px, mark = (self._pool(run), self._mark(run)) if not last else (None, None)
            return (px, mark, deal_book(self.conns.get(), run.did).delta(run.dec))

        def settle(cid, ticket):
            if entry:
                self._basis(run, cid)
            self._invariant(run)
            self._progress(run)

        def next_amounts(cid, done, remaining, ticket):
            nonlocal r
            remaining, r = self._after_clip(run, cid, remaining, ticket[0], ticket[1], r, done)
            return remaining

        def select_amount(planned, last):
            amount = self._all_units(run) if full and last else planned
            if run.op_id:
                amount = min(amount, store.operation_remaining(store.get_operation(self.conns.get(), run.op_id)))
            return amount
        OperationController(self.conns.get()).run_clips(ClipLifecycle(
            intent_id=run.iid, amounts=amounts, progress=progress, guard=lambda: self.guard(run),
            select_amount=select_amount, prepare=prepare,
            spot=lambda cid, u, ticket: self._dex(run, cid, run.stable if entry else run.token,
                                                   run.token if entry else run.stable, u),
            hedge=lambda cid, last, ticket: self._hedge_clip(run, cid, ticket[2], last_full=full and last),
            settle=settle, next_amounts=next_amounts, finish=lambda: self._finish_main(run)))

    # --- выход ---
    def _all_units(self, run: Run) -> int:
        """Последний клип полного выхода: все токены сделки, но не больше, чем есть в кошельке."""
        bk = deal_book(self.conns.get(), run.did)
        if bk.tokens_raw is None:
            raise Pause("book_unknown", f"книга сделки неизвестна: {bk.why}")
        units = bk.tokens_raw
        if not run.legs.sim:
            wal = run.legs.spot.balances(run.token).get("token")
            if wal is None:
                raise Pause("wallet_unknown", "баланс токена не прочитан — последний клип не начинаю")
            units = min(units, int(wal))
        return units

    def _exit(self, run: Run) -> None:
        con = self.conns.get()
        if run.spec.get("perp_only"):
            return self._exit_perp_only(run)
        bk0 = deal_book(con, run.did)
        if not run.spec.get("all") and not bk0.inst_ok:
            # страховка намерения, одобренного раньше (перекотировка обычно отказывает первой): частичный выход при
            # неподтверждённом m снял бы шорт больше хеджа (ревью 13.09, R6)
            raise Pause("inst_unverified", f"инструмент сделки {run.did} не подтверждён — частичный выход не посчитать, "
                                           f"только «выход {run.did}» целиком")
        if not bk0.m_known:
            return self._exit_blind(run)
        queue_ = [c.dex_in_units for c in run.plan.clips if c.dex_in_units > 0]
        full = bool(run.spec.get("all"))
        if not full and run.m != 1 and bk0.known and queue_ and _partial_closes_short(bk0, sum(queue_), run.dec,
                                                                                      run.f.step):
            # страховка (перекотировка у кнопки отказывает первой, ревью 13.09 M2): одобренный частичный выход откупил
            # бы весь шорт и оставил меньше шага·m токенов вне учёта — не начинаю
            raise Pause("changed", f"частичный выход откупил бы весь шорт — нужен новый план: «выход {run.did}»")
        self._deal_to(run, DealState.EXITING)
        run.n_total = len(queue_)
        self._approve(run, run.token, self._all_units(run) if full else sum(queue_))
        self._run_spot_clips(run, queue_, entry=False, full=full)

    def _exit_blind(self, run: Run) -> None:
        """Полный выход сделки с неизвестным m (ревью 13.09, M3; план — Desk._plan_blind_exit). Дельту ног в токенах не
        посчитать — клипы и инвариант дельты не нужны: весь спот сделки одним свопом (неопределённая нога первой), затем
        весь шорт журнала — BUY reduceOnly. Позиция биржи = журнал (контракты) сверяется до и после. Сбой посередине —
        пауза; дальше то же без m: «выход» (остаток спота и шорта) или «выход перп»."""
        con = self.conns.get()
        self._deal_to(run, DealState.EXITING)
        run.seq = run.n_total = 1
        self.current = (run.iid, 1, 1)
        self.guard(run)
        units = self._all_units(run)
        clip_id = None
        if units > 0:
            self._approve(run, run.token, units)
            clip_id = store.create_clip(con, run.iid, 1, units)
            self._dex(run, clip_id, run.token, run.stable, units)
        bk = deal_book(con, run.did)
        if not bk.known:
            raise Pause("book_unknown", f"книга сделки неизвестна: {bk.why}")
        if bk.short > 0:
            if clip_id is None:
                clip_id = store.create_clip(con, run.iid, 1, 0)
            self._after_hedge(run, clip_id, self._hedge(run, clip_id, "BUY", bk.short, True), None)
        elif clip_id is not None:
            store.set_clip_state(con, clip_id, ClipState.BALANCED, perp_qty=ZERO, perp_quote=ZERO)
        self._finish_main(run)

    def _exit_perp_only(self, run: Run) -> None:
        """«выход <id> перп» — явная команда: откупить весь шорт, спот остаётся (дальше «откат» продаст его; m не
        известен — «выход»: откат считает дельту)."""
        con = self.conns.get()
        self._deal_to(run, DealState.EXITING)
        run.seq = run.n_total = 1
        self.current = (run.iid, 1, 1)
        self.guard(run)
        bk = deal_book(con, run.did)
        clip_id = store.create_clip(con, run.iid, 1, 0)
        hr = self._hedge(run, clip_id, "BUY", bk.short, True)
        self._after_hedge(run, clip_id, hr, bk.delta(run.dec))
        self._backfill(run)
        from .operations import EndDecision
        OperationController(con).commit_end(run, EndDecision(DealState.PAUSED, IntentStatus.DONE,
            reason="перп закрыт, спот остался — «откат» продаст спот" if run.m_known else
                   "перп закрыт, спот остался — «выход» продаст спот"))
        after = deal_book(con, run.did)
        tokens = after.tokens(run.dec)
        avg = (hr.quote / hr.filled) if hr.filled else None       # оценка спота по цене откупа (за контракт)
        self.hooks.notice('perp_closed', dict(
            coin=run.deal["coin"], deal_id=run.did, qty=hr.filled, usd=hr.quote, spot_qty=tokens,
            spot_usd=(tokens * avg / run.m) if (tokens is not None and avg and run.m_known) else None,
            step=run.f.step, sim=run.legs.sim, m=run.mv))

    # --- дохедж / откат ---
    def _deal_fix_state(self, run: Run) -> None:
        con = self.conns.get()
        d = store.get_deal(con, run.did)
        if d["state"] == DealState.OPEN:
            store.set_deal_state(con, run.did, DealState.PAUSED, reason=run.kind)
        elif d["state"] != DealState.PAUSED:
            raise Pause("state", f"сделка {run.did} в состоянии {d['state']}")

    def _rehedge(self, run: Run) -> None:
        con = self.conns.get()
        self._deal_fix_state(run)
        run.seq = run.n_total = 1
        self.current = (run.iid, 1, 1)
        self.guard(run, invariant=False)
        bk = deal_book(con, run.did)
        if not bk.known:
            raise Pause("book_unknown", f"книга сделки неизвестна: {bk.why}")
        if not bk.m_known:                     # одобрено раньше, а m сделки теперь не известен (ревью 13.09, M3)
            raise Pause("inst_unverified", f"{formatters.m_unknown_text(run.did)} ({bk.inst_why}) — дохедж не отправляю")
        delta, step, m = bk.delta(run.dec), run.f.step, bk.m
        decision = Exposure(D(1), m, step).decide(bk.tokens(run.dec), bk.short, "rehedge")
        side, qty, ro = decision.side, decision.quantity, decision.reduce_only
        if side is None or qty <= 0:
            return self._settle_fix(run, noop="ноги уже ровно — ничего не отправлено")
        if side == "SELL" and not bk.inst_ok:  # одобрено раньше, а m сделки не подтверждён (ревью 13.09, R6)
            raise Pause("inst_unverified", f"инструмент сделки {run.did} не подтверждён ({bk.inst_why}) — дохедж "
                                           "продажей не отправляю")
        if side == "SELL" and m != 1:          # продажа контрактов с множителем — по СВЕЖЕМУ разрешению владельца (R8)
            try:
                allow = _allow_multiplier(self.owner_loader(), _deal_cv(run.deal)[1])
            except OwnerConfigError as e:
                raise Pause("owner", f"owner.toml не прочитан: {e}") from None
            if not allow:
                raise Pause("limit", f"контракт {run.symbol} с множителем — дохедж продажей не отправляю: нет "
                                     f"разрешения владельца ({MULT_KEY})")
        if side != run.spec.get("side"):
            raise Pause("changed", "дельта ног сменила знак после плана — пришлите «дохедж» заново")
        qty = min(qty, D(str(run.spec["qty"])))
        def apply(clip_id, action, hr):
            self._after_hedge(run, clip_id, hr, delta)

        # ``_after_hedge`` can pause on UNKNOWN or a partial/reduce-only
        # result; then coordinator never verifies or commits a terminal fix.
        # Preserve the actual filled/quote values for the successful case.
        result: dict[str, HedgeResult] = {}
        def submit(clip_id, action):
            hr = self._hedge(run, clip_id, action.side, action.quantity, action.reduce_only)
            result['value'] = hr
            return hr
        def finish_actual(action):
            hr = result['value']
            self._settle_fix(run, qty=hr.filled, usd=hr.quote, side=action.side)
        LifecycleCoordinator(con).run_hedge(HedgeProgram(
            intent_id=run.iid, prepare=lambda: HedgeAction(side, qty, ro), submit=submit, apply=apply,
            verify=lambda: self._invariant(run), finish=finish_actual))

    def _undo(self, run: Run) -> None:
        con = self.conns.get()
        self._deal_fix_state(run)
        run.seq = run.n_total = 1
        self.current = (run.iid, 1, 1)
        self.guard(run, invariant=False)
        bk = deal_book(con, run.did)
        if not bk.known:
            raise Pause("book_unknown", f"книга сделки неизвестна: {bk.why}")
        if not bk.m_known:                     # авто-откат и одобренный раньше: дельта по m = 1 — голый шорт (M3)
            raise Pause("inst_unverified", f"{formatters.m_unknown_text(run.did)} ({bk.inst_why}) — откат не отправляю")
        delta, ts = bk.delta(run.dec), bk.tstep(run.f.step)
        if delta < ts:
            return self._settle_fix(run, noop="голого лонга нет — ничего не продано")
        sell = delta - delta % ts              # токены, кратно шагу·m: остаток — меньше шага, захеджирован
        units = min(int((sell * D(10) ** run.dec).to_integral_value(ROUND_FLOOR)), int(run.spec.get("units") or 0))
        if not run.legs.sim:
            wal = run.legs.spot.balances(run.token).get("token")
            if wal is None:
                raise Pause("wallet_unknown", "баланс токена не прочитан — откат не начинаю")
            units = min(units, int(wal))
        if units <= 0:
            return self._settle_fix(run, noop="продавать нечего")
        self._approve(run, run.token, units)
        clip_id = store.create_clip(con, run.iid, 1, units)
        res = self._dex(run, clip_id, run.token, run.stable, units)
        after = deal_book(con, run.did)
        store.set_clip_state(con, clip_id, ClipState.BALANCED, perp_qty=ZERO, perp_quote=ZERO, carry_in=delta,
                             carry_out=after.delta(run.dec) if after.known else None)
        self._invariant(run)
        self._settle_fix(run, qty=D(res.amount_in) / D(10) ** run.dec, usd=D(res.amount_out) / D(10) ** run.sdec)

    def _settle_fix(self, run: Run, *, qty: D | None = None, usd: D | None = None, side: str | None = None,
                    noop: str | None = None) -> None:
        """Итог дохеджа/отката: состояние сделки и сообщение (views.fix_done). noop — ничего не отправлено."""
        con = self.conns.get()
        bk = deal_book(con, run.did)
        step = run.f.step
        last = con.execute("SELECT status, spec_json FROM intents WHERE deal_id=? AND kind IN ('entry','exit') "
                           "ORDER BY created DESC LIMIT 1", (run.did,)).fetchone()
        if bk.known and bk.short == 0 and bk.tokens(run.dec) < bk.tstep(step):
            new, fields = DealState.CLOSED, {"dust": bk.tokens(run.dec), "carry": bk.delta(run.dec)}
        elif last and last["status"] == IntentStatus.DONE and not json.loads(last["spec_json"]).get("perp_only"):
            new, fields = DealState.OPEN, {"carry": bk.delta(run.dec)}
        else:
            new, fields = DealState.PAUSED, {"carry": bk.delta(run.dec)}
        self._backfill(run)
        from .operations import EndDecision
        OperationController(con).commit_end(run, EndDecision(new, IntentStatus.DONE, fields=fields,
            reason="сбалансировано" if new == DealState.PAUSED else None))
        what = noop or f"{side or 'DEX'} {qty} = {usd}"                  # журнал: сырые числа
        store.event(con, "fixed", deal_id=run.did, intent_id=run.iid, intent_kind=run.kind, what=what, state=str(new))
        self.hooks.notice('fix_done', dict(
            kind=run.kind, coin=run.deal["coin"], deal_id=run.did, state=str(new), qty=qty, usd=usd, side=side,
            noop=noop, delta=bk.delta(run.dec) if bk.known else None, step=step, sim=run.legs.sim, m=bk.m_view))

    # --- итог ---
    def _backfill(self, run: Run) -> None:
        con = self.conns.get()
        venue = run.legs.fill_venue
        try:
            from .accounting import sync_fills
            sync_fills(con, run.deal, run.legs)
        except Exception as e:                 # noqa — отчёт возьмёт итоги заявок (комиссия — оценка)
            log.warning("userTrades %s не добраны: %s", run.symbol, redact(e))

    def _finish_main(self, run: Run) -> None:
        con = self.conns.get()
        bk = self._invariant(run)
        if not run.legs.sim:
            wal = run.legs.spot.balances(run.token).get("token")
            if wal is None:
                raise Pause("wallet_unknown", "баланс токена не прочитан — итог не сверен")
            # меньше, чем по журналу, — токены ушли мимо сделки; больше — свои токены владельца, сделку не касаются
            # (последний клип выхода и так продаёт min(журнал, кошелёк)) — та же мера, что в reconcile.check_deal
            ts = bk.tstep(run.f.step)
            if int(wal) < bk.tokens_raw:
                v = formatters
                raise Pause("position_mismatch", f"в кошельке {v.tok(D(int(wal)) / D(10) ** run.dec, step=ts)} "
                                                 f"— меньше, чем по журналу сделки "
                                                 f"{v.tok(bk.tokens(run.dec), step=ts)} {run.deal['coin']}")
        self._backfill(run)
        finished = self.clock()
        if run.kind == "entry":
            new, fields = DealState.OPEN, {"carry": bk.delta(run.dec)}
        elif bk.short == 0 and bk.tokens(run.dec) < bk.tstep(run.f.step):     # меньше шага·m токенов — пыль
            new, fields = DealState.CLOSED, {"carry": bk.delta(run.dec), "dust": bk.tokens(run.dec)}
        else:
            new, fields = DealState.OPEN, {"carry": bk.delta(run.dec)}
        if run.kind == "exit" and run.spec.get("all") and not run.spec.get("perp_only") and new != DealState.CLOSED:
            store.event(con, "exit_residual", deal_id=run.did, intent_id=run.iid, tokens=bk.tokens(run.dec),
                        short=bk.short, m=bk.m if bk.m_known else None)  # роутер DEX вернул часть токенов: остаток
            #                                                                 захеджирован (ревью 13.09, С2)
        partial = False
        root_states = ()
        if run.op_id:
            op = store.get_operation(con, run.op_id)
            if int(op['reserved_raw']):
                raise Pause('book_unknown', 'операция имеет неразрешённый резерв')
            partial = store.operation_remaining(op) > 0
            state = store.OpState.PARTIAL if partial else (store.OpState.OPEN if run.kind == 'entry' else store.OpState.CLOSED)
            root_states = (state,)
        from .operations import EndDecision
        OperationController(con).commit_end(run, EndDecision(
            new, IntentStatus.PARTIAL if partial else IntentStatus.DONE, root_states, fields))
        snapshot, cost, liq = self._final(run, finished, closed=new == DealState.CLOSED)
        store.event(con, "final", deal_id=run.did, intent_id=run.iid, cost_usd=cost, state=str(new), sim=run.legs.sim,
                    **liq)                     # порог тревоги ликвидации замораживается на входе («позиции» его читают)
        self.hooks.final_report(snapshot)

    def _deal_pnl(self, run: Run) -> tuple[D | None, D | None, D | None, D | None]:
        """Итог закрытой сделки: спот (выручка выходов − стоимость входов), перп (продано − откуплено − комиссии),
        фандинг (income с открытия; в симуляции не начисляется — «—»)."""
        con = self.conns.get()
        from . import accounting
        if accounting.is_bound(con, run.did):
            try:
                accounting.sync_funding(con, run.deal, run.legs.perp)
            except Exception as exc:
                log.warning('scoped funding not refreshed: %s', type(exc).__name__)
            from .marks import journal
            source = journal(con, run.deal)
            spot = source.spot_flow
            perp = source.perp_flow - source.fees if source.fees is not None and source.perp_flow is not None else None
            total = None
            if spot is not None and perp is not None and source.funding is not None and not source.missing_flows:
                gas = source.gas_usd(run.legs.native_px())
                if gas is not None:
                    total = spot + perp + source.funding - gas
            return spot, perp, source.funding, total
        spot = spot_quote_flows(con, run.did, run.sdec).net
        perps = perp_quote_flows(con, run.did)
        sell, buy = perps.credit, perps.debit
        fills = deal_fills(con, run.did)
        fees = [dget(f["commission_abs"]) for f in fills]
        perp = (sell - buy - sum((abs(f) for f in fees), ZERO)) if fills and None not in fees and not perps.missing else None
        fund = None
        if not run.legs.sim:
            try:
                start = int(float(run.deal["created"]) * 1000)
                store.add_funding_income(con, run.legs.perp.venue, run.legs.perp.funding_income(run.symbol, start))
                rows = con.execute("SELECT income FROM funding_income WHERE venue=? AND symbol=? AND ts>=?",
                                   (run.legs.perp.venue, run.symbol, start)).fetchall()
                fund = sum((D(r[0]) for r in rows), ZERO)
            except Exception as e:             # noqa
                log.warning("фандинг %s не добран: %s", run.symbol, redact(e))
        # газ (свопы и approve всех намерений сделки) — в итог: без него итог завышен на весь газ сделки
        gas = ZERO
        if not run.legs.sim:
            txs = [t for (iid,) in con.execute("SELECT id FROM intents WHERE deal_id=?", (run.did,))
                   for t in intent_txs(con, iid)]
            gas = report.gas_totals(txs, run.legs.native_px())["usd"] if txs else ZERO
        total = spot + perp + fund - gas if (spot is not None and perp is not None and fund is not None and gas is not None) else None
        return spot, perp, fund, total

    def _final(self, run: Run, finished: float, closed: bool):
        from ..ipc.reports import FinalView
        con = self.conns.get()
        legs, perp = run.legs, run.legs.perp
        fee = D(str(config.FEES_TAKER[perp.venue]))
        from . import accounting
        bound = accounting.is_bound(con, run.did)
        cost_source_before = accounting.cost_revision(con, run.did, run.iid) if bound else None
        txs = run.sim_txs if legs.sim else intent_txs(con, run.iid)
        execution = accounting.execution_summary(con, run.did, run.iid) if bound else None
        fills = deal_fills(con, run.did, run.iid)
        if not fills and not bound:
            fills = orders_as_fills(con, run.iid, fee)
        inp = run.plan.inputs
        fh = nxt = None
        try:
            _m, rate, nxt = perp.funding(run.symbol)
            fh = rate / D(str(run.spec.get("period_h") or 1))
        except Exception:                      # noqa
            pass
        posrow = None
        if not legs.sim and hasattr(perp, "position_risk"):
            try:
                posrow = next((r for r in perp.position_risk(run.symbol) if r.get("symbol") == run.symbol), None)
            except Exception:                  # noqa
                posrow = None
        entry = run.kind == "entry"
        plan_total = dget(run.plan.est.get("total_usd"))
        fn = report.final_numbers(kind="entry" if entry else "exit", clips=store.clips_of(con, run.iid), txs=txs,
                                  fills=fills, dec_token=run.dec, dec_stable=run.sdec, native_px=legs.native_px(),
                                  ref_px=dget((inp.get("calib") or {}).get("p_ref")),
                                  perp_mid_ref=dget((inp.get("book_top") or {}).get("mid")), plan_total_usd=plan_total,
                                  est_exit_usd=plan_total if entry else None, funding_h=fh, next_funding_ms=nxt,
                                  position=posrow, started=run.started, finished=finished, m=run.m,
                                  perp_summary=execution, gas_complete=not bound or accounting.gas_complete(txs))
        try:
            bal = legs.spot.balances(run.token)
        except Exception:                      # noqa
            bal = {}
        bk = deal_book(con, run.did)
        if not run.m_known:                    # m не известен (M3): курсовой по цене контракта / m не считается
            fn["basis_bps"] = None
        dex, pl, gas = fn["dex"], fn["perp"], fn["gas"]
        approves = sum(1 for t in txs if t.get("kind") == "approve" and t.get("gas_used") is not None)
        mid = dget((inp.get("book_top") or {}).get("mid"))
        dust = bk.delta(run.dec) if bk.known else None
        h = lambda u, d: None if u is None else D(u) / D(10) ** d
        pnl = self._deal_pnl(run) if closed else (None, None, None, None)
        try:
            margin = perp.available_margin()
        except Exception:                      # noqa
            margin = None
        cost_source_after = accounting.cost_revision(con, run.did, run.iid) if bound else None
        if bound and (any(x != 'funding:coverage_unproven' for x in accounting.sources(con, run.did).missing)
                      or cost_source_before != cost_source_after):
            fn['total_complete'] = False
            fn['breakeven_h'] = None
        cost = fn["total_usd"] if fn["total_complete"] else None
        liq_pct = (fn["liq_dist_frac"] * 100) if fn["liq_dist_frac"] is not None else None
        liq_set = run.cfg.get(f"perp.{run.deal['perp_venue']}.liq_alert_pct") if entry else None
        liq_thr = planner.liq_alert_pct(liq_set, liq_pct)      # «auto» — ½ расстояния на входе; число — как есть
        est = run.plan.est or {}
        plan_impact = ((dget(est.get("dex_fee_usd")) or ZERO) + (dget(est.get("dex_impact_usd")) or ZERO)
                       if "dex_fee_usd" in est else None)       # удар спота по плану — порог показа «×1.5»
        swaps = sum(1 for t in txs if t.get("kind") in report.SWAP_KINDS and t.get("gas_used") is not None)
        fv = FinalView(
            intent_id=run.iid, kind="entry" if entry else "exit", coin=run.deal["coin"], chain=run.deal["chain"],
            perp_venue=run.deal["perp_venue"], deal_id=run.did, leg_usd=run.plan.leg_usd if entry else None,
            deal_leg_usd=dget(run.deal["leg_usd"]), spot_qty=dex["tokens"], spot_usd=dex["usd"], perp_qty=pl["qty"],
            impact_usd=fn["impact_usd"], planned_impact_usd=plan_impact, gas_usd=gas["swap_usd"],
            perp_slip_usd=fn["perp_slip_usd"], perp_fee_usd=fn["commission_usd"],
            approve_gas_usd=gas["approve_usd"] if approves else None, swaps=swaps, native_px=legs.native_px(),
            leverage=run.cfg.get(f"perp.{run.deal['perp_venue']}.leverage"),
            margin_type=run.cfg.get(f"perp.{run.deal['perp_venue']}.margin_type"),
            liq_dist_pct=liq_pct, liq_alert_pct=liq_thr,
            dust_qty=dust, dust_usd=(dust * mid / run.m) if (dust is not None and mid) else None, step=run.f.step,
            basis_pct=(fn["basis_bps"] / 100) if fn["basis_bps"] is not None else None,
            expected_usd_h=fn["usd_per_h"], cost_usd=cost, planned_cost_usd=plan_total,
            exit_cost_usd=plan_total if entry else None, breakeven_h=fn["breakeven_h"],
            wallet_stable=h(bal.get("stable"), run.sdec), wallet_native=h(bal.get("native"), 18), margin_avail=margin,
            open_deals=len(store.active_deals(con)), max_open_deals=run.cfg.get("limits.max_open_deals"),
            tx_hashes=() if legs.sim else tuple(fn["tx_hashes"]), pnl_spot_usd=pnl[0], pnl_perp_usd=pnl[1],
            funding_usd=pnl[2], pnl_total_usd=pnl[3], partial=not entry and not closed,
            exit_all=bool(run.spec.get("all")), rest_qty=None if entry else bk.tokens(run.dec),
            partial_reason=(f"DEX продал не все токены — «выход {run.did}» ещё раз"
                            if (not entry and not closed and run.spec.get("all")) else None), sim=legs.sim, m=run.mv)
        extra = {"liq_dist_pct": liq_pct, "liq_alert_pct": liq_thr}
        if bound:
            extra['accounting_cost_revision'] = cost_source_after if cost_source_before == cost_source_after else None
        return fv, cost, extra


# --- CLI `funding_bot plan` ------------------------------------------------------------------------------------
def plan_cli(coin: str, spot: str, perp: str, usd: D, *, owner_path=None, db_path=None, table_loader=None,
             runtime: Runtime | None = None) -> str:
    """План входа в симуляции (dry) — тем же кодом, что у кнопок, без записи намерения и без ключей: для сверки
    плана с живым стаканом с Мака или сервера. Возвращает простой текст сообщения плана.
    Единственное место в этом модуле, где рендер плана в текст остаётся рядом с Desk: это не путь исполнителя —
    отдельная команда `funding_bot plan` в один процесс, без core/interface IPC (AC-07 — про границу процессов)."""
    from types import SimpleNamespace
    from ..tg import views as _tg_views      # исключение AC-07: однопроцессный CLI, границы core/interface нет
    from ..tg.sender import to_plain
    cfg = owner_mod.load(owner_path)
    conns = Conns(db_path)
    rt = runtime or build_runtime(cfg, conns, mode="dry")
    desk = Desk(conns, lambda sim: rt.sim if sim else None, owner_loader=lambda: cfg, table_loader=table_loader)
    plan, ctx = desk.plan_entry(coin, spot, perp, usd, cfg=cfg, sim=True, write_checks=False)
    return to_plain(_tg_views.plan(SimpleNamespace(**desk.plan_view("—", plan, ctx))))
