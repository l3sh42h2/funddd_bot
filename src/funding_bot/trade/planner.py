"""Планировщик клипов фазы 2 — чистые функции (trade_spec §7, отчёт clip): никаких сетей, ключей и часов,
всё на входе. Деньги и количества — Decimal; float из котировок (gas_usd) переводится через str.

Модель DEX-стороны (для суммы S, разбитой на n клипов по q = S/n):
- средняя цена клипа = p0·(1 + c0 + D + k·q), плюс газ g (tradeFee, USD) за своп;
  c0 — не зависящее от размера (комиссия пула, налог, сдвиг опорной цены OKX от пула), D — смещение цены пула,
  оставшееся от своих прошлых клипов; после клипа D ← (1 − r)·(D + 2kq) (предельная цена уходит на 2kq);
- r ∈ [0, 1] — доля смещения, которую пул вернул до следующего клипа, ИЗМЕРЯЕТСЯ ботом (априори 0).
  Почему это главное: при r = 0 суммарный удар = k·S² при любом n (клипы идут по той же кривой v3), и дробление
  лишь добавляет газ — оптимум n = 1. Только при восстановлении пула работает q* = √(g/k).
  В двух живых всплесках AIW3 пул за 1–60 мин не восстановился, поэтому сегодня план AIW3 = 1 клип на $500.
- k и c0 — из МНК по котировкам S/8…S (priceImpactPercent один их смешивает: он меряет против опорной цены OKX).

Перп-сторона: размер дочерней IOC задаёт стакан (α — доля лучшего уровня, β — предел цены в б.п. от лучшей), а
не клип DEX. Связь двух ног — только ограничение нехеджированного риска z·σ₁ₛ·√Δt·q ≤ unhedged_usd_max, где
Δt = подтверждение сети + RTT + (m − 1)·τ_p и m — число дочерних на клип.

Пусто в owner.toml = запрещено для live. Здесь пустой ключ не подменяется числом: план в dry строится без
соответствующего ограничения, а ключ попадает в Plan.missing_owner_keys (engine в live вызывает require_live()).
Технические константы ниже помечены [A] — допущения до первых клипов бота; место им — tconfig.py.

«auto» владельца (12.09) — число выбирает план и замораживает его в est; исполнитель берёт числа плана, слова
"auto" он не видит никогда:
- α/β: перебор α ∈ tconfig.AB_ALPHA_GRID × β ∈ beta_candidates() (расстояние до первых 5 уровней + тик, не уже
  max(3 тика, 5 б.п.)), для каждой пары — прежний перебор n; берётся минимум total, равенство — к меньшим
  дочерним, потом к меньшему β. В auto α — доля объёма в пределах кэпа β (band): при узком β это тот же лучший
  уровень, а β шире пускает дочернюю к ближним уровням, когда лучший уровень мельче минимума биржи (с числом α —
  доля лучшего уровня, как раньше: там β как предел размера не связывает никогда). Вариант, которому на клип нужно
  больше ASTER_IOC_PARTIAL_RETRIES ожиданий пополнения стакана, недопустим: исполнитель встал бы на недоборе;
- unhedged_usd_max: plan_cost_drift_pct % клипа q (риск — прежний z·σ₁ₛ·√Δt·q; без допуска — не разрешается);
- clips_max: без потолка владельца, перебор до tconfig.PLAN_N_SCAN;
- exec_time_max_s: 3 × ожидаемая длительность + 60 с; ожидаемая = n·(Δt + пауза между клипами при n > 1) +
  ожидания пополнения × refill_wait_max_s.
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from typing import Any, Iterable, Sequence
from . import tconfig
from .types import Book, ClipPlan, DexQuote, Filters, Plan

D = Decimal
ZERO = D(0)
ONE = D(1)
BPS = D(10_000)

# --- технические допущения [A] (не деньги; перенести в tconfig.py) --------------------------------------
T_CONFIRM_S = {"bsc": D("1.5")}   # чек BSC: блок ~0.45 с + опрос чека; 1–3 с по отчёту state [A]
T_CONFIRM_DEFAULT_S = D("3")      # неизвестная сеть — пессимистичнее [A]
RTT_S = D("0.3")                  # ireland → Aster, запрос-ответ [A]
TAU_P_S = D("1")                  # шаг между дочерними IOC (ждём восстановления лучшего уровня) [A]
CLIP_GAP_S = D("60")              # пауза между клипами (замер восстановления пула) — только для цены ожидания [A]
Z_UNHEDGED = D("3")               # квантиль риска голой ноги (≈99.9 % в одну сторону) [A]
MIN_NOTIONAL_MULT = D("1.5")      # клип ≥ 1.5·minNotional: цена сдвинется, а заявка ниже минимума не встанет
GAS_PAY_MULT = D("1.5")           # газ не ограничен, но должен быть оплачиваем с запасом (§6 предпроверки)
N_SCAN_NO_LIMIT = tconfig.PLAN_N_SCAN   # clips_max «auto» или пуст — сколько вариантов n перебрать
AUTO = "auto"                     # слово владельца вместо числа (owner.py): число подбирает план

KINDS = ("entry", "exit")


class PlanRefused(Exception):
    """План невозможен при заданных ограничениях. Текст — по-русски, уходит владельцу как есть."""


# --- Decimal-помощники ---------------------------------------------------------------------------------
def dnum(x: Any) -> D | None:
    """float → Decimal через str (без двоичного хвоста 0.1 → 0.1000000000000000055…); None остаётся None."""
    if x is None:
        return None
    if isinstance(x, bool):
        raise TypeError("bool не число")
    if isinstance(x, D):
        return x
    if isinstance(x, int):
        return D(x)
    if isinstance(x, float):
        if not math.isfinite(x):
            raise ValueError(f"не конечное число: {x}")
        return D(repr(x))
    return D(str(x))


def floor_to(x: D, step: D) -> D:
    """Вниз к сетке шага (количество перпа, цена SELL-кэпа): округление вверх дало бы заявку больше, чем есть."""
    return (x / step).to_integral_value(ROUND_FLOOR) * step


def ceil_to(x: D, step: D) -> D:
    return (x / step).to_integral_value(ROUND_CEILING) * step


def dsqrt(x: D) -> D:
    with localcontext() as ctx:
        ctx.prec = 34
        return x.sqrt() if x > 0 else ZERO


def _median(vals: Sequence[D]) -> D:
    s = sorted(vals)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


# --- калибровка k и c0 -----------------------------------------------------------------------------------
def calib_amounts(total_units: int) -> list[int]:
    """Суммы котировок калибровки S/8, S/4, S/2, S в сырых единицах входного токена (tconfig.CALIB_FRACS)."""
    out = []
    for f in tconfig.CALIB_FRACS:
        u = int((D(total_units) * f).to_integral_value(ROUND_FLOOR))
        if u > 0 and u not in out:
            out.append(u)
    return out


@dataclass(frozen=True)
class Calib:
    """Итог калибровки. k — доля цены на $ клипа (≥ 0), c0 — не зависящая от размера доля, g — газ за своп, $.
    p_ref — опорная цена токена, $ (USD за токен), от которой отсчитаны c0 и k; points — (usd, цена) по котировкам."""
    kind: str
    k: D
    c0: D
    g: D
    p_ref: D
    points: tuple[tuple[D, D], ...]
    k_raw: D                          # наклон МНК до обрезки снизу нулём (отрицательный — шум/смена маршрута)
    resid_bps: D                      # худшее отклонение котировки от прямой, б.п.
    k_v3: D | None = None             # сверка: 1/(L·√P) по slot0/liquidity пула v3
    k_ratio: D | None = None          # k / k_v3 — далеко от 1 = что-то не так с маршрутом или сдвигом

    def as_dict(self) -> dict:
        return {"kind": self.kind, "k": self.k, "c0": self.c0, "g": self.g, "p_ref": self.p_ref,
                "points": [list(p) for p in self.points], "k_raw": self.k_raw, "resid_bps": self.resid_bps,
                "k_v3": self.k_v3, "k_ratio": self.k_ratio}


def _quote_point(q: DexQuote, kind: str) -> tuple[D, D]:
    """Котировка → (размер в $, цена токена в $). entry: стейбл → токен (цена уплаченная, растёт с размером);
    exit: токен → стейбл (цена полученная, падает с размером)."""
    a_in = D(q.amount_in) / D(10) ** q.dec_in
    a_out = D(q.amount_out) / D(10) ** q.dec_out
    if a_in <= 0 or a_out <= 0:
        raise PlanRefused(f"котировка без суммы ({q.amount_in} → {q.amount_out}): маршрута нет")
    if kind == "entry":
        return a_in, a_in / a_out
    return a_out, a_out / a_in


def calibrate(quotes: Iterable[DexQuote], kind: str, p_ref: D | None = None,
              k_v3: D | None = None) -> Calib:
    """МНК p(s) = a + b·s по котировкам S/8…S; a = p0·(1 ± c0), b = ±p0·k (+ для покупки, − для продажи).

    p_ref — независимая опорная цена (цена пула/рыночная OKX), тогда c0 = сдвиг котировок от неё; без неё опорой
    служит свободный член (c0 = 0, весь не зависящий от размера сдвиг остаётся внутри цены). k < 0 обрезается до 0:
    «чем больше, тем дешевле» — это смена маршрута или шум, и дробление на нём экономить не должно.
    """
    if kind not in KINDS:
        raise ValueError(f"kind: {kind}")
    qs = list(quotes)
    if any(q.honeypot for q in qs):
        raise PlanRefused("OKX помечает токен как honeypot — не торгуем")
    pts = [_quote_point(q, kind) for q in qs]
    if len({s for s, _ in pts}) < 2:
        raise PlanRefused("для калибровки нужны котировки хотя бы двух разных размеров")
    n = D(len(pts))
    mx = sum((s for s, _ in pts), ZERO) / n
    my = sum((p for _, p in pts), ZERO) / n
    sxx = sum(((s - mx) ** 2 for s, _ in pts), ZERO)
    sxy = sum(((s - mx) * (p - my) for s, p in pts), ZERO)
    b = sxy / sxx
    a = my - b * mx
    if a <= 0:
        raise PlanRefused("калибровка дала неположительную цену — котировки несовместимы")
    ref = p_ref if p_ref is not None and p_ref > 0 else a
    if kind == "entry":
        c0, k_raw = a / ref - ONE, b / ref
    else:
        c0, k_raw = ONE - a / ref, -b / ref
    k = max(k_raw, ZERO)
    resid = max(abs(p - (a + b * s)) for s, p in pts) / a * BPS
    g = _median([dnum(q.gas_usd) for q in qs]) if qs else ZERO
    ratio = (k / k_v3) if (k_v3 and k_v3 > 0) else None
    return Calib(kind=kind, k=k, c0=c0, g=max(g, ZERO), p_ref=ref, points=tuple(pts), k_raw=k_raw,
                 resid_bps=resid, k_v3=k_v3, k_ratio=ratio)


def k_from_v3(liquidity: int, sqrt_price_x96: int, dec0: int, dec1: int, stable_is_token1: bool) -> D:
    """k = 1 / (виртуальный резерв стейбла в $) для пула Uniswap-v3 в текущем тике: L·√P (token1) или L/√P
    (token0), где √P = sqrtPriceX96 / 2^96 в сырых единицах. Верно, пока своп не выходит из тика."""
    if liquidity <= 0 or sqrt_price_x96 <= 0:
        raise ValueError("пул без ликвидности")
    with localcontext() as ctx:
        ctx.prec = 40
        sp = D(sqrt_price_x96) / D(2) ** 96
        y = D(liquidity) * sp / D(10) ** dec1 if stable_is_token1 else D(liquidity) / sp / D(10) ** dec0
        return +(ONE / y)


# --- стоимость DEX-стороны -----------------------------------------------------------------------------
@dataclass(frozen=True)
class DexCost:
    """Разбивка стоимости DEX-ноги, $: fee = c0-часть, impact = k·q + смещение D, gas = n·g. d_end — смещение после."""
    fee: D
    impact: D
    gas: D
    d_end: D
    clip_frac: tuple[D, ...]          # средняя доля цены каждого клипа: c0 + D_i + k·q_i

    @property
    def total(self) -> D:
        return self.fee + self.impact + self.gas


def clip_sizes(S: D, n: int) -> list[D]:
    """n клипов по S/n, последний забирает остаток округления (сумма ровно S)."""
    if n < 1:
        raise ValueError("n ≥ 1")
    q = S / n
    out = [q] * (n - 1)
    out.append(S - q * (n - 1))
    return out


def dex_cost(n: int, S: D, k: D, g: D, r: D, c0: D, d0: D = ZERO) -> DexCost:
    """Точная симуляция: клип i стоит q·(c0 + D + k·q) + g; после него D ← (1 − r)·(D + 2kq)."""
    if not (ZERO <= r <= ONE):
        raise ValueError(f"r вне [0, 1]: {r}")
    d = d0
    fee = impact = ZERO
    fr = []
    for q in clip_sizes(S, n):
        fee += q * c0
        impact += q * (d + k * q)
        fr.append(c0 + d + k * q)
        d = (ONE - r) * (d + 2 * k * q)
    return DexCost(fee=fee, impact=impact, gas=g * n, d_end=d, clip_frac=tuple(fr))


def q_star(g: D, k: D) -> D | None:
    """Газовый оптимум клипа при полном восстановлении пула: √(g/k). k = 0 — дробить незачем (None)."""
    return dsqrt(g / k) if k > 0 else None


def recovery(px_before: D, px_after: D, px_now: D, mark_before: D | None = None, mark_now: D | None = None) -> D | None:
    """Доля своего смещения цены пула, которую рынок вернул к моменту px_now, за вычетом собственного хода рынка
    (марк перпа за то же окно). px_before — до клипа, px_after — сразу после. 1 = вернулся полностью, 0 = нет.
    Смещения нет (≈0) — None: делить не на что, r не меняем."""
    if px_before <= 0:
        return None
    moved = px_after / px_before - ONE                         # наш след, доля
    if abs(moved) < D("1e-9"):
        return None
    drift = (mark_now / mark_before - ONE) if (mark_before and mark_now and mark_before > 0) else ZERO
    left = px_now / px_before - ONE - drift                     # что от следа осталось, без хода рынка
    r = ONE - left / moved
    return min(max(r, ZERO), ONE)


def recovered(r: D | None) -> bool:
    """Ждать дальше незачем: пул вернул ≥ 1 − ε своего смещения (ε — tconfig.RECOVERY_EPS)."""
    return r is not None and r >= ONE - tconfig.RECOVERY_EPS


# --- перп: стакан, дочерние заявки, стоимость ------------------------------------------------------------
def perp_side(kind: str) -> str:
    """entry: DEX покупка → перп SELL (бьём биды); exit: DEX продажа → перп BUY reduceOnly (бьём аски)."""
    return "SELL" if kind == "entry" else "BUY"


def _levels(book: Book, side: str) -> tuple[tuple[D, D], ...]:
    lv = book.bids if side == "SELL" else book.asks
    if not lv or lv[0][1] <= 0:
        raise PlanRefused(f"стакан пуст со стороны {'бидов' if side == 'SELL' else 'асков'}")
    return lv


def mid(book: Book) -> D | None:
    if not book.bids or not book.asks:
        return None
    return (book.bids[0][0] + book.asks[0][0]) / 2


def walk(levels: Sequence[tuple[D, D]], qty: D, px_cap: D | None = None, side: str = "SELL") -> tuple[D, D, D | None]:
    """Проход по уровням: (исполнено, сумма в $, последняя задетая цена). px_cap — не глубже кэпа IOC."""
    left, got, quote, last = qty, ZERO, ZERO, None
    for px, q in levels:
        if left <= 0:
            break
        if px_cap is not None and (px < px_cap if side == "SELL" else px > px_cap):
            break
        take = min(q, left)
        got += take
        quote += take * px
        left -= take
        last = px
    return got, quote, last


def _within_bps(levels: Sequence[tuple[D, D]], beta_bps: D, side: str) -> D:
    """Количество в пределах β б.п. от лучшей цены (walk_until_bps отчёта clip)."""
    best = levels[0][0]
    lim = best * (ONE - beta_bps / BPS) if side == "SELL" else best * (ONE + beta_bps / BPS)
    return sum((q for px, q in levels if (px >= lim if side == "SELL" else px <= lim)), ZERO)


def beta_px(best: D, side: str, f: Filters, beta_bps: D) -> D:
    """Кэп цены IOC для β: SELL — вниз к тику от best_bid·(1 − β), BUY — вверх от best_ask·(1 + β) (спека §6)."""
    return floor_to(best * (ONE - beta_bps / BPS), f.tick) if side == "SELL" \
        else ceil_to(best * (ONE + beta_bps / BPS), f.tick)


def _within_px(levels: Sequence[tuple[D, D]], px_cap: D, side: str) -> D:
    """Объём уровней, которые IOC с кэпом px_cap может взять из снимка."""
    return sum((q for px, q in levels if (px >= px_cap if side == "SELL" else px <= px_cap)), ZERO)


def child_cap(book: Book, side: str, f: Filters, alpha: D | None, beta_bps: D | None, band: bool = False) -> D:
    """Предел одной дочерней заявки, в токенах, по сетке шага: min(α·лучший уровень, объём в β б.п., maxQty).
    maxQty — меньший из LOT и MARKET_LOT: заявка LIMIT, но берём строже (для AIW3 80k ≈ $3.3k, не мешает).
    band (α/β «auto»): min(α·объём в пределах кэпа β, maxQty) — α как доля всей полосы до кэпа, а не лучшего уровня."""
    lv = _levels(book, side)
    caps = [min(f.max_qty_limit, f.max_qty_market)]
    if band and alpha is not None and beta_bps is not None:
        caps.append(alpha * _within_px(lv, beta_px(lv[0][0], side, f, beta_bps), side))
        return floor_to(min(caps), f.step)
    if alpha is not None:
        caps.append(alpha * lv[0][1])
    if beta_bps is not None:
        caps.append(_within_bps(lv, beta_bps, side))
    return floor_to(min(caps), f.step)


def px_cap_for(book: Book, side: str, f: Filters, qty: D, beta_bps: D | None) -> D:
    """Цена IOC. С β: SELL — вниз к тику от best_bid·(1 − β), BUY — вверх от best_ask·(1 + β) (спека §6).
    Без β (пусто, только dry): последний уровень, до которого план идёт этим количеством — «не глубже плана»."""
    lv = _levels(book, side)
    best = lv[0][0]
    if beta_bps is not None:
        return beta_px(best, side, f, beta_bps)
    got, _, last = walk(lv, qty, side=side)
    if got < qty:
        raise PlanRefused(f"в стакане меньше {format(qty, 'f')} — не на что поставить заявку")
    return last


def refill_waits(book: Book, side: str, qty: D, px_cap: D) -> int:
    """Сколько раз исполнитель встанет ждать пополнения стакана на одной перп-ноге: дочерние уходят подряд, без пауз,
    и за один заход берут не больше объёма снимка в пределах кэпа IOC; дальше — недобор, ожидание, новый заход."""
    if qty <= 0:
        return 0
    v = _within_px(_levels(book, side), px_cap, side)
    if v <= 0:
        return 10 ** 6                                   # в кэп не встаёт ничего — ждать бесконечно
    return max(int((qty / v).to_integral_value(ROUND_CEILING)) - 1, 0)


def beta_candidates(book: Book, side: str, f: Filters) -> list[D]:
    """β «auto»: расстояние от лучшей цены до каждого из первых AB_BETA_LEVELS уровней + один тик, б.п., но не уже
    пола max(3 тика, 5 б.п.). Вниз к 0.0001 б.п.: β, задуманный как k тиков, даёт кэп ровно в k тиках (кэп
    округляется наружу — вверх до тика β стал бы k + 1). Одинаковый кэп цены — один кандидат (меньший β)."""
    lv = _levels(book, side)
    best = lv[0][0]
    tick_bps = f.tick / best * BPS
    floor = max(tconfig.AB_BETA_FLOOR_TICKS * tick_bps, tconfig.AB_BETA_FLOOR_BPS)
    out: list[D] = []
    caps: set[D] = set()
    for px, _q in lv[:tconfig.AB_BETA_LEVELS]:
        b = max(abs(px / best - ONE) * BPS + tick_bps, floor).quantize(D("0.0001"), ROUND_FLOOR)
        cap = beta_px(best, side, f, b)
        if cap not in caps:
            caps.add(cap)
            out.append(b)
    return sorted(out)


def perp_children(book: Book, side: str, qty: D, f: Filters, alpha: D | None, beta_bps: D | None,
                  reduce_only: bool = False, band: bool = False) -> list[tuple[D, D]]:
    """Дочерние LIMIT IOC на количество qty: [(кол-во, кэп цены)]. Поровну на m = ⌈qty/h⌉ частей по сетке шага —
    каждая ≤ h (h уже на сетке), шаги остатка уходят первым. Меньше шага — [] (это перенос, а не заявка).
    Не reduceOnly: каждая ≥ minNotional и ≥ minQty — иначе отказ (биржа не примет, нога останется голой).
    band — α как доля объёма в пределах кэпа β (подобранные «auto», см. child_cap)."""
    qty = floor_to(qty, f.step)
    if qty <= 0:
        return []
    h = child_cap(book, side, f, alpha, beta_bps, band)
    if h < max(f.min_qty, f.step):
        where = "объём в пределах β" if (band and alpha is not None and beta_bps is not None) else "лучший уровень"
        raise PlanRefused(f"{where} тоньше минимума заявки (предел дочерней {format(h, 'f')} < "
                          f"minQty {format(f.min_qty, 'f')}) — ждать восстановления стакана")
    m = int((qty / h).to_integral_value(ROUND_CEILING))
    base = floor_to(qty / m, f.step)
    extra = int((qty - base * m) / f.step)
    sizes = [base + f.step if i < extra else base for i in range(m)]
    cap = px_cap_for(book, side, f, max(sizes), beta_bps)
    for s in sizes:
        if s < f.min_qty:
            raise PlanRefused(f"дочерняя {format(s, 'f')} меньше minQty {format(f.min_qty, 'f')}")
        if not reduce_only and s * cap < f.min_notional:
            raise PlanRefused(f"дочерняя {format(s, 'f')} × {format(cap, 'f')} меньше minNotional "
                              f"{format(f.min_notional, 'f')} $ — предел α/β слишком мал для этого стакана")
    return [(s, cap) for s in sizes]


@dataclass(frozen=True)
class PerpCost:
    """Стоимость перп-ноги против мида снимка, $. spread — полуспред + проход по уровням; fee — тейкер.
    unfilled — сколько не встало бы в кэп (только модель «без восстановления»)."""
    spread: D
    fee: D
    notional: D
    unfilled: D = ZERO

    @property
    def total(self) -> D:
        return self.spread + self.fee


def perp_cost(children: Sequence[tuple[D, D]], book: Book, side: str, fee_taker: D, refill: bool = True) -> PerpCost:
    """refill=True — план исполнения: каждая дочерняя идёт по восстановленному стакану (исполнитель ждёт
    лучший уровень). refill=False — пессимизм: все дочерние съедают снимок подряд (так дорог выход AIW3 одной
    заявкой: 18 б.п. при $41 на лучшем аске)."""
    m = mid(book)
    lv = list(_levels(book, side))
    if m is None:
        m = lv[0][0]
    spread = fee = notional = unfilled = ZERO
    for qty, cap in children:
        got, quote, _ = walk(lv, qty, cap, side)
        if not refill:
            rest, left = [], got
            for px, q in lv:
                take = min(q, left)
                left -= take
                if q - take > 0:
                    rest.append((px, q - take))
            lv = rest
        unfilled += qty - got
        notional += quote
        fee += quote * fee_taker
        spread += (m * got - quote) if side == "SELL" else (quote - m * got)
    return PerpCost(spread=spread, fee=fee, notional=notional, unfilled=unfilled)


# --- ограничения владельца и рынок -----------------------------------------------------------------------
@dataclass(frozen=True)
class Limits:
    """Денежные ограничения из owner.toml; None = пусто (в dry ограничение не действует, ключ — в missing).
    "auto" (владелец 12.09) — число подбирает план по правилу из docstring модуля и кладёт в Plan.est."""
    alpha: D | str | None = None              # perp.<venue>.touch_frac_max: число | "auto" | None
    beta_bps: D | str | None = None           # perp.<venue>.max_slip_bps: число | "auto" | None
    clips_max: int | str | None = None        # число | "auto" (без потолка, перебор до PLAN_N_SCAN) | None
    clip_max_usd: D | str | None = None       # число | "auto" (размер выбирает оптимизатор) | None
    unhedged_usd_max: D | str | None = None   # число | "auto" (= plan_cost_drift_pct % клипа) | None
    plan_cost_drift_pct: D | None = None      # exec.plan_cost_drift_pct — мера для unhedged_usd_max «auto»
    refill_wait_s: D | None = None            # exec.refill_wait_max_s — в ожидаемую длительность плана
    exec_time_max_s: D | str | None = None    # число | "auto" (= 3 × ожидаемая длительность + 60 с) | None
    ab_band: bool = False                     # α — доля полосы до кэпа β (замороженные «auto» остатку и дохеджу)
    deal_max_usd: D | None = None             # limits.deal_max_usd_per_leg (только вход)
    native_reserve: D | None = None           # неснижаемый остаток нативной монеты (BNB), в монетах
    min_entry_funding_pct_h: D | None = None  # справочно (входы по команде владельца)
    min_entry_basis_bps: D | None = None      # справочно
    missing: tuple[str, ...] = ()             # чего не хватает для live — в план как есть


def limits_from_owner(cfg, venue: str = "aster", chain: str = "bsc") -> Limits:
    """OwnerCfg → Limits. Пустое остаётся None — число здесь не придумывается."""
    g = cfg.get
    return Limits(alpha=g(f"perp.{venue}.touch_frac_max"), beta_bps=g(f"perp.{venue}.max_slip_bps"),
                  clips_max=g("exec.clips_max"), clip_max_usd=g("exec.clip_max_usd"),
                  unhedged_usd_max=g("exec.unhedged_usd_max"), plan_cost_drift_pct=g("exec.plan_cost_drift_pct"),
                  refill_wait_s=g("exec.refill_wait_max_s"), exec_time_max_s=g("exec.exec_time_max_s"),
                  deal_max_usd=g("limits.deal_max_usd_per_leg"),
                  native_reserve=g("dex.native_reserve"),
                  min_entry_funding_pct_h=g("limits.min_entry_funding_pct_h"),
                  min_entry_basis_bps=g("limits.min_entry_basis_bps"),
                  missing=tuple(cfg.live_missing(venue, chain)))


@dataclass(frozen=True)
class Market:
    """Снимок рынка на момент плана. sigma_1s — σ доходности марка перпа за 1 с (доля); funding_h — ставка в час
    (доля, плюс — шорт получает); native_usd — баланс газовой монеты кошелька в $ (None = неизвестен, проверка
    оплачиваемости газа не делается и помечается); approve_usd — газ апрувов этой стороны, $."""
    book: Book
    filters: Filters
    fee_taker: D
    sigma_1s: D | None = None
    funding_h: D | None = None
    native_usd: D | None = None
    native_px: D | None = None
    approve_usd: D = ZERO
    chain: str = "bsc"


def split_units(total: int, n: int) -> list[int]:
    """Сырые единицы на n клипов: поровну вниз, последний забирает остаток — сумма ровно total."""
    base = total // n
    return [base] * (n - 1) + [total - base * (n - 1)]


@dataclass
class _Cand:
    n: int
    q: D
    m: int = 0
    total: D | None = None
    reason: str | None = None
    dex: DexCost | None = None
    perp: PerpCost | None = None
    pace: D = ZERO
    risk: D | None = None
    risk_cap: D | None = None         # предел риска голой ноги для этого клипа (число владельца или «auto» от q)
    dt: D | None = None
    clip_s: D = ZERO
    clips: list = field(default_factory=list)
    tokens: D = ZERO
    waits: int = 0                    # ожиданий пополнения стакана по всем клипам (refill_waits)
    waits_max: int = 0                # … на самом тяжёлом клипе
    child_max: D = ZERO               # самая крупная дочерняя, токены — для равенства «к меньшим дочерним»
    dur: D = ZERO                     # ожидаемая длительность исполнения, с

    def row(self) -> dict:
        return {"n": self.n, "clip_usd": self.q, "m": self.m, "total_usd": self.total, "excluded": self.reason}


_REASON_KEYS = ("unhedged_usd_max", "clip_max_usd", "минимума перпа", "σ перпа", "натива", "minNotional", "minQty",
                "пополнени", "стакан")


def _tie(x: D) -> D:
    """Сравнение издержек вариантов с точностью до 10⁻⁶ $: разница меньше — равенство, решают дочерние и β."""
    return x.quantize(D("0.000001"))


def exec_time_max(lim: Limits, expected_s: D) -> D | None:
    """Предел времени исполнения: число владельца как есть; «auto» — 3 × ожидаемая + 60 с; пусто — None."""
    v = lim.exec_time_max_s
    if v == AUTO:
        return tconfig.EXEC_TIME_AUTO_MULT * expected_s + tconfig.EXEC_TIME_AUTO_ADD_S
    return v if isinstance(v, D) else None


def liq_alert_pct(setting: Any, entry_dist_pct: D | None) -> D | None:
    """Порог тревоги «до ликвидации меньше, %»: число владельца как есть; «auto» — доля LIQ_ALERT_AUTO_FRAC (½)
    от расстояния на входе. Расстояние на входе неизвестно (симуляция, позиция не прочитана) — None."""
    if setting == AUTO:
        return entry_dist_pct * tconfig.LIQ_ALERT_AUTO_FRAC if entry_dist_pct is not None else None
    return setting if isinstance(setting, D) else None


def _reason_class(reason: str) -> str:
    """Класс причины исключения варианта n — чтобы отказ владельцу не повторял одно и то же с разными числами."""
    return next((k for k in _REASON_KEYS if k in reason), reason)


def _pace(kind: str, funding_h: D | None, q: D, n: int, clip_s: D) -> D:
    """Цена ожидания между клипами: фандинг × ещё не построенный номинал × время. Только штраф, не премия:
    у модели нет цены риска, и «выгода» от затягивания выхода при плюсовом фандинге была бы ложным оптимумом."""
    if funding_h is None or n < 2:
        return ZERO
    rate = funding_h if kind == "entry" else -funding_h
    if rate <= 0:
        return ZERO
    return rate * q * (clip_s / 3600) * (n * (n - 1)) / 2


@dataclass
class _NBase:
    """Часть варианта n, не зависящая от α/β (считается один раз на n): сырые единицы, стоимость DEX, токены клипов,
    контракты клипов (m ≠ 1 — с переносом, clip_contracts; m = 1 — None: токены клипа как есть)."""
    units: list
    dex: DexCost
    toks: list
    qs: list | None = None


def clip_contracts(toks: Sequence[D], m: D, step: D, kind: str, carry0: D = ZERO) -> list[D]:
    """Контракты перп-ноги по клипам так, как их отправит исполнитель (ревью 13.09, M1/M2): с переносом остатка между
    клипами, начиная с дельты сделки carry0 (токены). Вход — SELL floor((δ0 + Σ≤i)/m) − floor((δ0 + Σ<i)/m) к шагу;
    выход — BUY ceil((Σ≤i − δ0)/m) − ceil((Σ<i − δ0)/m) к шагу (меньше нуля — 0: перенос покрывает продажу)."""
    out, cum, prev = [], ZERO, ZERO
    for t in toks:
        cum += t
        if kind == "entry":
            tot = floor_to((carry0 + cum) / m, step)
        else:
            x = (cum - carry0) / m
            tot = ceil_to(x, step) if x > 0 else ZERO
        out.append(max(tot - prev, ZERO))
        prev = max(prev, tot)
    return out


def plan(*, deal_id: str, kind: str, coin: str, spot: str, perp: str, symbol: str, leg_usd: D,
         total_in_units: int, dec_in: int, calib: Calib, mkt: Market, lim: Limits,
         r: D = tconfig.R_PRIOR, d0: D = ZERO, now: float, ttl_s: float = tconfig.PLAN_TTL_S,
         units_per_contract: D = ONE, carry0: D | None = None) -> Plan:
    """Выбор n = 1…clips_max по минимуму dex_cost + Σ perp_cost + ожидание при ограничениях: риск голой ноги ≤
    unhedged_usd_max, клип ≤ clip_max_usd (кроме "auto"), газ n·g·1.5 + апрувы ≤ доступное натива. α/β «auto» —
    тот же перебор n для каждой пары из сетки, выбор — минимум издержек (равенство — к меньшим дочерним, потом к
    меньшему β); выбранные числа — в est (alpha, beta_bps, ab_band), исполнитель берёт их оттуда.

    entry: total_in_units — стейбл (сырые), перп SELL; exit: total_in_units — токены (сырые), перп BUY reduceOnly.
    units_per_contract (m, ревью 13.09 С1) — токенов в одном контракте: токены клипа → контракты /m, стакан и
    дочерние — в контрактах (цена за контракт), базис — по цене контракта /m. m ≠ 1 (ревью 13.09, M1/M2): контракты
    клипов — с переносом от дельты сделки carry0 (токены), как у исполнителя (clip_contracts); m = 1 или carry0 None
    (дельта сделки не передана) — прежний путь: токены клипа / m вниз к шагу.
    Нет ни одного допустимого варианта — PlanRefused с причинами (владелец видит, что именно не сошлось).
    """
    if kind not in KINDS:
        raise ValueError(f"kind: {kind}")
    m = D(units_per_contract)
    if not m.is_finite() or m <= 0:
        raise ValueError(f"units_per_contract: {units_per_contract}")
    if calib.kind != kind:
        raise ValueError(f"калибровка {calib.kind} для плана {kind}")
    if total_in_units <= 0:
        raise PlanRefused("сумма сделки нулевая")
    f, book = mkt.filters, mkt.book
    side = perp_side(kind)
    reduce_only = kind == "exit"
    lv = _levels(book, side)
    best = lv[0][0]
    amount_in = D(total_in_units) / D(10) ** dec_in
    S = amount_in if kind == "entry" else amount_in * calib.p_ref      # размер в $
    if kind == "entry" and lim.deal_max_usd is not None and leg_usd > lim.deal_max_usd:
        raise PlanRefused(f"{format(leg_usd, 'f')} $ больше deal_max_usd_per_leg {format(lim.deal_max_usd, 'f')} $")
    q_min = max(f.min_qty, f.step) * best
    if not reduce_only:
        q_min = max(q_min, f.min_notional * MIN_NOTIONAL_MULT)
    if S < q_min:
        raise PlanRefused(f"сумма {S:.2f} $ меньше минимальной заявки перпа ({q_min:.2f} $)")
    clip_cap = lim.clip_max_usd if isinstance(lim.clip_max_usd, D) else None
    n_max = lim.clips_max if (isinstance(lim.clips_max, int) and not isinstance(lim.clips_max, bool)) \
        else N_SCAN_NO_LIMIT
    t_conf = T_CONFIRM_S.get(mkt.chain, T_CONFIRM_DEFAULT_S)
    avail_gas = None
    if mkt.native_usd is not None:
        reserve = (lim.native_reserve or ZERO) * (mkt.native_px or ZERO)
        avail_gas = mkt.native_usd - reserve
    a_auto, b_auto = lim.alpha == AUTO, lim.beta_bps == AUTO
    auto = a_auto or b_auto
    band = a_auto or lim.ab_band
    strict = auto or lim.ab_band                          # недобор сверх ASTER_IOC_PARTIAL_RETRIES — недопустимо
    alphas = tconfig.AB_ALPHA_GRID if a_auto else (lim.alpha,)
    betas = tuple(beta_candidates(book, side, f)) if b_auto else (lim.beta_bps,)
    unh = lim.unhedged_usd_max
    unh_frac = (lim.plan_cost_drift_pct / 100) if (unh == AUTO and lim.plan_cost_drift_pct is not None) else None
    refill_s = lim.refill_wait_s or ZERO
    bases: dict[int, _NBase] = {}

    def base(n: int) -> _NBase:
        b = bases.get(n)
        if b is None:
            sizes = clip_sizes(S, n)
            dex = dex_cost(n, S, calib.k, calib.g, r, calib.c0, d0)
            units = split_units(total_in_units, n)
            if kind == "entry":
                toks = [sizes[i] / (calib.p_ref * (ONE + dex.clip_frac[i])) for i in range(n)]
            else:
                toks = [D(u) / D(10) ** dec_in for u in units]
            qs = clip_contracts(toks, m, f.step, kind, D(carry0)) if (m != ONE and carry0 is not None) else None
            b = bases[n] = _NBase(units=units, dex=dex, toks=toks, qs=qs)
        return b

    def scan(alpha, beta) -> tuple[list[_Cand], _Cand | None]:
        cands: list[_Cand] = []
        best_c: _Cand | None = None
        for n in range(1, n_max + 1):
            sizes = clip_sizes(S, n)
            c = _Cand(n=n, q=max(sizes))
            cands.append(c)
            if min(sizes) < q_min:
                c.reason = f"клип {min(sizes):.2f} $ меньше минимума перпа {q_min:.2f} $"
                break                                        # дальше клипы только меньше
            if clip_cap is not None and c.q > clip_cap:
                c.reason = f"клип {c.q:.2f} $ больше clip_max_usd {format(clip_cap, 'f')} $"
                continue
            nb = base(n)
            c.dex = nb.dex
            try:
                for i, u in enumerate(nb.units):         # токены клипа → контракты перпа (m токенов в контракте)
                    q = nb.toks[i] / m if nb.qs is None else nb.qs[i]
                    ch = perp_children(book, side, q, f, alpha, beta, reduce_only, band=band)
                    c.clips.append(ClipPlan(seq=i + 1, dex_in_units=u, children=ch))
                    c.tokens += nb.toks[i]
                    if ch:
                        w = refill_waits(book, side, sum((q for q, _ in ch), ZERO), ch[0][1])
                        c.waits += w
                        c.waits_max = max(c.waits_max, w)
                        c.child_max = max(c.child_max, max(q for q, _ in ch))
            except PlanRefused as e:
                c.reason = str(e)
                continue
            if strict and c.waits_max > tconfig.ASTER_IOC_PARTIAL_RETRIES:
                c.reason = (f"стакан в пределах кэпа IOC мельче клипа: {c.waits_max} ожиданий пополнения > "
                            f"{tconfig.ASTER_IOC_PARTIAL_RETRIES} — исполнитель встал бы на недоборе")
                continue
            c.m = max((len(cp.children) for cp in c.clips), default=0)
            c.dt = t_conf + RTT_S + max(c.m - 1, 0) * TAU_P_S
            if mkt.sigma_1s is not None:
                c.risk = Z_UNHEDGED * mkt.sigma_1s * dsqrt(c.dt) * c.q
            c.risk_cap = unh if isinstance(unh, D) else (unh_frac * c.q if unh_frac is not None else None)
            if c.risk_cap is not None:
                if c.risk is None:
                    c.reason = "σ перпа неизвестна — риск голой ноги не оценить"
                    continue
                if c.risk > c.risk_cap:
                    lim_s = (f"unhedged_usd_max {format(unh, 'f')} $" if isinstance(unh, D) else
                             f"unhedged_usd_max auto {format(lim.plan_cost_drift_pct, 'f')} % клипа = "
                             f"{c.risk_cap:.2f} $")
                    c.reason = f"риск голой ноги {c.risk:.2f} $ (клип {c.q:.2f} $, {c.dt:.1f} с) > {lim_s}"
                    continue
            gas_need = c.dex.gas * GAS_PAY_MULT + mkt.approve_usd
            if avail_gas is not None and gas_need > avail_gas:
                c.reason = f"газ {gas_need:.4f} $ (×{GAS_PAY_MULT}) не покрыт: доступно {avail_gas:.4f} $ натива"
                continue
            pc = [perp_cost(cp.children, book, side, mkt.fee_taker) for cp in c.clips]
            c.perp = PerpCost(spread=sum((p.spread for p in pc), ZERO), fee=sum((p.fee for p in pc), ZERO),
                              notional=sum((p.notional for p in pc), ZERO),
                              unfilled=sum((p.unfilled for p in pc), ZERO))
            c.clip_s = c.dt + (CLIP_GAP_S if n > 1 else ZERO)
            c.pace = _pace(kind, mkt.funding_h, c.q, n, c.clip_s)
            c.total = c.dex.total + c.perp.total + c.pace + mkt.approve_usd
            c.dur = c.clip_s * n + refill_s * c.waits
            if best_c is None or c.total < best_c.total:
                best_c = c
        return cands, best_c

    runs = [(a, b, *scan(a, b)) for a in alphas for b in betas]
    if auto:
        ok = [x for x in runs if x[3] is not None]
        a, b, cands, best_c = min(ok, key=lambda x: (_tie(x[3].total), x[3].child_max, x[1] or ZERO, x[0] or ZERO)) \
            if ok else (None, None, runs[0][2], None)
    else:
        a, b, cands, best_c = runs[0]
    if best_c is None:
        seen: dict[str, str] = {}                            # класс причины → первый её текст (с числами n=1)
        for _a, _b, cs, _bc in runs:
            for c in cs:
                if c.reason:
                    seen.setdefault(_reason_class(c.reason), c.reason)
        head = (f"нет допустимого варианта α × β × n ({len(alphas)} × {len(betas)} × 1…"
                f"{max(x[2][-1].n for x in runs)})" if auto else f"нет допустимого числа клипов (n = 1…{cands[-1].n})")
        raise PlanRefused(f"{head}: " + "; ".join(list(seen.values())[:3]))
    grid = [{"alpha": x[0], "beta_bps": x[1], "n": x[3].n if x[3] else None,
             "total_usd": x[3].total if x[3] else None, "child_max": x[3].child_max if x[3] else None,
             "excluded": None if x[3] else next((c.reason for c in x[2] if c.reason), None)} for x in runs] \
        if auto else []
    ab = {"alpha": a, "beta_bps": b, "ab_band": bool(band and a is not None and b is not None),
          "alpha_auto": a_auto, "beta_auto": b_auto, "ab_grid": grid,
          "unhedged_auto_pct": lim.plan_cost_drift_pct if unh_frac is not None else None}
    return _assemble(best_c, cands, deal_id=deal_id, kind=kind, coin=coin, spot=spot, perp=perp, symbol=symbol,
                     leg_usd=leg_usd, S=S, calib=calib, mkt=mkt, lim=lim, r=r, d0=d0, side=side, t_conf=t_conf,
                     now=now, ttl_s=ttl_s, avail_gas=avail_gas, ab=ab, upc=m)


def _assemble(c: _Cand, cands: list[_Cand], *, deal_id, kind, coin, spot, perp, symbol, leg_usd, S, calib, mkt,
              lim, r, d0, side, t_conf, now, ttl_s, avail_gas, ab, upc: D = ONE) -> Plan:
    book, f = mkt.book, mkt.filters
    lv = _levels(book, side)
    flat = [ch for cp in c.clips for ch in cp.children]
    walk_all = perp_cost(flat, book, side, mkt.fee_taker, refill=False)     # если лучший уровень не восстановится
    n1 = next((x for x in cands if x.n == 1 and x.total is not None), None)
    dex_px = S / c.tokens if c.tokens > 0 else None                          # средняя цена DEX-ноги по плану
    basis_bps = (lv[0][0] / upc / dex_px - ONE) * BPS if dex_px else None   # цена контракта / m — цена токена
    notional = 2 * S
    # est["m"] — дочерних на клип (исторически); токенов в контракте — units_per_contract, контрактов — contracts
    est = {
        "n": c.n, "clip_usd": c.q, "m": c.m, "children": len(flat), "tokens": c.tokens, "dex_px": dex_px,
        "contracts": sum((q for q, _ in flat), ZERO), "units_per_contract": upc,
        "dex_fee_usd": c.dex.fee, "dex_impact_usd": c.dex.impact, "gas_usd": c.dex.gas, "gas_per_swap_usd": calib.g,
        "approve_usd": mkt.approve_usd, "perp_spread_usd": c.perp.spread, "perp_fee_usd": c.perp.fee,
        "perp_no_refill_usd": walk_all.total, "perp_no_refill_unfilled": walk_all.unfilled, "pace_usd": c.pace,
        "total_usd": c.total, "notional_usd": notional, "total_pct": c.total / notional * 100,
        "n1_total_usd": n1.total if n1 else None, "q_star_usd": q_star(calib.g, calib.k),
        "unhedged_risk_usd": c.risk, "unhedged_dt_s": c.dt, "unhedged_cap_usd": c.risk_cap, "basis_bps": basis_bps,
        "gas_payable": None if avail_gas is None else True,
        # числа исполнения, замороженные с планом (исполнитель берёт их отсюда, а не из owner.toml)
        **ab, "child_max_qty": c.child_max, "refill_waits": c.waits, "exec_expected_s": c.dur,
        "exec_time_max_s": exec_time_max(lim, c.dur), "exec_time_auto": lim.exec_time_max_s == AUTO,
        "clips_max_auto": lim.clips_max == AUTO,
        "candidates": [x.row() for x in cands],
    }
    top = {"bid": book.bids[0][0] if book.bids else None, "bid_qty": book.bids[0][1] if book.bids else None,
           "ask": book.asks[0][0] if book.asks else None, "ask_qty": book.asks[0][1] if book.asks else None,
           "mid": mid(book), "ts": book.ts}
    thresholds: dict[str, Any] = {}
    if kind == "entry":
        fpct = mkt.funding_h * 100 if mkt.funding_h is not None else None
        thresholds = {
            "min_entry_funding_pct_h": lim.min_entry_funding_pct_h, "funding_pct_h": fpct,
            "funding_ok": None if (fpct is None or lim.min_entry_funding_pct_h is None)
            else fpct >= lim.min_entry_funding_pct_h,
            "min_entry_basis_bps": lim.min_entry_basis_bps, "basis_bps": basis_bps,
            "basis_ok": None if (basis_bps is None or lim.min_entry_basis_bps is None)
            else basis_bps >= lim.min_entry_basis_bps,
        }
    inputs = {
        "calib": calib.as_dict(), "r": r, "d0": d0, "side": side, "reduce_only": kind == "exit", "size_usd": S,
        "book_top": top, "filters": {"tick": f.tick, "step": f.step, "min_qty": f.min_qty,
                                     "max_qty_limit": f.max_qty_limit, "max_qty_market": f.max_qty_market,
                                     "min_notional": f.min_notional},
        "fee_taker": mkt.fee_taker, "sigma_1s": mkt.sigma_1s, "funding_h": mkt.funding_h,
        "native_usd": mkt.native_usd, "native_px": mkt.native_px, "chain": mkt.chain,
        "tech": {"z": Z_UNHEDGED, "t_confirm_s": t_conf, "rtt_s": RTT_S, "tau_p_s": TAU_P_S, "clip_gap_s": CLIP_GAP_S,
                 "min_notional_mult": MIN_NOTIONAL_MULT, "gas_pay_mult": GAS_PAY_MULT},
        "limits": {"alpha": lim.alpha, "beta_bps": lim.beta_bps, "clips_max": lim.clips_max,
                   "clip_max_usd": lim.clip_max_usd, "unhedged_usd_max": lim.unhedged_usd_max,
                   "plan_cost_drift_pct": lim.plan_cost_drift_pct, "refill_wait_s": lim.refill_wait_s,
                   "exec_time_max_s": lim.exec_time_max_s, "ab_band": lim.ab_band,
                   "deal_max_usd": lim.deal_max_usd, "native_reserve": lim.native_reserve},
        "entry_thresholds": thresholds,
    }
    return Plan(deal_id=deal_id, kind=kind, coin=coin, spot=spot, perp=perp, symbol=symbol, leg_usd=leg_usd,
                clips=c.clips, est=est, inputs=inputs, missing_owner_keys=list(lim.missing), expires=now + ttl_s)


# --- перп без DEX-ноги: «выход перп» и «дохедж» ---------------------------------------------------------------
@dataclass(frozen=True)
class PerpPick:
    """Дочерние одной перп-операции без DEX-ноги и α/β, с которыми они построены (подобранные «auto» — тоже)."""
    alpha: D | None
    beta_bps: D | None
    band: bool
    alpha_auto: bool
    beta_auto: bool
    children: list
    cost: PerpCost
    waits: int
    expected_s: D
    exec_time_max_s: D | None

    def est(self) -> dict:
        """Поля Plan.est, которые исполнитель и сообщение плана читают так же, как у плана с клипами."""
        return {"alpha": self.alpha, "beta_bps": self.beta_bps, "ab_band": self.band, "alpha_auto": self.alpha_auto,
                "beta_auto": self.beta_auto, "refill_waits": self.waits, "exec_expected_s": self.expected_s,
                "exec_time_max_s": self.exec_time_max_s,
                "child_max_qty": max((q for q, _ in self.children), default=ZERO)}


def pick_perp(book: Book, side: str, qty: D, f: Filters, fee_taker: D, lim: Limits,
              reduce_only: bool = False) -> PerpPick:
    """α/β для заявки перпа без DEX-ноги. Числа владельца — как есть (ошибка стакана — как у perp_children);
    «auto» — перебор той же сетки, минимум perp_cost, равенство — к меньшим дочерним, потом к меньшему β."""
    a_auto, b_auto = lim.alpha == AUTO, lim.beta_bps == AUTO
    auto = a_auto or b_auto
    band = a_auto or lim.ab_band
    alphas = tconfig.AB_ALPHA_GRID if a_auto else (lim.alpha,)
    betas = tuple(beta_candidates(book, side, f)) if b_auto else (lim.beta_bps,)
    best, key_best = None, None
    reasons: dict[str, str] = {}
    for a in alphas:
        for b in betas:
            try:
                ch = perp_children(book, side, qty, f, a, b, reduce_only, band=band)
            except PlanRefused as e:
                if not auto:
                    raise
                reasons.setdefault(_reason_class(str(e)), str(e))
                continue
            w = refill_waits(book, side, sum((q for q, _ in ch), ZERO), ch[0][1]) if ch else 0
            if (auto or lim.ab_band) and w > tconfig.ASTER_IOC_PARTIAL_RETRIES:
                reasons.setdefault("пополнени", f"стакан в пределах кэпа IOC мельче заявки: {w} ожиданий пополнения > "
                                                f"{tconfig.ASTER_IOC_PARTIAL_RETRIES}")
                continue
            pc = perp_cost(ch, book, side, fee_taker)
            key = (_tie(pc.total), max((q for q, _ in ch), default=ZERO), b or ZERO, a or ZERO)
            if best is None or key < key_best:
                best, key_best = (a, b, ch, pc, w), key
    if best is None:
        raise PlanRefused("нет допустимых α/β для заявки перпа: " + "; ".join(list(reasons.values())[:3]))
    a, b, ch, pc, w = best
    exp = RTT_S + max(len(ch) - 1, 0) * TAU_P_S + (lim.refill_wait_s or ZERO) * w
    return PerpPick(alpha=a, beta_bps=b, band=bool(band and a is not None and b is not None), alpha_auto=a_auto,
                    beta_auto=b_auto, children=ch, cost=pc, waits=w, expected_s=exp,
                    exec_time_max_s=exec_time_max(lim, exp))
